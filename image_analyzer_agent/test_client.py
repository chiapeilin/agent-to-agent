"""最小 A2A client：直連 Image Analyzer Agent、送圖片、印出影像描述。

先跑 server（uv run python -m image_analyzer_agent），再跑這支。

用法（參數都可省略，會用預設值 / 環境變數）：
    uv run python -m image_analyzer_agent.test_client "描述這張圖片" --url https://.../cat.jpg
    prompt       文字提示（預設 "描述這張圖片"）
    --url        圖片網址；預設讀 IMAGE_URL
    --agent-url  agent base URL；預設讀 IMAGE_ANALYZER_AGENT_URL

OAuth 用法見 shared/client.py。
"""

import argparse
import asyncio
import os

from a2a.helpers import new_text_part, new_url_part
from dotenv import load_dotenv

from shared.client import send_and_print

load_dotenv()

DEFAULT_AGENT_URL = os.environ.get("IMAGE_ANALYZER_AGENT_URL", "http://127.0.0.1:8004")
DEFAULT_IMAGE_URL = os.environ.get(
    "IMAGE_URL",
    "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg",
)

MEDIA_TYPES = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="直連 Image Analyzer Agent 送圖片測試")
    parser.add_argument("prompt", nargs="?", default="描述這張圖片", help="給模型的文字提示")
    parser.add_argument("--url", default=DEFAULT_IMAGE_URL, help="要分析的圖片網址（http/https）")
    parser.add_argument("--agent-url", default=DEFAULT_AGENT_URL, help="agent base URL")
    return parser.parse_args()


def media_type_for(url: str) -> str:
    """從副檔名粗略推測 media type，取不到就退回 image/jpeg。"""
    ext = os.path.splitext(url.lower().split("?", 1)[0])[1]
    return MEDIA_TYPES.get(ext, "image/jpeg")


async def main() -> None:
    args = parse_args()
    # 一則訊息帶兩個 part：文字 prompt + A2A url image part。
    parts = [
        new_text_part(text=args.prompt),
        new_url_part(url=args.url, media_type=media_type_for(args.url)),
    ]
    await send_and_print(args.agent_url, parts)


if __name__ == "__main__":
    asyncio.run(main())
