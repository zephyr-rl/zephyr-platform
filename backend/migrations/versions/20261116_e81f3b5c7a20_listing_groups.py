"""listing groups (cross-platform cards)

Revision ID: e81f3b5c7a20
Revises: d2a7c4e19b03
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e81f3b5c7a20"
down_revision = "d2a7c4e19b03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "listing_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "store_id",
            sa.Integer(),
            sa.ForeignKey("stores.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("pool_id", sa.Integer(), sa.ForeignKey("stock_pools.id", ondelete="RESTRICT")),
        sa.Column("restock_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("restock_target", sa.Integer()),
        sa.Column("auto_resume", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("vault_additional_info", sa.Text()),
        sa.Column("include_email_details", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "devices", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("legacy_id", sa.String(120)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("store_id", "legacy_id"),
    )
    op.create_index("ix_listing_groups_store_id", "listing_groups", ["store_id"])
    op.add_column(
        "listings",
        sa.Column(
            "group_id", sa.Integer(), sa.ForeignKey("listing_groups.id", ondelete="SET NULL")
        ),
    )
    op.add_column("listings", sa.Column("device", sa.String(20)))
    op.create_index("ix_listings_group_id", "listings", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_listings_group_id", "listings")
    op.drop_column("listings", "device")
    op.drop_column("listings", "group_id")
    op.drop_index("ix_listing_groups_store_id", "listing_groups")
    op.drop_table("listing_groups")
