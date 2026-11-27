import asyncio
import contextlib
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError, RPCError
from telethon.tl.custom import Button

from antigravity_core.application.scheduler_service import TemporalSchedulerService
from antigravity_core.application.sensor_service import EventSensorService
from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import ExecutionMode, TopicKind, UserRole
from antigravity_core.domain.models import GoogleAccount, RemoteHost
from antigravity_core.infrastructure.agy_runtime.cli_inspector import AgyCliInspector
from antigravity_core.infrastructure.agy_runtime.environment_bootstrap import (
    AgyEnvironmentBootstrapper,
)
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.crypto.fernet_vault import FernetVault
from antigravity_core.infrastructure.database.repositories import (
    AccessControlRepository,
    AccountRepository,
    RemoteHostRepository,
    SettingsRepository,
    SshCredentialRepository,
)
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import (
    KeyringProfileManager,
    PendingOAuthSession,
)
from antigravity_core.infrastructure.remotes.sshfs_mount_mgr import RemoteMountManager
from antigravity_core.lexicon import SUPPORTED_LANGUAGES, UiLexicon, get_lexicon
from antigravity_core.presentation.formatters import (
    RICH_SPACER_MENU,
    format_wide_header_block,
    render_control_panel_dashboard,
)

_HOSTS_PAGE_SIZE: Final[int] = 6
_HOST_BTN_COLS: Final[int] = 2
_DEFAULT_SSH_PORT: Final[int] = 22
_NEW_TOPIC_DEFAULT_ICON_EMOJI: Final[str] = "✨"
from antigravity_core.presentation.tg_renderer import (
    edit_rich_or_standard_message,
    send_rich_or_standard_message,
)
from antigravity_core.presentation.keyboards import (
    build_control_panel_keyboard,
    build_default_account_picker_keyboard,
    build_default_model_groups_keyboard,
    build_default_model_variants_keyboard,
    build_oauth_url_keyboard,
    build_paginated_accounts_keyboard,
    build_setting_options_keyboard,
    build_single_account_manage_keyboard,
    build_tasks_management_keyboard,
    group_models_by_family,
)
from antigravity_core.presentation.workspace_topic_ui import WorkspaceTopicUI


@dataclass(frozen=True, slots=True)
class _ControlPanelInteractiveState:
    mode: str
    setting_key: str | None
    pending_oauth: PendingOAuthSession | None
    is_new_staged_account: bool


class ControlPanelUI:
    """Handles all interactions, wizards, and dashboard updates inside #Control-Panel (topic_id = 1)."""

    def __init__(
        self,
        client: TelegramClient,
        static_config: StaticConfig,
        bootstrapper: AgyEnvironmentBootstrapper,
        vault: FernetVault,
        keyring_mgr: KeyringProfileManager,
        mount_mgr: RemoteMountManager,
        worker_pool: AgyWorkerPool,
        cli_inspector: AgyCliInspector,
        scheduler_service: TemporalSchedulerService,
        sensor_service: EventSensorService,
        topic_lifecycle: TopicLifecycleService,
        workspace_ui: WorkspaceTopicUI,
        settings_repo: SettingsRepository,
        account_repo: AccountRepository,
        host_repo: RemoteHostRepository,
        cred_repo: SshCredentialRepository,
        access_repo: AccessControlRepository,
    ) -> None:
        self._client = client
        self._cfg = static_config
        self._bootstrapper = bootstrapper
        self._vault = vault
        self._keyring_mgr = keyring_mgr
        self._mount_mgr = mount_mgr
        self._pool = worker_pool
        self._inspector = cli_inspector
        self._scheduler = scheduler_service
        self._sensors = sensor_service
        self._topic_lifecycle = topic_lifecycle
        self._workspace_ui = workspace_ui
        self._settings_repo = settings_repo
        self._account_repo = account_repo
        self._host_repo = host_repo
        self._cred_repo = cred_repo
        self._access_repo = access_repo
        self._state: _ControlPanelInteractiveState | None = None
        self._last_cp_msg_id: int = 0
        self._pending_host_onboarding: dict[str, asyncio.Future[None]] = {}

    async def _load_lexicon(self) -> UiLexicon:
        settings = await self._settings_repo.load_settings()
        return get_lexicon(settings.ui_language)

    async def _safe_edit_event(
        self,
        event: events.CallbackQuery.Event,
        text: str,
        buttons: list[list[Button]] | None,
    ) -> None:
        with contextlib.suppress(MessageNotModifiedError):
            await edit_rich_or_standard_message(
                client=self._client,
                chat_id=self._cfg.telegram.forum_chat_id,
                message_id=event.message_id,
                markdown_text=text,
                fallback_text=text,
                fallback_entities=None,
                buttons=buttons,
            )
        await event.answer()

    async def render_and_pin_dashboard(self, recreate_at_bottom: bool) -> None:
        lex, text = await self._build_dashboard_text()
        buttons = build_control_panel_keyboard(lex)
        cp_topic_id = self._cfg.telegram.control_panel_topic_id
        stored_msg_id = await self._settings_repo.get_dashboard_msg_id()

        if stored_msg_id is not None and not recreate_at_bottom:
            with contextlib.suppress(MessageNotModifiedError):
                await edit_rich_or_standard_message(
                    client=self._client,
                    chat_id=self._cfg.telegram.forum_chat_id,
                    message_id=stored_msg_id,
                    markdown_text=text,
                    fallback_text=text,
                    fallback_entities=None,
                    buttons=buttons,
                )
            return

        if stored_msg_id is not None:
            with contextlib.suppress(RPCError):
                await self._client.delete_messages(
                    entity=self._cfg.telegram.forum_chat_id,
                    message_ids=[stored_msg_id],
                )

        msg = await send_rich_or_standard_message(
            client=self._client,
            chat_id=self._cfg.telegram.forum_chat_id,
            markdown_text=text,
            fallback_text=text,
            fallback_entities=None,
            reply_to=cp_topic_id,
            top_msg_id=cp_topic_id,
            silent=True,
            buttons=buttons,
        )
        self._last_cp_msg_id = max(self._last_cp_msg_id, msg.id)
        await self._settings_repo.set_dashboard_msg_id(msg.id)
        with contextlib.suppress(RPCError):
            await self._client.pin_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=msg.id,
                notify=False,
            )

    async def _build_dashboard_text(self) -> tuple[UiLexicon, str]:
        accounts = await self._account_repo.list_accounts()
        hosts = await self._host_repo.list_hosts()
        creds = await self._cred_repo.list_credentials()
        known_keys = {f"{h.ssh_user}@{h.alias}:{h.ssh_port}" for h in hosts}
        combined_hosts = list(hosts)
        mount_statuses: dict[str, bool] = {}

        for h in hosts:
            k = f"{h.ssh_user}@{h.alias}:{h.ssh_port}"
            matched_cred = next(
                (c for c in creds if c.credential_key == k or c.host_target == h.alias),
                None,
            )
            mount_statuses[h.alias] = (
                matched_cred.key_provisioned if matched_cred is not None else True
            )

        for idx, c in enumerate(creds, start=1000):
            if c.credential_key not in known_keys:
                combined_hosts.append(
                    RemoteHost(
                        id=idx,
                        alias=c.credential_key,
                        hostname_or_ip=c.host_target,
                        ssh_port=c.ssh_port,
                        ssh_user=c.ssh_user,
                        remote_root_path="/",
                        local_mount_path=f"{c.ssh_user}@{c.host_target}:/",
                        rule_file_path="",
                        auto_mount_enabled=False,
                        jump_host=None,
                    )
                )
                mount_statuses[c.credential_key] = c.key_provisioned

        pool_snapshots = await self._pool.get_pool_snapshots()
        tasks = await self._scheduler.list_tasks(topic_id=None)
        sensors = await self._sensors.list_sensors(topic_id=None)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        return lex, render_control_panel_dashboard(
            lex=lex,
            limits=self._cfg.limits,
            accounts=accounts,
            hosts=combined_hosts,
            mount_statuses=mount_statuses,
            pool_snapshots=pool_snapshots,
            tasks=tasks,
            settings=settings,
            sensors=sensors,
        )

    async def _send_fresh_bottom_panel(
        self, text: str, buttons: list[list[Button]]
    ) -> None:
        """Deletes the previous Control-Panel card above and posts a fresh one at the very bottom."""
        cp_topic_id = self._cfg.telegram.control_panel_topic_id
        stored_msg_id = await self._settings_repo.get_dashboard_msg_id()
        if stored_msg_id is not None:
            with contextlib.suppress(RPCError):
                await self._client.delete_messages(
                    entity=self._cfg.telegram.forum_chat_id,
                    message_ids=[stored_msg_id],
                )

        msg = await send_rich_or_standard_message(
            client=self._client,
            chat_id=self._cfg.telegram.forum_chat_id,
            markdown_text=text,
            fallback_text=text,
            fallback_entities=None,
            reply_to=cp_topic_id,
            top_msg_id=cp_topic_id,
            silent=True,
            buttons=buttons,
        )
        self._last_cp_msg_id = max(self._last_cp_msg_id, msg.id)
        await self._settings_repo.set_dashboard_msg_id(msg.id)
        with contextlib.suppress(RPCError):
            await self._client.pin_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=msg.id,
                notify=False,
            )

    async def _update_or_recreate_panel(
        self,
        event: events.CallbackQuery.Event,
        text: str,
        buttons: list[list[Button]],
    ) -> None:
        """Edits the panel in-place if no newer messages were sent after it; otherwise recreates at bottom."""
        if event.message_id >= self._last_cp_msg_id:
            self._last_cp_msg_id = max(self._last_cp_msg_id, event.message_id)
            await self._safe_edit_event(event, text, buttons)
        else:
            await self._send_fresh_bottom_panel(text, buttons)

    async def handle_text_input(self, event: events.NewMessage.Event) -> None:
        self._last_cp_msg_id = max(self._last_cp_msg_id, event.message.id)
        raw_text = (event.message.message or "").strip()

        if raw_text in ("/start", "/panel", "/dashboard") or self._state is None:
            if self._state is not None and self._state.pending_oauth is not None:
                self._keyring_mgr.cancel_pending_oauth(self._state.pending_oauth)
                if self._state.is_new_staged_account:
                    await self._discard_staged_account(
                        self._state.pending_oauth.account
                    )
            self._state = None
            await self.render_and_pin_dashboard(recreate_at_bottom=True)
            return

        state = self._state
        self._state = None

        if state.mode == "await_oauth_code" and state.pending_oauth is not None:
            await self._process_oauth_code_input(
                state.pending_oauth, raw_text, state.is_new_staged_account
            )
            return

        await self.render_and_pin_dashboard(recreate_at_bottom=True)

    async def handle_callback(
        self, event: events.CallbackQuery.Event, data_str: str
    ) -> None:
        lex = await self._load_lexicon()

        if data_str == "cp:refresh":
            self._state = None
            lex, text = await self._build_dashboard_text()
            await self._update_or_recreate_panel(
                event, text, build_control_panel_keyboard(lex)
            )
            return

        if data_str == "cp:probe_quotas":
            await event.answer(lex.cp_toast_probing_quotas)
            accounts = await self._account_repo.list_enabled_accounts()
            for acc in accounts:
                await self._keyring_mgr.backup_account_token_to_db(acc)
                q_info = await self._inspector.fetch_account_quota(acc)
                if "detected_email" in q_info and q_info["detected_email"]:
                    await self._account_repo.update_email_label(
                        acc.id, str(q_info["detected_email"])
                    )
                await self._account_repo.update_quota_snapshot(acc.id, q_info)
            lex, text = await self._build_dashboard_text()
            await self._safe_edit_event(event, text, build_control_panel_keyboard(lex))
            return

        if data_str == "cp:auth_noop":
            await event.answer()
            return

        if data_str == "cp:cancel_oauth":
            if self._state is not None and self._state.pending_oauth is not None:
                self._keyring_mgr.cancel_pending_oauth(self._state.pending_oauth)
                if self._state.is_new_staged_account:
                    await self._discard_staged_account(
                        self._state.pending_oauth.account
                    )
            self._state = None
            await event.answer(lex.cp_toast_oauth_canceled)
            await self._render_accounts_hub(event, lex, 0, False)
            return

        if data_str == "cp:auth_menu":
            await self._render_accounts_hub(event, lex, 0, False)
            return

        if data_str.startswith("cp:auth_page:"):
            page = int(data_str.split(":")[2])
            await self._render_accounts_hub(event, lex, page, False)
            return

        if data_str.startswith("cp:acc_view:"):
            acc_id = int(data_str.split(":")[2])
            await self._render_single_account_menu(event, lex, acc_id)
            return

        if data_str.startswith("cp:del_acc:"):
            acc_id = int(data_str.split(":")[2])
            acc = await self._account_repo.get_by_id(acc_id)
            await event.answer(lex.cp_toast_logging_out_account)
            await self._keyring_mgr.logout_account(acc)
            await self._account_repo.delete_account(acc_id)
            shutil.rmtree(Path(acc.xdg_data_dir), ignore_errors=True)
            await self._render_accounts_hub(event, lex, 0, False)
            return

        if data_str == "cp:add_acc_oauth":
            await self._start_oauth_for_new_staged_account(event, lex)
            return

        if data_str.startswith("cp:oauth_start:"):
            acc_id = int(data_str.split(":")[2])
            account = await self._account_repo.get_by_id(acc_id)
            await self._start_oauth_for_account(event, lex, account, False)
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
            await event.answer(lex.cp_toast_default_acc_updated)
            await self._render_single_account_menu(event, lex, acc_id)
            return

        if data_str == "cp:remount_hosts":
            await self._render_hosts_and_credentials_menu(event, lex, 0)
            return

        if data_str.startswith("cp:hosts_page:"):
            page = int(data_str.split(":", 2)[2])
            await self._render_hosts_and_credentials_menu(event, lex, page)
            return

        if data_str.startswith("cp:host_"):
            await self._handle_host_tools_callback(event, lex, data_str)
            return

        if data_str.startswith("cp:del_cred:"):
            _, _, cred_key = data_str.split(":", 2)
            await self._cred_repo.delete_credential(cred_key)
            await event.answer(lex.cp_toast_cred_reset.format(cred_key=cred_key))
            await self._render_hosts_and_credentials_menu(event, lex, 0)
            return

        if data_str.startswith("cp:del_host:"):
            _, _, alias = data_str.split(":", 2)
            host_entry = await self._host_repo.get_by_alias(alias)
            if host_entry is not None:
                await self._host_repo.delete_host(alias)
                self._mount_mgr.remove_host_config(alias)
                cred_key_alias = (
                    f"{host_entry.ssh_user}@{host_entry.alias}:{host_entry.ssh_port}"
                )
                cred_key_ip = f"{host_entry.ssh_user}@{host_entry.hostname_or_ip}:{host_entry.ssh_port}"
                await self._cred_repo.delete_credential(cred_key_alias)
                await self._cred_repo.delete_credential(cred_key_ip)
                all_hosts = await self._host_repo.list_hosts()
                self._bootstrapper.sync_global_agents_md_host_map(all_hosts)
            await event.answer(lex.cp_toast_host_deleted.format(alias=alias))
            await self._render_hosts_and_credentials_menu(event, lex, 0)
            return

        if data_str == "cp:new_topic":
            await self._create_topic_immediately(event, lex)
            return

        if data_str == "cp:tasks_menu":
            tasks = await self._scheduler.list_tasks(topic_id=None)
            sensors = await self._sensors.list_sensors(topic_id=None)
            t_lines = lex.cp_tasks_menu_prompt.format(
                count=len(tasks) + len(sensors)
            ).splitlines()
            wide_tasks = "\n".join([format_wide_header_block(t_lines[0]), *t_lines[1:]])
            await self._safe_edit_event(
                event,
                wide_tasks,
                build_tasks_management_keyboard(lex, self._cfg.limits, tasks, sensors),
            )
            return

        if data_str.startswith("cp:cancel_task:"):
            task_uuid = UUID(data_str.split(":", 2)[2])
            await self._scheduler.cancel_task(task_uuid)
            tasks = await self._scheduler.list_tasks(topic_id=None)
            sensors = await self._sensors.list_sensors(topic_id=None)
            await self._safe_edit_event(
                event,
                format_wide_header_block(
                    lex.cp_tasks_menu_updated.format(count=len(tasks) + len(sensors))
                ),
                build_tasks_management_keyboard(lex, self._cfg.limits, tasks, sensors),
            )
            await event.answer(lex.cp_toast_task_canceled)
            return

        if data_str.startswith("cp:cancel_sensor:"):
            sensor_uuid = UUID(data_str.split(":", 2)[2])
            await self._sensors.cancel_sensor(sensor_uuid)
            tasks = await self._scheduler.list_tasks(topic_id=None)
            sensors = await self._sensors.list_sensors(topic_id=None)
            await self._safe_edit_event(
                event,
                format_wide_header_block(
                    lex.cp_tasks_menu_updated.format(count=len(tasks) + len(sensors))
                ),
                build_tasks_management_keyboard(lex, self._cfg.limits, tasks, sensors),
            )
            await event.answer(lex.cp_toast_sensor_canceled)
            return

        if data_str == "cp:settings_menu":
            await self._render_settings_menu(event, lex)
            return

        if data_str.startswith("cp:edit_set:"):
            key = data_str.split(":", 2)[2]
            await self._open_setting_selector(event, lex, key)
            return

        if data_str.startswith("cp:def_mgrp:"):
            grp_idx = int(data_str.split(":", 2)[2])
            await self._open_default_model_group(event, lex, grp_idx)
            return

        if data_str.startswith("cp:set_val:"):
            _, _, key, raw_val = data_str.split(":", 3)
            if key == "default_model":
                default_acc = await self._account_repo.get_default_account()
                available_models = await self._inspector.list_available_models(
                    default_acc
                )
                if raw_val not in available_models:
                    await event.answer(
                        lex.ws_model_unknown_msg.format(
                            model_slug=raw_val,
                            available_models=", ".join(available_models[:6]),
                        )[:180],
                        alert=True,
                    )
                    return
            updated_settings = await self._settings_repo.update_setting(key, raw_val)
            new_lex = get_lexicon(updated_settings.ui_language)
            await event.answer(
                new_lex.cp_setting_updated.format(key=key, value_json=raw_val)
            )
            await self._render_settings_menu(event, new_lex)
            return

        if data_str == "cp:show_ssh_key":
            pub_key = await self._mount_mgr.ensure_local_ssh_keypair()
            await self._safe_edit_event(
                event,
                lex.cp_ssh_key_card.format(pub_key=pub_key),
                [[Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")]],
            )
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

    async def _discard_staged_account(self, account: GoogleAccount) -> None:
        await self._keyring_mgr.logout_account(account)
        await self._account_repo.delete_account(account.id)
        shutil.rmtree(Path(account.xdg_data_dir), ignore_errors=True)

    async def _start_oauth_for_new_staged_account(
        self, event: events.CallbackQuery.Event, lex: UiLexicon
    ) -> None:
        existing = await self._account_repo.list_accounts()
        staging_token = f"oauth_{secrets.token_hex(4)}"
        email_label = "Pending Google Sign-In"
        profile_root = (self._cfg.paths.profiles_data_dir / staging_token).resolve()
        xdg_dir = str(profile_root)
        dbus_sock = str((profile_root / "run" / "dbus.sock").resolve())
        keyring_password = secrets.token_urlsafe(
            self._cfg.runtime.keyring_password_bytes
        )
        encrypted_pass = self._vault.encrypt_text(keyring_password)

        staged_acc = await self._account_repo.create_account(
            slug=staging_token,
            email_label=email_label,
            xdg_data_dir=xdg_dir,
            dbus_socket_path=dbus_sock,
            encrypted_keyring_pass=encrypted_pass,
            is_default=(len(existing) == 0),
            priority=len(existing) + 1,
        )
        await self._start_oauth_for_account(event, lex, staged_acc, True)

    async def _start_oauth_for_account(
        self,
        event: events.CallbackQuery.Event,
        lex: UiLexicon,
        account: GoogleAccount,
        is_new_staged: bool,
    ) -> None:
        await event.answer(lex.cp_toast_launching_oauth)
        await self._update_or_recreate_panel(
            event,
            lex.cp_oauth_preparing_card.format(slug=account.display_email),
            [[Button.inline(lex.btn_cp_cancel_short, b"cp:auth_menu")]],
        )
        pending, err_msg = await self._keyring_mgr.try_start_remote_oauth_flow(account)
        if pending is None:
            if is_new_staged:
                await self._discard_staged_account(account)
            max_err = self._cfg.limits.error_summary_max_chars
            err_detail = err_msg[:max_err]
            text, kb = await self._build_accounts_hub_view(lex, 0)
            await self._update_or_recreate_panel(
                event,
                lex.cp_oauth_failed_banner.format(error=err_detail) + text,
                kb,
            )
            return

        self._state = _ControlPanelInteractiveState(
            mode="await_oauth_code",
            setting_key=None,
            pending_oauth=pending,
            is_new_staged_account=is_new_staged,
        )
        await self._update_or_recreate_panel(
            event,
            lex.cp_oauth_instructions.format(
                slug=account.display_email,
                email_label=account.display_email,
                auth_url=pending.auth_url,
            ),
            build_oauth_url_keyboard(lex, pending.auth_url),
        )

    async def _process_oauth_code_input(
        self,
        pending: PendingOAuthSession,
        verification_code: str,
        is_new_staged: bool,
    ) -> None:
        lex = await self._load_lexicon()
        ok, err_msg = await self._keyring_mgr.try_complete_remote_oauth_flow(
            pending, verification_code
        )
        if not ok:
            if is_new_staged:
                await self._discard_staged_account(pending.account)
            max_err = self._cfg.limits.error_summary_max_chars
            err_detail = err_msg[:max_err]
            status_banner = lex.cp_oauth_failed_banner.format(error=err_detail)
            text, kb = await self._build_accounts_hub_view(lex, 0)
            await self._send_fresh_bottom_panel(status_banner + text, kb)
            return

        q_info = await self._inspector.fetch_account_quota(pending.account)
        if "detected_email" in q_info and q_info["detected_email"]:
            await self._account_repo.update_email_label(
                pending.account.id, str(q_info["detected_email"])
            )
        await self._account_repo.update_quota_snapshot(pending.account.id, q_info)
        updated_acc = await self._account_repo.get_by_id(pending.account.id)
        status_banner = lex.cp_oauth_success_banner.format(
            email_label=updated_acc.email_label
        )
        text, kb = await self._build_accounts_hub_view(lex, 0)
        await self._send_fresh_bottom_panel(status_banner + text, kb)

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
                (
                    self._cfg.paths.workspaces_root_dir / f"topic_{topic.topic_id}"
                ).resolve()
            )
            final_title = lex.ws_auto_topic_title.format(topic_id=topic.topic_id)
            await self._topic_lifecycle.rename_topic(
                topic.topic_id, final_title, _NEW_TOPIC_DEFAULT_ICON_EMOJI
            )
            await self._workspace_ui_init_new_topic(topic.topic_id, assigned_dir)
            internal_chat_id = str(abs(self._cfg.telegram.forum_chat_id)).removeprefix(
                "100"
            )
            topic_url = f"https://t.me/c/{internal_chat_id}/{topic.topic_id}"
            cp_topic_id = self._cfg.telegram.control_panel_topic_id
            sent_link = await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=lex.cp_topic_created_link_msg.format(title=final_title),
                reply_to=cp_topic_id,
                buttons=[[Button.url(lex.btn_cp_open_topic, topic_url)]],
            )
            self._last_cp_msg_id = max(self._last_cp_msg_id, sent_link.id)
            await event.answer(
                lex.cp_new_topic_created.format(
                    title=final_title, topic_id=topic.topic_id
                )
            )
        except (RPCError, OSError) as exc:
            max_err = self._cfg.limits.error_summary_max_chars
            err_detail = f"{type(exc).__name__}: {exc}"[:max_err]
            logger.opt(exception=True).error(
                "Failed to create forum topic", error=err_detail
            )
            await event.respond(lex.cp_new_topic_failed.format(error=err_detail))
            raise

    async def _workspace_ui_init_new_topic(
        self, topic_id: int, assigned_dir: str
    ) -> None:
        await self._workspace_ui.initialize_created_topic(topic_id, assigned_dir)

    async def _build_accounts_hub_view(
        self, lex: UiLexicon, page: int
    ) -> tuple[str, list[list[Button]]]:
        accounts = await self._account_repo.list_accounts()
        lines = [
            format_wide_header_block(lex.cp_accounts_hub_title),
            "",
            lex.cp_accounts_hub_subtitle,
        ]
        if not accounts:
            lines.append(lex.cp_accounts_hub_empty_hint)
        lines.append(
            f"{RICH_SPACER_MENU}\n\n---\n*👤 Аккаунтов: {len(accounts)} | Google AI Pro*"
        )
        return "\n".join(lines), build_paginated_accounts_keyboard(lex, accounts, page)

    async def _render_accounts_hub(
        self,
        event: events.CallbackQuery.Event,
        lex: UiLexicon,
        page: int,
        recreate_at_bottom: bool,
    ) -> None:
        text, kb = await self._build_accounts_hub_view(lex, page)
        if recreate_at_bottom:
            await self._send_fresh_bottom_panel(text, kb)
        else:
            await self._update_or_recreate_panel(event, text, kb)

    async def _render_single_account_menu(
        self, event: events.CallbackQuery.Event, lex: UiLexicon, acc_id: int
    ) -> None:
        acc = await self._account_repo.get_by_id(acc_id)
        role_label = (
            lex.cp_acc_role_default if acc.is_default else lex.cp_acc_role_secondary
        )
        status_label = (
            lex.cp_acc_status_authed
            if acc.encrypted_token_backup
            else lex.cp_acc_status_needs_login
        )
        quota_line = (
            lex.cp_acc_quota_line.format(
                min_pct=acc.last_quota_json["min_remaining_pct"]
            )
            if acc.last_quota_json and "min_remaining_pct" in acc.last_quota_json
            else ""
        )
        lines = [
            format_wide_header_block(
                lex.cp_acc_card_header.format(email_label=acc.display_email)
            ),
            "",
            lex.cp_acc_card_profile_line.format(
                slug=acc.display_email, role_label=role_label
            ),
            lex.cp_acc_card_status_line.format(
                status_label=status_label, quota_line=quota_line
            ),
            f"{RICH_SPACER_MENU}\n\n---\n*👤 {acc.display_email} | Google AI Pro*",
        ]

        await self._update_or_recreate_panel(
            event,
            "\n".join(lines),
            build_single_account_manage_keyboard(lex, acc),
        )

    async def _render_hosts_and_credentials_menu(
        self, event: events.CallbackQuery.Event, lex: UiLexicon, page: int
    ) -> None:
        hosts = await self._host_repo.list_hosts()
        creds = await self._cred_repo.list_credentials()
        cred_map = {c.credential_key: c for c in creds}

        matched_cred_keys: set[str] = set()
        for h in hosts:
            k_alias = f"{h.ssh_user}@{h.alias}:{h.ssh_port}"
            k_ip = f"{h.ssh_user}@{h.hostname_or_ip}:{h.ssh_port}"
            if k_alias in cred_map:
                matched_cred_keys.add(k_alias)
            elif k_ip in cred_map:
                matched_cred_keys.add(k_ip)

        orphan_creds = [c for c in creds if c.credential_key not in matched_cred_keys]
        total = len(hosts)
        max_page = max(0, (total - 1) // _HOSTS_PAGE_SIZE) if total > 0 else 0
        curr_page = max(0, min(page, max_page))
        start_idx = curr_page * _HOSTS_PAGE_SIZE
        page_hosts = hosts[start_idx : start_idx + _HOSTS_PAGE_SIZE]

        lines: list[str] = [
            format_wide_header_block(lex.cp_hosts_menu_title),
            "",
            lex.cp_hosts_menu_subtitle,
            "",
        ]
        rows: list[list[Button]] = []

        if not hosts and not orphan_creds:
            lines.append(lex.cp_hosts_menu_empty)
        elif page_hosts:
            lines.append(lex.dashboard_hosts_table_header)
            btn_buf: list[Button] = []
            for h in page_hosts:
                k_alias = f"{h.ssh_user}@{h.alias}:{h.ssh_port}"
                k_ip = f"{h.ssh_user}@{h.hostname_or_ip}:{h.ssh_port}"
                matched_c = (
                    cred_map[k_alias]
                    if k_alias in cred_map
                    else (cred_map[k_ip] if k_ip in cred_map else None)
                )
                key_icon = "🟢" if (matched_c and matched_c.key_provisioned) else "🔑"
                sudo_icon = "🔒" if matched_c is not None else "—"
                port_s = f":{h.ssh_port}" if h.ssh_port != _DEFAULT_SSH_PORT else ""
                lines.append(
                    f"| `{h.alias}` | `{h.ssh_user}@{h.hostname_or_ip}{port_s}` | {key_icon} {sudo_icon} |"
                )
                btn_buf.append(
                    Button.inline(
                        f"{key_icon} {h.alias}",
                        f"cp:host_view:{h.alias}".encode("utf-8"),
                    )
                )
                if len(btn_buf) == _HOST_BTN_COLS:
                    rows.append(btn_buf)
                    btn_buf = []
            if btn_buf:
                rows.append(btn_buf)

        if total > _HOSTS_PAGE_SIZE:
            nav_row: list[Button] = []
            if curr_page > 0:
                nav_row.append(
                    Button.inline(
                        "⬅️", f"cp:hosts_page:{curr_page - 1}".encode("ascii")
                    )
                )
            nav_row.append(
                Button.inline(f"{curr_page + 1}/{max_page + 1}", b"cp:auth_noop")
            )
            if curr_page < max_page:
                nav_row.append(
                    Button.inline(
                        "➡️", f"cp:hosts_page:{curr_page + 1}".encode("ascii")
                    )
                )
            rows.append(nav_row)

        for oc in orphan_creds:
            lines.append(f"\n- ⚠️ `{oc.credential_key}` ({lex.cp_host_sudo_saved_badge})")
            rows.append(
                [
                    Button.inline(
                        f"🗑 {oc.credential_key}",
                        f"cp:del_cred:{oc.credential_key}".encode("utf-8"),
                    )
                ]
            )

        lines.append(
            f"{RICH_SPACER_MENU}\n\n---\n*🖥 Серверов: {total} | 📄 {curr_page + 1}/{max_page + 1}*"
        )
        rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")])
        await self._safe_edit_event(event, "\n".join(lines), rows)

    def _build_host_tools_panel(
        self,
        lex: UiLexicon,
        host: RemoteHost,
        status: dict[str, bool],
        status_line: str,
        onboarding_req_id: str | None,
        cred_key_to_reset: str | None,
    ) -> tuple[str, list[list[Button]]]:
        mark = lambda ok: "✅" if ok else "❌"
        route_note = f" via {host.jump_host}" if host.jump_host else ""
        body = lex.cp_host_tools_body.format(
            target=f"{host.ssh_user}@{host.hostname_or_ip}:{host.ssh_port}{route_note}",
            rtk=mark(status["rtk"]),
            rg=mark(status["rg"]),
            fd=mark(status["fd"]),
            tree=mark(status["tree"]),
            crg=mark(status["code-review-graph"]),
            status_line=status_line,
        )
        text = (
            format_wide_header_block(lex.cp_host_tools_title.format(alias=host.alias))
            + "\n\n"
            + body
            + f"\n{RICH_SPACER_MENU}\n\n---\n*📄 hosts/infra-{host.alias}.md*"
        )
        prefix = (
            f"cp:host_inst_ob:{onboarding_req_id}:{host.alias}"
            if onboarding_req_id is not None
            else f"cp:host_inst:{host.alias}"
        )
        rows: list[list[Button]] = [
            [Button.inline(lex.btn_cp_host_inst_all, f"{prefix}:all".encode("utf-8"))],
            [
                Button.inline(
                    lex.btn_cp_host_inst_rtk, f"{prefix}:rtk".encode("utf-8")
                ),
                Button.inline(
                    lex.btn_cp_host_inst_crg, f"{prefix}:crg".encode("utf-8")
                ),
            ],
            [
                Button.inline(
                    lex.btn_cp_host_inst_nav, f"{prefix}:cli_nav".encode("utf-8")
                )
            ],
        ]
        if onboarding_req_id is not None:
            rows.append(
                [
                    Button.inline(
                        lex.btn_cp_host_done_onboarding,
                        f"cp:host_done:{onboarding_req_id}:{host.alias}".encode("utf-8"),
                    )
                ]
            )
        else:
            mgmt_row: list[Button] = [
                Button.inline(
                    lex.btn_cp_host_sync_passport,
                    f"cp:host_sync:{host.alias}".encode("utf-8"),
                )
            ]
            if cred_key_to_reset is not None:
                mgmt_row.append(
                    Button.inline(
                        lex.btn_cp_reset_host_pass.format(label=host.alias),
                        f"cp:del_cred:{cred_key_to_reset}".encode("utf-8"),
                    )
                )
            rows.append(mgmt_row)
            rows.append(
                [
                    Button.inline(
                        lex.btn_cp_delete_host.format(alias=host.alias),
                        f"cp:del_host:{host.alias}".encode("utf-8"),
                    ),
                    Button.inline(lex.btn_cp_host_back_hosts, b"cp:remount_hosts"),
                ]
            )
        return text, rows

    async def _find_host_cred_key(self, host: RemoteHost) -> str | None:
        key_alias = f"{host.ssh_user}@{host.alias}:{host.ssh_port}"
        if await self._cred_repo.get_credential(key_alias) is not None:
            return key_alias
        key_ip = f"{host.ssh_user}@{host.hostname_or_ip}:{host.ssh_port}"
        if await self._cred_repo.get_credential(key_ip) is not None:
            return key_ip
        return None

    async def run_host_tools_onboarding_prompt(
        self, topic_id: int, host: RemoteHost
    ) -> None:
        """Displays the interactive host software installer panel during register_remote_host onboarding."""
        lex = await self._load_lexicon()
        status = await self._mount_mgr.probe_remote_tools(host)
        req_id = secrets.token_hex(6)
        text, buttons = self._build_host_tools_panel(
            lex, host, status, "", req_id, None
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._pending_host_onboarding[req_id] = fut
        await send_rich_or_standard_message(
            client=self._client,
            chat_id=self._cfg.telegram.forum_chat_id,
            markdown_text=text,
            fallback_text=text,
            fallback_entities=None,
            reply_to=topic_id,
            top_msg_id=topic_id,
            silent=False,
            buttons=buttons,
        )
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    fut,
                    timeout=self._cfg.remotes.interactive_approval_timeout_sec,
                )
        finally:
            self._pending_host_onboarding.pop(req_id, None)

    async def _handle_host_tools_callback(
        self,
        event: events.CallbackQuery.Event,
        lex: UiLexicon,
        data_str: str,
    ) -> None:
        if data_str.startswith("cp:host_done:"):
            _, _, req_id, alias = data_str.split(":", 3)
            fut = self._pending_host_onboarding.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_result(None)
            await self._safe_edit_event(
                event,
                lex.cp_host_tools_onboarding_done.format(alias=alias),
                None,
            )
            return

        if data_str.startswith(("cp:host_view:", "cp:host_tools:")):
            alias = data_str.split(":", 2)[2]
            host = await self._host_repo.get_by_alias(alias)
            if host is None:
                await event.answer()
                return
            await event.answer(lex.cp_toast_host_probing)
            status = await self._mount_mgr.probe_remote_tools(host)
            cred_key = await self._find_host_cred_key(host)
            text, buttons = self._build_host_tools_panel(
                lex, host, status, "", None, cred_key
            )
            await self._safe_edit_event(event, text, buttons)
            return

        if data_str.startswith("cp:host_sync:"):
            alias = data_str.split(":", 2)[2]
            host = await self._host_repo.get_by_alias(alias)
            if host is None:
                await event.answer()
                return
            await event.answer(lex.cp_toast_host_probing)
            await self._mount_mgr.sync_host_telemetry_and_rules(host)
            status = await self._mount_mgr.probe_remote_tools(host)
            cred_key = await self._find_host_cred_key(host)
            banner = lex.cp_host_tools_install_ok.format(
                detail=lex.cp_toast_host_sync_done
            )
            text, buttons = self._build_host_tools_panel(
                lex, host, status, banner, None, cred_key
            )
            await self._safe_edit_event(event, text, buttons)
            return

        if data_str.startswith("cp:host_inst_ob:"):
            _, _, req_id, alias, tool_key = data_str.split(":", 4)
            await self._execute_host_tool_install(event, lex, alias, tool_key, req_id)
            return

        if data_str.startswith("cp:host_inst:"):
            _, _, alias, tool_key = data_str.split(":", 3)
            await self._execute_host_tool_install(event, lex, alias, tool_key, None)
            return

    async def _execute_host_tool_install(
        self,
        event: events.CallbackQuery.Event,
        lex: UiLexicon,
        alias: str,
        tool_key: str,
        onboarding_req_id: str | None,
    ) -> None:
        host = await self._host_repo.get_by_alias(alias)
        if host is None:
            await event.answer()
            return
        await event.answer(lex.cp_toast_host_installing)
        ok, detail = await self._mount_mgr.install_remote_tool(host, tool_key)
        status = await self._mount_mgr.probe_remote_tools(host)
        banner = (
            lex.cp_host_tools_install_ok.format(detail=detail)
            if ok
            else lex.cp_host_tools_install_fail.format(detail=detail)
        )
        cred_key = (
            await self._find_host_cred_key(host) if onboarding_req_id is None else None
        )
        text, buttons = self._build_host_tools_panel(
            lex, host, status, banner, onboarding_req_id, cred_key
        )
        await self._safe_edit_event(event, text, buttons)

    async def _render_settings_menu(
        self, event: events.CallbackQuery.Event, lex: UiLexicon
    ) -> None:
        from telethon.tl.custom import Button

        settings = await self._settings_repo.load_settings()
        mode_display = (
            lex.mode_option_full_access
            if settings.default_execution_mode == ExecutionMode.FULL_ACCESS
            else lex.mode_option_read_only
        )

        items = [
            (lex.setting_label_ui_language, lex.language_self_name),
            (lex.setting_label_default_model, settings.default_model),
            (lex.setting_label_default_mode, mode_display),
        ]
        lines = [
            format_wide_header_block(lex.cp_settings_menu_header.strip()),
            "",
        ]
        for label, val in items:
            lines.append(
                lex.cp_settings_menu_item.format(
                    key="", value_json=val, description=label
                )
            )
        lines.append(
            f"{RICH_SPACER_MENU}\n\n---\n"
            f"*🌐 {settings.ui_language} | 🧠 {settings.default_model} | "
            f"🛡 {settings.default_execution_mode.value}*"
        )
        btn_labels = [
            (lex.btn_cp_set_language, "ui_language"),
            (lex.btn_cp_set_model, "default_model"),
            (lex.btn_cp_set_mode, "default_execution_mode"),
        ]
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
            options = [
                (get_lexicon(code).language_self_name, code)
                for code in SUPPORTED_LANGUAGES
            ]
            await self._safe_edit_event(
                event,
                format_wide_header_block(lex.cp_select_language_prompt),
                build_setting_options_keyboard(lex, key, options, settings.ui_language),
            )
            return

        if key == "default_model":
            default_acc = await self._account_repo.get_default_account()
            models = await self._inspector.list_available_models(default_acc)
            await self._safe_edit_event(
                event,
                format_wide_header_block(
                    lex.ws_select_model_group_prompt.format(
                        current_model=settings.default_model
                    )
                ),
                build_default_model_groups_keyboard(
                    lex, self._cfg.limits, models, settings.default_model
                ),
            )
            return

        if key == "default_execution_mode":
            options = [
                (lex.mode_option_full_access, ExecutionMode.FULL_ACCESS.value),
                (lex.mode_option_read_only, ExecutionMode.READ_ONLY.value),
            ]
            await self._safe_edit_event(
                event,
                format_wide_header_block(lex.cp_select_mode_prompt),
                build_setting_options_keyboard(
                    lex, key, options, settings.default_execution_mode.value
                ),
            )
            return

    async def _open_default_model_group(
        self, event: events.CallbackQuery.Event, lex: UiLexicon, grp_idx: int
    ) -> None:
        settings = await self._settings_repo.load_settings()
        default_acc = await self._account_repo.get_default_account()
        models = await self._inspector.list_available_models(default_acc)
        groups = group_models_by_family(
            models[: self._cfg.limits.models_menu_display_limit]
        )
        if 0 <= grp_idx < len(groups):
            family_label, variants = groups[grp_idx]
            await self._safe_edit_event(
                event,
                format_wide_header_block(
                    lex.ws_select_model_variant_prompt.format(family=family_label)
                ),
                build_default_model_variants_keyboard(
                    lex, variants, settings.default_model
                ),
            )
        await event.answer()
