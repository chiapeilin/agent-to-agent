import os
from contextlib import asynccontextmanager

import uvicorn
from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
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
from starlette.applications import Starlette

from shared.auth import (
    AuthConfig,
    OAuth2Middleware,
    build_card_security,
    load_auth_config,
)

load_dotenv()

HOST = os.environ.get("UPPERCASE_HOST", "127.0.0.1")
PORT = int(os.environ.get("UPPERCASE_PORT", "8003"))
PUBLIC_URL = os.environ.get("UPPERCASE_PUBLIC_URL", f"http://{HOST}:{PORT}")


def build_agent_card(auth_config: AuthConfig | None = None) -> AgentCard:
    skill = AgentSkill(
        id="uppercase",
        name="Uppercase",
        description="Convert text to uppercase.",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["uppercase", "transform", "text"],
        examples=["hello world"],
    )
    card = AgentCard(
        name="Uppercase Agent",
        description="Converts text to uppercase",
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
    if auth_config is not None:
        schemes, requirements = build_card_security(auth_config)
        for name, scheme in schemes.items():
            card.security_schemes[name].CopyFrom(scheme)
        card.security_requirements.extend(requirements)
    return card


class UppercaseAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Converting..."),
        )
        user_text = get_message_text(context.message)
        await updater.add_artifact(
            parts=[new_text_part(text=f"Uppercase: {user_text.upper()}")],
            name="uppercase-response",
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
        agent_executor=UppercaseAgentExecutor(),
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
