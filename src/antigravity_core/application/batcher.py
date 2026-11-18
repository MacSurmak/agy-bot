import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from loguru import logger

from antigravity_core.infrastructure.database.repositories import SettingsRepository
from antigravity_core.lexicon import get_lexicon

LONG_PROMPT_FILE_THRESHOLD_CHARS: Final[int] = 2500
SPLIT_MESSAGE_CHUNK_HINT_CHARS: Final[int] = 3500
SPLIT_MESSAGE_DEBOUNCE_SEC: Final[float] = 2.5
SHORT_TEXT_DEBOUNCE_SEC: Final[float] = 0.12

type FlushCallback = Callable[[int, int, str], Awaitable[None]]
type StagedMediaCallback = Callable[[int, int, tuple[str, ...]], Awaitable[None]]


def _log_task_exception_immediately(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.opt(exception=exc).error(
            "Background batcher task failed: task_name={}",
            task.get_name(),
        )


@dataclass(frozen=True, slots=True)
class BatchedItem:
    message_id: int
    sender_id: int
    text_segment: str
    downloaded_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TopicBatchBuffer:
    topic_id: int
    items: tuple[BatchedItem, ...]
    first_item_monotonic: float
    debounce_task: asyncio.Task[None] | None


class MessageBatcher:
    """Debounces consecutive Telegram messages, holds text-less media attachments, and converts long prompts to .md files."""

    def __init__(
        self,
        inbox_dir: Path,
        settings_repo: SettingsRepository,
        on_flush: FlushCallback,
        on_staged_media: StagedMediaCallback,
    ) -> None:
        self._inbox_dir = inbox_dir
        self._settings_repo = settings_repo
        self._on_flush = on_flush
        self._on_staged_media = on_staged_media
        self._buffers: dict[int, _TopicBatchBuffer] = {}
        self._staged_files: dict[int, tuple[int, list[str]]] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        topic_id: int,
        message_id: int,
        sender_id: int,
        text_segment: str,
        downloaded_files: list[str],
        is_multi_part_or_forward: bool,
    ) -> None:
        clean_text = text_segment.strip()
        if not clean_text and downloaded_files:
            async with self._lock:
                if topic_id not in self._staged_files:
                    self._staged_files[topic_id] = (message_id, list(downloaded_files))
                else:
                    _prev_mid, existing_list = self._staged_files[topic_id]
                    existing_list.extend(downloaded_files)
                    self._staged_files[topic_id] = (message_id, existing_list)
                staged_snapshot = tuple(self._staged_files[topic_id][1])

            logger.info(
                "Held text-less media attachment in staging buffer: topic_id={} msg_id={} files_count={}",
                topic_id,
                message_id,
                len(staged_snapshot),
            )
            await self._on_staged_media(topic_id, message_id, staged_snapshot)
            return

        settings = await self._settings_repo.load_settings()
        loop = asyncio.get_running_loop()
        now = loop.time()

        flush_immediately_buffer: _TopicBatchBuffer | None = None

        async with self._lock:
            combined_files = list(downloaded_files)
            if topic_id in self._staged_files:
                _staged_mid, held_files = self._staged_files.pop(topic_id)
                combined_files = held_files + combined_files
                logger.info(
                    "Merged {} previously staged file(s) with incoming text prompt in topic_id={}",
                    len(held_files),
                    topic_id,
                )

            new_item = BatchedItem(
                message_id=message_id,
                sender_id=sender_id,
                text_segment=text_segment,
                downloaded_files=tuple(combined_files),
            )

            if topic_id not in self._buffers:
                first_mono = now
                updated_items: tuple[BatchedItem, ...] = (new_item,)
            else:
                prev_buf = self._buffers[topic_id]
                if prev_buf.debounce_task is not None and not prev_buf.debounce_task.done():
                    prev_buf.debounce_task.cancel()
                first_mono = prev_buf.first_item_monotonic
                updated_items = (*prev_buf.items, new_item)

            elapsed = now - first_mono
            remaining_max = settings.max_batch_window_sec - elapsed
            if remaining_max <= 0.0:
                self._buffers.pop(topic_id, None)
                flush_immediately_buffer = _TopicBatchBuffer(
                    topic_id=topic_id,
                    items=updated_items,
                    first_item_monotonic=first_mono,
                    debounce_task=None,
                )
            else:
                if len(clean_text) >= SPLIT_MESSAGE_CHUNK_HINT_CHARS:
                    target_debounce = SPLIT_MESSAGE_DEBOUNCE_SEC
                elif is_multi_part_or_forward:
                    target_debounce = settings.debounce_window_sec
                else:
                    target_debounce = SHORT_TEXT_DEBOUNCE_SEC

                wait_sec = min(target_debounce, remaining_max)
                task = asyncio.create_task(self._delayed_flush(topic_id, wait_sec))
                task.add_done_callback(_log_task_exception_immediately)
                self._buffers[topic_id] = _TopicBatchBuffer(
                    topic_id=topic_id,
                    items=updated_items,
                    first_item_monotonic=first_mono,
                    debounce_task=task,
                )

        if flush_immediately_buffer is not None:
            await self._deliver_buffer(flush_immediately_buffer)

    async def flush_staged_files_now(
        self, topic_id: int, default_prompt: str
    ) -> bool:
        async with self._lock:
            if topic_id not in self._staged_files:
                return False
            last_mid, held_files = self._staged_files.pop(topic_id)

        if not held_files:
            return False

        item = BatchedItem(
            message_id=last_mid,
            sender_id=0,
            text_segment=default_prompt,
            downloaded_files=tuple(held_files),
        )
        buf = _TopicBatchBuffer(
            topic_id=topic_id,
            items=(item,),
            first_item_monotonic=0.0,
            debounce_task=None,
        )
        await self._deliver_buffer(buf)
        return True

    async def clear_staged_files(self, topic_id: int) -> int:
        async with self._lock:
            if topic_id not in self._staged_files:
                return 0
            _mid, held_files = self._staged_files.pop(topic_id)
            return len(held_files)

    async def _delayed_flush(self, topic_id: int, wait_sec: float) -> None:
        await asyncio.sleep(wait_sec)
        async with self._lock:
            buf = self._buffers.pop(topic_id, None)
        if buf is not None:
            await self._deliver_buffer(buf)

    def _save_long_prompt_as_markdown_file(
        self, topic_id: int, msg_id: int, full_text: str
    ) -> str:
        date_folder = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        target_dir = self._inbox_dir / date_folder / f"topic_{topic_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        md_path = (target_dir / f"{msg_id}_prompt.md").resolve()
        md_path.write_text(full_text, encoding="utf-8")
        logger.info(
            "Converted long user prompt ({} chars) into attached markdown file: {}",
            len(full_text),
            md_path,
        )
        return str(md_path)

    async def _deliver_buffer(self, buf: _TopicBatchBuffer) -> None:
        if not buf.items:
            return

        last_msg_id = buf.items[-1].message_id
        text_parts: list[str] = []
        all_files: list[str] = []

        for item in buf.items:
            if item.text_segment.strip():
                text_parts.append(item.text_segment.strip())
            all_files.extend(item.downloaded_files)

        joined_text = "\n\n".join(text_parts).strip()
        final_parts: list[str] = []

        if len(joined_text) >= LONG_PROMPT_FILE_THRESHOLD_CHARS:
            md_file_path = self._save_long_prompt_as_markdown_file(
                buf.topic_id, last_msg_id, joined_text
            )
            all_files.insert(0, md_file_path)
        elif joined_text:
            final_parts.append(joined_text)

        if all_files:
            settings = await self._settings_repo.load_settings()
            lex = get_lexicon(settings.ui_language)
            files_block = "\n".join(f"- `{path}`" for path in all_files)
            final_parts.append(f"{lex.batch_attached_files_header}\n{files_block}")

        combined_prompt = "\n\n".join(final_parts).strip()
        if combined_prompt:
            logger.info(
                "Flushing batched turn prompt: topic_id={} msg_id={} items={} prompt_chars={} files={}",
                buf.topic_id,
                last_msg_id,
                len(buf.items),
                len(combined_prompt),
                len(all_files),
            )
            await self._on_flush(buf.topic_id, last_msg_id, combined_prompt)
