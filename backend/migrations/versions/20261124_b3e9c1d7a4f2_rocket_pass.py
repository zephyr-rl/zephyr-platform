"""tools: Rocket Pass check (saved Epic accounts and check history)

Revision ID: b3e9c1d7a4f2
Revises: a7d3e2b04c51
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b3e9c1d7a4f2"
down_revision = "a7d3e2b04c51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rp_epic_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("refresh_token", sa.Text()),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True)),
        sa.Column("xbox_refresh_token", sa.Text()),
        sa.Column("xbox_gamertag", sa.String(120)),
        sa.Column("last_result", postgresql.JSONB()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("account_id", name="uq_rp_epic_accounts_account_id"),
    )
    kind = postgresql.ENUM("check", "claim", "xbox", name="rp_check_kind")
    state = postgresql.ENUM("waiting", "queued", "running", "done", "failed", name="rp_check_state")
    op.create_table(
        "rp_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "epic_account_id",
            sa.Integer(),
            sa.ForeignKey(
                "rp_epic_accounts.id",
                ondelete="SET NULL",
                name="fk_rp_checks_epic_account_id_rp_epic_accounts",
            ),
        ),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("kind", kind, nullable=False),
        sa.Column("state", state, nullable=False),
        sa.Column("progress", sa.String(200)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("xbox_user_code", sa.String(32)),
        sa.Column("xbox_verification_uri", sa.String(300)),
        sa.Column("xbox_device_code", sa.Text()),
        sa.Column("xbox_expires_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_rp_checks_epic_account_id", "rp_checks", ["epic_account_id"])


def downgrade() -> None:
    op.drop_index("ix_rp_checks_epic_account_id", table_name="rp_checks")
    op.drop_table("rp_checks")
    op.drop_table("rp_epic_accounts")
    sa.Enum(name="rp_check_state").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="rp_check_kind").drop(op.get_bind(), checkfirst=True)
