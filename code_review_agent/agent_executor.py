"""code-review skill 的 A2A 執行層：
收到需求 → 讀 REPO_PATH 原始碼 → 套 system prompt 做 review → 回傳 artifact。
"""

import os
import subprocess
from pathlib import Path

from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from dotenv import load_dotenv
from openai import AsyncOpenAI

# 須在建立 OpenAI client 前載入 .env（OPENAI_API_KEY 等）。
load_dotenv()

_PROMPT_PATH = Path(__file__).parent / "prompt.md"
REPO_PATH = os.environ.get("CODE_REVIEW_REPO_PATH", ".")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

_system_prompt: str | None = None


def _load_system_prompt() -> str:
    """延後讀取並快取 system prompt；缺檔時給明確錯誤，避免 import 期 crash。"""
    global _system_prompt
    if _system_prompt is None:
        try:
            _system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"讀不到 code review 的 system prompt：{_PROMPT_PATH}（{exc}）"
            ) from exc
    return _system_prompt


class CodeReviewAgent:
    """實際做 code review 的 agent —— 呼叫 OpenAI Chat Completions API。"""

    def __init__(self) -> None:
        self.client = AsyncOpenAI()

    async def review(self, code: str, user_request: str) -> str:
        # skill 內容當 system prompt；使用者需求 + 專案原始碼當 user message。
        resp = await self.client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _load_system_prompt()},
                {
                    "role": "user",
                    "content": (
                        f"使用者的需求：{user_request}\n\n"
                        f"以下是要 review 的專案原始碼檔案：\n\n{code}"
                    ),
                },
            ],
        )
        return resp.choices[0].message.content or ""


_DENY_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519"}
_DENY_SUFFIXES = {".pem", ".key", ".p12"}
_MAX_FILE_BYTES = 100_000


def _read_reviewable(rel: str) -> str | None:
    """讀單一檔並回傳內容；機密檔、太大、二進位、讀不到都回 None。"""
    name = os.path.basename(rel)
    if name in _DENY_NAMES or os.path.splitext(name)[1].lower() in _DENY_SUFFIXES:
        return None
    path = os.path.join(REPO_PATH, rel)
    try:
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            return None
        data = Path(path).read_bytes()
    except OSError:
        return None
    if b"\x00" in data:  # 含 NUL byte → 視為二進位
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _collect_code() -> str:
    """收集 REPO_PATH 下可 review 的文字檔（git 追蹤、尊重 .gitignore）給 LLM。"""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_PATH, capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    parts = [
        f"### {rel}\n```\n{content}\n```"
        for rel in listed
        if (content := _read_reviewable(rel)) is not None
    ]
    return "\n\n".join(parts)


class CodeReviewAgentExecutor(AgentExecutor):
    """A2A executor：把 code-review agent 接到 A2A 協定上。"""

    def __init__(self) -> None:
        self.agent = CodeReviewAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 1. 取得或建立 task
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("正在進行 code review..."),
        )

        # 2. 讀取根目錄原始碼 + 使用者需求，呼叫 agent
        user_request = get_message_text(context.message)
        code = _collect_code()
        if not code.strip():
            result = f"在 {REPO_PATH} 找不到可 review 的原始碼檔案。"
        else:
            result = await self.agent.review(code, user_request)

        # 3. 回傳 review 結果作為 artifact，並標記完成
        await updater.add_artifact(
            parts=[new_text_part(text=result, media_type="text/markdown")],
            name="code-review",
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Code review 完成。"),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")
