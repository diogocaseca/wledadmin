"""ota_updates

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ota_updates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "device_id",
            sa.Integer(),
            sa.ForeignKey("devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("device_mac", sa.String(12), nullable=False),
        sa.Column("device_name", sa.String(255), nullable=False),
        sa.Column("from_version", sa.String(64), nullable=True),
        sa.Column("to_version", sa.String(64), nullable=True),
        sa.Column("release_name", sa.String(64), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("steps", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ota_updates_device_id", "ota_updates", ["device_id"])
    op.create_index("ix_ota_updates_device_mac", "ota_updates", ["device_mac"])


def downgrade() -> None:
    op.drop_index("ix_ota_updates_device_mac", table_name="ota_updates")
    op.drop_index("ix_ota_updates_device_id", table_name="ota_updates")
    op.drop_table("ota_updates")
