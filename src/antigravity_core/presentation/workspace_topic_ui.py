import asyncio
import contextlib
from pathlib import Path
import time
from uuid import UUID

from collections.abc import Awaitable, Callable
from typing import Final

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError, RPCError
from telethon.tl.custom import Message
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
)
from antigravity_core.presentation.tg_renderer import (
    edit_rich_or_standard_message,
    send_rich_or_standard_message,
)
from antigravity_core.presentation.keyboards import (
    build_live_progress_keyboard,
    build_model_picker_keyboard,
    build_model_variants_keyboard,
    build_session_history_keyboard,
    build_staged_media_keyboard,
    build_sudo_approval_keyboard,
    build_topic_account_picker_keyboard,
    build_topic_header_keyboard,
    build_turn_response_keyboard,
    build_workspace_dir_picker_keyboard,
    group_models_by_family,
)

_CMD_WITH_ARG_PARTS: Final[int] = 2
_SUDO_CB_MIN_PARTS: Final[int] = 4
_TITLE_SYNC_ATTEMPTS: Final[int] = 4


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
        self._last_trace_edit_ts: dict[int, float] = {}
        self._staged_media_msg_ids: dict[int, int] = {}
        self._flush_staged_cb: Callable[[int, str], Awaitable[bool]] | None = None
        self._clear_staged_cb: Callable[[int], Awaitable[int]] | None = None

    def bind_staged_media_callbacks(
        self,
        flush_cb: Callable[[int, str], Awaitable[bool]],
        clear_cb: Callable[[int], Awaitable[int]],
    ) -> None:
        self._flush_staged_cb = flush_cb
        self._clear_staged_cb = clear_cb

    async def _clear_staged_media_card(self, topic_id: int) -> None:
        if topic_id in self._staged_media_msg_ids:
            stale_mid = self._staged_media_msg_ids.pop(topic_id)
            with contextlib.suppress(RPCError):
                await self._client.delete_messages(
                    entity=self._cfg.telegram.forum_chat_id,
                    message_ids=[stale_mid],
                )

    async def notify_staged_media_held(
        self, topic_id: int, message_id: int, staged_files: tuple[str, ...]
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        files_list = "\n".join(f"• `{Path(p).name}`" for p in staged_files)
        card_text = lex.ws_staged_media_card.format(
            count=len(staged_files),
            files_list=files_list,
        )
        kb = build_staged_media_keyboard(lex, topic_id)
        await self._clear_staged_media_card(topic_id)

        sent = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=card_text,
            reply_to=message_id,
            silent=True,
            buttons=kb,
        )
        self._staged_media_msg_ids[topic_id] = sent.id

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
            with contextlib.suppress(MessageNotModifiedError):
                await edit_rich_or_standard_message(
                    client=self._client,
                    chat_id=self._cfg.telegram.forum_chat_id,
                    message_id=topic.pinned_card_msg_id,
                    markdown_text=card_text,
                    fallback_text=card_text,
                    fallback_entities=None,
                    buttons=buttons,
                )
            return

        sent = await send_rich_or_standard_message(
            client=self._client,
            chat_id=self._cfg.telegram.forum_chat_id,
            markdown_text=card_text,
            fallback_text=card_text,
            fallback_entities=None,
            reply_to=topic_id,
            silent=False,
            buttons=buttons,
        )
        await self._topic_repo.set_pinned_card_msg_id(topic_id, sent.id)
        with contextlib.suppress(RPCError):
            await self._client.pin_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=sent.id,
                notify=False,
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
                    min_p = (
                        q_info["min_remaining_pct"]
                        if "min_remaining_pct" in q_info
                        else "?"
                    )
                    body = f"{hdr_q}\n" + lex.ws_quota_fallback_body.format(
                        min_pct=min_p
                    )
                await wait_msg.edit(body)
            except AgyProcessError as exc:
                max_err = self._cfg.limits.error_summary_max_chars
                await wait_msg.edit(
                    lex.ws_quota_error_msg.format(error=str(exc)[:max_err])
                )
                raise
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
                raise

            if len(parts) == _CMD_WITH_ARG_PARTS and parts[1].strip():
                requested = parts[1].strip()
                matched_slug = next(
                    (m for m in models if m.lower() == requested.lower()),
                    None,
                )
                if matched_slug is None or not is_valid_model_slug_syntax(matched_slug):
                    formatted_list = ", ".join(f"`{m}`" for m in models)
                    await self._client.send_message(
                        entity=self._cfg.telegram.forum_chat_id,
                        message=lex.ws_model_unknown_msg.format(
                            model_slug=requested,
                            available_models=formatted_list,
                        ),
                        reply_to=topic_id,
                    )
                    return True

                await self._lifecycle.change_model(
                    topic_id, matched_slug, topic.effort_level
                )
                await self.refresh_pinned_card(topic_id)
                await self._client.send_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=lex.ws_model_updated_msg.format(model_slug=matched_slug),
                    reply_to=topic_id,
                )
                return True

            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=lex.ws_select_model_group_prompt.format(
                    current_model=topic.model_slug
                ),
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

        if cmd == "/rename":
            parts = raw_text.split(maxsplit=1)
            if len(parts) == _CMD_WITH_ARG_PARTS and parts[1].strip():
                new_title = parts[1].strip()
                await self._lifecycle.rename_topic(topic_id, new_title)
                await self.refresh_pinned_card(topic_id)
                return True

        return False

    async def _sync_single_trace_page_list(
        self,
        topic_id: int,
        trace_msgs: list[Message],
        rendered_page_state: dict[int, tuple[str, bool]],
        pages: list[tuple[str, list[TypeMessageEntity]]],
        is_final: bool,
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        if topic_id in self._relocate_trace_topics:
            self._relocate_trace_topics.discard(topic_id)
            old_ids = [m.id for m in trace_msgs]
            if old_ids:
                with contextlib.suppress(RPCError):
                    await self._client.delete_messages(
                        entity=self._cfg.telegram.forum_chat_id,
                        message_ids=old_ids,
                    )
            trace_msgs.clear()
            rendered_page_state.clear()

        total_pages = len(pages)
        for idx, (page_text, ents) in enumerate(pages):
            has_cancel_btn = (not is_final) and (idx == total_pages - 1)
            desired_state = (page_text, has_cancel_btn)
            kb = build_live_progress_keyboard(lex, topic_id) if has_cancel_btn else None

            if idx < len(trace_msgs):
                if idx in rendered_page_state and rendered_page_state[idx] == desired_state:
                    continue
                msg_id = trace_msgs[idx].id
                with contextlib.suppress(MessageNotModifiedError, RPCError):
                    await self._client.edit_message(
                        entity=self._cfg.telegram.forum_chat_id,
                        message=msg_id,
                        text=page_text,
                        formatting_entities=ents,
                        buttons=kb,
                    )
                    rendered_page_state[idx] = desired_state
                if is_final and idx not in rendered_page_state:
                    with contextlib.suppress(RPCError):
                        await self._client.edit_message(
                            entity=self._cfg.telegram.forum_chat_id,
                            message=msg_id,
                            buttons=None,
                        )
            else:
                with contextlib.suppress(RPCError):
                    new_msg: Message = await self._client.send_message(
                        entity=self._cfg.telegram.forum_chat_id,
                        message=page_text,
                        formatting_entities=ents,
                        reply_to=topic_id,
                        silent=True,
                        buttons=kb,
                    )
                    trace_msgs.append(new_msg)
                    rendered_page_state[idx] = desired_state

    async def _deliver_formatted_turn_output(
        self,
        topic_id: int,
        reply_to_msg_id: int,
        body_text: str,
        footer_bar: str,
        updated_topic: ForumTopic,
    ) -> None:
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        formatted_chunks = render_Formatted_response_chunks(
            body_markdown=body_text,
            footer_bar_text=footer_bar,
            max_chars=settings.tg_max_message_chars,
            lex=lex,
        )
        total_chunks = len(formatted_chunks)
        for idx, (chunk_text, chunk_entities) in enumerate(formatted_chunks):
            is_first = idx == 0
            is_last = idx == total_chunks - 1
            rich_md = (
                f"{body_text}\n\n`{footer_bar.strip()}`"
                if total_chunks == 1
                else chunk_text
            )
            await send_rich_or_standard_message(
                client=self._client,
                chat_id=self._cfg.telegram.forum_chat_id,
                markdown_text=rich_md,
                fallback_text=chunk_text,
                fallback_entities=chunk_entities,
                reply_to=reply_to_msg_id if is_first else topic_id,
                silent=not is_first,
                buttons=(
                    build_turn_response_keyboard(lex, updated_topic)
                    if is_last
                    else None
                ),
            )

    async def _finalize_trace_messages(
        self,
        topic_id: int,
        trace_msgs: list[Message],
        rendered_page_state: dict[int, tuple[str, bool]],
        sync_lock: asyncio.Lock,
        final_pages: list[tuple[str, list[TypeMessageEntity]]],
    ) -> None:
        if final_pages:
            async with sync_lock:
                await self._sync_single_trace_page_list(
                    topic_id, trace_msgs, rendered_page_state, final_pages, True
                )
            if len(trace_msgs) > len(final_pages):
                extra_ids = [m.id for m in trace_msgs[len(final_pages) :]]
                del trace_msgs[len(final_pages) :]
                if extra_ids:
                    with contextlib.suppress(RPCError):
                        await self._client.delete_messages(
                            entity=self._cfg.telegram.forum_chat_id,
                            message_ids=extra_ids,
                        )
        else:
            del_ids = [m.id for m in trace_msgs]
            if del_ids:
                with contextlib.suppress(RPCError):
                    await self._client.delete_messages(
                        entity=self._cfg.telegram.forum_chat_id,
                        message_ids=del_ids,
                    )

    async def _handle_custom_dir_reply(
        self, topic_id: int, reply_to_msg_id: int, prompt: str
    ) -> None:
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

    async def execute_batched_prompt(
        self, topic_id: int, reply_to_msg_id: int, prompt: str
    ) -> None:
        if topic_id in self._awaiting_custom_dir_topics:
            await self._handle_custom_dir_reply(topic_id, reply_to_msg_id, prompt)
            return

        topic = await self.ensure_topic_registered(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        logger.info(
            "Starting user turn in topic: topic_id={} model={} account={} workspace={}",
            topic_id,
            topic.model_slug,
            account.slug,
            topic.workspace_path,
        )
        await self._clear_staged_media_card(topic_id)
        start_ts = time.monotonic()
        self._last_trace_edit_ts[topic_id] = start_ts

        acc_display = account.display_email
        status_msg: Message = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.ws_starting_turn_msg.format(
                model_slug=topic.model_slug, account_slug=acc_display
            ),
            reply_to=topic_id,
            silent=True,
            buttons=build_live_progress_keyboard(lex, topic_id),
        )
        trace_msgs: list[Message] = [status_msg]
        rendered_page_state: dict[int, tuple[str, bool]] = {}
        sync_lock = asyncio.Lock()

        async def _bg_sync_pages(
            pages: list[tuple[str, list[TypeMessageEntity]]],
        ) -> None:
            if sync_lock.locked() or not self._client.is_connected():
                return
            async with sync_lock:
                if self._client.is_connected():
                    await self._sync_single_trace_page_list(
                        topic_id, trace_msgs, rendered_page_state, pages, False
                    )

        async def _on_progress(progress: TurnProgressEvent) -> None:
            now = time.monotonic()
            prev_ts = (
                self._last_trace_edit_ts[topic_id]
                if topic_id in self._last_trace_edit_ts
                else 0.0
            )
            if (now - prev_ts) < settings.tg_edit_throttle_sec:
                return
            self._last_trace_edit_ts[topic_id] = now
            if not self._client.is_connected():
                return
            pages = build_live_progress_pages(
                lex=lex,
                limits=self._cfg.limits,
                event=progress,
                elapsed_sec=now - start_ts,
                model_slug=topic.model_slug,
                account_slug=acc_display,
            )
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
            logger.error("Turn execution failed: topic_id={} error={}", topic_id, err_short)
            last_mid = trace_msgs[-1].id
            with contextlib.suppress(RPCError):
                await self._client.edit_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=last_mid,
                    text=lex.ws_turn_error_msg.format(error=err_short),
                    buttons=None,
                )
            raise
        finally:
            self._mount_mgr.cleanup_topic_staged_files(topic_id)

        updated_topic = await self._topic_repo.get_topic_strict(topic_id)
        updated_account = await self._account_repo.get_by_id(
            updated_topic.assigned_account_id
        )
        upd_acc_display = updated_account.display_email

        final_pages = build_final_execution_trace_pages(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=upd_acc_display,
        )
        await self._finalize_trace_messages(
            topic_id, trace_msgs, rendered_page_state, sync_lock, final_pages
        )

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
        await self._deliver_formatted_turn_output(
            topic_id, reply_to_msg_id, body_text, footer_bar, updated_topic
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
        summary = None
        for _ in range(_TITLE_SYNC_ATTEMPTS):
            summary = self._history_reader.get_conversation_summary(conversation_id)
            if (
                summary is not None
                and summary.title.strip()
                and summary.title != "Untitled Session"
            ):
                break
            await asyncio.sleep(0.5)

        if (
            summary is not None
            and summary.title.strip()
            and summary.title != "Untitled Session"
        ):
            new_title = summary.title.strip()[: self._cfg.limits.topic_title_max_chars]
            await self._lifecycle.rename_topic(topic.topic_id, new_title)
            logger.info(
                "Auto-renamed topic title from agy conversation summary",
                topic_id=topic.topic_id,
                new_title=new_title,
            )

    async def _run_interactive_wake_turn(
        self,
        topic_id: int,
        wake_prompt: str,
        badge_prefix: str,
        conversation_id: UUID | None,
    ) -> None:
        """Executes an automated wake-up turn (timer or sensor) with full live tool trace in the topic."""
        existing_topic = await self._topic_repo.get_topic(topic_id)
        if existing_topic is None or existing_topic.status.value == "ARCHIVED":
            logger.warning(
                "Skipping wake turn for missing or archived topic",
                topic_id=topic_id,
            )
            return

        topic = existing_topic
        account = await self._account_repo.get_by_id(topic.assigned_account_id)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        start_ts = time.monotonic()
        self._last_trace_edit_ts[topic_id] = start_ts
        acc_display = account.display_email

        status_msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=f"{badge_prefix}\n"
            + lex.ws_starting_turn_msg.format(
                model_slug=topic.model_slug, account_slug=acc_display
            ),
            reply_to=topic_id,
            silent=True,
            buttons=build_live_progress_keyboard(lex, topic_id),
        )
        trace_msgs: list[Message] = [status_msg]
        rendered_page_state: dict[int, tuple[str, bool]] = {}
        sync_lock = asyncio.Lock()

        async def _bg_sync_pages(
            pages: list[tuple[str, list[TypeMessageEntity]]],
        ) -> None:
            if sync_lock.locked() or not self._client.is_connected():
                return
            async with sync_lock:
                if self._client.is_connected():
                    await self._sync_single_trace_page_list(
                        topic_id, trace_msgs, rendered_page_state, pages, False
                    )

        async def _on_progress(progress: TurnProgressEvent) -> None:
            now = time.monotonic()
            prev_ts = (
                self._last_trace_edit_ts[topic_id]
                if topic_id in self._last_trace_edit_ts
                else 0.0
            )
            if (now - prev_ts) < settings.tg_edit_throttle_sec:
                return
            self._last_trace_edit_ts[topic_id] = now
            if not self._client.is_connected():
                return
            pages = build_live_progress_pages(
                lex=lex,
                limits=self._cfg.limits,
                event=progress,
                elapsed_sec=now - start_ts,
                model_slug=topic.model_slug,
                account_slug=f"{badge_prefix} │ {acc_display}",
            )
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
            last_mid = trace_msgs[-1].id
            with contextlib.suppress(RPCError):
                await self._client.edit_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=last_mid,
                    text=lex.ws_turn_error_msg.format(error=str(exc)[:max_err]),
                    buttons=None,
                )
            raise
        finally:
            self._mount_mgr.cleanup_topic_staged_files(topic_id)

        cleaned_resp = result.response_text.strip()
        updated_topic = await self._topic_repo.get_topic_strict(topic_id)
        updated_account = await self._account_repo.get_by_id(
            updated_topic.assigned_account_id
        )
        upd_acc_display = updated_account.display_email

        final_pages = build_final_execution_trace_pages(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=f"{badge_prefix} │ {upd_acc_display}",
        )
        await self._finalize_trace_messages(
            topic_id, trace_msgs, rendered_page_state, sync_lock, final_pages
        )

        footer_bar = f"{badge_prefix}  │  " + render_turn_footer(
            lex=lex,
            limits=self._cfg.limits,
            result=result,
            model_slug=updated_topic.model_slug,
            account_slug=upd_acc_display,
        )
        body_text = (
            cleaned_resp
            if cleaned_resp
            else lex.ws_empty_response_fallback.format(status=result.status.value)
        )
        await self._deliver_formatted_turn_output(
            topic_id, topic_id, body_text, footer_bar, updated_topic
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
            account_slug=account.display_email,
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

        parts = data_str.split(":", 3)
        action = parts[1]
        topic_id = int(parts[2])

        if action == "close_sub":
            await event.delete()
            return

        if action == "back_card":
            await self._edit_event_to_topic_passport(event, topic_id)
            return

        if action == "flush_files":
            self._staged_media_msg_ids.pop(topic_id, None)
            await event.delete()
            if self._flush_staged_cb is not None:
                await self._flush_staged_cb(
                    topic_id, lex.ws_staged_media_default_prompt
                )
            return

        if action == "clear_files":
            self._staged_media_msg_ids.pop(topic_id, None)
            if self._clear_staged_cb is not None:
                await self._clear_staged_cb(topic_id)
            await event.answer(lex.ws_toast_staged_media_cleared)
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

        if action in ("sudo_ok", "sudo_cb", "sudo_no") and len(parts) >= _SUDO_CB_MIN_PARTS:
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
            await event.edit(
                format_wide_header_block(lex.ws_select_past_session_prompt),
                buttons=build_session_history_keyboard(
                    lex,
                    self._cfg.limits,
                    topic_id,
                    sessions,
                    topic.active_conversation_id,
                ),
            )
            return

        if action == "resume":
            conv_uuid = UUID(parts[3])
            await self._lifecycle.resume_past_session(topic_id, conv_uuid)
            await event.answer(
                lex.ws_session_resumed_msg.format(conversation_id=conv_uuid)
            )
            await self._edit_event_to_topic_passport(event, topic_id)
            return

        if action == "models":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            account = await self._account_repo.get_by_id(topic.assigned_account_id)
            models = await self._inspector.list_available_models(account)
            await event.edit(
                format_wide_header_block(
                    lex.ws_select_model_group_prompt.format(
                        current_model=topic.model_slug
                    )
                ),
                buttons=build_model_picker_keyboard(
                    lex,
                    self._cfg.limits,
                    topic_id,
                    models,
                    topic.model_slug,
                ),
            )
            return

        if action == "mgrp":
            grp_idx = int(parts[3])
            topic = await self._topic_repo.get_topic_strict(topic_id)
            account = await self._account_repo.get_by_id(topic.assigned_account_id)
            models = await self._inspector.list_available_models(account)
            groups = group_models_by_family(models)
            if 0 <= grp_idx < len(groups):
                family_label, variants = groups[grp_idx]
                await event.edit(
                    format_wide_header_block(
                        lex.ws_select_model_variant_prompt.format(family=family_label)
                    ),
                    buttons=build_model_variants_keyboard(
                        lex,
                        topic_id,
                        variants,
                        topic.model_slug,
                    ),
                )
            return

        if action == "set_model":
            model_slug = parts[3]
            topic = await self._topic_repo.get_topic_strict(topic_id)
            await self._lifecycle.change_model(topic_id, model_slug, topic.effort_level)
            await event.answer(
                lex.ws_model_updated_msg.format(model_slug=model_slug)
            )
            await self._edit_event_to_topic_passport(event, topic_id)
            return

        if action == "set_effort":
            effort = EffortLevel(parts[3])
            topic = await self._topic_repo.get_topic_strict(topic_id)
            await self._lifecycle.change_model(topic_id, topic.model_slug, effort)
            await event.answer(
                lex.ws_effort_updated_msg.format(effort=effort.value)
            )
            await self._edit_event_to_topic_passport(event, topic_id)
            return

        if action == "accounts":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            accounts = await self._account_repo.list_enabled_accounts()
            await event.edit(
                format_wide_header_block(lex.ws_select_account_prompt),
                buttons=build_topic_account_picker_keyboard(
                    lex, topic_id, accounts, topic.assigned_account_id
                ),
            )
            return

        if action == "set_acc":
            acc_id = int(parts[3])
            await self._lifecycle.switch_topic_account(topic_id, acc_id)
            await event.answer(lex.ws_account_switched_msg)
            await self._edit_event_to_topic_passport(event, topic_id)
            return

        if action == "dirs":
            choices = list_workspace_directories(
                lex,
                self._cfg.limits,
                self._cfg.paths.workspaces_root_dir,
                self._cfg.paths.remotes_mount_dir,
            )
            await event.edit(
                format_wide_header_block(lex.ws_select_workspace_prompt),
                buttons=build_workspace_dir_picker_keyboard(lex, topic_id, choices),
            )
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
                await event.answer(
                    lex.ws_workspace_updated_msg.format(workspace_path=target_path)
                )
                await self._edit_event_to_topic_passport(event, topic_id)

    async def _edit_event_to_topic_passport(
        self, event: events.CallbackQuery.Event, topic_id: int
    ) -> None:
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
        with contextlib.suppress(MessageNotModifiedError):
            await event.edit(card_text, buttons=buttons)
        await event.answer()
        if topic.pinned_card_msg_id is not None and topic.pinned_card_msg_id != event.message_id:
            await self.refresh_pinned_card(topic_id)
