"""backups, app_settings e devices.last_backup_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("last_backup_at", sa.DateTime(), nullable=True))

    op.create_table(
        "backups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "device_id",
            sa.Integer(),
            sa.ForeignKey("devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("device_mac", sa.String(12), nullable=False),
        sa.Column("device_name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("firmware_version", sa.String(64), nullable=True),
        sa.Column("vid", sa.Integer(), nullable=True),
        sa.Column("release", sa.String(64), nullable=True),
        sa.Column("arch", sa.String(16), nullable=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_backups_device_id", "backups", ["device_id"])
    op.create_index(
        "ix_backups_mac_kind_created", "backups", ["device_mac", "kind", "created_at"]
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index("ix_backups_mac_kind_created", table_name="backups")
    op.drop_index("ix_backups_device_id", table_name="backups")
    op.drop_table("backups")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("last_backup_at")
