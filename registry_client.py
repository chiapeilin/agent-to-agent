"""Router client：收到文字需求 → 上 registry 拿目錄 → 讓 LLM 依 skill 描述挑 agent → 委派。

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
from openai import AsyncOpenAI

from code_review_agent.auth import bearer_header, build_auth_interceptor

load_dotenv()

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://127.0.0.1:8000")
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "gpt-5.4-nano")


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


async def pick_agent(request_text: str, catalog: list[dict]) -> dict | None:
    """讓 LLM 讀 registry 目錄，依需求挑一個最合適的 agent；挑不到回 None。"""
    print(f"[router] 詢問 LLM({ROUTER_MODEL})選擇 agent 中...")
    client = AsyncOpenAI(timeout=60)
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
    url = choice.get("url")
    if not isinstance(url, str):
        print("[router] LLM 沒有回傳有效的 url")
        return None
    print(f"[router] LLM 選擇：{url}\n[router] 理由：{choice.get('reason')}\n")

    # 確認 url 真的在目錄裡
    return next((a for a in catalog if a["url"] == url), None)


async def main() -> None:
    # 1. 在 terminal 取得使用者需求
    request_text = read_request()
    if not request_text:
        print("沒有輸入需求")
        return

    # 2. 抓 registry 全部目錄（registry 有啟用 OAuth 時自動帶 token）
    catalog = httpx.get(f"{REGISTRY_URL}/agents", headers=await bearer_header()).json()
    if not catalog:
        print("registry 目錄是空的")
        return
    print(
        f"\n[registry] 目錄有 {len(catalog)} 個 agent：{[a['name'] for a in catalog]}\n"
    )

    # 3. 依需求讓 LLM 挑合適的 agent
    print(f"[需求] {request_text}\n")
    chosen = await pick_agent(request_text, catalog)
    if chosen is None:
        print("找不到合適的 agent 處理這個需求")
        return
    print(f"[router] 委派給：{chosen['name']} @ {chosen['url']}\n")

    # 4. 連上選中的 agent，送出需求（agent 有啟用 OAuth 時自動帶 token）
    config = ClientConfig(httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(120.0)))
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
