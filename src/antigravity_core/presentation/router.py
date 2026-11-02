import structlog
from telethon import TelegramClient, events
from telethon.tl.custom import Message

from antigravity_core.application.batcher import MessageBatcher
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import UserRole
from antigravity_core.infrastructure.database.repositories import AccessControlRepository
from antigravity_core.infrastructure.media.mtproto_media import MediaService
from antigravity_core.presentation.control_panel_ui import ControlPanelUI
from antigravity_core.presentation.workspace_topic_ui import WorkspaceTopicUI

logger = structlog.get_logger(__name__)


def extract_topic_id(message: Message) -> int:
    """Section 5.1 Master Spec: Canonical MTProto Forum Topic ID extractor."""
    reply_to = getattr(message, "reply_to", None)
    if reply_to is not None and getattr(reply_to, "forum_topic", False):
        top_id = getattr(reply_to, "reply_to_top_id", None)
        if top_id is not None:
            return int(top_id)
        msg_id = getattr(reply_to, "reply_to_msg_id", None)
        if msg_id is not None:
            return int(msg_id)
    return 1


class TelegramEventRouter:
    """Routes incoming Telethon events between #Control-Panel and parallel Workspace Topics."""

    def __init__(
        self,
        client: TelegramClient,
        static_config: StaticConfig,
        access_repo: AccessControlRepository,
        media_service: MediaService,
        batcher: MessageBatcher,
        control_panel_ui: ControlPanelUI,
        workspace_ui: WorkspaceTopicUI,
    ) -> None:
        self._client = client
        self._cfg = static_config
        self._access_repo = access_repo
        self._media = media_service
        self._batcher = batcher
        self._cp_ui = control_panel_ui
        self._ws_ui = workspace_ui

    def register_handlers(self) -> None:
        self._client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=[self._cfg.telegram.forum_chat_id], incoming=True),
        )
        self._client.add_event_handler(
            self._on_callback_query,
            events.CallbackQuery(chats=[self._cfg.telegram.forum_chat_id]),
        )

    async def _check_authorized(self, user_id: int, topic_id: int) -> bool:
        if user_id == self._cfg.telegram.owner_telegram_id:
            return True
        user = await self._access_repo.get_user(user_id)
        if user is None or not user.is_active:
            return False
        if user.role == UserRole.OWNER:
            return True
        if topic_id == self._cfg.telegram.control_panel_topic_id:
            return False
        return not user.allowed_topic_ids or topic_id in user.allowed_topic_ids

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        message: Message = event.message
        if message.out or message.action is not None:
            return

        sender_id = int(event.sender_id or 0)
        topic_id = extract_topic_id(message)
        raw_text = (message.message or "").strip()

        if raw_text.startswith("/redeem "):
            token = raw_text.split(maxsplit=1)[1].strip()
            try:
                sender = await event.get_sender()
                uname = getattr(sender, "username", None) or str(sender_id)
                await self._access_repo.redeem_invite(token, sender_id, uname)
                await event.reply("✅ Invite token redeemed! You now have access.")
            except Exception as exc:
                await event.reply(f"❌ Invite redemption failed: `{exc}`")
            return

        if not await self._check_authorized(sender_id, topic_id):
            return

        if topic_id == self._cfg.telegram.control_panel_topic_id:
            await self._cp_ui.handle_text_input(event)
            return

        if raw_text.startswith("/") and message.media is None:
            handled = await self._ws_ui.handle_slash_command(topic_id, raw_text)
            if handled:
                return

        downloaded_files: list[str] = []
        if message.media is not None:
            try:
                saved_path = await self._media.download_message_media(message, topic_id)
                downloaded_files.append(str(saved_path))
            except Exception as exc:
                logger.error("Failed to download incoming media", error=str(exc))
                return

        await self._batcher.enqueue(
            topic_id=topic_id,
            message_id=message.id,
            sender_id=sender_id,
            text_segment=raw_text,
            downloaded_files=downloaded_files,
        )

    async def _on_callback_query(self, event: events.CallbackQuery.Event) -> None:
        sender_id = int(event.sender_id or 0)
        data_str = event.data.decode("utf-8", errors="replace")

        if data_str.startswith("cp:"):
            if sender_id != self._cfg.telegram.owner_telegram_id:
                await event.answer("Access denied: Owner only.", alert=True)
                return
            await self._cp_ui.handle_callback(event, data_str)
            return

        if data_str.startswith("ws:"):
            parts = data_str.split(":")
            topic_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
            if not await self._check_authorized(sender_id, topic_id):
                await event.answer("Access denied.", alert=True)
                return
            await self._ws_ui.handle_callback(event, data_str)
