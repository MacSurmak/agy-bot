"""Add reply_to_message_id column to scheduled_tasks and event_sensors.

Revision ID: 0003_reply_to_message_id
Revises: 0002_ssh_credentials_and_sudo
Create Date: 2026-11-24 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_reply_to_message_id"
down_revision: str | None = "0002_ssh_credentials_and_sudo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "scheduled_tasks" in existing_tables:
        cols = {col["name"] for col in inspector.get_columns("scheduled_tasks")}
        if "reply_to_message_id" not in cols:
            op.add_column(
                "scheduled_tasks",
                sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
            )

    if "event_sensors" in existing_tables:
        cols = {col["name"] for col in inspector.get_columns("event_sensors")}
        if "reply_to_message_id" not in cols:
            op.add_column(
                "event_sensors",
                sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "event_sensors" in existing_tables:
        cols = {col["name"] for col in inspector.get_columns("event_sensors")}
        if "reply_to_message_id" in cols:
            op.drop_column("event_sensors", "reply_to_message_id")

    if "scheduled_tasks" in existing_tables:
        cols = {col["name"] for col in inspector.get_columns("scheduled_tasks")}
        if "reply_to_message_id" in cols:
            op.drop_column("scheduled_tasks", "reply_to_message_id")
