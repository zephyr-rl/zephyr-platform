"""listings: daily schedule that pauses an offer and turns it back on

Revision ID: d4f7a2c9e1b5
Revises: c5a2d8e6f1b3
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f7a2c9e1b5"
down_revision = "c5a2d8e6f1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("schedule_off", sa.Time(), nullable=True))
    op.add_column("listings", sa.Column("schedule_on", sa.Time(), nullable=True))
    op.add_column("listings", sa.Column("schedule_applied", sa.String(3), nullable=True))
    op.add_column(
        "listings", sa.Column("schedule_last_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("listings", sa.Column("schedule_note", sa.Text(), nullable=True))


def downgrade() -> None:
    for name in (
        "schedule_note",
        "schedule_last_at",
        "schedule_applied",
        "schedule_on",
        "schedule_off",
    ):
        op.drop_column("listings", name)
