"""ARD-style A2A registry：聚合 agent card、建立 ai-catalog.json，並提供語義搜尋與候選選擇。

執行：uv run python registry.py    （預設 127.0.0.1:8000）
"""

import json
import math
import os
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.auth import OAuth2Middleware, load_auth_config

# 顯式載入 .env，讓 A2A_OIDC_* / REGISTRY_* 讀得到（別靠 import 副作用）。
load_dotenv()

HOST = os.environ.get("REGISTRY_HOST", "127.0.0.1")
PORT = int(os.environ.get("REGISTRY_PORT", "8000"))
EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
CATALOG_PATH = Path(__file__).with_name("ai-catalog.json")

# curated 白名單：只有列在這裡的 agent 才會被收錄。
DEFAULT_AGENT_URLS = (
    "http://127.0.0.1:8001,"  # code review agent
    "http://127.0.0.1:8002,"  # translation agent
    "http://127.0.0.1:8003,"  # uppercase agent
    "http://127.0.0.1:8004,"  # image analyzer agent
)
CURATED_URLS = [
    u.strip()
    for u in os.environ.get("REGISTRY_AGENT_URLS", DEFAULT_AGENT_URLS).split(",")
    if u.strip()
]

# self-registration：agent 啟動時可 POST /register 把自己加入（push 模式）。
REGISTERED_URLS: set[str] = set()

# 設了則 /register 需帶對的 token（x-registry-token），避免任意人亂註冊。
REGISTER_TOKEN = os.environ.get("REGISTRY_REGISTER_TOKEN")


def _all_urls() -> list[str]:
    """curated 白名單 + 動態註冊，去重後的完整清單。"""
    return list(dict.fromkeys([*CURATED_URLS, *sorted(REGISTERED_URLS)]))


def _is_valid_agent_url(url: str) -> bool:
    """只接受 http/https 且有 host 的 URL。"""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def build_catalog_payload(entries: list[dict]) -> dict:
    """建立 ARD 風格的 ai-catalog.json payload。"""
    return {
        "specVersion": "1.0",
        "host": {
            "displayName": "A2A Agent Registry",
            "identifier": "urn:registry:a2a-agent",
        },
        "entries": entries,
    }


def write_catalog_file(entries: list[dict], file_path: Path | None = None) -> Path:
    """把 catalog 寫到工作區的 ai-catalog.json。"""
    target_path = file_path or CATALOG_PATH
    payload = build_catalog_payload(entries)
    target_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target_path


def _normalize_text(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return re.sub(r"\s+", " ", text)


def _tokenize(text: str) -> list[str]:
    return [token for token in _normalize_text(text).split() if token]


def rank_catalog_entries(entries: list[dict], query_text: str) -> list[dict]:
    """以關鍵字重疊做回退排序，讓沒有 embedding 時也能選出最相似候選。"""
    if not entries:
        return []

    query_tokens = set(_tokenize(query_text))
    ranked: list[tuple[float, dict]] = []
    for entry in entries:
        combined_text = " ".join(
            [
                entry.get("displayName", ""),
                entry.get("description", ""),
                " ".join(entry.get("tags", [])),
            ]
        )
        text_tokens = set(_tokenize(combined_text))
        overlap = len(query_tokens & text_tokens)
        tag_bonus = sum(
            1 for tag in entry.get("tags", []) if _normalize_text(tag) in query_tokens
        )
        score = overlap + tag_bonus * 2
        ranked.append((score, entry))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in ranked]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def register(request):
    """POST /register {"url": "..."} → agent 自我註冊（可選 token 驗證）。"""
    if REGISTER_TOKEN and request.headers.get("x-registry-token") != REGISTER_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    data = await request.json()
    url = (data.get("url") or "").strip().rstrip("/")
    if not _is_valid_agent_url(url):
        return JSONResponse({"error": "invalid url"}, status_code=400)
    REGISTERED_URLS.add(url)
    logger.info("[registry] 收到註冊：{}", url)
    return JSONResponse({"ok": True, "registered": url})


async def _fetch_agent_catalogs() -> list[dict]:
    """從每個 agent 的 agent-card.json 建立 catalog entries。"""
    entries: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for agent_url in _all_urls():
            try:
                card_url = f"{agent_url.rstrip('/')}/.well-known/agent-card.json"
                response = await client.get(card_url)
                if response.status_code != 200:
                    logger.warning(
                        "[registry] 跳過 {}: HTTP {}", agent_url, response.status_code
                    )
                    continue

                agent_card = response.json()
                tags = []
                if isinstance(agent_card.get("skills"), list):
                    for skill in agent_card["skills"]:
                        tags.extend(skill.get("tags", []))

                entry = {
                    "identifier": f"urn:air:{agent_card.get('name', 'unknown').lower().replace(' ', '-')}:a2a",
                    "type": "application/a2a-agent-card+json",
                    "url": agent_url.rstrip("/"),
                    "displayName": agent_card.get("name", "Unknown Agent"),
                    "description": agent_card.get("description", ""),
                    "tags": list(dict.fromkeys(tags)),
                    "version": agent_card.get("version", "0.0.0"),
                }
                entries.append(entry)
                logger.info(
                    "[registry] 已發現 agent：{} @ {}", entry["displayName"], agent_url
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[registry] 跳過 {}: {}", agent_url, exc)
    return entries


async def ai_catalog_handler(request):
    """GET /.well-known/ai-catalog.json - 返回聚合 catalog。"""
    entries = await _fetch_agent_catalogs()
    write_catalog_file(entries)
    return JSONResponse(build_catalog_payload(entries))


async def search_handler(request):
    """POST /search - 使用 embeddings 或關鍵字回退做語義搜尋。"""
    try:
        body = await request.json()
        query_text = (body.get("query") or {}).get("text", "").strip()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"invalid request: {exc}"}, status_code=400)

    if not query_text:
        return JSONResponse({"results": []})

    entries = await _fetch_agent_catalogs()
    if not entries:
        return JSONResponse({"results": []})

    ranked_entries = rank_catalog_entries(entries, query_text)

    try:
        client = AsyncOpenAI()
        docs = [
            f"{entry.get('displayName', '')} {entry.get('description', '')} {' '.join(entry.get('tags', []))}"
            for entry in entries
        ]
        embeddings_response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=docs + [query_text],
        )
        vectors = [item.embedding for item in embeddings_response.data]
        query_vector = vectors[-1]
        similarities = [
            _cosine_similarity(query_vector, vector) for vector in vectors[:-1]
        ]
        ranked_pairs = sorted(
            zip(entries, similarities),
            key=lambda item: item[1],
            reverse=True,
        )
        ranked_entries = [entry for entry, _ in ranked_pairs]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[registry] embedding search failed, fallback to keyword search: {}", exc
        )

    results = []
    for entry in ranked_entries[:3]:
        results.append(
            {
                "identifier": entry.get("identifier", ""),
                "displayName": entry.get("displayName", "Unknown"),
                "description": entry.get("description", ""),
                "url": entry.get("url", ""),
                "type": entry.get("type", "unknown"),
                "score": 0.0,
                "tags": entry.get("tags", []),
            }
        )

    if results and len(results) == 3:
        results[0]["score"] = 1.0
    return JSONResponse({"results": results})


async def list_agents(request):
    """GET /agents[?skill=<id>] → 回傳目錄（可依 skill 篩選）。"""
    wanted_skill = request.query_params.get("skill")

    result = []
    for entry in await _fetch_agent_catalogs():
        skill_ids = entry.get("tags", [])
        if wanted_skill and wanted_skill not in skill_ids:
            continue
        result.append(
            {
                "name": entry.get("displayName"),
                "url": entry.get("url"),
                "description": entry.get("description"),
                "skills": [
                    {"id": tag, "name": tag, "description": tag}
                    for tag in entry.get("tags", [])
                ],
            }
        )
    return JSONResponse(result)


app = Starlette(
    routes=[
        Route("/.well-known/ai-catalog.json", ai_catalog_handler),
        Route("/search", search_handler, methods=["POST"]),
        Route("/agents", list_agents),
        Route("/register", register, methods=["POST"]),
    ]
)

# 跟 agent 同一套 middleware，兩點差異：
#   - required_scope 清成 None：讀目錄只驗身分，不需要 code_review.invoke scope。
#   - /register 走自己的 x-registry-token，豁免 OAuth。
_auth_config = load_auth_config()
if _auth_config is not None:
    app.add_middleware(
        OAuth2Middleware,
        config=replace(_auth_config, required_scope=None),
        public_path_prefixes=("/register",),
    )
    logger.info("[registry] OAuth2/OIDC 驗證已啟用（issuer={}）", _auth_config.issuer)
else:
    logger.warning("[registry] 未設 A2A_OIDC_*，GET /agents /search 以無認證模式開放")


if __name__ == "__main__":
    logger.info("[registry] curated agents: {}", CURATED_URLS)
    uvicorn.run(app, host=HOST, port=PORT)
