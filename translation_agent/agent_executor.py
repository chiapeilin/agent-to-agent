"""Translation agent 的 A2A 執行層：收文字 → 呼叫 OpenAI 翻譯 → 回 artifact。"""

import os

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

load_dotenv()

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")


class TranslationAgentExecutor(AgentExecutor):
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
            message=new_text_message("Translating..."),
        )
        user_text = get_message_text(context.message)
        response = await self.client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Translate to the other language. Return only the translated text.",
                },
                {"role": "user", "content": user_text},
            ],
            stream=False,
        )
        result = response.choices[0].message.content or ""
        await updater.add_artifact(
            parts=[new_text_part(text=result)], name="translation-response"
        )
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")
