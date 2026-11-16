"""modules: no store default target for restock; Deliveries always on

Revision ID: a7d3e2b04c51
Revises: f4c2d8a1b9e6
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op

revision = "a7d3e2b04c51"
down_revision = "f4c2d8a1b9e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Listings without their own target keep 1 (owner's decision): the store default goes.
    op.execute(
        "UPDATE store_modules SET settings = settings - 'target_quantity', version = version + 1 "
        "WHERE module_key = 'restock' AND settings ? 'target_quantity'"
    )
    # Deliveries cannot be turned off any more (orders feed every other module).
    op.execute(
        "UPDATE store_modules SET enabled = true, version = version + 1 "
        "WHERE module_key = 'deliveries' AND enabled = false"
    )


def downgrade() -> None:
    pass
