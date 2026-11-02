import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import structlog
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError, RPCError

from antigravity_core.application.scheduler_service import TemporalSchedulerService
from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import ExecutionMode, TopicKind, UserRole
from antigravity_core.infrastructure.agy_runtime.cli_inspector import AgyCliInspector
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.crypto.fernet_vault import FernetVault
from antigravity_core.infrastructure.database.repositories import (
    AccessControlRepository,
    AccountRepository,
    RemoteHostRepository,
    SettingsRepository,
)
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import (
    KeyringProfileManager,
    PendingOAuthSession,
)
from antigravity_core.infrastructure.remotes.sshfs_mount_mgr import RemoteMountManager
from antigravity_core.lexicon import SUPPORTED_LANGUAGES, UiLexicon, get_lexicon
from antigravity_core.presentation.formatters import (
    format_wide_header_block,
    render_control_panel_dashboard,
)
from antigravity_core.presentation.keyboards import (
    build_account_auth_keyboard,
    build_control_panel_keyboard,
    build_default_account_picker_keyboard,
    build_oauth_url_keyboard,
    build_setting_options_keyboard,
    build_settings_menu_keyboard,
    build_tasks_management_keyboard,
)
from antigravity_core.presentation.workspace_topic_ui import WorkspaceTopicUI

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _ControlPanelInteractiveState:
    mode: str
    setting_key: str | None
    pending_oauth: PendingOAuthSession | None


class ControlPanelUI:
    """Handles all interactions, wizards, and dashboard updates inside #Control-Panel (topic_id = 1)."""

    def __init__(
        self,
        client: TelegramClient,
        static_config: StaticConfig,
        vault: FernetVault,
        keyring_mgr: KeyringProfileManager,
        mount_mgr: RemoteMountManager,
        worker_pool: AgyWorkerPool,
        cli_inspector: AgyCliInspector,
        scheduler_service: TemporalSchedulerService,
        topic_lifecycle: TopicLifecycleService,
        workspace_ui: WorkspaceTopicUI,
        settings_repo: SettingsRepository,
        account_repo: AccountRepository,
        host_repo: RemoteHostRepository,
        access_repo: AccessControlRepository,
    ) -> None:
        self._client = client
        self._cfg = static_config
        self._vault = vault
        self._keyring_mgr = keyring_mgr
        self._mount_mgr = mount_mgr
        self._pool = worker_pool
        self._inspector = cli_inspector
        self._scheduler = scheduler_service
        self._topic_lifecycle = topic_lifecycle
        self._workspace_ui = workspace_ui
        self._settings_repo = settings_repo
        self._account_repo = account_repo
        self._host_repo = host_repo
        self._access_repo = access_repo
        self._state: _ControlPanelInteractiveState | None = None

    async def _load_lexicon(self) -> UiLexicon:
        settings = await self._settings_repo.load_settings()
        return get_lexicon(settings.ui_language)

    @staticmethod
    async def _safe_edit_event(
        event: events.CallbackQuery.Event, text: str, buttons: object
    ) -> None:
        try:
            await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            await event.answer()

    async def render_and_pin_dashboard(self, recreate_at_bottom: bool = False) -> None:
        lex, text = await self._build_dashboard_text()
        buttons = build_control_panel_keyboard(lex)
        cp_topic_id = self._cfg.telegram.control_panel_topic_id
        stored_msg_id = await self._settings_repo.get_dashboard_msg_id()

        if stored_msg_id is not None and not recreate_at_bottom:
            try:
                await self._client.edit_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=stored_msg_id,
                    text=text,
                    buttons=buttons,
                )
                return
            except MessageNotModifiedError:
                return
            except RPCError as exc:
                logger.debug("Stored dashboard message no longer editable; recreating", error=str(exc))

        if stored_msg_id is not None:
            try:
                await self._client.delete_messages(
                    entity=self._cfg.telegram.forum_chat_id,
                    message_ids=[stored_msg_id],
                )
            except RPCError as exc:
                logger.debug("Could not delete old dashboard message", error=str(exc))

        msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=text,
            reply_to=cp_topic_id,
            buttons=buttons,
        )
        await self._settings_repo.set_dashboard_msg_id(msg.id)
        try:
            await self._client.pin_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=msg.id,
                notify=False,
            )
        except RPCError as exc:
            logger.warning("Could not pin Control-Panel dashboard message", error=str(exc))

    async def _build_dashboard_text(self) -> tuple[UiLexicon, str]:
        accounts = await self._account_repo.list_accounts()
        hosts = await self._host_repo.list_hosts()
        mount_statuses = {
            h.alias: self._mount_mgr.is_mounted(Path(h.local_mount_path)) for h in hosts
        }
        pool_snapshots = await self._pool.get_pool_snapshots()
        tasks = await self._scheduler.list_tasks(topic_id=None)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        return lex, render_control_panel_dashboard(
            lex=lex,
            limits=self._cfg.limits,
            accounts=accounts,
            hosts=hosts,
            mount_statuses=mount_statuses,
            pool_snapshots=pool_snapshots,
            tasks=tasks,
            settings=settings,
        )

    async def handle_text_input(self, event: events.NewMessage.Event) -> None:
        raw_text = (event.message.message or "").strip()
        cp_topic_id = self._cfg.telegram.control_panel_topic_id

        if raw_text in ("/start", "/panel", "/dashboard"):
            self._state = None
            await self.render_and_pin_dashboard(recreate_at_bottom=True)
            return

        if self._state is None:
            await self.render_and_pin_dashboard(recreate_at_bottom=True)
            return

        state = self._state
        if state.mode == "await_new_account":
            self._state = None
            await self._process_new_account_input(raw_text)
            return

        if state.mode == "await_oauth_code" and state.pending_oauth is not None:
            self._state = None
            await self._process_oauth_code_input(state.pending_oauth, raw_text)
            return

    async def handle_callback(self, event: events.CallbackQuery.Event, data_str: str) -> None:
        lex = await self._load_lexicon()

        if data_str == "cp:refresh":
            self._state = None
            lex, text = await self._build_dashboard_text()
            await self._safe_edit_event(event, text, build_control_panel_keyboard(lex))
            return

        if data_str == "cp:probe_quotas":
            await event.answer(lex.cp_toast_probing_quotas)
            accounts = await self._account_repo.list_enabled_accounts()
            for acc in accounts:
                try:
                    q_info = await self._inspector.fetch_account_quota(acc)
                    await self._account_repo.update_quota_snapshot(acc.id, q_info)
                except Exception as exc:
                    logger.warning("Quota probe failed", account=acc.slug, error=str(exc))
            lex, text = await self._build_dashboard_text()
            await self._safe_edit_event(event, text, build_control_panel_keyboard(lex))
            return

        if data_str == "cp:auth_menu":
            accounts = await self._account_repo.list_accounts()
            prompt_lines = lex.cp_auth_menu_prompt.splitlines()
            wide_prompt = "\n".join(
                [format_wide_header_block(prompt_lines[0]), *prompt_lines[1:]]
            )
            await self._safe_edit_event(
                event,
                wide_prompt,
                build_account_auth_keyboard(lex, accounts),
            )
            return

        if data_str == "cp:register_new_acc":
            existing = await self._account_repo.list_accounts()
            next_idx = len(existing) + 1
            slug = f"acc_{next_idx}"
            email_label = f"Google Account #{next_idx}"
            xdg_dir = str((self._cfg.paths.profiles_data_dir / slug).resolve())
            dbus_sock = str((self._cfg.paths.runtime_dir / f"bus_{slug}.sock").resolve())
            keyring_password = secrets.token_urlsafe(self._cfg.runtime.keyring_password_bytes)
            encrypted_pass = self._vault.encrypt_text(keyring_password)

            account = await self._account_repo.create_account(
                slug=slug,
                email_label=email_label,
                xdg_data_dir=xdg_dir,
                dbus_socket_path=dbus_sock,
                encrypted_keyring_pass=encrypted_pass,
                is_default=(next_idx == 1),
                priority=next_idx,
            )
            await event.answer(lex.cp_toast_launching_oauth)
            try:
                pending = await self._keyring_mgr.start_remote_oauth_flow(account)
            except Exception as exc:
                max_err = self._cfg.limits.error_summary_max_chars
                err_detail = f"{type(exc).__name__}: {exc}"[:max_err]
                logger.error("OAuth flow start failed", account_slug=account.slug, error=err_detail, exc_info=True)
                await event.respond(lex.cp_oauth_start_failed.format(slug=account.slug, error=err_detail))
                return

            self._state = _ControlPanelInteractiveState(
                mode="await_oauth_code",
                setting_key=None,
                pending_oauth=pending,
            )
            await event.respond(
                lex.cp_oauth_instructions.format(
                    slug=account.slug,
                    email_label=account.email_label,
                    auth_url=pending.auth_url,
                ),
                buttons=build_oauth_url_keyboard(lex, pending.auth_url),
            )
            return

        if data_str.startswith("cp:oauth_start:"):
            acc_id = int(data_str.split(":")[2])
            account = await self._account_repo.get_by_id(acc_id)
            await event.answer(lex.cp_toast_launching_oauth)
            try:
                pending = await self._keyring_mgr.start_remote_oauth_flow(account)
            except Exception as exc:
                max_err = self._cfg.limits.error_summary_max_chars
                err_detail = f"{type(exc).__name__}: {exc}"[:max_err]
                logger.error(
                    "OAuth flow start failed",
                    account_slug=account.slug,
                    error=err_detail,
                    exc_info=True,
                )
                await event.respond(
                    lex.cp_oauth_start_failed.format(slug=account.slug, error=err_detail)
                )
                return

            self._state = _ControlPanelInteractiveState(
                mode="await_oauth_code",
                setting_key=None,
                pending_oauth=pending,
            )
            await event.respond(
                lex.cp_oauth_instructions.format(
                    slug=account.slug,
                    email_label=account.email_label,
                    auth_url=pending.auth_url,
                ),
                buttons=build_oauth_url_keyboard(lex, pending.auth_url),
            )
            return

        if data_str == "cp:switch_default_acc":
            accounts = await self._account_repo.list_accounts()
            await self._safe_edit_event(
                event,
                format_wide_header_block(lex.cp_switch_default_acc_prompt),
                build_default_account_picker_keyboard(lex, accounts),
            )
            return

        if data_str.startswith("cp:set_def_acc:"):
            acc_id = int(data_str.split(":")[2])
            await self._account_repo.set_default_account(acc_id)
            lex, text = await self._build_dashboard_text()
            await self._safe_edit_event(event, text, build_control_panel_keyboard(lex))
            await event.answer(lex.cp_toast_default_acc_updated)
            return

        if data_str == "cp:remount_hosts":
            await event.answer(lex.cp_toast_remounting_hosts)
            hosts = await self._host_repo.list_hosts()
            for host in hosts:
                if host.auto_mount_enabled:
                    try:
                        await self._mount_mgr.ensure_mounted(host)
                    except Exception as exc:
                        max_err = self._cfg.limits.error_summary_max_chars
                        err_short = str(exc)[:max_err]
                        await event.respond(
                            lex.cp_remount_failed.format(alias=host.alias, error=err_short)
                        )
            lex, text = await self._build_dashboard_text()
            await self._safe_edit_event(event, text, build_control_panel_keyboard(lex))
            return

        if data_str == "cp:new_topic":
            await self._create_topic_immediately(event, lex)
            return

        if data_str == "cp:tasks_menu":
            tasks = await self._scheduler.list_tasks(topic_id=None)
            t_lines = lex.cp_tasks_menu_prompt.format(count=len(tasks)).splitlines()
            wide_tasks = "\n".join([format_wide_header_block(t_lines[0]), *t_lines[1:]])
            await self._safe_edit_event(
                event,
                wide_tasks,
                build_tasks_management_keyboard(lex, self._cfg.limits, tasks),
            )
            return

        if data_str.startswith("cp:cancel_task:"):
            task_uuid = UUID(data_str.split(":", 2)[2])
            await self._scheduler.cancel_task(task_uuid)
            tasks = await self._scheduler.list_tasks(topic_id=None)
            await self._safe_edit_event(
                event,
                lex.cp_tasks_menu_updated.format(count=len(tasks)),
                build_tasks_management_keyboard(lex, self._cfg.limits, tasks),
            )
            await event.answer(lex.cp_toast_task_canceled)
            return

        if data_str == "cp:settings_menu":
            await self._render_settings_menu(event, lex)
            return

        if data_str.startswith("cp:edit_set:"):
            key = data_str.split(":", 2)[2]
            await self._open_setting_selector(event, lex, key)
            return

        if data_str.startswith("cp:set_val:"):
            _, _, key, raw_val = data_str.split(":", 3)
            updated_settings = await self._settings_repo.update_setting(key, raw_val)
            new_lex = get_lexicon(updated_settings.ui_language)
            await event.answer(
                new_lex.cp_setting_updated.format(key=key, value_json=raw_val)
            )
            await self._render_settings_menu(event, new_lex)
            return

        if data_str == "cp:create_invite":
            token = f"inv_{secrets.token_urlsafe(self._cfg.runtime.invite_token_bytes)}"
            expires_at = datetime.now(tz=UTC) + timedelta(
                hours=self._cfg.runtime.invite_ttl_hours
            )
            await self._access_repo.create_invite(
                token=token,
                role=UserRole.MEMBER,
                allowed_topic_ids=(),
                created_by_user_id=self._cfg.telegram.owner_telegram_id,
                expires_at=expires_at,
            )
            await event.respond(lex.cp_invite_generated_msg.format(token=token))
            await event.answer(lex.cp_toast_invite_generated)

    async def _process_new_account_input(self, raw_text: str) -> None:
        lex = await self._load_lexicon()
        parts = raw_text.split(maxsplit=1)
        if len(parts) < 2:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=lex.cp_new_acc_invalid_format,
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
            return

        slug = parts[0].strip()
        email_label = parts[1].strip()
        existing = await self._account_repo.list_accounts()
        is_first = len(existing) == 0
        priority = len(existing) + 1

        xdg_dir = str((self._cfg.paths.profiles_data_dir / slug).resolve())
        dbus_sock = str((self._cfg.paths.runtime_dir / f"bus_{slug}.sock").resolve())
        keyring_password = secrets.token_urlsafe(self._cfg.runtime.keyring_password_bytes)
        encrypted_pass = self._vault.encrypt_text(keyring_password)

        account = await self._account_repo.create_account(
            slug=slug,
            email_label=email_label,
            xdg_data_dir=xdg_dir,
            dbus_socket_path=dbus_sock,
            encrypted_keyring_pass=encrypted_pass,
            is_default=is_first,
            priority=priority,
        )
        await self._keyring_mgr.ensure_account_bus(account)
        await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=lex.cp_new_acc_created.format(slug=account.slug),
            reply_to=self._cfg.telegram.control_panel_topic_id,
        )
        await self.render_and_pin_dashboard(recreate_at_bottom=True)

    async def _process_oauth_code_input(
        self, pending: PendingOAuthSession, verification_code: str
    ) -> None:
        lex = await self._load_lexicon()
        try:
            await self._keyring_mgr.complete_remote_oauth_flow(pending, verification_code)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=lex.cp_oauth_success.format(slug=pending.account.slug),
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
        except Exception as exc:
            max_err = self._cfg.limits.error_summary_max_chars
            err_detail = f"{type(exc).__name__}: {exc}"[:max_err]
            logger.error(
                "OAuth verification failed",
                account_slug=pending.account.slug,
                error=err_detail,
                exc_info=True,
            )
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=lex.cp_oauth_failed.format(error=err_detail),
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
        await self.render_and_pin_dashboard(recreate_at_bottom=True)

    async def _create_topic_immediately(
        self, event: events.CallbackQuery.Event, lex: UiLexicon
    ) -> None:
        try:
            temp_title = f"💬 {lex.cp_new_topic_default_title}"
            placeholder_dir = str(self._cfg.paths.workspaces_root_dir / "pending_topic")
            topic = await self._topic_lifecycle.create_forum_topic_with_card(
                title=temp_title,
                kind=TopicKind.PERMANENT_WORKSPACE,
                workspace_path=placeholder_dir,
                execution_mode=None,
            )
            # Assign clean topic_<id> path without creating the directory on disk yet
            assigned_dir = str(
                (self._cfg.paths.workspaces_root_dir / f"topic_{topic.topic_id}").resolve()
            )
            final_title = lex.ws_auto_topic_title.format(topic_id=topic.topic_id)
            await self._topic_lifecycle.rename_topic(topic.topic_id, final_title)
            await self._workspace_ui_init_new_topic(topic.topic_id, assigned_dir)
            await event.answer(
                lex.cp_new_topic_created.format(title=final_title, topic_id=topic.topic_id)
            )
        except Exception as exc:
            max_err = self._cfg.limits.error_summary_max_chars
            err_detail = f"{type(exc).__name__}: {exc}"[:max_err]
            logger.error("Failed to create forum topic", error=err_detail, exc_info=True)
            await event.respond(lex.cp_new_topic_failed.format(error=err_detail))

    async def _workspace_ui_init_new_topic(
        self, topic_id: int, assigned_dir: str
    ) -> None:
        await self._workspace_ui.initialize_created_topic(topic_id, assigned_dir)

    async def _render_settings_menu(
        self, event: events.CallbackQuery.Event, lex: UiLexicon
    ) -> None:
        settings = await self._settings_repo.load_settings()
        is_ru = settings.ui_language == "ru"
        lang_display = "🇷🇺 Русский" if is_ru else "🇬🇧 English"
        mode_display = (
            ("⚡ Полный доступ (Запись)" if is_ru else "⚡ Full Write")
            if settings.default_execution_mode == ExecutionMode.FULL_ACCESS
            else ("🛡 Только чтение (Plan)" if is_ru else "🛡 Read-Only (Plan)")
        )

        items = [
            ("🌐 Язык интерфейса" if is_ru else "🌐 UI Language", lang_display),
            ("🧠 Модель по умолчанию" if is_ru else "🧠 Default Model", settings.default_model),
            ("🛡 Режим прав по умолчанию" if is_ru else "🛡 Default Permissions", mode_display),
        ]
        lines = [format_wide_header_block(lex.cp_settings_menu_header.strip())]
        for label, val in items:
            lines.append(lex.cp_settings_menu_item.format(key="", value_json=val, description=label))

        btn_labels = [
            ("🌐 Язык (Language)", "ui_language"),
            ("🧠 Модель (Model)", "default_model"),
            ("🛡 Режим прав (Permissions)", "default_execution_mode"),
        ]
        from telethon.tl.custom import Button

        rows: list[list[Button]] = [
            [Button.inline(lbl, f"cp:edit_set:{k}".encode("ascii"))]
            for lbl, k in btn_labels
        ]
        rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")])

        await self._safe_edit_event(event, "\n".join(lines), rows)

    async def _open_setting_selector(
        self, event: events.CallbackQuery.Event, lex: UiLexicon, key: str
    ) -> None:
        settings = await self._settings_repo.load_settings()

        if key == "ui_language":
            lang_labels = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
            options = [(lang_labels.get(code, code), code) for code in SUPPORTED_LANGUAGES]
            await self._safe_edit_event(
                event,
                format_wide_header_block("🌐 Выберите язык интерфейса / Select UI Language"),
                build_setting_options_keyboard(lex, key, options, settings.ui_language),
            )
            return

        if key == "default_model":
            try:
                default_acc = await self._account_repo.get_default_account()
                models = await self._inspector.list_available_models(default_acc)
            except Exception as exc:
                max_err = self._cfg.limits.error_summary_max_chars
                err_short = str(exc)[:max_err]
                await event.answer(err_short, alert=True)
                return
            options = [(m, m) for m in models[: self._cfg.limits.models_menu_display_limit]]
            await self._safe_edit_event(
                event,
                format_wide_header_block(lex.ws_select_model_prompt),
                build_setting_options_keyboard(lex, key, options, settings.default_model),
            )
            return

        if key == "default_execution_mode":
            is_ru = settings.ui_language == "ru"
            options = [
                (
                    "⚡ Полный доступ (Чтение и Запись)" if is_ru else "⚡ Full Access (Read & Write)",
                    ExecutionMode.FULL_ACCESS.value,
                ),
                (
                    "🛡 Только чтение (Режим Plan)" if is_ru else "🛡 Read-Only (Plan Mode)",
                    ExecutionMode.READ_ONLY.value,
                ),
            ]
            await self._safe_edit_event(
                event,
                format_wide_header_block("🛡 Режим прав по умолчанию / Default Permissions"),
                build_setting_options_keyboard(
                    lex, key, options, settings.default_execution_mode.value
                ),
            )
            return
