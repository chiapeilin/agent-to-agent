"""Router client：收到文字需求 → 先上 registry 做 semantic discover / top-3 選候選 → 再讓 LLM 從這三個挑一個 → 委派。

先開好 agent 與 registry 再跑：
    uv run python registry_client.py
"""

import asyncio
import json
import os
import sys

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_message, new_text_part
from a2a.types import Role, SendMessageRequest
from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI

from shared.auth import bearer_header, build_auth_interceptor

load_dotenv()

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://127.0.0.1:8000")
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "gpt-5.4-nano")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


def read_request() -> str:
    """需求來源：命令列參數，或互動輸入。"""
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:]).strip()
    return input("請輸入你的需求：\n> ").strip()


ROUTER_SYSTEM_PROMPT = (
    "你是一個 agent router。根據使用者需求，從候選 agent 清單中挑出最合適的一個。"
    "只能從清單挑，挑不到就回 null。"
    '嚴格回傳 JSON：{"url": "<選中的 url 或 null>", "reason": "<一句話理由>"}'
)


def _parse_choice(content: str | None) -> dict:
    """解析 LLM 回傳的 JSON；內容為空或不合法都當成空 dict（視為選不出）。"""
    try:
        choice = json.loads(content) if content else {}
    except json.JSONDecodeError:
        choice = {}
    return choice if isinstance(choice, dict) else {}


async def discover_candidates(request_text: str) -> list[dict]:
    """先調 registry /search，拿到最相似的 3 個 agent。"""
    headers = await bearer_header()
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                f"{REGISTRY_URL}/search",
                json={"query": {"text": request_text}},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[router] /search 失敗，改用 /agents 兜底：{}", exc)
            results = []

        if not results:
            try:
                response = await client.get(f"{REGISTRY_URL}/agents", headers=headers)
                response.raise_for_status()
                catalog = response.json()
                return [
                    {
                        "url": item.get("url"),
                        "displayName": item.get("name"),
                        "description": item.get("description", ""),
                        "tags": [skill.get("id") for skill in item.get("skills", [])],
                    }
                    for item in catalog[:3]
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning("[router] /agents 也失敗：{}", exc)
                return []

    return list(results[:3])


async def pick_agent(request_text: str, catalog: list[dict]) -> dict | None:
    """讓 LLM 從 top-3 候選中挑一個最合適的 agent；挑不到回 None。"""
    if not OPENAI_API_KEY:
        logger.error(
            "[router] 缺少 OPENAI_API_KEY，請先在 .env 中填入有效的 OpenAI API key"
        )
        return None

    logger.info(
        "[router] 詢問 LLM({})從 {} 個候選中選擇 agent...", ROUTER_MODEL, len(catalog)
    )
    client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=60)
    try:
        resp = await client.chat.completions.create(
            model=ROUTER_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"使用者需求：\n{request_text}\n\n"
                        f"候選 agent（含 skills 描述）：\n"
                        f"{json.dumps(catalog, ensure_ascii=False, indent=2)}"
                    ),
                },
            ],
        )

        choice = _parse_choice(resp.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001
        logger.error("[router] OpenAI 呼叫失敗：{}", exc)
        return None

    url = choice.get("url")
    if not isinstance(url, str):
        logger.warning("[router] LLM 沒有回傳有效的 url")
        return None
    logger.info("[router] LLM 選擇：{}｜理由：{}", url, choice.get("reason"))

    # 確認 url 真的在目錄裡
    return next((a for a in catalog if a.get("url") == url), None)


async def main() -> None:
    # 1. 在 terminal 取得使用者需求
    request_text = read_request()
    if not request_text:
        logger.warning("沒有輸入需求")
        return

    logger.info("[需求] {}", request_text)

    # 2. 先經 registry /search 做 semantic discover，拿出最相似的 3 個 agent
    candidates = await discover_candidates(request_text)
    if not candidates:
        logger.warning("registry 沒有找到可用候選 agent")
        return

    logger.info(
        "[registry] 語義候選有 {} 個 agent：{}",
        len(candidates),
        [
            item.get("displayName") or item.get("name") or item.get("url")
            for item in candidates
        ],
    )

    # 3. 依需求讓 LLM 從這三個候選中挑合適的 agent
    chosen = await pick_agent(request_text, candidates)
    if chosen is None:
        logger.warning("找不到合適的 agent 處理這個需求")
        return

    display_name = chosen.get("displayName") or chosen.get("name") or chosen.get("url")
    logger.info("[router] 委派給：{} @ {}", display_name, chosen.get("url"))

    # 4. 連上選中的 agent，送出需求。
    agent_headers = await bearer_header()
    config = ClientConfig(
        httpx_client=httpx.AsyncClient(
            timeout=httpx.Timeout(120.0), headers=agent_headers
        )
    )
    auth = build_auth_interceptor()
    client = await create_client(
        chosen["url"], config, interceptors=[auth] if auth else None
    )
    request = SendMessageRequest(
        message=new_message(
            parts=[new_text_part(text=request_text)], role=Role.ROLE_USER
        )
    )
    async for event in client.send_message(request):
        text = get_stream_response_text(event)
        if text:
            print(text)
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
