"""最小 A2A client：直連 Translation Agent、送一段文字、印出翻譯結果。

先跑 server（uv run python -m translation_agent），再跑這支。
OAuth 用法見 shared/client.py。
"""

import asyncio
import os

from a2a.helpers import new_text_part
from dotenv import load_dotenv

from shared.client import send_and_print

load_dotenv()

AGENT_URL = os.environ.get("TRANSLATION_AGENT_URL", "http://127.0.0.1:8002")
REQUEST = "Hello, how are you? 幫我翻成中文"


if __name__ == "__main__":
    asyncio.run(send_and_print(AGENT_URL, [new_text_part(text=REQUEST)]))
