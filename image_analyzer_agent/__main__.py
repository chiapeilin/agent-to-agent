"""定義 Image Analyzer Agent 的 AgentCard，並把 executor 掛上 A2A server。

執行：uv run python -m image_analyzer_agent
"""

import os

import uvicorn
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from dotenv import load_dotenv
from starlette.applications import Starlette

from image_analyzer_agent.agent_executor import ImageAnalyzerAgentExecutor
from shared.auth import AuthConfig, load_auth_config
from shared.server import apply_card_security, build_agent_app

load_dotenv()

HOST = os.environ.get("IMAGE_ANALYZER_HOST", "127.0.0.1")
PORT = int(os.environ.get("IMAGE_ANALYZER_PORT", "8004"))
PUBLIC_URL = os.environ.get("IMAGE_ANALYZER_PUBLIC_URL", f"http://{HOST}:{PORT}")


def build_agent_card(auth_config: AuthConfig | None = None) -> AgentCard:
    skill = AgentSkill(
        id="image_analysis",
        name="Image Analysis",
        description="Analyze uploaded images and describe them.",
        input_modes=["image/jpeg", "image/png", "text/plain"],
        output_modes=["text/markdown"],
        tags=["image", "vision", "analysis"],
        examples=["describe this image"],
    )
    card = AgentCard(
        name="Image Analyzer Agent",
        description="Analyzes images using vision models",
        version="0.1.0",
        default_input_modes=["image/jpeg", "image/png", "text/plain"],
        default_output_modes=["text/markdown"],
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
    return build_agent_app(
        card, ImageAnalyzerAgentExecutor(), auth_config=auth_config
    )


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT)
