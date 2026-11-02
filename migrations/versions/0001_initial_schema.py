"""Initial PostgreSQL 18 schema and mandatory dynamic settings seed.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-02 00:00:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    system_settings = op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "google_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True),
        sa.Column("email_label", sa.String(length=255), nullable=False),
        sa.Column("xdg_data_dir", sa.Text(), nullable=False, unique=True),
        sa.Column("dbus_socket_path", sa.Text(), nullable=False, unique=True),
        sa.Column("encrypted_keyring_pass", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_token_backup", sa.LargeBinary(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("last_quota_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_quota_check_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "remote_hosts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("alias", sa.String(length=64), nullable=False, unique=True),
        sa.Column("hostname_or_ip", sa.String(length=255), nullable=False),
        sa.Column("ssh_port", sa.Integer(), nullable=False),
        sa.Column("ssh_user", sa.String(length=128), nullable=False),
        sa.Column("remote_root_path", sa.Text(), nullable=False),
        sa.Column("local_mount_path", sa.Text(), nullable=False, unique=True),
        sa.Column("rule_file_path", sa.Text(), nullable=False),
        sa.Column("auto_mount_enabled", sa.Boolean(), nullable=False),
    )

    op.create_table(
        "forum_topics",
        sa.Column("topic_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column(
            "additional_dirs_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("model_slug", sa.String(length=128), nullable=False),
        sa.Column("effort_level", sa.String(length=32), nullable=True),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "assigned_account_id",
            sa.Integer(),
            sa.ForeignKey("google_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("active_conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pinned_card_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "topic_sessions",
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "topic_id",
            sa.BigInteger(),
            sa.ForeignKey("forum_topics.topic_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("google_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("turns_count", sa.Integer(), nullable=False),
        sa.Column("cumulative_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_duration_sec", sa.Double(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_turn_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_topic_sessions_topic_id", "topic_sessions", ["topic_id"])

    op.create_table(
        "scheduled_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "topic_id",
            sa.BigInteger(),
            sa.ForeignKey("forum_topics.topic_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("schedule_kind", sa.String(length=32), nullable=False),
        sa.Column("trigger_spec", sa.String(length=255), nullable=False),
        sa.Column("instruction_prompt", sa.Text(), nullable=False),
        sa.Column("notify_policy", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_scheduled_tasks_topic_id", "scheduled_tasks", ["topic_id"])

    op.create_table(
        "authorized_users",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("username_label", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "allowed_topic_ids_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "invite_tokens",
        sa.Column("token", sa.String(length=128), primary_key=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "allowed_topic_ids_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("redeemed_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    now = datetime.now(tz=UTC)
    op.bulk_insert(
        system_settings,
        [
            {
                "key": "ui_language",
                "value_json": "ru",
                "description": "Telegram UI language code (ru or en).",
                "updated_at": now,
            },
            {
                "key": "default_model",
                "value_json": "gemini-4-argon-medium",
                "description": "Default agy model slug for newly created workspace topics.",
                "updated_at": now,
            },
            {
                "key": "default_effort",
                "value_json": "high",
                "description": "Default thinking effort level for Gemini models (low/medium/high/max or null).",
                "updated_at": now,
            },
            {
                "key": "default_execution_mode",
                "value_json": "FULL_ACCESS",
                "description": "Default execution mode for new topics (FULL_ACCESS or READ_ONLY).",
                "updated_at": now,
            },
            {
                "key": "debounce_window_sec",
                "value_json": 1.8,
                "description": "Debounce window in seconds for batching consecutive messages and forwards.",
                "updated_at": now,
            },
            {
                "key": "max_batch_window_sec",
                "value_json": 10.0,
                "description": "Maximum total window in seconds to accumulate messages in a batch.",
                "updated_at": now,
            },
            {
                "key": "worker_idle_ttl_sec",
                "value_json": 900,
                "description": "Idle time in seconds before a warm agy worker process is gracefully closed.",
                "updated_at": now,
            },
            {
                "key": "turn_timeout_sec",
                "value_json": 900,
                "description": "Maximum execution time in seconds for a single turn (--print-timeout).",
                "updated_at": now,
            },
            {
                "key": "tg_edit_throttle_sec",
                "value_json": 2.0,
                "description": "Minimum interval in seconds between live Telegram progress message edits.",
                "updated_at": now,
            },
            {
                "key": "tg_max_message_chars",
                "value_json": 3800,
                "description": "Maximum character length of a single Telegram message chunk.",
                "updated_at": now,
            },
            {
                "key": "stdout_buffer_limit_bytes",
                "value_json": 33554432,
                "description": "Subprocess stdout line buffer limit in bytes (32 MB).",
                "updated_at": now,
            },
            {
                "key": "quota_auto_switch_threshold_pct",
                "value_json": 5,
                "description": "Remaining quota percentage threshold to trigger automatic Google account failover.",
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("invite_tokens")
    op.drop_table("authorized_users")
    op.drop_index("ix_scheduled_tasks_topic_id", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
    op.drop_index("ix_topic_sessions_topic_id", table_name="topic_sessions")
    op.drop_table("topic_sessions")
    op.drop_table("forum_topics")
    op.drop_table("remote_hosts")
    op.drop_table("google_accounts")
    op.drop_table("system_settings")
