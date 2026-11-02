import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from antigravity_core.infrastructure.database.repositories import SettingsRepository
from antigravity_core.lexicon import get_lexicon


@dataclass(slots=True)
class BatchedItem:
    message_id: int
    sender_id: int
    text_segment: str
    downloaded_files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _TopicBatchBuffer:
    topic_id: int
    items: list[BatchedItem]
    first_item_monotonic: float
    debounce_task: asyncio.Task[None] | None = None


FlushCallback = Callable[[int, int, str], Awaitable[None]]


class MessageBatcher:
    """Debounces consecutive Telegram messages, forwards, and media albums strictly per topic_id."""

    def __init__(
        self,
        settings_repo: SettingsRepository,
        on_flush: FlushCallback,
    ) -> None:
        self._settings_repo = settings_repo
        self._on_flush = on_flush
        self._buffers: dict[int, _TopicBatchBuffer] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        topic_id: int,
        message_id: int,
        sender_id: int,
        text_segment: str,
        downloaded_files: list[str],
    ) -> None:
        settings = await self._settings_repo.load_settings()
        loop = asyncio.get_running_loop()
        now = loop.time()

        flush_immediately_buffer: _TopicBatchBuffer | None = None

        async with self._lock:
            buf = self._buffers.get(topic_id)
            if buf is None:
                buf = _TopicBatchBuffer(
                    topic_id=topic_id,
                    items=[],
                    first_item_monotonic=now,
                    debounce_task=None,
                )
                self._buffers[topic_id] = buf

            buf.items.append(
                BatchedItem(
                    message_id=message_id,
                    sender_id=sender_id,
                    text_segment=text_segment,
                    downloaded_files=downloaded_files,
                )
            )

            if buf.debounce_task is not None and not buf.debounce_task.done():
                buf.debounce_task.cancel()

            elapsed = now - buf.first_item_monotonic
            remaining_max = settings.max_batch_window_sec - elapsed
            if remaining_max <= 0.0:
                self._buffers.pop(topic_id, None)
                flush_immediately_buffer = buf
            else:
                wait_sec = min(settings.debounce_window_sec, remaining_max)
                buf.debounce_task = asyncio.create_task(
                    self._delayed_flush(topic_id, wait_sec)
                )

        if flush_immediately_buffer is not None:
            await self._deliver_buffer(flush_immediately_buffer)

    async def _delayed_flush(self, topic_id: int, wait_sec: float) -> None:
        try:
            await asyncio.sleep(wait_sec)
        except asyncio.CancelledError:
            return

        async with self._lock:
            buf = self._buffers.pop(topic_id, None)

        if buf is not None:
            await self._deliver_buffer(buf)

    async def _deliver_buffer(self, buf: _TopicBatchBuffer) -> None:
        if not buf.items:
            return

        last_msg_id = buf.items[-1].message_id
        parts: list[str] = []
        all_files: list[str] = []

        for item in buf.items:
            if item.text_segment.strip():
                parts.append(item.text_segment.strip())
            all_files.extend(item.downloaded_files)

        if all_files:
            settings = await self._settings_repo.load_settings()
            lex = get_lexicon(settings.ui_language)
            files_block = "\n".join(f"- `{path}`" for path in all_files)
            parts.append(f"{lex.batch_attached_files_header}\n{files_block}")

        combined_prompt = "\n\n".join(parts).strip()
        if combined_prompt:
            await self._on_flush(buf.topic_id, last_msg_id, combined_prompt)
