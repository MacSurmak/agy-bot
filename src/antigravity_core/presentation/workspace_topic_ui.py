import contextlib
import time
from uuid import UUID

import structlog
from telethon import TelegramClient, events

from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.application.turn_orchestrator import TurnOrchestrator
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import EffortLevel, TopicKind
from antigravity_core.domain.models import ForumTopic, TurnProgressEvent
from antigravity_core.infrastructure.agy_runtime.cli_inspector import AgyCliInspector
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.database.repositories import (
    AccountRepository,
    SessionRepository,
    SettingsRepository,
    TopicRepository,
)
from antigravity_core.presentation.formatters import (
    list_workspace_directories,
    render_live_progress,
    render_new_session_divider,
    render_topic_header_card,
    render_turn_footer,
    split_message_chunks,
)
from antigravity_core.presentation.keyboards import (
    build_live_progress_keyboard,
    build_model_picker_keyboard,
    build_session_history_keyboard,
    build_topic_account_picker_keyboard,
    build_topic_header_keyboard,
    build_turn_response_keyboard,
    build_workspace_dir_picker_keyboard,
)

logger = structlog.get_logger(__name__)


class WorkspaceTopicUI:
    """Manages pinned passport cards, live turn progress, and inline actions inside workspace topics."""

    def __init__(
        self,
        client: TelegramClient,
        static_config: StaticConfig,
        worker_pool: AgyWorkerPool,
        turn_orchestrator: TurnOrchestrator,
        topic_lifecycle: TopicLifecycleService,
        cli_inspector: AgyCliInspector,
        settings_repo: SettingsRepository,
        topic_repo: TopicRepository,
        account_repo: AccountRepository,
        session_repo: SessionRepository,
    ) -> None:
        self._client = client
        self._cfg = static_config
        self._pool = worker_pool
        self._orchestrator = turn_orchestrator
        self._lifecycle = topic_lifecycle
        self._inspector = cli_inspector
        self._settings_repo = settings_repo
        self._topic_repo = topic_repo
        self._account_repo = account_repo
        self._session_repo = session_repo

    async def ensure_topic_registered(self, topic_id: int) -> ForumTopic:
        existing = await self._topic_repo.get_topic(topic_id)
        if existing is not None:
            return existing

        settings = await self._settings_repo.load_settings()
        default_account = await self._account_repo.get_default_account()
        default_dir = self._cfg.paths.workspaces_root_dir / f"topic_{topic_id}"
        default_dir.mkdir(parents=True, exist_ok=True)

        topic = await self._topic_repo.create_topic(
            topic_id=topic_id,
            title=f"Workspace #{topic_id}",
            kind=TopicKind.PERMANENT_WORKSPACE,
            workspace_path=str(default_dir.resolve()),
            additional_dirs=(),
            model_slug=settings.default_model,
            effort_level=settings.default_effort,
            execution_mode=settings.default_execution_mode,
            assigned_account_id=default_account.id,
            pinned_card_msg_id=None,
        )
        await self.refresh_pinned_card(topic_id)
        return topic

    async def refresh_pinned_card(self, topic_id: int) -> None:
        topic = await self._topic_repo.get_topic_strict(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)
        settings = await self._settings_repo.load_settings()

        active_session = (
            await self._session_repo.get_session(topic.active_conversation_id)
            if topic.active_conversation_id is not None
            else None
        )
        snapshots = await self._pool.get_pool_snapshots()
        is_warm = any(s.topic_id == topic_id and s.is_warm for s in snapshots)

        card_text = render_topic_header_card(
            topic=topic,
            account=account,
            active_session=active_session,
            is_worker_warm=is_warm,
            worker_ttl_sec=settings.worker_idle_ttl_sec,
        )
        buttons = build_topic_header_keyboard(topic)

        if topic.pinned_card_msg_id is not None:
            try:
                await self._client.edit_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=topic.pinned_card_msg_id,
                    text=card_text,
                    buttons=buttons,
                )
                return
            except Exception:
                pass

        sent = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=card_text,
            reply_to=topic_id,
            buttons=buttons,
        )
        await self._topic_repo.set_pinned_card_msg_id(topic_id, sent.id)
        with contextlib.suppress(Exception):
            await self._client.pin_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=sent.id,
                notify=False,
            )

    async def handle_slash_command(self, topic_id: int, raw_text: str) -> bool:
        cmd = raw_text.split()[0].lower()
        if cmd in ("/new", "/clear"):
            topic = await self._lifecycle.reset_topic_session(topic_id)
            await self.refresh_pinned_card(topic_id)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=render_new_session_divider(topic),
                reply_to=topic_id,
            )
            return True

        if cmd == "/cancel":
            stopped = await self._pool.cancel_active_turn(topic_id)
            msg = "⏹ Active turn stopped." if stopped else "ℹ️ No running turn in this topic."
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=msg,
                reply_to=topic_id,
            )
            return True

        if cmd == "/panel":
            await self.refresh_pinned_card(topic_id)
            return True

        if cmd == "/archive":
            await self._lifecycle.archive_topic(topic_id)
            return True

        return False

    async def execute_batched_prompt(
        self, topic_id: int, reply_to_msg_id: int, prompt: str
    ) -> None:
        topic = await self.ensure_topic_registered(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)
        settings = await self._settings_repo.load_settings()

        start_ts = time.monotonic()
        last_edit_ts = start_ts

        status_msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=f"⏳ **Starting turn** (`{topic.model_slug}` on `{account.slug}`)...",
            reply_to=reply_to_msg_id,
            buttons=build_live_progress_keyboard(topic_id),
        )

        async def _on_progress(progress: TurnProgressEvent) -> None:
            nonlocal last_edit_ts
            now = time.monotonic()
            if (now - last_edit_ts) < settings.tg_edit_throttle_sec:
                return
            last_edit_ts = now
            live_text = render_live_progress(
                event=progress,
                elapsed_sec=now - start_ts,
                model_slug=topic.model_slug,
                account_slug=account.slug,
            )
            with contextlib.suppress(Exception):
                await status_msg.edit(
                    live_text,
                    buttons=build_live_progress_keyboard(topic_id),
                )

        try:
            result = await self._orchestrator.execute_topic_turn(
                topic_id=topic_id,
                prompt=prompt,
                on_progress=_on_progress,
            )
        except Exception as exc:
            logger.error("Turn execution failed", topic_id=topic_id, error=str(exc))
            with contextlib.suppress(Exception):
                await status_msg.edit(f"❌ **Turn Error:**\n`{exc}`", buttons=None)
            return

        with contextlib.suppress(Exception):
            await status_msg.delete()

        updated_topic = await self._topic_repo.get_topic_strict(topic_id)
        updated_account = await self._account_repo.get_by_id(updated_topic.assigned_account_id)

        footer = render_turn_footer(
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=updated_account.slug,
        )
        body_text = (
            result.response_text.strip()
            if result.response_text.strip()
            else f"*(Completed with status `{result.status.value}`)*"
        )
        full_output = body_text + footer

        chunks = split_message_chunks(full_output, settings.tg_max_message_chars)
        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=chunk,
                reply_to=reply_to_msg_id if idx == 0 else topic_id,
                buttons=build_turn_response_keyboard(updated_topic) if is_last else None,
            )

        await self.refresh_pinned_card(topic_id)

    async def deliver_scheduled_report(self, topic_id: int, report_text: str) -> None:
        settings = await self._settings_repo.load_settings()
        chunks = split_message_chunks(report_text, settings.tg_max_message_chars)
        for chunk in chunks:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=chunk,
                reply_to=topic_id,
            )
        await self.refresh_pinned_card(topic_id)

    async def handle_callback(self, event: events.CallbackQuery.Event, data_str: str) -> None:
        parts = data_str.split(":")
        action = parts[1]
        topic_id = int(parts[2])

        if action == "close_sub":
            await event.delete()
            return

        if action == "new":
            topic = await self._lifecycle.reset_topic_session(topic_id)
            await self.refresh_pinned_card(topic_id)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=render_new_session_divider(topic),
                reply_to=topic_id,
            )
            await event.answer("Started a clean session (0 tokens).")
            return

        if action == "toggle_mode":
            updated = await self._lifecycle.toggle_execution_mode(topic_id)
            await self.refresh_pinned_card(topic_id)
            await event.answer(f"Switched mode to {updated.execution_mode.value}.")
            return

        if action == "cancel":
            await self._pool.cancel_active_turn(topic_id)
            await event.answer("Turn canceled.")
            return

        if action == "archive":
            await self._lifecycle.archive_topic(topic_id)
            await event.answer("Topic archived and worker stopped.")
            return

        if action == "history":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            sessions = await self._session_repo.list_topic_sessions(topic_id, limit=8)
            await event.respond(
                "🕰 **Select a Past Session to Resume:**",
                reply_to=topic_id,
                buttons=build_session_history_keyboard(
                    topic_id, sessions, topic.active_conversation_id
                ),
            )
            await event.answer()
            return

        if action == "resume":
            conv_uuid = UUID(parts[3])
            await self._lifecycle.resume_past_session(topic_id, conv_uuid)
            await self.refresh_pinned_card(topic_id)
            await event.edit(f"✅ Resumed session `{conv_uuid}`.", buttons=None)
            return

        if action == "models":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            account = await self._account_repo.get_by_id(topic.assigned_account_id)
            try:
                models = await self._inspector.list_available_models(account)
            except Exception:
                models = [
                    "gemini-4-argon-high",
                    "gemini-4-argon-medium",
                    "gemini-3.8-flash-high",
                    "claude-sonnet-4-6",
                    "claude-opus-4-6-thinking",
                ]
            await event.respond(
                "🧠 **Select Model or Thinking Effort:**",
                reply_to=topic_id,
                buttons=build_model_picker_keyboard(topic_id, models, topic.model_slug),
            )
            await event.answer()
            return

        if action == "set_model":
            model_slug = parts[3]
            topic = await self._topic_repo.get_topic_strict(topic_id)
            await self._lifecycle.change_model(topic_id, model_slug, topic.effort_level)
            await self.refresh_pinned_card(topic_id)
            await event.edit(f"✅ Model updated to `{model_slug}`.", buttons=None)
            return

        if action == "set_effort":
            effort = EffortLevel(parts[3])
            topic = await self._topic_repo.get_topic_strict(topic_id)
            await self._lifecycle.change_model(topic_id, topic.model_slug, effort)
            await self.refresh_pinned_card(topic_id)
            await event.edit(f"✅ Thinking effort updated to `{effort.value}`.", buttons=None)
            return

        if action == "accounts":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            accounts = await self._account_repo.list_enabled_accounts()
            await event.respond(
                "👤 **Select Google Account for this Topic:**",
                reply_to=topic_id,
                buttons=build_topic_account_picker_keyboard(
                    topic_id, accounts, topic.assigned_account_id
                ),
            )
            await event.answer()
            return

        if action == "set_acc":
            acc_id = int(parts[3])
            await self._lifecycle.switch_topic_account(topic_id, acc_id)
            await self.refresh_pinned_card(topic_id)
            await event.edit("✅ Switched Google Account for this topic.", buttons=None)
            return

        if action == "dirs":
            choices = list_workspace_directories(
                self._cfg.paths.workspaces_root_dir,
                self._cfg.paths.remotes_mount_dir,
            )
            await event.respond(
                "📂 **Select Workspace Directory:**",
                reply_to=topic_id,
                buttons=build_workspace_dir_picker_keyboard(topic_id, choices),
            )
            await event.answer()
            return

        if action == "set_dir":
            idx = int(parts[3])
            choices = list_workspace_directories(
                self._cfg.paths.workspaces_root_dir,
                self._cfg.paths.remotes_mount_dir,
            )
            if 0 <= idx < len(choices):
                _label, target_path = choices[idx]
                await self._topic_repo.update_workspace_path(topic_id, target_path)
                await self._pool.evict_topic_worker(topic_id)
                await self.refresh_pinned_card(topic_id)
                await event.edit(f"✅ Workspace updated to `{target_path}`.", buttons=None)
