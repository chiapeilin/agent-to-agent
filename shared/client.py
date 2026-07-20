"""共用的最小 A2A client：連上 agent、送出 message parts、串流印出回應。

有設 A2A_OAUTH_* 環境變數就自動換 token 並帶上；沒設就走無認證連線。
"""

from typing import Sequence

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_message
from a2a.types import Part, Role, SendMessageRequest
from loguru import logger

from shared.auth import build_auth_interceptor


async def send_and_print(agent_url: str, parts: Sequence[Part]) -> None:
    """連上 *agent_url*，送出 *parts* 組成的一則 user message，把串流回應印到 stdout。"""
    # Bearer 一律由 interceptor 注入（card 未宣告 security 時也會帶），不再另外塞 httpx header。
    config = ClientConfig(httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(120.0)))
    auth = build_auth_interceptor()
    client = await create_client(
        agent_url, config, interceptors=[auth] if auth else None
    )

    request = SendMessageRequest(
        message=new_message(parts=list(parts), role=Role.ROLE_USER)
    )
    logger.info("送出需求到 {} ...", agent_url)
    try:
        async for event in client.send_message(request):
            text = get_stream_response_text(event)
            if text:
                print(text)
    finally:
        await client.close()
