"""Migrate stored workspace, profile, D-Bus, and host passport paths to FHS roots.

Revision ID: 0005_fhs_paths
Revises: 0004_account_email_slug
Create Date: 2026-11-26 05:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_fhs_paths"
down_revision: str | None = "0004_account_email_slug"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "forum_topics" in tables:
        op.execute(
            sa.text(
                "UPDATE forum_topics "
                "SET workspace_path = replace(workspace_path, '/srv/antigravity/app/runtime_data/workspaces', '/home/agy-agent/workspaces') "
                "WHERE workspace_path LIKE '/srv/antigravity/app/runtime_data/workspaces%'"
            )
        )

    if "google_accounts" in tables:
        op.execute(
            sa.text(
                "UPDATE google_accounts "
                "SET xdg_data_dir = replace(xdg_data_dir, '/srv/antigravity/app/runtime_data/profiles', '/home/agy-agent/profiles') "
                "WHERE xdg_data_dir LIKE '/srv/antigravity/app/runtime_data/profiles%'"
            )
        )
        op.execute(
            sa.text(
                "UPDATE google_accounts "
                "SET dbus_socket_path = replace(dbus_socket_path, '/srv/antigravity/app/runtime_data/profiles', '/home/agy-agent/profiles') "
                "WHERE dbus_socket_path LIKE '/srv/antigravity/app/runtime_data/profiles%'"
            )
        )
        op.execute(
            sa.text(
                "UPDATE google_accounts "
                "SET dbus_socket_path = replace(dbus_socket_path, '/srv/antigravity/app/runtime_data/run', '/run/agy') "
                "WHERE dbus_socket_path LIKE '/srv/antigravity/app/runtime_data/run%'"
            )
        )

    if "remote_hosts" in tables:
        op.execute(
            sa.text(
                "UPDATE remote_hosts "
                "SET rule_file_path = '/home/agy-agent/hosts/infra-' || alias || '.md' "
                "WHERE rule_file_path NOT LIKE '/home/agy-agent/hosts/%'"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "forum_topics" in tables:
        op.execute(
            sa.text(
                "UPDATE forum_topics "
                "SET workspace_path = replace(workspace_path, '/home/agy-agent/workspaces', '/srv/antigravity/app/runtime_data/workspaces') "
                "WHERE workspace_path LIKE '/home/agy-agent/workspaces%'"
            )
        )
