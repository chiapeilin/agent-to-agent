"""定義 AgentCard / AgentSkill，並把 executor 掛上 A2A server。

執行：uv run python -m code_review_agent
"""

import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette

from code_review_agent.agent_executor import CodeReviewAgentExecutor

HOST = os.environ.get("CODE_REVIEW_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODE_REVIEW_PORT", "9999"))
PUBLIC_URL = os.environ.get("CODE_REVIEW_PUBLIC_URL", f"http://{HOST}:{PORT}")

# agent 啟動時主動向這個 registry 報到（push / self-registration）
REGISTRY_URL = os.environ.get("REGISTRY_URL")
# registry 若要求註冊 token，兩邊設同一個值（防止任意人往 registry 亂註冊）
REGISTRY_REGISTER_TOKEN = os.environ.get("REGISTRY_REGISTER_TOKEN")


def build_agent_card() -> AgentCard:
    skill = AgentSkill(
        id="code_review",
        name="Code Review",
        description=(
            "讀取目標 repo 的原始碼檔案進行全面 code review，涵蓋正確性、安全性、可讀性、"
            "架構一致性、功能性、效能，並檢測 code bad smells。支援 Rust / TypeScript / "
            "Svelte / Tauri v2 等語言的客製化標準。"
        ),
        input_modes=["text/plain"],
        output_modes=["text/markdown"],
        tags=["code-review", "security", "rust", "typescript", "svelte", "tauri"],
        examples=[
            "幫我 review 現有的程式碼架構",
            "檢查專案有沒有安全性問題",
        ],
    )

    return AgentCard(
        name="Code Review Agent",
        description="專業跨語言 code review agent，把 code-review skill 包成 A2A 服務。",
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/markdown"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=PUBLIC_URL,  # 與註冊給 registry 的 URL 一致（反代/容器後也正確）
                protocol_version="1.0",
            )
        ],
        skills=[skill],
    )


async def _register_with_registry() -> None:
    """啟動時向 registry 報到。registry 掛了也不影響 agent 自己。"""
    if not REGISTRY_URL:
        return
    headers = {}
    if REGISTRY_REGISTER_TOKEN:
        headers["x-registry-token"] = REGISTRY_REGISTER_TOKEN
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            await http.post(
                f"{REGISTRY_URL}/register", json={"url": PUBLIC_URL}, headers=headers
            )
        print(f"[agent] 已向 registry 註冊：{PUBLIC_URL} → {REGISTRY_URL}")
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] 向 registry 註冊失敗（不影響 agent 運作）：{exc}")


@asynccontextmanager
async def _lifespan(app):
    await _register_with_registry()  # server 啟動時自我註冊
    yield


def build_app() -> Starlette:
    """把 executor 掛上 A2A server，開兩組路由。"""
    card = build_agent_card()

    request_handler = DefaultRequestHandler(
        agent_executor=CodeReviewAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(request_handler, "/"),
    ]
    return Starlette(routes=routes, lifespan=_lifespan)


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT)
