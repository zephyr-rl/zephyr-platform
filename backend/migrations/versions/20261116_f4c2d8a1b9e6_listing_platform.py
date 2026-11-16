"""listings: platform (from the marketplace)

Revision ID: f4c2d8a1b9e6
Revises: e81f3b5c7a20
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4c2d8a1b9e6"
down_revision = "e81f3b5c7a20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("platform", sa.String(80)))


def downgrade() -> None:
    op.drop_column("listings", "platform")
