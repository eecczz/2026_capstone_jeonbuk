"""Add crawl_target table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-06 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from open_webui.migrations.util import get_existing_tables

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(get_existing_tables())

    if "crawl_target" not in existing_tables:
        op.create_table(
            "crawl_target",
            sa.Column("id", sa.Text(), nullable=False, primary_key=True),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("label", sa.Text(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("max_depth", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("crawl_interval_hours", sa.Integer(), nullable=False, server_default="24"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("last_crawl_at", sa.BigInteger(), nullable=True),
            sa.Column("last_crawl_status", sa.Text(), nullable=True),
            sa.Column("last_crawl_page_count", sa.Integer(), nullable=True),
            sa.Column("collection_name", sa.Text(), nullable=True),
            sa.Column("meta", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
        )
        op.create_index("idx_crawl_target_is_active", "crawl_target", ["is_active"])
        op.create_index("idx_crawl_target_user_id", "crawl_target", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_crawl_target_user_id", table_name="crawl_target")
    op.drop_index("idx_crawl_target_is_active", table_name="crawl_target")
    op.drop_table("crawl_target")
