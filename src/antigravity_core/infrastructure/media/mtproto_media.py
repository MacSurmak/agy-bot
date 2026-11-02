import contextlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from telethon import TelegramClient
from telethon.tl.custom import Message

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import AntigravityError
from antigravity_core.infrastructure.database.repositories import SettingsRepository
from antigravity_core.lexicon import get_lexicon

logger = structlog.get_logger(__name__)


class MediaTransferError(AntigravityError):
    """Raised when downloading or uploading a file via MTProto fails."""


class MediaService:
    """Handles downloading and uploading files up to 2 GB with throttled Telegram progress updates."""

    def __init__(
        self,
        client: TelegramClient,
        static_config: StaticConfig,
        settings_repo: SettingsRepository,
    ) -> None:
        self._client = client
        self._cfg = static_config
        self._settings_repo = settings_repo

    async def download_message_media(self, message: Message, topic_id: int) -> Path:
        if message.media is None:
            raise MediaTransferError("Message does not contain downloadable media.")

        settings = await self._settings_repo.load_settings()
        date_folder = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        target_dir = self._cfg.paths.inbox_dir / date_folder / f"topic_{topic_id}"
        target_dir.mkdir(parents=True, exist_ok=True)

        orig_name = getattr(message.file, "name", None) if message.file else None
        ext = getattr(message.file, "ext", "") if message.file else ""
        safe_name = (
            Path(orig_name).name
            if orig_name
            else f"msg_{message.id}_{int(time.time())}{ext}"
        )
        dest_path = target_dir / f"{message.id}_{safe_name}"
        lex = get_lexicon(settings.ui_language)

        status_msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.media_downloading_init.format(
                file_name=safe_name, dest_path=dest_path
            ),
            reply_to=message.id,
        )
        last_edit_ts = time.monotonic()

        async def _progress_cb(current: int, total: int) -> None:
            nonlocal last_edit_ts
            now = time.monotonic()
            if total > 0 and (now - last_edit_ts) >= settings.tg_edit_throttle_sec:
                last_edit_ts = now
                pct = (current * 100.0) / total
                mb_cur = current / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                with contextlib.suppress(Exception):
                    await status_msg.edit(
                        lex.media_downloading_progress.format(
                            file_name=safe_name,
                            pct=pct,
                            mb_cur=mb_cur,
                            mb_tot=mb_tot,
                        )
                    )

        try:
            downloaded = await self._client.download_media(
                message=message,
                file=str(dest_path),
                progress_callback=_progress_cb,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await status_msg.edit(
                    lex.media_download_failed.format(file_name=safe_name, error=exc)
                )
            raise MediaTransferError(f"Failed to download media from message {message.id}: {exc}") from exc

        with contextlib.suppress(Exception):
            await status_msg.delete()

        if downloaded is None:
            raise MediaTransferError(f"Telethon download_media returned None for message {message.id}.")

        resolved_path = Path(str(downloaded)).resolve()
        logger.info(
            "Downloaded incoming Telegram media",
            topic_id=topic_id,
            path=str(resolved_path),
            size_bytes=resolved_path.stat().st_size,
        )
        return resolved_path

    async def send_file_to_topic(
        self,
        topic_id: int,
        file_path_str: str,
        caption: str,
    ) -> dict[str, Any]:
        resolved = Path(file_path_str).expanduser().resolve()
        if not resolved.is_file():
            raise MediaTransferError(f"File does not exist or is not a regular file: {resolved}")

        size_bytes = resolved.stat().st_size
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        status_msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.media_uploading_init.format(
                file_name=resolved.name,
                size_mb=size_bytes / (1024 * 1024),
            ),
            reply_to=topic_id,
        )
        last_edit_ts = time.monotonic()

        async def _progress_cb(current: int, total: int) -> None:
            nonlocal last_edit_ts
            now = time.monotonic()
            if total > 0 and (now - last_edit_ts) >= settings.tg_edit_throttle_sec:
                last_edit_ts = now
                pct = (current * 100.0) / total
                mb_cur = current / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                with contextlib.suppress(Exception):
                    await status_msg.edit(
                        lex.media_uploading_progress.format(
                            file_name=resolved.name,
                            pct=pct,
                            mb_cur=mb_cur,
                            mb_tot=mb_tot,
                        )
                    )

        try:
            await self._client.send_file(
                entity=self._cfg.telegram.forum_chat_id,
                file=str(resolved),
                caption=caption,
                reply_to=topic_id,
                force_document=True,
                progress_callback=_progress_cb,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await status_msg.edit(
                    lex.media_upload_failed.format(file_name=resolved.name, error=exc)
                )
            raise MediaTransferError(f"Failed to send file '{resolved}' to topic {topic_id}: {exc}") from exc

        with contextlib.suppress(Exception):
            await status_msg.delete()

        logger.info(
            "Sent file to Telegram topic via MTProto",
            topic_id=topic_id,
            file_name=resolved.name,
            size_bytes=size_bytes,
        )
        return {
            "status": "sent",
            "file_name": resolved.name,
            "file_path": str(resolved),
            "size_bytes": size_bytes,
            "topic_id": topic_id,
        }
