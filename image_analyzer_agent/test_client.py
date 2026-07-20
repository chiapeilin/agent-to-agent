"""最小 A2A client：直連 Image Analyzer Agent、送圖片、印出影像描述。

先跑 server（uv run python -m image_analyzer_agent），再跑這支。

用法：
    uv run python -m image_analyzer_agent.test_client "描述這張圖片" --url https://.../cat.jpg

參數（都可省略，會用預設值）：
    prompt         位置參數，給模型的文字提示（預設 "Describe this image in detail."）
    --url          要分析的圖片網址（http/https）
    --agent-url    agent base URL（預設 http://127.0.0.1:8004）

對應環境變數（CLI 參數優先於環境變數）：
    IMAGE_URL                  預設圖片網址
    IMAGE_ANALYZER_AGENT_URL   預設 agent base URL

若 server 的 Agent Card 有宣告 OAuth2，設定以下環境變數即可自動帶 token：
    A2A_OAUTH_TOKEN_URL      OAuth token endpoint
    A2A_OAUTH_CLIENT_ID      client id
    A2A_OAUTH_CLIENT_SECRET  client secret
    A2A_OAUTH_SCOPE          （選填）scope，空白分隔
沒設這些變數時就走無認證連線。
"""

import argparse
import asyncio
import os

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import (
    get_stream_response_text,
    new_message,
    new_text_part,
    new_url_part,
)
from a2a.types import Role, SendMessageRequest
from dotenv import load_dotenv
from loguru import logger

from shared.auth import bearer_header, build_auth_interceptor

# 讀取 repo 根目錄的 .env，讓 A2A_OAUTH_* 免手動 export
load_dotenv()

DEFAULT_AGENT_URL = os.environ.get("IMAGE_ANALYZER_AGENT_URL", "http://127.0.0.1:8004")
DEFAULT_IMAGE_URL = os.environ.get(
    "IMAGE_URL",
    "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="直連 Image Analyzer Agent 送圖片測試")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="描述這張圖片",
        help="給模型的文字提示",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_IMAGE_URL,
        help="要分析的圖片網址（http/https）",
    )
    parser.add_argument(
        "--agent-url",
        default=DEFAULT_AGENT_URL,
        help=f"agent base URL（預設：{DEFAULT_AGENT_URL}）",
    )
    return parser.parse_args()


def _media_type_for(url: str) -> str:
    """從副檔名粗略推測 media type，取不到就退回 image/jpeg。"""
    lowered = url.lower().split("?", 1)[0]
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith((".gif",)):
        return "image/gif"
    if lowered.endswith((".webp",)):
        return "image/webp"
    return "image/jpeg"


async def main() -> None:
    args = parse_args()

    # Card 未宣告 security requirements 時也帶 Bearer。
    headers = await bearer_header()
    config = ClientConfig(
        httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(120.0), headers=headers)
    )

    # 有設 OAuth 環境變數就掛 interceptor，token 會自動加到每次請求
    auth = build_auth_interceptor()
    interceptors = [auth] if auth else None

    # create_client 會自動去抓 /.well-known/agent-card.json 解析出 AgentCard
    client = await create_client(args.agent_url, config, interceptors=interceptors)

    # 一則訊息帶兩個 part：文字 prompt + 正式的 A2A url image part。
    request = SendMessageRequest(
        message=new_message(
            parts=[
                new_text_part(text=args.prompt),
                new_url_part(url=args.url, media_type=_media_type_for(args.url)),
            ],
            role=Role.ROLE_USER,
        )
    )

    logger.info("送出圖片 {} 到 {} ...", args.url, args.agent_url)
    async for event in client.send_message(request):
        # server 會依序推 task / status_update / artifact_update 等事件
        text = get_stream_response_text(event)
        if text:
            print(text)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
