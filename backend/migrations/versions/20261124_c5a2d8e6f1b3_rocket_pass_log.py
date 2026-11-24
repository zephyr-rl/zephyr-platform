"""tools: a step-by-step log on each Rocket Pass check

Revision ID: c5a2d8e6f1b3
Revises: b3e9c1d7a4f2
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c5a2d8e6f1b3"
down_revision = "b3e9c1d7a4f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rp_checks",
        sa.Column("log", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("rp_checks", "log")
