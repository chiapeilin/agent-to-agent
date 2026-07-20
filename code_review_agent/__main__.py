"""定義 AgentCard / AgentSkill，並把 executor 掛上 A2A server。

執行：uv run python -m code_review_agent
"""

import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from dotenv import load_dotenv
from loguru import logger
from starlette.applications import Starlette

from code_review_agent.agent_executor import CodeReviewAgentExecutor
from shared.auth import AuthConfig, bearer_header, load_auth_config
from shared.server import apply_card_security, build_agent_app

# 顯式載入 .env：認證設定（A2A_OIDC_*）沒讀到會靜默退回無認證，別靠 import 副作用。
load_dotenv()

HOST = os.environ.get("CODE_REVIEW_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODE_REVIEW_PORT", "8001"))
PUBLIC_URL = os.environ.get("CODE_REVIEW_PUBLIC_URL", f"http://{HOST}:{PORT}")

# 啟動時主動向 registry 報到（push）；token 兩邊設同值，防止任意人亂註冊。
REGISTRY_URL = os.environ.get("REGISTRY_URL")
REGISTRY_REGISTER_TOKEN = os.environ.get("REGISTRY_REGISTER_TOKEN")


def build_agent_card(auth_config: AuthConfig | None = None) -> AgentCard:
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

    card = AgentCard(
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
    return apply_card_security(card, auth_config)


async def _register_with_registry() -> None:
    """啟動時向 registry 報到。registry 掛了也不影響 agent 自己。"""
    if not REGISTRY_URL:
        return
    # 內部呼叫一律帶 Bearer。
    headers = await bearer_header()
    if REGISTRY_REGISTER_TOKEN:
        headers["x-registry-token"] = REGISTRY_REGISTER_TOKEN
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            await http.post(
                f"{REGISTRY_URL}/register", json={"url": PUBLIC_URL}, headers=headers
            )
        logger.info("[agent] 已向 registry 註冊：{} → {}", PUBLIC_URL, REGISTRY_URL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent] 向 registry 註冊失敗（不影響 agent 運作）：{}", exc)


@asynccontextmanager
async def _lifespan(app):
    await _register_with_registry()  # server 啟動時自我註冊
    yield


def build_app() -> Starlette:
    """把 executor 掛上 A2A server（JSON-RPC 掛在 "/"），並在啟動時向 registry 報到。"""
    auth_config = load_auth_config()
    card = build_agent_card(auth_config)
    if auth_config is not None:
        logger.info("[agent] OAuth2/OIDC 驗證已啟用（issuer={}）", auth_config.issuer)
    else:
        logger.warning("[agent] 未設 A2A_OIDC_*，以無認證模式啟動（僅適合本機開發）")
    return build_agent_app(
        card,
        CodeReviewAgentExecutor(),
        rpc_path="/",
        auth_config=auth_config,
        lifespan=_lifespan,
    )


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT)
