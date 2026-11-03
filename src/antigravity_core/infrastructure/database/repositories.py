from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update

from antigravity_core.config.constants import DASHBOARD_MSG_ID_SETTING_KEY
from antigravity_core.config.dynamic_settings import (
    MANDATORY_SYSTEM_SETTING_KEYS,
    DynamicSettings,
    parse_dynamic_settings,
)
from antigravity_core.domain.enums import (
    EffortLevel,
    ExecutionMode,
    NotifyPolicy,
    ScheduleKind,
    SensorKind,
    SensorTriggerCondition,
    TopicKind,
    TopicStatus,
    UserRole,
)
from antigravity_core.domain.exceptions import DatabaseIntegrityError
from antigravity_core.domain.models import (
    AuthorizedUser,
    EventSensor,
    ForumTopic,
    GoogleAccount,
    InviteToken,
    RemoteHost,
    ScheduledTask,
    TopicSession,
)
from antigravity_core.infrastructure.database.engine import DatabaseManager
from antigravity_core.infrastructure.database.tables import (
    AuthorizedUserRow,
    EventSensorRow,
    ForumTopicRow,
    GoogleAccountRow,
    InviteTokenRow,
    RemoteHostRow,
    ScheduledTaskRow,
    SystemSettingRow,
    TopicSessionRow,
)


def _map_account(row: GoogleAccountRow) -> GoogleAccount:
    return GoogleAccount(
        id=row.id,
        slug=row.slug,
        email_label=row.email_label,
        xdg_data_dir=row.xdg_data_dir,
        dbus_socket_path=row.dbus_socket_path,
        encrypted_keyring_pass=bytes(row.encrypted_keyring_pass),
        encrypted_token_backup=(
            bytes(row.encrypted_token_backup) if row.encrypted_token_backup is not None else None
        ),
        is_enabled=row.is_enabled,
        is_default=row.is_default,
        priority=row.priority,
        last_quota_json=dict(row.last_quota_json) if row.last_quota_json is not None else None,
        last_quota_check_at=row.last_quota_check_at,
    )


def _map_remote_host(row: RemoteHostRow) -> RemoteHost:
    return RemoteHost(
        id=row.id,
        alias=row.alias,
        hostname_or_ip=row.hostname_or_ip,
        ssh_port=row.ssh_port,
        ssh_user=row.ssh_user,
        remote_root_path=row.remote_root_path,
        local_mount_path=row.local_mount_path,
        rule_file_path=row.rule_file_path,
        auto_mount_enabled=row.auto_mount_enabled,
    )


def _map_topic(row: ForumTopicRow) -> ForumTopic:
    return ForumTopic(
        topic_id=row.topic_id,
        title=row.title,
        kind=TopicKind(row.kind),
        status=TopicStatus(row.status),
        workspace_path=row.workspace_path,
        additional_dirs=tuple(str(item) for item in row.additional_dirs_json),
        model_slug=row.model_slug,
        effort_level=EffortLevel(row.effort_level) if row.effort_level is not None else None,
        execution_mode=ExecutionMode(row.execution_mode),
        assigned_account_id=row.assigned_account_id,
        active_conversation_id=row.active_conversation_id,
        pinned_card_msg_id=row.pinned_card_msg_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _map_session(row: TopicSessionRow) -> TopicSession:
    return TopicSession(
        conversation_id=row.conversation_id,
        topic_id=row.topic_id,
        account_id=row.account_id,
        workspace_path=row.workspace_path,
        turns_count=row.turns_count,
        cumulative_tokens=row.cumulative_tokens,
        cumulative_duration_sec=row.cumulative_duration_sec,
        created_at=row.created_at,
        last_turn_at=row.last_turn_at,
    )


def _map_scheduled_task(row: ScheduledTaskRow) -> ScheduledTask:
    return ScheduledTask(
        id=row.id,
        topic_id=row.topic_id,
        conversation_id=row.conversation_id,
        schedule_kind=ScheduleKind(row.schedule_kind),
        trigger_spec=row.trigger_spec,
        instruction_prompt=row.instruction_prompt,
        notify_policy=NotifyPolicy(row.notify_policy),
        is_active=row.is_active,
        last_run_at=row.last_run_at,
        created_at=row.created_at,
    )


def _map_authorized_user(row: AuthorizedUserRow) -> AuthorizedUser:
    return AuthorizedUser(
        telegram_user_id=row.telegram_user_id,
        username_label=row.username_label,
        role=UserRole(row.role),
        allowed_topic_ids=tuple(int(tid) for tid in row.allowed_topic_ids_json),
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _map_invite_token(row: InviteTokenRow) -> InviteToken:
    return InviteToken(
        token=row.token,
        role=UserRole(row.role),
        allowed_topic_ids=tuple(int(tid) for tid in row.allowed_topic_ids_json),
        created_by_user_id=row.created_by_user_id,
        redeemed_by_user_id=row.redeemed_by_user_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


class SettingsRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def load_settings(self) -> DynamicSettings:
        async with self._db.session() as session:
            stmt = select(SystemSettingRow)
            rows = (await session.scalars(stmt)).all()
            raw_map: dict[str, Any] = {row.key: row.value_json for row in rows}

            if "ui_language" not in raw_map:
                now = datetime.now(tz=UTC)
                lang_row = SystemSettingRow(
                    key="ui_language",
                    value_json="ru",
                    description="Язык интерфейса Telegram (ru / en).",
                    updated_at=now,
                )
                session.add(lang_row)
                await session.flush()
                raw_map["ui_language"] = "ru"

            allowed_keys = {*MANDATORY_SYSTEM_SETTING_KEYS, DASHBOARD_MSG_ID_SETTING_KEY}
            obsolete_keys = [k for k in raw_map if k not in allowed_keys]
            if obsolete_keys:
                await session.execute(
                    delete(SystemSettingRow).where(SystemSettingRow.key.in_(obsolete_keys))
                )
                await session.flush()

            return parse_dynamic_settings(raw_map)

    async def list_all_with_descriptions(self) -> list[tuple[str, Any, str, datetime]]:
        async with self._db.session() as session:
            stmt = (
                select(SystemSettingRow)
                .where(SystemSettingRow.key.in_(MANDATORY_SYSTEM_SETTING_KEYS))
                .order_by(SystemSettingRow.key.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [(r.key, r.value_json, r.description, r.updated_at) for r in rows]

    async def get_dashboard_msg_id(self) -> int | None:
        async with self._db.session() as session:
            row = await session.get(SystemSettingRow, DASHBOARD_MSG_ID_SETTING_KEY)
            if row is None or not isinstance(row.value_json, int):
                return None
            return int(row.value_json)

    async def set_dashboard_msg_id(self, msg_id: int | None) -> None:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            row = await session.get(SystemSettingRow, DASHBOARD_MSG_ID_SETTING_KEY)
            if msg_id is None:
                if row is not None:
                    await session.delete(row)
                    await session.flush()
                return
            if row is not None:
                row.value_json = msg_id
                row.updated_at = now
            else:
                session.add(
                    SystemSettingRow(
                        key=DASHBOARD_MSG_ID_SETTING_KEY,
                        value_json=msg_id,
                        description="Internal Control-Panel active dashboard message ID.",
                        updated_at=now,
                    )
                )
            await session.flush()

    async def update_setting(self, key: str, new_value: Any) -> DynamicSettings:
        async with self._db.session() as session:
            row = await session.get(SystemSettingRow, key)
            if row is None:
                raise DatabaseIntegrityError(f"System setting key '{key}' does not exist.")
            row.value_json = new_value
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()

            stmt = select(SystemSettingRow)
            all_rows = (await session.scalars(stmt)).all()
            raw_map: dict[str, Any] = {r.key: r.value_json for r in all_rows}
            return parse_dynamic_settings(raw_map)


class AccountRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def list_accounts(self) -> list[GoogleAccount]:
        async with self._db.session() as session:
            stmt = select(GoogleAccountRow).order_by(
                GoogleAccountRow.priority.asc(), GoogleAccountRow.id.asc()
            )
            rows = (await session.scalars(stmt)).all()
            return [_map_account(r) for r in rows]

    async def list_enabled_accounts(self) -> list[GoogleAccount]:
        async with self._db.session() as session:
            stmt = (
                select(GoogleAccountRow)
                .where(GoogleAccountRow.is_enabled.is_(True))
                .order_by(GoogleAccountRow.priority.asc(), GoogleAccountRow.id.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [_map_account(r) for r in rows]

    async def get_by_id(self, account_id: int) -> GoogleAccount:
        async with self._db.session() as session:
            row = await session.get(GoogleAccountRow, account_id)
            if row is None:
                raise DatabaseIntegrityError(f"GoogleAccount id={account_id} does not exist.")
            return _map_account(row)

    async def get_by_slug(self, slug: str) -> GoogleAccount | None:
        async with self._db.session() as session:
            stmt = select(GoogleAccountRow).where(GoogleAccountRow.slug == slug)
            row = (await session.scalars(stmt)).first()
            return _map_account(row) if row is not None else None

    async def get_default_account(self) -> GoogleAccount:
        async with self._db.session() as session:
            stmt = (
                select(GoogleAccountRow)
                .where(GoogleAccountRow.is_enabled.is_(True))
                .order_by(
                    GoogleAccountRow.is_default.desc(),
                    GoogleAccountRow.priority.asc(),
                    GoogleAccountRow.id.asc(),
                )
            )
            row = (await session.scalars(stmt)).first()
            if row is None:
                raise DatabaseIntegrityError(
                    "No enabled Google account is registered in google_accounts table."
                )
            return _map_account(row)

    async def create_account(
        self,
        slug: str,
        email_label: str,
        xdg_data_dir: str,
        dbus_socket_path: str,
        encrypted_keyring_pass: bytes,
        is_default: bool,
        priority: int,
    ) -> GoogleAccount:
        async with self._db.session() as session:
            if is_default:
                await session.execute(update(GoogleAccountRow).values(is_default=False))
            row = GoogleAccountRow(
                slug=slug,
                email_label=email_label,
                xdg_data_dir=xdg_data_dir,
                dbus_socket_path=dbus_socket_path,
                encrypted_keyring_pass=encrypted_keyring_pass,
                encrypted_token_backup=None,
                is_enabled=True,
                is_default=is_default,
                priority=priority,
                last_quota_json=None,
                last_quota_check_at=None,
            )
            session.add(row)
            await session.flush()
            return _map_account(row)

    async def set_default_account(self, account_id: int) -> GoogleAccount:
        async with self._db.session() as session:
            target = await session.get(GoogleAccountRow, account_id)
            if target is None:
                raise DatabaseIntegrityError(f"GoogleAccount id={account_id} does not exist.")
            await session.execute(update(GoogleAccountRow).values(is_default=False))
            target.is_default = True
            target.is_enabled = True
            await session.flush()
            return _map_account(target)

    async def update_token_backup(self, account_id: int, encrypted_token: bytes) -> GoogleAccount:
        async with self._db.session() as session:
            row = await session.get(GoogleAccountRow, account_id)
            if row is None:
                raise DatabaseIntegrityError(f"GoogleAccount id={account_id} does not exist.")
            row.encrypted_token_backup = encrypted_token
            await session.flush()
            return _map_account(row)

    async def update_quota_snapshot(
        self, account_id: int, quota_json: dict[str, Any]
    ) -> GoogleAccount:
        async with self._db.session() as session:
            row = await session.get(GoogleAccountRow, account_id)
            if row is None:
                raise DatabaseIntegrityError(f"GoogleAccount id={account_id} does not exist.")
            row.last_quota_json = quota_json
            row.last_quota_check_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_account(row)


class RemoteHostRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def list_hosts(self) -> list[RemoteHost]:
        async with self._db.session() as session:
            stmt = select(RemoteHostRow).order_by(RemoteHostRow.alias.asc())
            rows = (await session.scalars(stmt)).all()
            return [_map_remote_host(r) for r in rows]

    async def get_by_alias(self, alias: str) -> RemoteHost | None:
        async with self._db.session() as session:
            stmt = select(RemoteHostRow).where(RemoteHostRow.alias == alias)
            row = (await session.scalars(stmt)).first()
            return _map_remote_host(row) if row is not None else None

    async def upsert_host(
        self,
        alias: str,
        hostname_or_ip: str,
        ssh_port: int,
        ssh_user: str,
        remote_root_path: str,
        local_mount_path: str,
        rule_file_path: str,
        auto_mount_enabled: bool,
    ) -> RemoteHost:
        async with self._db.session() as session:
            stmt = select(RemoteHostRow).where(RemoteHostRow.alias == alias)
            existing = (await session.scalars(stmt)).first()
            if existing is not None:
                existing.hostname_or_ip = hostname_or_ip
                existing.ssh_port = ssh_port
                existing.ssh_user = ssh_user
                existing.remote_root_path = remote_root_path
                existing.local_mount_path = local_mount_path
                existing.rule_file_path = rule_file_path
                existing.auto_mount_enabled = auto_mount_enabled
                await session.flush()
                return _map_remote_host(existing)

            row = RemoteHostRow(
                alias=alias,
                hostname_or_ip=hostname_or_ip,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                remote_root_path=remote_root_path,
                local_mount_path=local_mount_path,
                rule_file_path=rule_file_path,
                auto_mount_enabled=auto_mount_enabled,
            )
            session.add(row)
            await session.flush()
            return _map_remote_host(row)


class TopicRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def get_topic(self, topic_id: int) -> ForumTopic | None:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            return _map_topic(row) if row is not None else None

    async def get_topic_strict(self, topic_id: int) -> ForumTopic:
        topic = await self.get_topic(topic_id)
        if topic is None:
            raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} is not registered.")
        return topic

    async def list_active_topics(self) -> list[ForumTopic]:
        async with self._db.session() as session:
            stmt = (
                select(ForumTopicRow)
                .where(ForumTopicRow.status == TopicStatus.ACTIVE.value)
                .order_by(ForumTopicRow.topic_id.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [_map_topic(r) for r in rows]

    async def create_topic(
        self,
        topic_id: int,
        title: str,
        kind: TopicKind,
        workspace_path: str,
        additional_dirs: tuple[str, ...],
        model_slug: str,
        effort_level: EffortLevel | None,
        execution_mode: ExecutionMode,
        assigned_account_id: int,
        pinned_card_msg_id: int | None,
    ) -> ForumTopic:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            row = ForumTopicRow(
                topic_id=topic_id,
                title=title,
                kind=kind.value,
                status=TopicStatus.ACTIVE.value,
                workspace_path=workspace_path,
                additional_dirs_json=list(additional_dirs),
                model_slug=model_slug,
                effort_level=effort_level.value if effort_level is not None else None,
                execution_mode=execution_mode.value,
                assigned_account_id=assigned_account_id,
                active_conversation_id=None,
                pinned_card_msg_id=pinned_card_msg_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            return _map_topic(row)

    async def set_active_conversation(
        self, topic_id: int, conversation_id: UUID | None
    ) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.active_conversation_id = conversation_id
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)

    async def set_pinned_card_msg_id(self, topic_id: int, msg_id: int) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.pinned_card_msg_id = msg_id
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)

    async def update_execution_mode(self, topic_id: int, mode: ExecutionMode) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.execution_mode = mode.value
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)

    async def update_model_and_effort(
        self, topic_id: int, model_slug: str, effort_level: EffortLevel | None
    ) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.model_slug = model_slug
            row.effort_level = effort_level.value if effort_level is not None else None
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)

    async def update_assigned_account(self, topic_id: int, account_id: int) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.assigned_account_id = account_id
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)

    async def update_workspace_path(self, topic_id: int, workspace_path: str) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.workspace_path = workspace_path
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)

    async def update_topic_title(self, topic_id: int, new_title: str) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.title = new_title
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)

    async def archive_topic(self, topic_id: int, archived_title: str) -> ForumTopic:
        async with self._db.session() as session:
            row = await session.get(ForumTopicRow, topic_id)
            if row is None:
                raise DatabaseIntegrityError(f"ForumTopic topic_id={topic_id} does not exist.")
            row.status = TopicStatus.ARCHIVED.value
            row.title = archived_title
            row.updated_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_topic(row)


class SessionRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def get_session(self, conversation_id: UUID) -> TopicSession | None:
        async with self._db.session() as session:
            row = await session.get(TopicSessionRow, conversation_id)
            return _map_session(row) if row is not None else None

    async def list_topic_sessions(self, topic_id: int, limit: int) -> list[TopicSession]:
        async with self._db.session() as session:
            stmt = (
                select(TopicSessionRow)
                .where(TopicSessionRow.topic_id == topic_id)
                .order_by(TopicSessionRow.last_turn_at.desc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return [_map_session(r) for r in rows]

    async def ensure_session_initialized(
        self,
        conversation_id: UUID,
        topic_id: int,
        account_id: int,
        workspace_path: str,
    ) -> TopicSession:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            existing = await session.get(TopicSessionRow, conversation_id)
            if existing is not None:
                existing.account_id = account_id
                existing.last_turn_at = now
                await session.flush()
                return _map_session(existing)

            row = TopicSessionRow(
                conversation_id=conversation_id,
                topic_id=topic_id,
                account_id=account_id,
                workspace_path=workspace_path,
                turns_count=0,
                cumulative_tokens=0,
                cumulative_duration_sec=0.0,
                created_at=now,
                last_turn_at=now,
            )
            session.add(row)
            await session.flush()
            return _map_session(row)

    async def record_turn_completion(
        self,
        conversation_id: UUID,
        account_id: int,
        turn_delta_tokens: int,
        turn_delta_duration_sec: float,
    ) -> TopicSession:
        async with self._db.session() as session:
            row = await session.get(TopicSessionRow, conversation_id)
            if row is None:
                raise DatabaseIntegrityError(
                    f"TopicSession conversation_id={conversation_id} does not exist."
                )
            row.account_id = account_id
            row.turns_count += 1
            row.cumulative_tokens += max(0, turn_delta_tokens)
            row.cumulative_duration_sec += max(0.0, turn_delta_duration_sec)
            row.last_turn_at = datetime.now(tz=UTC)
            await session.flush()
            return _map_session(row)


class ScheduleRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def create_task(
        self,
        task_id: UUID,
        topic_id: int,
        conversation_id: UUID | None,
        schedule_kind: ScheduleKind,
        trigger_spec: str,
        instruction_prompt: str,
        notify_policy: NotifyPolicy,
    ) -> ScheduledTask:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            row = ScheduledTaskRow(
                id=task_id,
                topic_id=topic_id,
                conversation_id=conversation_id,
                schedule_kind=schedule_kind.value,
                trigger_spec=trigger_spec,
                instruction_prompt=instruction_prompt,
                notify_policy=notify_policy.value,
                is_active=True,
                last_run_at=None,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return _map_scheduled_task(row)

    async def get_task(self, task_id: UUID) -> ScheduledTask | None:
        async with self._db.session() as session:
            row = await session.get(ScheduledTaskRow, task_id)
            return _map_scheduled_task(row) if row is not None else None

    async def list_active_tasks(self, topic_id: int | None) -> list[ScheduledTask]:
        async with self._db.session() as session:
            stmt = select(ScheduledTaskRow).where(ScheduledTaskRow.is_active.is_(True))
            if topic_id is not None:
                stmt = stmt.where(ScheduledTaskRow.topic_id == topic_id)
            stmt = stmt.order_by(ScheduledTaskRow.created_at.asc())
            rows = (await session.scalars(stmt)).all()
            return [_map_scheduled_task(r) for r in rows]

    async def mark_task_executed(self, task_id: UUID, deactivate: bool) -> ScheduledTask:
        async with self._db.session() as session:
            row = await session.get(ScheduledTaskRow, task_id)
            if row is None:
                raise DatabaseIntegrityError(f"ScheduledTask id={task_id} does not exist.")
            row.last_run_at = datetime.now(tz=UTC)
            if deactivate:
                row.is_active = False
            await session.flush()
            return _map_scheduled_task(row)

    async def cancel_task(self, task_id: UUID) -> ScheduledTask:
        async with self._db.session() as session:
            row = await session.get(ScheduledTaskRow, task_id)
            if row is None:
                raise DatabaseIntegrityError(f"ScheduledTask id={task_id} does not exist.")
            row.is_active = False
            await session.flush()
            return _map_scheduled_task(row)


class AccessControlRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def ensure_owner_registered(
        self, owner_telegram_id: int, username_label: str
    ) -> AuthorizedUser:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            existing = await session.get(AuthorizedUserRow, owner_telegram_id)
            if existing is not None:
                existing.role = UserRole.OWNER.value
                existing.is_active = True
                await session.flush()
                return _map_authorized_user(existing)

            row = AuthorizedUserRow(
                telegram_user_id=owner_telegram_id,
                username_label=username_label,
                role=UserRole.OWNER.value,
                allowed_topic_ids_json=[],
                is_active=True,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return _map_authorized_user(row)

    async def get_user(self, telegram_user_id: int) -> AuthorizedUser | None:
        async with self._db.session() as session:
            row = await session.get(AuthorizedUserRow, telegram_user_id)
            return _map_authorized_user(row) if row is not None else None

    async def create_invite(
        self,
        token: str,
        role: UserRole,
        allowed_topic_ids: tuple[int, ...],
        created_by_user_id: int,
        expires_at: datetime,
    ) -> InviteToken:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            row = InviteTokenRow(
                token=token,
                role=role.value,
                allowed_topic_ids_json=list(allowed_topic_ids),
                created_by_user_id=created_by_user_id,
                redeemed_by_user_id=None,
                expires_at=expires_at,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return _map_invite_token(row)

    async def redeem_invite(
        self, token: str, telegram_user_id: int, username_label: str
    ) -> AuthorizedUser:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            invite = await session.get(InviteTokenRow, token)
            if invite is None:
                raise DatabaseIntegrityError("Invite token does not exist.")
            if invite.redeemed_by_user_id is not None:
                raise DatabaseIntegrityError("Invite token has already been redeemed.")
            if invite.expires_at < now:
                raise DatabaseIntegrityError("Invite token has expired.")

            invite.redeemed_by_user_id = telegram_user_id
            user_row = await session.get(AuthorizedUserRow, telegram_user_id)
            if user_row is not None:
                user_row.username_label = username_label
                user_row.role = invite.role
                user_row.allowed_topic_ids_json = list(invite.allowed_topic_ids_json)
                user_row.is_active = True
                await session.flush()
                return _map_authorized_user(user_row)

            new_user = AuthorizedUserRow(
                telegram_user_id=telegram_user_id,
                username_label=username_label,
                role=invite.role,
                allowed_topic_ids_json=list(invite.allowed_topic_ids_json),
                is_active=True,
                created_at=now,
            )
            session.add(new_user)
            await session.flush()
            return _map_authorized_user(new_user)


def _map_event_sensor(row: EventSensorRow) -> EventSensor:
    return EventSensor(
        id=row.id,
        topic_id=row.topic_id,
        conversation_id=row.conversation_id,
        name=row.name,
        sensor_kind=SensorKind(row.sensor_kind),
        command=row.command,
        trigger_condition=SensorTriggerCondition(row.trigger_condition),
        match_pattern=row.match_pattern,
        poll_interval_sec=row.poll_interval_sec,
        cooldown_sec=row.cooldown_sec,
        one_shot=row.one_shot,
        instruction_prompt=row.instruction_prompt,
        notify_policy=NotifyPolicy(row.notify_policy),
        is_active=row.is_active,
        triggers_count=row.triggers_count,
        last_triggered_at=row.last_triggered_at,
        created_at=row.created_at,
    )


class SensorRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def create_sensor(
        self,
        sensor_id: UUID,
        topic_id: int,
        conversation_id: UUID | None,
        name: str,
        sensor_kind: SensorKind,
        command: str,
        trigger_condition: SensorTriggerCondition,
        match_pattern: str | None,
        poll_interval_sec: int,
        cooldown_sec: int,
        one_shot: bool,
        instruction_prompt: str,
        notify_policy: NotifyPolicy,
    ) -> EventSensor:
        now = datetime.now(tz=UTC)
        async with self._db.session() as session:
            row = EventSensorRow(
                id=sensor_id,
                topic_id=topic_id,
                conversation_id=conversation_id,
                name=name,
                sensor_kind=sensor_kind.value,
                command=command,
                trigger_condition=trigger_condition.value,
                match_pattern=match_pattern,
                poll_interval_sec=poll_interval_sec,
                cooldown_sec=cooldown_sec,
                one_shot=one_shot,
                instruction_prompt=instruction_prompt,
                notify_policy=notify_policy.value,
                is_active=True,
                triggers_count=0,
                last_triggered_at=None,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return _map_event_sensor(row)

    async def get_sensor(self, sensor_id: UUID) -> EventSensor | None:
        async with self._db.session() as session:
            row = await session.get(EventSensorRow, sensor_id)
            return _map_event_sensor(row) if row is not None else None

    async def list_active_sensors(self, topic_id: int | None) -> list[EventSensor]:
        async with self._db.session() as session:
            stmt = select(EventSensorRow).where(EventSensorRow.is_active.is_(True))
            if topic_id is not None:
                stmt = stmt.where(EventSensorRow.topic_id == topic_id)
            stmt = stmt.order_by(EventSensorRow.created_at.asc())
            rows = (await session.scalars(stmt)).all()
            return [_map_event_sensor(r) for r in rows]

    async def mark_sensor_triggered(self, sensor_id: UUID, deactivate: bool) -> EventSensor:
        async with self._db.session() as session:
            row = await session.get(EventSensorRow, sensor_id)
            if row is None:
                raise DatabaseIntegrityError(f"EventSensor id={sensor_id} does not exist.")
            row.triggers_count += 1
            row.last_triggered_at = datetime.now(tz=UTC)
            if deactivate:
                row.is_active = False
            await session.flush()
            return _map_event_sensor(row)

    async def cancel_sensor(self, sensor_id: UUID) -> EventSensor:
        async with self._db.session() as session:
            row = await session.get(EventSensorRow, sensor_id)
            if row is None:
                raise DatabaseIntegrityError(f"EventSensor id={sensor_id} does not exist.")
            row.is_active = False
            await session.flush()
            return _map_event_sensor(row)
