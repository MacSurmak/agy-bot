import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from telethon import TelegramClient, events

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
from antigravity_core.presentation.formatters import render_control_panel_dashboard
from antigravity_core.presentation.keyboards import (
    build_account_auth_keyboard,
    build_control_panel_keyboard,
    build_default_account_picker_keyboard,
    build_settings_menu_keyboard,
    build_tasks_management_keyboard,
)
from antigravity_core.presentation.workspace_topic_ui import WorkspaceTopicUI

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _ControlPanelInteractiveState:
    mode: str
    setting_key: str | None = None
    pending_oauth: PendingOAuthSession | None = None


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
        self._dashboard_msg_id: int | None = None
        self._state: _ControlPanelInteractiveState | None = None

    async def render_and_pin_dashboard(self) -> None:
        text = await self._build_dashboard_text()
        buttons = build_control_panel_keyboard()
        cp_topic_id = self._cfg.telegram.control_panel_topic_id

        if self._dashboard_msg_id is not None:
            try:
                await self._client.edit_message(
                    entity=self._cfg.telegram.forum_chat_id,
                    message=self._dashboard_msg_id,
                    text=text,
                    buttons=buttons,
                )
                return
            except Exception:
                self._dashboard_msg_id = None

        msg = await self._client.send_message(
            entity=self._cfg.telegram.forum_chat_id,
            message=text,
            reply_to=cp_topic_id,
            buttons=buttons,
        )
        self._dashboard_msg_id = msg.id
        try:
            await self._client.pin_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=msg.id,
                notify=False,
            )
        except Exception as exc:
            logger.warning("Could not pin Control-Panel dashboard message", error=str(exc))

    async def _build_dashboard_text(self) -> str:
        accounts = await self._account_repo.list_accounts()
        hosts = await self._host_repo.list_hosts()
        mount_statuses = {
            h.alias: self._mount_mgr.is_mounted(Path(h.local_mount_path)) for h in hosts
        }
        pool_snapshots = await self._pool.get_pool_snapshots()
        tasks = await self._scheduler.list_tasks(topic_id=None)
        settings = await self._settings_repo.load_settings()

        return render_control_panel_dashboard(
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
            await self.render_and_pin_dashboard()
            return

        if self._state is None:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=(
                    "ℹ️ **#Control-Panel** is reserved for system administration.\n"
                    "Use the dashboard buttons above or send `/panel` to refresh."
                ),
                reply_to=cp_topic_id,
            )
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

        if state.mode == "await_new_topic":
            self._state = None
            await self._process_new_topic_input(raw_text)
            return

        if state.mode == "await_setting_value" and state.setting_key is not None:
            key = state.setting_key
            self._state = None
            await self._process_setting_update_input(key, raw_text)
            return

    async def handle_callback(self, event: events.CallbackQuery.Event, data_str: str) -> None:
        if data_str == "cp:refresh":
            self._state = None
            text = await self._build_dashboard_text()
            await event.edit(text, buttons=build_control_panel_keyboard())
            return

        if data_str == "cp:probe_quotas":
            await event.answer("Probing quotas across all accounts...")
            accounts = await self._account_repo.list_enabled_accounts()
            for acc in accounts:
                try:
                    q_info = await self._inspector.fetch_account_quota(acc)
                    await self._account_repo.update_quota_snapshot(acc.id, q_info)
                except Exception as exc:
                    logger.warning("Quota probe failed", account=acc.slug, error=str(exc))
            text = await self._build_dashboard_text()
            await event.edit(text, buttons=build_control_panel_keyboard())
            return

        if data_str == "cp:auth_menu":
            accounts = await self._account_repo.list_accounts()
            await event.edit(
                "🔑 **Google AI Pro Account Management**\n"
                "Select an existing account to re-authenticate via Remote SSH OAuth flow, "
                "or register a new isolated D-Bus/Keyring profile:",
                buttons=build_account_auth_keyboard(accounts),
            )
            return

        if data_str == "cp:register_new_acc":
            self._state = _ControlPanelInteractiveState(mode="await_new_account")
            await event.respond(
                "➕ **Register New Google Account Profile**\n"
                "Reply in this topic with `<slug> <email_label>` (for example: `acc_2 backup@gmail.com`)."
            )
            await event.answer()
            return

        if data_str.startswith("cp:oauth_start:"):
            acc_id = int(data_str.split(":")[2])
            account = await self._account_repo.get_by_id(acc_id)
            await event.answer("Launching agy login (Remote SSH OAuth Flow)...")
            try:
                pending = await self._keyring_mgr.start_remote_oauth_flow(account)
            except Exception as exc:
                await event.respond(f"❌ Failed to start OAuth flow for `{account.slug}`:\n`{exc}`")
                return

            self._state = _ControlPanelInteractiveState(
                mode="await_oauth_code",
                pending_oauth=pending,
            )
            await event.respond(
                f"🔐 **OAuth Login for `{account.slug}` ({account.email_label})**\n\n"
                f"1. Open this URL in your browser:\n{pending.auth_url}\n\n"
                f"2. Sign into `{account.email_label}`, copy the single-use verification code, "
                f"and **send it as your next message in this topic**."
            )
            return

        if data_str == "cp:switch_default_acc":
            accounts = await self._account_repo.list_accounts()
            await event.edit(
                "🔀 **Select Default Google Account for New Topics:**",
                buttons=build_default_account_picker_keyboard(accounts),
            )
            return

        if data_str.startswith("cp:set_def_acc:"):
            acc_id = int(data_str.split(":")[2])
            await self._account_repo.set_default_account(acc_id)
            text = await self._build_dashboard_text()
            await event.edit(text, buttons=build_control_panel_keyboard())
            await event.answer("Default account updated.")
            return

        if data_str == "cp:remount_hosts":
            await event.answer("Verifying and mounting SSHFS hosts...")
            hosts = await self._host_repo.list_hosts()
            for host in hosts:
                if host.auto_mount_enabled:
                    try:
                        await self._mount_mgr.ensure_mounted(host)
                    except Exception as exc:
                        await event.respond(f"❌ SSHFS mount failed for `{host.alias}`: `{exc}`")
            text = await self._build_dashboard_text()
            await event.edit(text, buttons=build_control_panel_keyboard())
            return

        if data_str == "cp:new_topic":
            self._state = _ControlPanelInteractiveState(mode="await_new_topic")
            await event.respond(
                "➕ **Create New Workspace Topic**\n"
                "Reply with `<Topic Title> | <Workspace Path> | <PERMANENT or TASK>`\n"
                f"Example:\n`🚀 Project Alpha | {self._cfg.paths.workspaces_root_dir}/alpha | TASK`"
            )
            await event.answer()
            return

        if data_str == "cp:tasks_menu":
            tasks = await self._scheduler.list_tasks(topic_id=None)
            await event.edit(
                f"⏰ **Active Scheduled Tasks ({len(tasks)}):**\nClick a task below to cancel it:",
                buttons=build_tasks_management_keyboard(tasks),
            )
            return

        if data_str.startswith("cp:cancel_task:"):
            task_uuid = UUID(data_str.split(":", 2)[2])
            await self._scheduler.cancel_task(task_uuid)
            tasks = await self._scheduler.list_tasks(topic_id=None)
            await event.edit(
                f"⏰ **Active Scheduled Tasks ({len(tasks)}):**",
                buttons=build_tasks_management_keyboard(tasks),
            )
            await event.answer("Task canceled.")
            return

        if data_str == "cp:settings_menu":
            rows = await self._settings_repo.list_all_with_descriptions()
            lines = ["⚙️ **PostgreSQL Dynamic Settings:**\n"]
            keys: list[str] = []
            for k, val, desc, _ in rows:
                keys.append(k)
                lines.append(f"• `{k}` = `{json.dumps(val)}`\n  _{desc}_")
            await event.edit(
                "\n".join(lines),
                buttons=build_settings_menu_keyboard(keys),
            )
            return

        if data_str.startswith("cp:edit_set:"):
            key = data_str.split(":", 2)[2]
            self._state = _ControlPanelInteractiveState(
                mode="await_setting_value",
                setting_key=key,
            )
            await event.respond(
                f"✏️ **Update Setting `{key}`**\n"
                "Reply with the new JSON value (e.g. `\"gemini-4-argon-high\"`, `900`, `1.8`)."
            )
            await event.answer()
            return

        if data_str == "cp:create_invite":
            token = f"inv_{secrets.token_urlsafe(18)}"
            expires_at = datetime.now(tz=UTC) + timedelta(hours=24)
            await self._access_repo.create_invite(
                token=token,
                role=UserRole.MEMBER,
                allowed_topic_ids=(),
                created_by_user_id=self._cfg.telegram.owner_telegram_id,
                expires_at=expires_at,
            )
            await event.respond(
                f"🎟 **Single-Use Member Invite Generated (valid 24h):**\n"
                f"Ask the user to send the following command in the forum:\n"
                f"`/redeem {token}`"
            )
            await event.answer("Invite generated.")

    async def _process_new_account_input(self, raw_text: str) -> None:
        parts = raw_text.split(maxsplit=1)
        if len(parts) < 2:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message="❌ Format must be `<slug> <email_label>` (e.g. `acc_1 user@gmail.com`).",
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
        keyring_password = secrets.token_urlsafe(32)
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
            message=(
                f"✅ Registered and unlocked isolated D-Bus/Keyring profile `{account.slug}`.\n"
                "Now open **[🔑 Add / Auth Account]** to complete Google OAuth sign-in."
            ),
            reply_to=self._cfg.telegram.control_panel_topic_id,
        )
        await self.render_and_pin_dashboard()

    async def _process_oauth_code_input(
        self, pending: PendingOAuthSession, verification_code: str
    ) -> None:
        try:
            await self._keyring_mgr.complete_remote_oauth_flow(pending, verification_code)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=(
                    f"✅ **OAuth Sign-In Complete for `{pending.account.slug}`!**\n"
                    "Token stored in isolated `gnome-keyring` and backed up to PostgreSQL 18 via `Fernet`."
                ),
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
        except Exception as exc:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=f"❌ **OAuth Verification Failed:**\n`{exc}`",
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
        await self.render_and_pin_dashboard()

    async def _process_new_topic_input(self, raw_text: str) -> None:
        parts = [p.strip() for p in raw_text.split("|")]
        title = parts[0] if parts else "New Workspace"
        workspace_path = (
            parts[1]
            if len(parts) >= 2 and parts[1]
            else str(self._cfg.paths.workspaces_root_dir / title.lower().replace(" ", "_"))
        )
        kind_raw = parts[2].upper() if len(parts) >= 3 else "TASK"
        kind = (
            TopicKind.PERMANENT_WORKSPACE
            if "PERM" in kind_raw
            else TopicKind.TASK_TOPIC
        )

        try:
            topic = await self._topic_lifecycle.create_forum_topic_with_card(
                title=title,
                kind=kind,
                workspace_path=workspace_path,
                execution_mode=ExecutionMode.FULL_ACCESS,
            )
            await self._workspace_ui.refresh_pinned_card(topic.topic_id)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=f"✅ Created Forum Topic **{topic.title}** (`topic_id={topic.topic_id}`).",
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
        except Exception as exc:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=f"❌ Failed to create topic: `{exc}`",
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )

    async def _process_setting_update_input(self, key: str, raw_text: str) -> None:
        try:
            parsed_val: Any = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed_val = raw_text.strip()

        try:
            await self._settings_repo.update_setting(key, parsed_val)
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=f"✅ Updated setting `{key}` = `{json.dumps(parsed_val)}`.",
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
            await self.render_and_pin_dashboard()
        except Exception as exc:
            await self._client.send_message(
                entity=self._cfg.telegram.forum_chat_id,
                message=f"❌ Validation failed for `{key}`:\n`{exc}`",
                reply_to=self._cfg.telegram.control_panel_topic_id,
            )
