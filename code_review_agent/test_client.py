"""最小 A2A client：直連 Code Review Agent、送需求、印出 review。

先跑 server（uv run python -m code_review_agent），再跑這支。
OAuth 用法見 shared/client.py。
"""

import asyncio
import os

from a2a.helpers import new_text_part
from dotenv import load_dotenv

from shared.client import send_and_print

load_dotenv()

AGENT_URL = os.environ.get("CODE_REVIEW_AGENT_URL", "http://127.0.0.1:8001")
REQUEST = "幫我 review 現有的程式碼架構"


if __name__ == "__main__":
    asyncio.run(send_and_print(AGENT_URL, [new_text_part(text=REQUEST)]))
