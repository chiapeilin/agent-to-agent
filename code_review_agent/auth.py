"""A2A 的 OAuth2 / OIDC 驗證：server 端驗票、client 端取票，共用一份設定。

server：一個 ASGI middleware，驗每個請求的 Bearer JWT（簽章 + iss/aud/exp），
再視需要查 scope；Agent Card（/.well-known/）依規範免認證發現。
client：用 client_credentials flow 換 token 並自動帶上。

全靠環境變數開關，issuer + audience 兩個必填變數沒設就整個關閉（無認證，僅供本機開發）：
    A2A_OIDC_ISSUER / A2A_OIDC_AUDIENCE   必填，開啟 server 端驗票
    A2A_OIDC_JWKS_URL / A2A_OIDC_ALGORITHMS / A2A_REQUIRED_SCOPE   選填
    A2A_OAUTH_TOKEN_URL / _CLIENT_ID / _CLIENT_SECRET / _SCOPE     client 端取票
"""

import logging
import os
import time
from dataclasses import dataclass

import anyio
import httpx
import jwt
from a2a.client import AuthInterceptor, ClientCallContext, CredentialService
from a2a.types import (
    OpenIdConnectSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from jwt import PyJWKClient, PyJWKClientConnectionError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_SCHEME_NAME = "oidc"


def setup_auth_logging() -> None:
    """讓 auth 的 INFO log 印得出來（uvicorn 預設把 root 卡在 WARNING 會吞掉）。

    只替本套件 logger 掛專屬 handler，避開 httpx / openai 的雜訊。entry point 呼叫一次。
    """
    pkg_logger = logging.getLogger("code_review_agent")
    if pkg_logger.handlers:  # 已設過就不重複掛
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(logging.INFO)
    pkg_logger.propagate = False


class _IdPUnavailable(Exception):
    """取不到 JWKS / 連不上 IdP —— 基礎設施問題，與 token 本身無關（回 503）。"""


@dataclass(frozen=True)
class AuthConfig:
    """從環境變數解析出的驗證設定。"""

    issuer: str
    audience: str
    jwks_url: str | None
    algorithms: tuple[str, ...]
    required_scope: str | None  # None = 只驗身分，不查 scope

    @property
    def openid_config_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"


def load_auth_config() -> AuthConfig | None:
    """有設 issuer + audience 才回傳設定；否則回 None（= 關閉認證）。"""
    issuer = os.environ.get("A2A_OIDC_ISSUER")
    audience = os.environ.get("A2A_OIDC_AUDIENCE")
    if not (issuer and audience):
        return None

    algorithms = tuple(
        a.strip()
        for a in os.environ.get("A2A_OIDC_ALGORITHMS", "RS256").split(",")
        if a.strip()
    )
    return AuthConfig(
        issuer=issuer.rstrip("/"),
        audience=audience,
        jwks_url=os.environ.get("A2A_OIDC_JWKS_URL"),
        algorithms=algorithms,
        required_scope=os.environ.get("A2A_REQUIRED_SCOPE"),
    )


def build_card_security(
    config: AuthConfig,
) -> tuple[dict[str, SecurityScheme], list[SecurityRequirement]]:
    """宣告 AgentCard 的 OIDC scheme；client 端的 AuthInterceptor 看到就會自動帶 token。"""
    scheme = SecurityScheme(
        open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
            description="OIDC / OAuth2 Bearer JWT。請帶 Authorization: Bearer <token>。",
            open_id_connect_url=config.openid_config_url,
        )
    )
    scopes = [config.required_scope] if config.required_scope else []
    requirement = SecurityRequirement(
        schemes={_SCHEME_NAME: StringList(list=scopes)}
    )
    return {_SCHEME_NAME: scheme}, [requirement]


def _granted_scopes(claims: dict) -> list[str]:
    """從 claims 取出已授予的 scope。相容 `scope`（空白分隔字串）與 `scp`（陣列）。"""
    raw = claims.get("scope") or claims.get("scp")
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


class OAuth2Middleware:
    """純 ASGI middleware：驗 JWT + 查 scope。放行則原樣轉給內層（不緩衝 SSE 串流）。"""

    def __init__(
        self,
        app: ASGIApp,
        config: AuthConfig,
        public_path_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.config = config
        # 免 OAuth 的路徑前綴（如 registry 的 /register，走自己的 x-registry-token）。
        self._public_prefixes = tuple(public_path_prefixes)
        self._jwk_client: PyJWKClient | None = None

    def _client(self) -> PyJWKClient:
        # 首用才建；沒給 jwks_url 就走 discovery。之後由 PyJWKClient 快取。
        if self._jwk_client is None:
            jwks_url = self.config.jwks_url or self._discover_jwks_url()
            self._jwk_client = PyJWKClient(jwks_url)
        return self._jwk_client

    def _discover_jwks_url(self) -> str:
        resp = httpx.get(self.config.openid_config_url, timeout=5)
        resp.raise_for_status()
        return resp.json()["jwks_uri"]

    def _verify(self, token: str) -> dict:
        # 阻塞式，由呼叫端丟 thread 跑。連不上 IdP/JWKS 是基礎設施故障（→ 503），
        # 轉 _IdPUnavailable 與「token 無效」（→ 401）區隔。
        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
        except (PyJWKClientConnectionError, httpx.HTTPError) as exc:
            raise _IdPUnavailable(str(exc)) from exc
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=list(self.config.algorithms),
            audience=self.config.audience,
            issuer=self.config.issuer,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Agent Card 依規範免認證發現；另放行呼叫端指定的豁免前綴。
        path = scope.get("path", "")
        if path.startswith("/.well-known/") or any(
            path.startswith(p) for p in self._public_prefixes
        ):
            await self.app(scope, receive, send)
            return

        auth = Headers(scope=scope).get("authorization", "")
        if not auth.startswith("Bearer "):
            await self._reject(scope, receive, send, 401, "invalid_token", "缺少 Bearer token")
            return
        token = auth[len("Bearer ") :].strip()

        try:
            claims = await anyio.to_thread.run_sync(self._verify, token)
        except _IdPUnavailable as exc:
            # 合法 token 也會走到這：回 503 而非 401，免得把基礎設施故障誤判成 token 壞掉。
            logger.error("OIDC 驗證失敗：連不上 IdP/JWKS：%s", exc)
            await self._reject_unavailable(scope, receive, send, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - JWT 本身的驗證失敗都視為 401
            logger.info("OIDC 驗證失敗：token 無效：%s", exc)
            await self._reject(scope, receive, send, 401, "invalid_token", str(exc))
            return

        # 有設 required_scope 才查 scope（沒設 = 只驗身分）
        if self.config.required_scope and self.config.required_scope not in _granted_scopes(claims):
            await self._reject_scope(scope, receive, send)
            return

        # 通過：記一筆讓 log 看得見認證有生效，並把 claims 傳給下游。
        logger.info(
            "OIDC ✓ 通過 %s  sub=%s client=%s scope=[%s]",
            path,
            claims.get("sub", "?"),
            claims.get("azp") or claims.get("client_id", "?"),
            " ".join(_granted_scopes(claims)) or "-",
        )
        scope.setdefault("state", {})["claims"] = claims
        await self.app(scope, receive, send)

    async def _reject(
        self, scope, receive, send, status: int, error: str, desc: str
    ) -> None:
        # WWW-Authenticate 只能是 latin-1（ASCII），中文描述放 JSON body。
        response = JSONResponse(
            {"error": error, "error_description": desc},
            status_code=status,
            headers={"WWW-Authenticate": f'Bearer error="{error}"'},
        )
        await response(scope, receive, send)

    async def _reject_unavailable(self, scope, receive, send, desc: str) -> None:
        # 非認證錯誤：不帶 WWW-Authenticate，用 503 + Retry-After 表示暫時性、可重試。
        response = JSONResponse(
            {"error": "temporarily_unavailable", "error_description": desc},
            status_code=503,
            headers={"Retry-After": "5"},
        )
        await response(scope, receive, send)

    async def _reject_scope(self, scope, receive, send) -> None:
        required = self.config.required_scope
        response = JSONResponse(
            {"error": "insufficient_scope", "required_scope": required},
            status_code=403,
            headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{required}"'},
        )
        await response(scope, receive, send)


# ─────────────────────────────────────────────────────────────
# Client 端：取 token 並帶上（供 test_client / registry_client 共用）
# ─────────────────────────────────────────────────────────────


class OAuth2Credentials(CredentialService):
    """client_credentials flow 取 token，快取到過期前重用。

    AuthInterceptor 每次送請求前呼叫 get_credentials()，回傳值會塞進 Bearer header。
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get_credentials(
        self, security_scheme_name: str, context: ClientCallContext | None
    ) -> str | None:
        """回傳有效的 access token；快取過期才重新去 token endpoint 換。"""
        # 留 60 秒緩衝，避免臨界過期
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope

        async with httpx.AsyncClient() as http:
            resp = await http.post(self._token_url, data=data)
            resp.raise_for_status()
            payload = resp.json()

        self._token = payload["access_token"]
        self._expires_at = time.time() + payload.get("expires_in", 3600)
        return self._token


def build_credentials() -> OAuth2Credentials | None:
    """A2A_OAUTH_* 都有設就回傳 credential service，沒設回 None（走無認證）。"""
    token_url = os.environ.get("A2A_OAUTH_TOKEN_URL")
    client_id = os.environ.get("A2A_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("A2A_OAUTH_CLIENT_SECRET")
    if not (token_url and client_id and client_secret):
        return None

    return OAuth2Credentials(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=os.environ.get("A2A_OAUTH_SCOPE"),
    )


def build_auth_interceptor() -> AuthInterceptor | None:
    """給 a2a client 用：有設 A2A_OAUTH_* 就回 AuthInterceptor，沒設回 None。"""
    creds = build_credentials()
    return AuthInterceptor(creds) if creds else None


async def bearer_header() -> dict[str, str]:
    """給非 a2a 的原始 httpx 呼叫用（如打 registry 的 GET /agents）。

    有設 A2A_OAUTH_* 回 {"Authorization": "Bearer <token>"}，沒設回空 dict（展開後等於不加）。
    """
    creds = build_credentials()
    if creds is None:
        return {}
    token = await creds.get_credentials(_SCHEME_NAME, None)
    return {"Authorization": f"Bearer {token}"} if token else {}
