"""chat threads: no_reply_needed

Revision ID: d2a7c4e19b03
Revises: c56665b3a2d4
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d2a7c4e19b03"
down_revision = "c56665b3a2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_threads",
        sa.Column("no_reply_needed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("chat_threads", "no_reply_needed")
