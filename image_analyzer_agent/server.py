import base64
import os
from contextlib import asynccontextmanager

import uvicorn
from a2a.helpers import new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI
from starlette.applications import Starlette

from shared.auth import (
    AuthConfig,
    OAuth2Middleware,
    build_card_security,
    load_auth_config,
)

load_dotenv()

HOST = os.environ.get("IMAGE_ANALYZER_HOST", "127.0.0.1")
PORT = int(os.environ.get("IMAGE_ANALYZER_PORT", "8004"))
PUBLIC_URL = os.environ.get("IMAGE_ANALYZER_PUBLIC_URL", f"http://{HOST}:{PORT}")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")


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
    if auth_config is not None:
        schemes, requirements = build_card_security(auth_config)
        for name, scheme in schemes.items():
            card.security_schemes[name].CopyFrom(scheme)
        card.security_requirements.extend(requirements)
    return card


class ImageAnalyzerAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.client = AsyncOpenAI()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Analyzing image..."),
        )
        parts = context.message.parts or []
        image_parts = []
        text_parts = []
        for part in parts:
            if hasattr(part, "text") and getattr(part, "text"):
                text_parts.append(part.text)
            elif hasattr(part, "data") and getattr(part, "data"):
                media_type = getattr(part, "media_type", "image/jpeg")
                encoded = base64.b64encode(part.data).decode("utf-8")
                image_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    }
                )
        if not image_parts:
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("No image provided."),
            )
            return
        response = await self.client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Describe the image in detail."},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": " ".join(text_parts) or "Describe the image.",
                        },
                        *image_parts,
                    ],
                },
            ],
            max_tokens=1000,
        )
        result = response.choices[0].message.content or ""
        await updater.add_artifact(
            parts=[new_text_part(text=result, media_type="text/markdown")],
            name="image-analysis",
        )
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


@asynccontextmanager
async def lifespan(app):
    yield


def build_app() -> Starlette:
    auth_config = load_auth_config()
    card = build_agent_card(auth_config)
    request_handler = DefaultRequestHandler(
        agent_executor=ImageAnalyzerAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(request_handler, "/jsonrpc"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    if auth_config is not None:
        app.add_middleware(OAuth2Middleware, config=auth_config)
    return app


app = build_app()
