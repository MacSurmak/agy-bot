"""Widen google_accounts.slug to 255 and normalize legacy slugs to account emails.

Revision ID: 0004_account_email_slug
Revises: 0003_reply_to_message_id
Create Date: 2026-11-25 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_account_email_slug"
down_revision: str | None = "0003_reply_to_message_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "google_accounts" not in set(inspector.get_table_names()):
        return

    op.alter_column(
        "google_accounts",
        "slug",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.execute(
        sa.text(
            "UPDATE google_accounts "
            "SET slug = lower(trim(email_label)), email_label = lower(trim(email_label)) "
            "WHERE position('@' in email_label) > 0 "
            "AND lower(trim(email_label)) NOT IN ("
            "  SELECT g2.slug FROM google_accounts g2 WHERE g2.id <> google_accounts.id"
            ")"
        )
    )


def downgrade() -> None:
    op.alter_column(
        "google_accounts",
        "slug",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
