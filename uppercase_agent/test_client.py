"""最小 A2A client：直連 Uppercase Agent、送一段文字、印出轉大寫結果。

先跑 server（uv run python -m uppercase_agent），再跑這支。

若 server 的 Agent Card 有宣告 OAuth2，設定以下環境變數即可自動帶 token：
    A2A_OAUTH_TOKEN_URL      OAuth token endpoint
    A2A_OAUTH_CLIENT_ID      client id
    A2A_OAUTH_CLIENT_SECRET  client secret
    A2A_OAUTH_SCOPE          （選填）scope，空白分隔
沒設這些變數時就走無認證連線。
"""

import asyncio
import os

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_message, new_text_part
from a2a.types import Role, SendMessageRequest
from dotenv import load_dotenv
from loguru import logger

from shared.auth import bearer_header, build_auth_interceptor

# 讀取 repo 根目錄的 .env，讓 A2A_OAUTH_* 免手動 export
load_dotenv()

AGENT_URL = os.environ.get("UPPERCASE_AGENT_URL", "http://127.0.0.1:8003")

REQUEST = "hello world 幫我改成大寫"


async def main() -> None:
    # Card 未宣告 security requirements 時也帶 Bearer。
    headers = await bearer_header()
    config = ClientConfig(
        httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(120.0), headers=headers)
    )

    # 有設 OAuth 環境變數就掛 interceptor，token 會自動加到每次請求
    auth = build_auth_interceptor()
    interceptors = [auth] if auth else None

    # create_client 會自動去抓 /.well-known/agent-card.json 解析出 AgentCard
    client = await create_client(AGENT_URL, config, interceptors=interceptors)

    request = SendMessageRequest(
        message=new_message(parts=[new_text_part(text=REQUEST)], role=Role.ROLE_USER)
    )

    logger.info("送出需求到 {} ...", AGENT_URL)
    async for event in client.send_message(request):
        # server 會依序推 task / status_update / artifact_update 等事件
        text = get_stream_response_text(event)
        if text:
            print(text)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
