from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, Double, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SystemSettingRow(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoogleAccountRow(Base):
    __tablename__ = "google_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email_label: Mapped[str] = mapped_column(String(255), nullable=False)
    xdg_data_dir: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    dbus_socket_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    encrypted_keyring_pass: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_token_backup: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    last_quota_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_quota_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RemoteHostRow(Base):
    __tablename__ = "remote_hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    hostname_or_ip: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_port: Mapped[int] = mapped_column(Integer, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(128), nullable=False)
    remote_root_path: Mapped[str] = mapped_column(Text, nullable=False)
    local_mount_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    rule_file_path: Mapped[str] = mapped_column(Text, nullable=False)
    auto_mount_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ForumTopicRow(Base):
    __tablename__ = "forum_topics"

    topic_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    additional_dirs_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    model_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    effort_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    assigned_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("google_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    active_conversation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    pinned_card_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TopicSessionRow(Base):
    __tablename__ = "topic_sessions"

    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False
    )
    topic_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("forum_topics.topic_id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("google_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    turns_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cumulative_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_duration_sec: Mapped[float] = mapped_column(Double, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_turn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScheduledTaskRow(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    topic_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("forum_topics.topic_id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    schedule_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_spec: Mapped[str] = mapped_column(String(255), nullable=False)
    instruction_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    notify_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthorizedUserRow(Base):
    __tablename__ = "authorized_users"

    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    username_label: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    allowed_topic_ids_json: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InviteTokenRow(Base):
    __tablename__ = "invite_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    allowed_topic_ids_json: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    redeemed_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventSensorRow(Base):
    __tablename__ = "event_sensors"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    topic_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("forum_topics.topic_id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sensor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_condition: Mapped[str] = mapped_column(String(32), nullable=False)
    match_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    poll_interval_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    cooldown_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    one_shot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    instruction_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    notify_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    triggers_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
