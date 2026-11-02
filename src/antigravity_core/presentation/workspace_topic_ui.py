import time
from uuid import UUID

import structlog
from telethon import TelegramClient, events
from telethon.errors import RPCError

from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.application.turn_orchestrator import TurnOrchestrator
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import EffortLevel, TopicKind
from antigravity_core.domain.exceptions import AgyProcessError
from antigravity_core.domain.models import ForumTopic, TurnProgressEvent
from antigravity_core.infrastructure.agy_runtime.cli_inspector import AgyCliInspector
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.database.repositories import (
    AccountRepository,
    SessionRepository,
    SettingsRepository,
    TopicRepository,
)
from antigravity_core.lexicon import get_lexicon
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
        lex = get_lexicon(settings.ui_language)
        default_account = await self._account_repo.get_default_account()
        default_dir = self._cfg.paths.workspaces_root_dir / f"topic_{topic_id}"
        default_dir.mkdir(parents=True, exist_ok=True)

        topic = await self._topic_repo.create_topic(
            topic_id=topic_id,
            title=lex.ws_auto_topic_title.format(topic_id=topic_id),
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
        lex = get_lexicon(settings.ui_language)

        card_text = render_topic_header_card(
            lex=lex,
            limits=self._cfg.limits,
            topic=topic,
            account=account,
            active_session=active_session,
            is_worker_warm=is_warm,
            worker_ttl_sec=settings.worker_idle_ttl_sec,
        )
        buttons = build_topic_header_keyboard(lex, topic)

        if topic.pinned_card_msg_id is not None:
            try:
                await self._client.edit_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=topic.pinned_card_msg_id,
                    text=card_text,
                    buttons=buttons,
                )
                return
            except RPCError as exc:
                logger.debug(
                    "Pinned card message edit skipped or expired; sending fresh card",
                    topic_id=topic_id,
                    error=str(exc),
                )

        sent = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=card_text,
            reply_to=topic_id,
            buttons=buttons,
        )
        await self._topic_repo.set_pinned_card_msg_id(topic_id, sent.id)
        try:
            await self._client.pin_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=sent.id,
                notify=False,
            )
        except RPCError as exc:
            logger.warning("Could not pin topic card message", topic_id=topic_id, error=str(exc))

    async def handle_slash_command(self, topic_id: int, raw_text: str) -> bool:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        cmd = raw_text.split()[0].lower()
        if cmd in ("/new", "/clear"):
            topic = await self._lifecycle.reset_topic_session(topic_id)
            await self.refresh_pinned_card(topic_id)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=render_new_session_divider(lex, topic),
                reply_to=topic_id,
            )
            return True

        if cmd == "/cancel":
            stopped = await self._pool.cancel_active_turn(topic_id)
            msg = lex.ws_turn_stopped_msg if stopped else lex.ws_no_running_turn_msg
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
        lex = get_lexicon(settings.ui_language)

        start_ts = time.monotonic()
        last_edit_ts = start_ts

        status_msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.ws_starting_turn_msg.format(
                model_slug=topic.model_slug, account_slug=account.slug
            ),
            reply_to=reply_to_msg_id,
            buttons=build_live_progress_keyboard(lex, topic_id),
        )

        async def _on_progress(progress: TurnProgressEvent) -> None:
            nonlocal last_edit_ts
            now = time.monotonic()
            if (now - last_edit_ts) < settings.tg_edit_throttle_sec:
                return
            last_edit_ts = now
            live_text = render_live_progress(
                lex=lex,
                limits=self._cfg.limits,
                event=progress,
                elapsed_sec=now - start_ts,
                model_slug=topic.model_slug,
                account_slug=account.slug,
            )
            try:
                await status_msg.edit(
                    live_text,
                    buttons=build_live_progress_keyboard(lex, topic_id),
                )
            except RPCError as exc:
                logger.debug("Live progress message edit skipped", topic_id=topic_id, error=str(exc))

        try:
            result = await self._orchestrator.execute_topic_turn(
                topic_id=topic_id,
                prompt=prompt,
                on_progress=_on_progress,
            )
        except Exception as exc:
            logger.error("Turn execution failed", topic_id=topic_id, error=str(exc))
            try:
                await status_msg.edit(lex.ws_turn_error_msg.format(error=exc), buttons=None)
            except RPCError as edit_exc:
                logger.warning("Failed to update status message with turn error", error=str(edit_exc))
            return

        try:
            await status_msg.delete()
        except RPCError as exc:
            logger.debug("Could not delete live progress status message", error=str(exc))

        updated_topic = await self._topic_repo.get_topic_strict(topic_id)
        updated_account = await self._account_repo.get_by_id(updated_topic.assigned_account_id)

        footer = render_turn_footer(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=updated_account.slug,
        )
        body_text = (
            result.response_text.strip()
            if result.response_text.strip()
            else lex.ws_empty_response_fallback.format(status=result.status.value)
        )
        full_output = body_text + footer

        chunks = split_message_chunks(full_output, settings.tg_max_message_chars)
        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=chunk,
                reply_to=reply_to_msg_id if idx == 0 else topic_id,
                buttons=build_turn_response_keyboard(lex, updated_topic) if is_last else None,
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
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

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
                message=render_new_session_divider(lex, topic),
                reply_to=topic_id,
            )
            await event.answer(lex.ws_toast_new_session)
            return

        if action == "toggle_mode":
            updated = await self._lifecycle.toggle_execution_mode(topic_id)
            await self.refresh_pinned_card(topic_id)
            await event.answer(
                lex.ws_toast_mode_switched.format(mode=updated.execution_mode.value)
            )
            return

        if action == "cancel":
            await self._pool.cancel_active_turn(topic_id)
            await event.answer(lex.ws_toast_turn_canceled)
            return

        if action == "archive":
            await self._lifecycle.archive_topic(topic_id)
            await event.answer(lex.ws_toast_topic_archived)
            return

        if action == "history":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            sessions = await self._session_repo.list_topic_sessions(
                topic_id,
                limit=self._cfg.limits.sessions_history_display_limit,
            )
            await event.respond(
                lex.ws_select_past_session_prompt,
                reply_to=topic_id,
                buttons=build_session_history_keyboard(
                    lex,
                    self._cfg.limits,
                    topic_id,
                    sessions,
                    topic.active_conversation_id,
                ),
            )
            await event.answer()
            return

        if action == "resume":
            conv_uuid = UUID(parts[3])
            await self._lifecycle.resume_past_session(topic_id, conv_uuid)
            await self.refresh_pinned_card(topic_id)
            await event.edit(
                lex.ws_session_resumed_msg.format(conversation_id=conv_uuid),
                buttons=None,
            )
            return

        if action == "models":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            account = await self._account_repo.get_by_id(topic.assigned_account_id)
            try:
                models = await self._inspector.list_available_models(account)
            except AgyProcessError as exc:
                await event.respond(
                    lex.ws_turn_error_msg.format(error=exc),
                    reply_to=topic_id,
                )
                await event.answer()
                return
            await event.respond(
                lex.ws_select_model_prompt,
                reply_to=topic_id,
                buttons=build_model_picker_keyboard(
                    lex,
                    self._cfg.limits,
                    topic_id,
                    models,
                    topic.model_slug,
                ),
            )
            await event.answer()
            return

        if action == "set_model":
            model_slug = parts[3]
            topic = await self._topic_repo.get_topic_strict(topic_id)
            await self._lifecycle.change_model(topic_id, model_slug, topic.effort_level)
            await self.refresh_pinned_card(topic_id)
            await event.edit(
                lex.ws_model_updated_msg.format(model_slug=model_slug),
                buttons=None,
            )
            return

        if action == "set_effort":
            effort = EffortLevel(parts[3])
            topic = await self._topic_repo.get_topic_strict(topic_id)
            await self._lifecycle.change_model(topic_id, topic.model_slug, effort)
            await self.refresh_pinned_card(topic_id)
            await event.edit(
                lex.ws_effort_updated_msg.format(effort=effort.value),
                buttons=None,
            )
            return

        if action == "accounts":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            accounts = await self._account_repo.list_enabled_accounts()
            await event.respond(
                lex.ws_select_account_prompt,
                reply_to=topic_id,
                buttons=build_topic_account_picker_keyboard(
                    lex, topic_id, accounts, topic.assigned_account_id
                ),
            )
            await event.answer()
            return

        if action == "set_acc":
            acc_id = int(parts[3])
            await self._lifecycle.switch_topic_account(topic_id, acc_id)
            await self.refresh_pinned_card(topic_id)
            await event.edit(lex.ws_account_switched_msg, buttons=None)
            return

        if action == "dirs":
            choices = list_workspace_directories(
                lex,
                self._cfg.limits,
                self._cfg.paths.workspaces_root_dir,
                self._cfg.paths.remotes_mount_dir,
            )
            await event.respond(
                lex.ws_select_workspace_prompt,
                reply_to=topic_id,
                buttons=build_workspace_dir_picker_keyboard(lex, topic_id, choices),
            )
            await event.answer()
            return

        if action == "set_dir":
            idx = int(parts[3])
            choices = list_workspace_directories(
                lex,
                self._cfg.limits,
                self._cfg.paths.workspaces_root_dir,
                self._cfg.paths.remotes_mount_dir,
            )
            if 0 <= idx < len(choices):
                _label, target_path = choices[idx]
                await self._topic_repo.update_workspace_path(topic_id, target_path)
                await self._pool.evict_topic_worker(topic_id)
                await self.refresh_pinned_card(topic_id)
                await event.edit(
                    lex.ws_workspace_updated_msg.format(workspace_path=target_path),
                    buttons=None,
                )
