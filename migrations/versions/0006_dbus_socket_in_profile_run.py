"""Place per-account D-Bus session sockets inside each profile's XDG_RUNTIME_DIR.

Revision ID: 0006_dbus_socket_in_profile_run
Revises: 0005_fhs_paths
Create Date: 2026-11-26 06:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_dbus_socket_in_profile_run"
down_revision: str | None = "0005_fhs_paths"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "google_accounts" not in tables:
        return

    op.execute(
        sa.text(
            "UPDATE google_accounts "
            "SET dbus_socket_path = rtrim(xdg_data_dir, '/') || '/run/dbus.sock' "
            "WHERE dbus_socket_path NOT LIKE '%/run/dbus.sock'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "google_accounts" not in tables:
        return

    op.execute(
        sa.text(
            "UPDATE google_accounts "
            "SET dbus_socket_path = '/run/agy/bus_' || slug || '.sock' "
            "WHERE dbus_socket_path LIKE '%/run/dbus.sock'"
        )
    )
