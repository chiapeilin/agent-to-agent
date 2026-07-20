"""共用的 A2A server 組裝：把 card 安全宣告與 Starlette app 建立抽成一處。"""

from typing import Callable

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard
from starlette.applications import Starlette

from shared.auth import AuthConfig, OAuth2Middleware, build_card_security


def apply_card_security(card: AgentCard, auth_config: AuthConfig | None) -> AgentCard:
    """有啟用認證才在 card 宣告 securityScheme；client 端看到才會帶 token。"""
    if auth_config is not None:
        schemes, requirements = build_card_security(auth_config)
        for name, scheme in schemes.items():
            card.security_schemes[name].CopyFrom(scheme)
        card.security_requirements.extend(requirements)
    return card


def build_agent_app(
    card: AgentCard,
    executor: AgentExecutor,
    *,
    rpc_path: str = "/jsonrpc",
    auth_config: AuthConfig | None = None,
    lifespan: Callable | None = None,
) -> Starlette:
    """把 executor 掛上 A2A server：開 agent-card + JSON-RPC 路由，有 OIDC 就掛驗證 middleware。"""
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(request_handler, rpc_path),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    if auth_config is not None:
        app.add_middleware(OAuth2Middleware, config=auth_config)
    return app
