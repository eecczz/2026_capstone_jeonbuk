"""Add crawled_page table for Jeonbuk homepage crawler — chatbot_db root revision

Revision ID: e7f8a9b0c1d2
Revises: None  (chatbot_db root — OWI customui 의 alembic head 와 독립)
Create Date: 2026-04-12 12:00:00.000000

도청 + 직속기관 홈페이지를 일별 배치로 크롤링하며 각 페이지의 상태를 추적한다.
별도 chatbot_db 에 운영 (Phase 1a~1c 분리). customui (OWI 본체) 의 alembic
head 와 독립 chain — down_revision 을 None 으로 변경해 chatbot 의 root revision
으로 한다. 향후 chatbot 측 새 마이그레이션은 이 위에 chain.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from open_webui.migrations.util import get_existing_tables

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(get_existing_tables())

    if "crawled_page" not in existing_tables:
        op.create_table(
            "crawled_page",
            sa.Column("id", sa.String(), nullable=False, primary_key=True),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("site_code", sa.String(), nullable=True),
            sa.Column("institution", sa.String(), nullable=True),
            sa.Column("category", sa.String(), nullable=True),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("content_hash", sa.String(), nullable=True),
            sa.Column("http_etag", sa.String(), nullable=True),
            sa.Column("http_last_modified", sa.String(), nullable=True),
            sa.Column("published_at", sa.BigInteger(), nullable=True),
            sa.Column("first_crawled_at", sa.BigInteger(), nullable=True),
            sa.Column("last_crawled_at", sa.BigInteger(), nullable=True),
            sa.Column("last_changed_at", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.String(), nullable=True, server_default="success"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("chunks_count", sa.Integer(), nullable=True, server_default="0"),
            sa.UniqueConstraint("url", name="uq_crawled_page_url"),
        )
        op.create_index(
            "idx_crawled_page_url",
            "crawled_page",
            ["url"],
        )
        op.create_index(
            "idx_crawled_page_site_code",
            "crawled_page",
            ["site_code"],
        )
        op.create_index(
            "idx_crawled_page_category",
            "crawled_page",
            ["category"],
        )


def downgrade() -> None:
    try:
        op.drop_index("idx_crawled_page_category", table_name="crawled_page")
    except Exception:
        pass
    try:
        op.drop_index("idx_crawled_page_site_code", table_name="crawled_page")
    except Exception:
        pass
    try:
        op.drop_index("idx_crawled_page_url", table_name="crawled_page")
    except Exception:
        pass
    op.drop_table("crawled_page")
