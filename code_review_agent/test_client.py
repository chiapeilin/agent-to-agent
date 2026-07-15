"""最小 A2A client：直連 Code Review Agent、送需求、印出 review。

先跑 server（uv run python -m code_review_agent），再跑這支。
"""

import asyncio
import os

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_message, new_text_part
from a2a.types import Role, SendMessageRequest

AGENT_URL = os.environ.get("CODE_REVIEW_AGENT_URL", "http://127.0.0.1:9999")

REQUEST = "幫我 review 現有的程式碼架構"


async def main() -> None:
    config = ClientConfig(httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(120.0)))

    # create_client 會自動去抓 /.well-known/agent-card.json 解析出 AgentCard
    client = await create_client(AGENT_URL, config)

    request = SendMessageRequest(
        message=new_message(parts=[new_text_part(text=REQUEST)], role=Role.ROLE_USER)
    )

    print(f"送出需求到 {AGENT_URL} ...\n")
    async for event in client.send_message(request):
        # server 會依序推 task / status_update / artifact_update 等事件
        text = get_stream_response_text(event)
        if text:
            print(text)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
