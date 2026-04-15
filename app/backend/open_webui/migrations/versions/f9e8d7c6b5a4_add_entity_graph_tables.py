"""Add entity / entity_mention / entity_relation tables for GraphRAG

Revision ID: f9e8d7c6b5a4
Revises: e7f8a9b0c1d2
Create Date: 2026-04-13 17:00:00.000000

GraphRAG 도입을 위한 경량 그래프 스토어를 PostgreSQL 위에 구축한다.
별도 Neo4j 없이 3개 테이블로 (엔티티 ↔ mention ↔ 청크) 관계를 표현한다.

- entity: 정규화된 엔티티 (기관, 프로그램, 담당자, 대상, 기간, 연락처, 장소, 카테고리)
- entity_mention: 어떤 청크(벡터 DB point id)에서 어떤 엔티티가 언급됐는지
- entity_relation: 엔티티 간 관계 트리플 (head → relation → tail, 출처 청크 포함)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from open_webui.migrations.util import get_existing_tables

revision: str = "f9e8d7c6b5a4"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(get_existing_tables())

    if "entity" not in existing:
        op.create_table(
            "entity",
            sa.Column("id", sa.String(), nullable=False, primary_key=True),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("canonical_name", sa.String(), nullable=False),
            sa.Column(
                "aliases",
                postgresql.ARRAY(sa.Text()),
                nullable=True,
                server_default="{}",
            ),
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column("created_at", sa.BigInteger(), nullable=True),
            sa.Column("updated_at", sa.BigInteger(), nullable=True),
        )
        op.create_index(
            "idx_entity_type_name", "entity", ["type", "canonical_name"]
        )
        op.create_index(
            "idx_entity_aliases_gin",
            "entity",
            ["aliases"],
            postgresql_using="gin",
        )

    if "entity_mention" not in existing:
        op.create_table(
            "entity_mention",
            sa.Column("id", sa.String(), nullable=False, primary_key=True),
            sa.Column("entity_id", sa.String(), nullable=False),
            sa.Column("chunk_id", sa.String(), nullable=False),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("span", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=True),
        )
        op.create_index(
            "idx_mention_entity", "entity_mention", ["entity_id"]
        )
        op.create_index(
            "idx_mention_chunk", "entity_mention", ["chunk_id"]
        )

    if "entity_relation" not in existing:
        op.create_table(
            "entity_relation",
            sa.Column("id", sa.String(), nullable=False, primary_key=True),
            sa.Column("head_id", sa.String(), nullable=False),
            sa.Column("relation_type", sa.String(), nullable=False),
            sa.Column("tail_id", sa.String(), nullable=False),
            sa.Column("source_chunk_id", sa.String(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=True),
        )
        op.create_index(
            "idx_rel_head_type",
            "entity_relation",
            ["head_id", "relation_type"],
        )
        op.create_index(
            "idx_rel_tail_type",
            "entity_relation",
            ["tail_id", "relation_type"],
        )


def downgrade() -> None:
    for idx in (
        "idx_rel_tail_type",
        "idx_rel_head_type",
    ):
        try:
            op.drop_index(idx, table_name="entity_relation")
        except Exception:
            pass
    try:
        op.drop_table("entity_relation")
    except Exception:
        pass

    for idx in ("idx_mention_chunk", "idx_mention_entity"):
        try:
            op.drop_index(idx, table_name="entity_mention")
        except Exception:
            pass
    try:
        op.drop_table("entity_mention")
    except Exception:
        pass

    for idx in ("idx_entity_aliases_gin", "idx_entity_type_name"):
        try:
            op.drop_index(idx, table_name="entity")
        except Exception:
            pass
    try:
        op.drop_table("entity")
    except Exception:
        pass
