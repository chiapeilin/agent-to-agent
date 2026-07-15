"""最小 curated A2A registry：維護可信 agent 清單，提供 GET /agents 供 client 查詢 / 依 skill 篩選。

執行：uv run python registry.py    （預設 127.0.0.1:8000）
"""

import os
from urllib.parse import urlparse

import httpx
import uvicorn
from a2a.client.card_resolver import A2ACardResolver
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

HOST = os.environ.get("REGISTRY_HOST", "127.0.0.1")
PORT = int(os.environ.get("REGISTRY_PORT", "8000"))

# curated 白名單：只有列在這裡的 agent 才會被收錄。
CURATED_URLS = [
    u.strip()
    for u in os.environ.get("REGISTRY_AGENT_URLS", "http://127.0.0.1:9999").split(",")
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


async def register(request):
    """POST /register {"url": "..."} → agent 自我註冊（可選 token 驗證）。"""
    if REGISTER_TOKEN and request.headers.get("x-registry-token") != REGISTER_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    data = await request.json()
    url = (data.get("url") or "").strip().rstrip("/")
    if not _is_valid_agent_url(url):
        return JSONResponse({"error": "invalid url"}, status_code=400)
    REGISTERED_URLS.add(url)
    print(f"[registry] 收到註冊：{url}")
    return JSONResponse({"ok": True, "registered": url})


async def _resolve_cards() -> list[tuple[str, object]]:
    """對每個 URL 抓名片；抓不到的（agent 沒開）就略過，不收錄。"""
    resolved = []
    async with httpx.AsyncClient(timeout=10) as http:
        for url in _all_urls():
            try:
                card = await A2ACardResolver(http, url).get_agent_card()
                resolved.append((url, card))
            except Exception as exc:  # noqa: BLE001
                print(f"[registry] 跳過 {url}: {exc}")
    return resolved


async def list_agents(request):
    """GET /agents[?skill=<id>] → 回傳目錄（可依 skill 篩選）。"""
    wanted_skill = request.query_params.get("skill")

    result = []
    for url, card in await _resolve_cards():
        skill_ids = [s.id for s in card.skills]
        if wanted_skill and wanted_skill not in skill_ids:
            continue
        result.append(
            {
                "name": card.name,
                "url": url,
                "description": card.description,
                "skills": [
                    {"id": s.id, "name": s.name, "description": s.description}
                    for s in card.skills
                ],
            }
        )
    return JSONResponse(result)


app = Starlette(
    routes=[
        Route("/agents", list_agents),
        Route("/register", register, methods=["POST"]),
    ]
)


if __name__ == "__main__":
    print(f"[registry] curated agents: {CURATED_URLS}")
    uvicorn.run(app, host=HOST, port=PORT)
