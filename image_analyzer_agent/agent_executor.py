"""Image Analyzer agent 的 A2A 執行層：
解析 A2A text / url / raw parts → 呼叫 vision 模型 → 回影像描述 artifact。
"""

import base64
import os
import re
from urllib.parse import urlsplit

import httpx
from a2a.helpers import new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from dotenv import load_dotenv
from google.protobuf.json_format import MessageToDict
from loguru import logger
from openai import AsyncOpenAI

load_dotenv()

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
TEXT_URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')


def _preview(text: str, limit: int = 80) -> str:
    """截斷過長內容，供 A2A-ARD 相容的 part 診斷輸出。"""
    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return repr(text)
    return repr(text[:limit]) + f"...(+{len(text) - limit} chars)"


def _is_local_url(url: str) -> bool:
    """判斷是否為本機位址（依 hostname 精確比對，避免子字串誤判）。"""
    return (urlsplit(url).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


def describe_part(index: int, part) -> str:
    """用與 A2A-ARD 相同的欄位描述單一 A2A ``Part``。"""
    kind = part.WhichOneof("content")
    fields = [f"type={kind or 'UNSET'}"]
    if kind == "text":
        fields.extend((f"chars={len(part.text)}", f"text={_preview(part.text)}"))
    elif kind == "url":
        fields.append(f"url={part.url}")
    elif kind == "raw":
        fields.extend((f"bytes={len(part.raw)}", f"magic={part.raw[:8].hex() or '-'}"))
    elif kind == "data":
        fields.append(f"data={_preview(str(MessageToDict(part.data)))}")
    fields.extend(
        (f"media_type={part.media_type or '-'}", f"filename={part.filename or '-'}")
    )
    return f"[image-analyzer]   part[{index}] " + " ".join(fields)


def message_parts_to_openai_content(parts) -> tuple[list[str], list[dict]]:
    """把 A2A 的 text / URL / raw 影像 parts 轉成 OpenAI vision 內容。

    A2A ``Part`` 的內容存在 protobuf ``oneof`` 裡：URL part 用 ``url``、
    上傳檔案用 ``raw``，兩者都不是舊的 ``data`` 欄位。
    """
    text_parts: list[str] = []
    image_parts: list[dict] = []
    for index, part in enumerate(parts):
        content_type = part.WhichOneof("content")
        logger.info("{}", describe_part(index, part))
        if content_type == "text":
            text_parts.append(part.text)
        elif content_type == "url":
            image_parts.append(
                {"type": "image_url", "image_url": {"url": part.url}}
            )
        elif content_type == "raw":
            media_type = part.media_type or "image/jpeg"
            encoded = base64.b64encode(part.raw).decode("utf-8")
            image_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                }
            )
    return text_parts, image_parts


def image_urls_from_text(text_parts: list[str]) -> list[str]:
    """回傳文字 parts 中夾帶的 HTTP(S) URL，比照 A2A-ARD 的做法。"""
    return TEXT_URL_PATTERN.findall(" ".join(text_parts))


async def local_image_as_data_uri(url: str) -> str | None:
    """抓取本機圖片並轉成 data URI，讓 OpenAI 拿到它自己抓不到的 bytes。"""
    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(url, timeout=10.0)
            response.raise_for_status()
    except httpx.HTTPError:
        return None

    media_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
    encoded = base64.b64encode(response.content).decode("utf-8")
    return f"data:{media_type};base64,{encoded}"


class ImageAnalyzerAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.client = AsyncOpenAI()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Analyzing image..."),
        )
        text_parts, image_parts = message_parts_to_openai_content(
            context.message.parts or []
        )

        # 回退：有些 client 把圖片 URL 放進純文字，而非正式的 A2A url part。
        if not image_parts:
            for url in image_urls_from_text(text_parts):
                if _is_local_url(url):
                    data_uri = await local_image_as_data_uri(url)
                    if data_uri:
                        image_parts.append(
                            {"type": "image_url", "image_url": {"url": data_uri}}
                        )
                else:
                    image_parts.append(
                        {"type": "image_url", "image_url": {"url": url}}
                    )
        if not image_parts:
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("No image provided."),
            )
            return
        response = await self.client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Describe the image in detail."},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": " ".join(text_parts) or "Describe the image.",
                        },
                        *image_parts,
                    ],
                },
            ],
            max_completion_tokens=1000,
        )
        result = response.choices[0].message.content or ""
        await updater.add_artifact(
            parts=[new_text_part(text=result, media_type="text/markdown")],
            name="image-analysis",
        )
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")
