import base64
import os
import re
from contextlib import asynccontextmanager

import httpx
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
from google.protobuf.json_format import MessageToDict
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
TEXT_URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')


def _preview(text: str, limit: int = 80) -> str:
    """Truncate long content for A2A-ARD-compatible part diagnostics."""
    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return repr(text)
    return repr(text[:limit]) + f"...(+{len(text) - limit} chars)"


def describe_part(index: int, part) -> str:
    """Describe one A2A ``Part`` using the same fields as A2A-ARD."""
    kind = part.WhichOneof("content")
    fields = [f"type={kind or 'UNSET'}"]
    if kind == "text":
        fields.extend((f"chars={len(part.text)}", f"text={_preview(part.text)}"))
    elif kind == "url":
        fields.append(f"url={part.url}")
    elif kind == "raw":
        fields.extend((f"bytes={len(part.raw)}", f"magic={part.raw[:8].hex() or '-'}"))
    elif kind == "data":
        fields.append(f"data={_preview(str(MessageToDict(part.data)))}")
    fields.extend(
        (f"media_type={part.media_type or '-'}", f"filename={part.filename or '-'}")
    )
    return f"[image-analyzer]   part[{index}] " + " ".join(fields)


def message_parts_to_openai_content(parts) -> tuple[list[str], list[dict]]:
    """Convert A2A text, URL, and raw image parts to OpenAI vision content.

    A2A ``Part`` stores its payload in a protobuf ``oneof``. URL parts use
    ``url`` and uploaded files use ``raw``; neither is represented by the
    legacy ``data`` field.
    """
    text_parts: list[str] = []
    image_parts: list[dict] = []
    for index, part in enumerate(parts):
        content_type = part.WhichOneof("content")
        print(describe_part(index, part))
        if content_type == "text":
            text_parts.append(part.text)
        elif content_type == "url":
            image_parts.append(
                {"type": "image_url", "image_url": {"url": part.url}}
            )
        elif content_type == "raw":
            media_type = part.media_type or "image/jpeg"
            encoded = base64.b64encode(part.raw).decode("utf-8")
            image_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                }
            )
    return text_parts, image_parts


def image_urls_from_text(text_parts: list[str]) -> list[str]:
    """Return HTTP(S) URLs embedded in text parts, as A2A-ARD does."""
    return TEXT_URL_PATTERN.findall(" ".join(text_parts))


async def local_image_as_data_uri(url: str) -> str | None:
    """Fetch a local image so OpenAI can receive bytes it cannot fetch itself."""
    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(url, timeout=10.0)
            response.raise_for_status()
    except httpx.HTTPError:
        return None

    media_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
    encoded = base64.b64encode(response.content).decode("utf-8")
    return f"data:{media_type};base64,{encoded}"


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
        text_parts, image_parts = message_parts_to_openai_content(
            context.message.parts or []
        )

        # Match A2A-ARD's compatibility fallback for clients that place an
        # image URL in plain text instead of sending a formal A2A url part.
        if not image_parts:
            for url in image_urls_from_text(text_parts):
                if "localhost" in url or "127.0.0.1" in url:
                    data_uri = await local_image_as_data_uri(url)
                    if data_uri:
                        image_parts.append(
                            {"type": "image_url", "image_url": {"url": data_uri}}
                        )
                else:
                    image_parts.append(
                        {"type": "image_url", "image_url": {"url": url}}
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
            max_completion_tokens=1000,
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
