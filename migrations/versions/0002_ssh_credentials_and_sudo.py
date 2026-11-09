"""Add ssh_credentials table, event_sensors table, and sudo_carte_blanche column.

Revision ID: 0002_ssh_credentials_and_sudo
Revises: 0001_initial_schema
Create Date: 2026-11-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_ssh_credentials_and_sudo"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "event_sensors" not in existing_tables:
        op.create_table(
            "event_sensors",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "topic_id",
                sa.BigInteger(),
                sa.ForeignKey("forum_topics.topic_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("sensor_kind", sa.String(length=32), nullable=False),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("trigger_condition", sa.String(length=32), nullable=False),
            sa.Column("match_pattern", sa.Text(), nullable=True),
            sa.Column("action_command", sa.Text(), nullable=True),
            sa.Column(
                "wake_mode",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'ALWAYS'"),
            ),
            sa.Column("poll_interval_sec", sa.Integer(), nullable=False),
            sa.Column("cooldown_sec", sa.Integer(), nullable=False),
            sa.Column("one_shot", sa.Boolean(), nullable=False),
            sa.Column("instruction_prompt", sa.Text(), nullable=False),
            sa.Column("notify_policy", sa.String(length=32), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("triggers_count", sa.Integer(), nullable=False),
            sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_event_sensors_topic_id", "event_sensors", ["topic_id"])
    else:
        sensor_cols = {col["name"] for col in inspector.get_columns("event_sensors")}
        if "action_command" not in sensor_cols:
            op.add_column("event_sensors", sa.Column("action_command", sa.Text(), nullable=True))
        if "wake_mode" not in sensor_cols:
            op.add_column(
                "event_sensors",
                sa.Column(
                    "wake_mode",
                    sa.String(length=32),
                    nullable=False,
                    server_default=sa.text("'ALWAYS'"),
                ),
            )

    if "ssh_credentials" not in existing_tables:
        op.create_table(
            "ssh_credentials",
            sa.Column("credential_key", sa.String(length=255), primary_key=True),
            sa.Column("ssh_user", sa.String(length=128), nullable=False),
            sa.Column("host_target", sa.String(length=255), nullable=False),
            sa.Column("ssh_port", sa.Integer(), nullable=False),
            sa.Column("encrypted_password", sa.LargeBinary(), nullable=False),
            sa.Column("is_root", sa.Boolean(), nullable=False),
            sa.Column("key_provisioned", sa.Boolean(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    remote_host_cols = {col["name"] for col in inspector.get_columns("remote_hosts")}
    if "jump_host" not in remote_host_cols:
        op.add_column(
            "remote_hosts", sa.Column("jump_host", sa.String(length=64), nullable=True)
        )

    forum_topic_cols = {col["name"] for col in inspector.get_columns("forum_topics")}
    if "sudo_carte_blanche" not in forum_topic_cols:
        op.add_column(
            "forum_topics",
            sa.Column(
                "sudo_carte_blanche",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "forum_topics" in existing_tables:
        forum_topic_cols = {col["name"] for col in inspector.get_columns("forum_topics")}
        if "sudo_carte_blanche" in forum_topic_cols:
            op.drop_column("forum_topics", "sudo_carte_blanche")

    if "remote_hosts" in existing_tables:
        remote_host_cols = {col["name"] for col in inspector.get_columns("remote_hosts")}
        if "jump_host" in remote_host_cols:
            op.drop_column("remote_hosts", "jump_host")

    if "ssh_credentials" in existing_tables:
        op.drop_table("ssh_credentials")

    if "event_sensors" in existing_tables:
        op.drop_table("event_sensors")
