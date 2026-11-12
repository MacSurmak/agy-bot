import asyncio
import contextlib
from pathlib import Path
import time
from uuid import UUID

import structlog
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError, RPCError
from telethon.tl.types import TypeMessageEntity

from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.application.turn_orchestrator import TurnOrchestrator
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import EffortLevel, TopicKind
from antigravity_core.domain.exceptions import AgyProcessError
from antigravity_core.domain.models import ForumTopic, TurnProgressEvent
from antigravity_core.infrastructure.agy_runtime.cli_inspector import (
    AgyCliInspector,
    is_valid_model_slug_syntax,
)
from antigravity_core.infrastructure.agy_runtime.history_reader import AgyHistoryReader
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.database.repositories import (
    AccountRepository,
    SessionRepository,
    SettingsRepository,
    TopicRepository,
)
from antigravity_core.infrastructure.remotes.sshfs_mount_mgr import RemoteMountManager
from antigravity_core.lexicon import get_lexicon
from antigravity_core.presentation.formatters import (
    build_final_execution_trace_pages,
    build_live_progress_pages,
    format_account_quota_lines,
    format_wide_header_block,
    list_workspace_directories,
    render_Formatted_response_chunks,
    render_new_session_divider,
    render_topic_header_card,
    render_turn_footer,
    split_message_chunks,
)
from antigravity_core.presentation.keyboards import (
    build_live_progress_keyboard,
    build_model_picker_keyboard,
    build_session_history_keyboard,
    build_sudo_approval_keyboard,
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
        history_reader: AgyHistoryReader,
        mount_mgr: RemoteMountManager,
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
        self._history_reader = history_reader
        self._mount_mgr = mount_mgr
        self._settings_repo = settings_repo
        self._topic_repo = topic_repo
        self._account_repo = account_repo
        self._session_repo = session_repo
        self._awaiting_custom_dir_topics: set[int] = set()
        self._password_prompt_msgs: dict[int, tuple[str, int]] = {}
        self._sudo_prompt_meta: dict[str, tuple[str, str]] = {}
        self._relocate_trace_topics: set[int] = set()

    async def send_ssh_password_prompt(self, topic_id: int, target_label: str) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        sent = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.ssh_password_prompt_card.format(target=target_label),
            reply_to=topic_id,
        )
        self._password_prompt_msgs[topic_id] = (target_label, sent.id)
        self._relocate_trace_topics.add(topic_id)

    async def acknowledge_password_received(self, topic_id: int) -> None:
        if topic_id not in self._password_prompt_msgs:
            return
        target_label, msg_id = self._password_prompt_msgs.pop(topic_id)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        await self._client.edit_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=msg_id,
            text=lex.ssh_password_accepted_card.format(target=target_label),
        )
        self._relocate_trace_topics.add(topic_id)

    async def send_sudo_approval_prompt(
        self,
        topic_id: int,
        request_id: str,
        target_label: str,
        action_summary: str,
        diff_or_cmd_preview: str,
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        self._sudo_prompt_meta[request_id] = (target_label, action_summary)
        preview_trimmed = diff_or_cmd_preview[:2800]
        await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.sudo_approval_prompt_card.format(
                target=target_label,
                action_summary=action_summary,
                preview=preview_trimmed,
            ),
            reply_to=topic_id,
            buttons=build_sudo_approval_keyboard(lex, topic_id, request_id),
        )
        self._relocate_trace_topics.add(topic_id)

    async def initialize_created_topic(self, topic_id: int, assigned_dir: str) -> None:
        Path(assigned_dir).mkdir(parents=True, exist_ok=True)
        await self._topic_repo.update_workspace_path(topic_id, assigned_dir)
        await self.refresh_pinned_card(topic_id)

    async def ensure_topic_registered(self, topic_id: int) -> ForumTopic:
        existing = await self._topic_repo.get_topic(topic_id)
        if existing is not None:
            Path(existing.workspace_path).mkdir(parents=True, exist_ok=True)
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
            except MessageNotModifiedError:
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
            logger.warning(
                "Could not pin topic card message", topic_id=topic_id, error=str(exc)
            )

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
            self._mount_mgr.cancel_topic_prompts(topic_id)
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

        if cmd in ("/quota", "/usage"):
            topic = await self.ensure_topic_registered(topic_id)
            account = await self._account_repo.get_by_id(topic.assigned_account_id)
            wait_msg = await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=f"📊 {lex.cp_toast_probing_quotas}",
                reply_to=topic_id,
            )
            try:
                q_info = await self._inspector.fetch_account_quota(account)
                await self._account_repo.update_quota_snapshot(account.id, q_info)
                q_lines = format_account_quota_lines(q_info, lex)
                hdr_q = format_wide_header_block(
                    lex.ws_quota_card_header.format(
                        slug=account.slug, email_label=account.email_label
                    )
                )
                if q_lines:
                    body = f"{hdr_q}\n" + "\n".join(q_lines)
                else:
                    min_p = q_info.get("min_remaining_pct", "?")
                    body = f"{hdr_q}\n" + lex.ws_quota_fallback_body.format(
                        min_pct=min_p
                    )
                await wait_msg.edit(body)
            except Exception as exc:
                max_err = self._cfg.limits.error_summary_max_chars
                await wait_msg.edit(
                    lex.ws_quota_error_msg.format(error=str(exc)[:max_err])
                )
            return True

        if cmd == "/model":
            parts = raw_text.split(maxsplit=1)
            topic = await self.ensure_topic_registered(topic_id)
            account = await self._account_repo.get_by_id(topic.assigned_account_id)
            try:
                models = await self._inspector.list_available_models(account)
            except AgyProcessError as exc:
                await self._client.send_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=lex.ws_turn_error_msg.format(error=exc),
                    reply_to=topic_id,
                )
                return True

            if len(parts) == 2 and parts[1].strip():
                new_slug = parts[1].strip().lower()
                if not is_valid_model_slug_syntax(new_slug) or new_slug not in models:
                    formatted_list = ", ".join(f"`{m}`" for m in models)
                    await self._client.send_message(
                        entity=self._cfg.telegram.forum_chat_id,
                        message=lex.ws_model_unknown_msg.format(
                            model_slug=new_slug,
                            available_models=formatted_list,
                        ),
                        reply_to=topic_id,
                    )
                    return True

                await self._lifecycle.change_model(
                    topic_id, new_slug, topic.effort_level
                )
                await self.refresh_pinned_card(topic_id)
                await self._client.send_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=lex.ws_model_updated_msg.format(model_slug=new_slug),
                    reply_to=topic_id,
                )
                return True

            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=lex.ws_select_model_prompt,
                reply_to=topic_id,
                buttons=build_model_picker_keyboard(
                    lex,
                    self._cfg.limits,
                    topic_id,
                    models,
                    topic.model_slug,
                ),
            )
            return True

        if cmd == "/archive":
            await self._lifecycle.archive_topic(topic_id)
            return True

        return False

    async def execute_batched_prompt(
        self, topic_id: int, reply_to_msg_id: int, prompt: str
    ) -> None:
        if topic_id in self._awaiting_custom_dir_topics:
            self._awaiting_custom_dir_topics.discard(topic_id)
            settings = await self._settings_repo.load_settings()
            lex = get_lexicon(settings.ui_language)
            custom_path = Path(prompt.strip()).expanduser().resolve()
            custom_path.mkdir(parents=True, exist_ok=True)
            await self._topic_repo.update_workspace_path(topic_id, str(custom_path))
            await self._pool.evict_topic_worker(topic_id)
            await self.refresh_pinned_card(topic_id)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=lex.ws_dir_confirmed_msg.format(workspace_path=custom_path),
                reply_to=reply_to_msg_id,
                silent=True,
            )
            return

        topic = await self.ensure_topic_registered(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        logger.info(
            "Starting user turn in topic",
            topic_id=topic_id,
            model=topic.model_slug,
            account=account.slug,
            workspace=topic.workspace_path,
            prompt_preview=prompt[:100].replace("\n", " "),
        )

        start_ts = time.monotonic()
        last_edit_ts = start_ts

        acc_display = (
            account.email_label if "@" in account.email_label else account.slug
        )
        status_msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.ws_starting_turn_msg.format(
                model_slug=topic.model_slug, account_slug=acc_display
            ),
            reply_to=topic_id,
            silent=True,
            buttons=build_live_progress_keyboard(lex, topic_id),
        )
        trace_msgs = [status_msg]
        rendered_page_state: dict[int, tuple[str, bool]] = {}

        async def _sync_trace_pages(
            pages: list[tuple[str, list[TypeMessageEntity]]], is_final: bool
        ) -> None:
            if topic_id in self._relocate_trace_topics:
                self._relocate_trace_topics.discard(topic_id)
                for old_msg in trace_msgs:
                    with contextlib.suppress(RPCError):
                        await old_msg.delete()
                trace_msgs.clear()
                rendered_page_state.clear()

            total_pages = len(pages)
            for idx, (page_text, raw_ents) in enumerate(pages):
                ents: list[TypeMessageEntity] = [
                    e for e in raw_ents if isinstance(e, TypeMessageEntity)
                ]
                has_cancel_btn = (not is_final) and (idx == total_pages - 1)
                desired_state = (page_text, has_cancel_btn)

                if idx < len(trace_msgs):
                    if rendered_page_state.get(idx) == desired_state:
                        continue
                    try:
                        await self._client.edit_message(
                            entity=self._cfg.telegram.forum_chat_id,
                            message=trace_msgs[idx].id,
                            text=page_text,
                            formatting_entities=ents,
                            buttons=(
                                build_live_progress_keyboard(lex, topic_id)
                                if has_cancel_btn
                                else None
                            ),
                        )
                        rendered_page_state[idx] = desired_state
                    except MessageNotModifiedError:
                        rendered_page_state[idx] = desired_state
                    except RPCError as exc:
                        if is_final:
                            # Ensure the cancel button is removed even if text edit hit a minor RPC limit
                            try:
                                await self._client.edit_message(
                                    entity=self._cfg.telegram.forum_chat_id,
                                    message=trace_msgs[idx].id,
                                    buttons=None,
                                )
                            except RPCError as btn_exc:
                                logger.debug(
                                    "Could not clear cancel button on final trace page",
                                    error=str(btn_exc),
                                )
                        logger.debug(
                            "Trace page edit skipped",
                            topic_id=topic_id,
                            page=idx + 1,
                            error=str(exc),
                        )
                else:
                    try:
                        new_msg = await self._client.send_message(
                            entity=self._cfg.telegram.forum_chat_id,
                            message=page_text,
                            formatting_entities=ents,
                            reply_to=topic_id,
                            silent=True,
                            buttons=(
                                build_live_progress_keyboard(lex, topic_id)
                                if has_cancel_btn
                                else None
                            ),
                        )
                        trace_msgs.append(new_msg)
                        rendered_page_state[idx] = desired_state
                    except RPCError as exc:
                        logger.warning(
                            "Failed to send overflow trace page",
                            topic_id=topic_id,
                            page=idx + 1,
                            error=str(exc),
                        )

        sync_lock = asyncio.Lock()

        async def _bg_sync_pages(pages: list[tuple[str, list[object]]]) -> None:
            if sync_lock.locked():
                return
            try:
                async with sync_lock:
                    await _sync_trace_pages(pages, is_final=False)
            except Exception as exc:
                logger.error(
                    "Background trace page sync failed",
                    topic_id=topic_id,
                    error=str(exc),
                    exc_info=True,
                )

        async def _on_progress(progress: TurnProgressEvent) -> None:
            nonlocal last_edit_ts
            now = time.monotonic()
            pages = build_live_progress_pages(
                lex=lex,
                limits=self._cfg.limits,
                event=progress,
                elapsed_sec=now - start_ts,
                model_slug=topic.model_slug,
                account_slug=account.slug,
            )
            is_new_page_spillover = len(pages) > len(trace_msgs)
            if (
                not is_new_page_spillover
                and (now - last_edit_ts) < settings.tg_edit_throttle_sec
            ):
                return
            last_edit_ts = now
            # Fire-and-forget UI sync so Telegram FloodWaits never block agy stdout reading
            asyncio.create_task(_bg_sync_pages(pages))

        self._mount_mgr.cancel_topic_prompts(topic_id)
        try:
            result = await self._orchestrator.execute_topic_turn(
                topic_id=topic_id,
                prompt=prompt,
                on_progress=_on_progress,
            )
        except Exception as exc:
            max_err = self._cfg.limits.error_summary_max_chars
            err_short = str(exc)[:max_err]
            logger.error("Turn execution failed", topic_id=topic_id, error=err_short)
            try:
                await trace_msgs[-1].edit(
                    lex.ws_turn_error_msg.format(error=err_short), buttons=None
                )
            except RPCError as edit_exc:
                logger.warning(
                    "Failed to update status message with turn error",
                    error=str(edit_exc),
                )
            return

        updated_topic = await self._topic_repo.get_topic_strict(topic_id)
        updated_account = await self._account_repo.get_by_id(
            updated_topic.assigned_account_id
        )
        upd_acc_display = (
            updated_account.email_label
            if "@" in updated_account.email_label
            else updated_account.slug
        )

        final_pages = build_final_execution_trace_pages(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=upd_acc_display,
        )
        if final_pages:
            async with sync_lock:
                await _sync_trace_pages(final_pages, is_final=True)
            if len(trace_msgs) > len(final_pages):
                extra_msgs = trace_msgs[len(final_pages) :]
                del trace_msgs[len(final_pages) :]
                for extra_msg in extra_msgs:
                    with contextlib.suppress(RPCError):
                        await extra_msg.delete()
        else:
            for msg in trace_msgs:
                with contextlib.suppress(RPCError):
                    await msg.delete()

        footer_bar = render_turn_footer(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=upd_acc_display,
        )
        body_text = (
            result.response_text.strip()
            if result.response_text.strip()
            else lex.ws_empty_response_fallback.format(status=result.status.value)
        )

        try:
            formatted_chunks = render_Formatted_response_chunks(
                body_markdown=body_text,
                footer_bar_text=footer_bar,
                max_chars=settings.tg_max_message_chars,
                lex=lex,
            )
            for idx, (chunk_text, chunk_entities) in enumerate(formatted_chunks):
                is_first = idx == 0
                is_last = idx == len(formatted_chunks) - 1
                await self._client.send_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=chunk_text,
                    formatting_entities=chunk_entities,
                    reply_to=reply_to_msg_id if is_first else topic_id,
                    silent=not is_first,
                    buttons=(
                        build_turn_response_keyboard(lex, updated_topic)
                        if is_last
                        else None
                    ),
                )
        except Exception as exc:
            logger.error(
                "Failed to render or send formatted response chunks; sending plain fallback",
                topic_id=topic_id,
                error=str(exc),
                exc_info=True,
            )
            for plain_chunk in split_message_chunks(
                f"{body_text}\n\n{footer_bar}", settings.tg_max_message_chars
            ):
                await self._client.send_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=plain_chunk,
                    reply_to=topic_id,
                )

        await self._auto_sync_topic_title_if_generic(
            updated_topic, result.conversation_id
        )
        await self.refresh_pinned_card(topic_id)

    async def _auto_sync_topic_title_if_generic(
        self, topic: ForumTopic, conversation_id: UUID
    ) -> None:
        if not topic.title.startswith(("💬", "Рабочий топик #", "Workspace #")):
            return
        try:
            summary = self._history_reader.get_conversation_summary(conversation_id)
        except AgyProcessError:
            return
        if (
            summary is not None
            and summary.title.strip()
            and summary.title != "Untitled Session"
        ):
            new_title = summary.title.strip()
            try:
                await self._lifecycle.rename_topic(topic.topic_id, new_title)
                logger.info(
                    "Auto-renamed topic title from agy conversation summary",
                    topic_id=topic.topic_id,
                    new_title=new_title,
                )
            except RPCError as exc:
                logger.debug(
                    "Could not auto-rename forum topic",
                    topic_id=topic.topic_id,
                    error=str(exc),
                )

    async def _run_interactive_wake_turn(
        self,
        topic_id: int,
        wake_prompt: str,
        badge_prefix: str,
        conversation_id: UUID | None,
    ) -> None:
        """Executes an automated wake-up turn (timer or sensor) with full live tool trace in the topic."""
        topic = await self.ensure_topic_registered(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        start_ts = time.monotonic()
        last_edit_ts = start_ts

        status_msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=f"{badge_prefix}\n"
            + lex.ws_starting_turn_msg.format(
                model_slug=topic.model_slug, account_slug=account.slug
            ),
            reply_to=topic_id,
            silent=True,
            buttons=build_live_progress_keyboard(lex, topic_id),
        )
        trace_msgs = [status_msg]
        rendered_page_state: dict[int, tuple[str, bool]] = {}
        sync_lock = asyncio.Lock()

        async def _sync_trace_pages(
            pages: list[tuple[str, list[object]]], is_final: bool
        ) -> None:
            from telethon.tl.types import TypeMessageEntity

            total_pages = len(pages)
            for idx, (page_text, raw_ents) in enumerate(pages):
                ents: list[TypeMessageEntity] = [
                    e for e in raw_ents if isinstance(e, TypeMessageEntity)
                ]
                has_cancel_btn = (not is_final) and (idx == total_pages - 1)
                desired_state = (page_text, has_cancel_btn)

                if idx < len(trace_msgs):
                    if rendered_page_state.get(idx) == desired_state:
                        continue
                    try:
                        await self._client.edit_message(
                            entity=self._cfg.telegram.forum_chat_id,
                            message=trace_msgs[idx].id,
                            text=page_text,
                            formatting_entities=ents,
                            buttons=(
                                build_live_progress_keyboard(lex, topic_id)
                                if has_cancel_btn
                                else None
                            ),
                        )
                        rendered_page_state[idx] = desired_state
                    except MessageNotModifiedError:
                        rendered_page_state[idx] = desired_state
                    except RPCError:
                        if is_final:
                            with contextlib.suppress(RPCError):
                                await self._client.edit_message(
                                    entity=self._cfg.telegram.forum_chat_id,
                                    message=trace_msgs[idx].id,
                                    buttons=None,
                                )
                else:
                    with contextlib.suppress(RPCError):
                        new_msg = await self._client.send_message(
                            entity=self._cfg.telegram.forum_chat_id,
                            message=page_text,
                            formatting_entities=ents,
                            reply_to=topic_id,
                            silent=True,
                            buttons=(
                                build_live_progress_keyboard(lex, topic_id)
                                if has_cancel_btn
                                else None
                            ),
                        )
                        trace_msgs.append(new_msg)
                        rendered_page_state[idx] = desired_state

        async def _bg_sync_pages(pages: list[tuple[str, list[object]]]) -> None:
            if sync_lock.locked():
                return
            with contextlib.suppress(Exception):
                async with sync_lock:
                    await _sync_trace_pages(pages, is_final=False)

        async def _on_progress(progress: TurnProgressEvent) -> None:
            nonlocal last_edit_ts
            now = time.monotonic()
            pages = build_live_progress_pages(
                lex=lex,
                limits=self._cfg.limits,
                event=progress,
                elapsed_sec=now - start_ts,
                model_slug=topic.model_slug,
                account_slug=account.slug,
            )
            is_new_page_spillover = len(pages) > len(trace_msgs)
            if (
                not is_new_page_spillover
                and (now - last_edit_ts) < settings.tg_edit_throttle_sec
            ):
                return
            last_edit_ts = now
            asyncio.create_task(_bg_sync_pages(pages))

        try:
            result = await self._orchestrator.execute_scheduled_turn(
                topic_id=topic_id,
                prompt=wake_prompt,
                conversation_id=conversation_id,
                on_progress=_on_progress,
            )
        except Exception as exc:
            max_err = self._cfg.limits.error_summary_max_chars
            with contextlib.suppress(RPCError):
                await trace_msgs[-1].edit(
                    lex.ws_turn_error_msg.format(error=str(exc)[:max_err]),
                    buttons=None,
                )
            return

        cleaned_resp = result.response_text.strip()
        updated_topic = await self._topic_repo.get_topic_strict(topic_id)
        updated_account = await self._account_repo.get_by_id(
            updated_topic.assigned_account_id
        )

        final_pages = build_final_execution_trace_pages(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=updated_account.slug,
        )
        if final_pages:
            async with sync_lock:
                await _sync_trace_pages(final_pages, is_final=True)
            if len(trace_msgs) > len(final_pages):
                for extra_msg in trace_msgs[len(final_pages) :]:
                    with contextlib.suppress(RPCError):
                        await extra_msg.delete()
        else:
            for msg in trace_msgs:
                with contextlib.suppress(RPCError):
                    await msg.delete()

        footer_bar = f"{badge_prefix}  │  " + render_turn_footer(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=updated_account.slug,
        )
        body_text = (
            cleaned_resp
            if cleaned_resp
            else lex.ws_empty_response_fallback.format(status=result.status.value)
        )
        formatted_chunks = render_Formatted_response_chunks(
            body_markdown=body_text,
            footer_bar_text=footer_bar,
            max_chars=settings.tg_max_message_chars,
            lex=lex,
        )
        for idx, (chunk_text, chunk_entities) in enumerate(formatted_chunks):
            is_last = idx == len(formatted_chunks) - 1
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=chunk_text,
                formatting_entities=chunk_entities,
                reply_to=topic_id,
                buttons=(
                    build_turn_response_keyboard(lex, updated_topic)
                    if is_last
                    else None
                ),
            )
        await self.refresh_pinned_card(topic_id)

    async def execute_scheduled_wake_turn(
        self, topic_id: int, wake_prompt: str, conversation_id: UUID | None
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        await self._run_interactive_wake_turn(
            topic_id=topic_id,
            wake_prompt=wake_prompt,
            badge_prefix=lex.wake_badge_timer,
            conversation_id=conversation_id,
        )

    async def execute_sensor_wake_turn(
        self,
        topic_id: int,
        sensor_name: str,
        wake_prompt: str,
        conversation_id: UUID | None,
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        await self._run_interactive_wake_turn(
            topic_id=topic_id,
            wake_prompt=wake_prompt,
            badge_prefix=lex.wake_badge_sensor.format(sensor_name=sensor_name),
            conversation_id=conversation_id,
        )

    async def deliver_sensor_reflex_report(
        self, topic_id: int, sensor_name: str, reflex_report_text: str
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        topic = await self.ensure_topic_registered(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)

        sensor_bar = lex.sensor_footer_bar.format(
            sensor_name=sensor_name,
            model_slug=topic.model_slug,
            account_slug=account.slug,
        )
        formatted_chunks = render_Formatted_response_chunks(
            body_markdown=reflex_report_text,
            footer_bar_text=sensor_bar,
            max_chars=settings.tg_max_message_chars,
            lex=lex,
        )
        for chunk_text, chunk_entities in formatted_chunks:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=chunk_text,
                formatting_entities=chunk_entities,
                reply_to=topic_id,
            )
        await self.refresh_pinned_card(topic_id)

    async def handle_callback(
        self, event: events.CallbackQuery.Event, data_str: str
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        parts = data_str.split(":")
        action = parts[1]
        topic_id = int(parts[2])

        if action == "close_sub":
            await event.delete()
            return

        if action == "confirm_dir":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            Path(topic.workspace_path).mkdir(parents=True, exist_ok=True)
            await event.edit(
                lex.ws_dir_confirmed_msg.format(workspace_path=topic.workspace_path),
                buttons=None,
            )
            return

        if action == "custom_dir":
            self._awaiting_custom_dir_topics.add(topic_id)
            await event.edit(lex.ws_custom_dir_prompt, buttons=None)
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

        if action == "toggle_sudo":
            updated = await self._lifecycle.toggle_sudo_carte_blanche(topic_id)
            await self.refresh_pinned_card(topic_id)
            toast_msg = (
                lex.ws_toast_sudo_cb_on
                if updated.sudo_carte_blanche
                else lex.ws_toast_sudo_cb_off
            )
            await event.answer(toast_msg)
            return

        if action in ("sudo_ok", "sudo_cb", "sudo_no") and len(parts) >= 4:
            req_id = parts[3]
            decision_map = {
                "sudo_ok": "allow",
                "sudo_cb": "carte_blanche",
                "sudo_no": "deny",
            }
            decision = decision_map[action]
            self._mount_mgr.resolve_sudo_approval(req_id, decision)
            meta = (
                self._sudo_prompt_meta.pop(req_id)
                if req_id in self._sudo_prompt_meta
                else ("remote", "sudo")
            )
            target_lbl, summary_lbl = meta
            if action == "sudo_cb":
                await self._lifecycle.set_sudo_carte_blanche(topic_id, True)
                await self.refresh_pinned_card(topic_id)
                status_text = lex.sudo_carte_blanche_status.format(
                    action_summary=summary_lbl, target=target_lbl
                )
            elif action == "sudo_ok":
                status_text = lex.sudo_approved_status.format(
                    action_summary=summary_lbl, target=target_lbl
                )
            else:
                status_text = lex.sudo_rejected_status.format(
                    action_summary=summary_lbl, target=target_lbl
                )
            self._relocate_trace_topics.add(topic_id)
            await event.edit(status_text, buttons=None)
            return

        if action == "cancel":
            self._mount_mgr.cancel_topic_prompts(topic_id)
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
