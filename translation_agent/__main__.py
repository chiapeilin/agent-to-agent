"""定義 Translation Agent 的 AgentCard，並把 executor 掛上 A2A server。

執行：uv run python -m translation_agent
"""

import os

import uvicorn
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from dotenv import load_dotenv
from starlette.applications import Starlette

from shared.auth import AuthConfig, load_auth_config
from shared.server import apply_card_security, build_agent_app
from translation_agent.agent_executor import TranslationAgentExecutor

load_dotenv()

HOST = os.environ.get("TRANSLATION_HOST", "127.0.0.1")
PORT = int(os.environ.get("TRANSLATION_PORT", "8002"))
PUBLIC_URL = os.environ.get("TRANSLATION_PUBLIC_URL", f"http://{HOST}:{PORT}")


def build_agent_card(auth_config: AuthConfig | None = None) -> AgentCard:
    skill = AgentSkill(
        id="translation",
        name="Translation",
        description="Translate English and Chinese text bidirectionally.",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["translation", "translate", "english", "chinese"],
        examples=["Hello, how are you?", "你好嗎？"],
    )
    card = AgentCard(
        name="Translation Agent",
        description="Bilingual translation agent",
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=f"{PUBLIC_URL}/jsonrpc",
                protocol_version="1.0",
            )
        ],
        skills=[skill],
    )
    return apply_card_security(card, auth_config)


def build_app() -> Starlette:
    auth_config = load_auth_config()
    card = build_agent_card(auth_config)
    return build_agent_app(card, TranslationAgentExecutor(), auth_config=auth_config)


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT)
