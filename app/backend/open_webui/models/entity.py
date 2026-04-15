"""
GraphRAG 엔티티 그래프 모델.

경량 그래프 스토어를 PostgreSQL 위에 구축. Neo4j 등 별도 그래프 DB 없이
3개 테이블(entity / entity_mention / entity_relation)로 표현한다.

엔티티 타입:
- institution  : 기관 (전북특별자치도, 농업기술원, 인재개발원)
- program      : 프로그램/사업 (정보화 교육, 농업인 지원금)
- person       : 담당자 (이름 + 직책)
- audience     : 대상 (도민, 농업인, 공무원, 어린이)
- period       : 기간 (2026-04-01~2026-12-31)
- contact      : 연락처 (phone/email)
- location     : 장소 (주소/건물)
- category     : 카테고리 (교육, 농업, 문화예술)

관계 타입:
- operates     : (institution, operates, program)
- staffs       : (program, staffs, person)
- targets      : (program, targets, audience)
- scheduled    : (program, scheduled, period)
- contact_of   : (institution|person, contact_of, contact)
- located_at   : (institution|program, located_at, location)
- categorized  : (program|institution, categorized, category)
- child_of     : (institution, child_of, institution)  # 상위 기관 관계
"""

import logging
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Float, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from open_webui.internal.db import Base, get_db_context

log = logging.getLogger(__name__)


####################
# SQLAlchemy 스키마
####################


class Entity(Base):
    __tablename__ = "entity"

    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)  # institution, program, person, ...
    canonical_name = Column(String, nullable=False)
    aliases = Column(ARRAY(Text), nullable=True, default=list)
    meta = Column("metadata", JSONB, nullable=True)  # Python 'metadata' 충돌 회피
    created_at = Column(BigInteger, nullable=True)
    updated_at = Column(BigInteger, nullable=True)


class EntityMention(Base):
    __tablename__ = "entity_mention"

    id = Column(String, primary_key=True)
    entity_id = Column(String, nullable=False)
    chunk_id = Column(String, nullable=False)
    url = Column(Text, nullable=True)
    span = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(BigInteger, nullable=True)


class EntityRelation(Base):
    __tablename__ = "entity_relation"

    id = Column(String, primary_key=True)
    head_id = Column(String, nullable=False)
    relation_type = Column(String, nullable=False)
    tail_id = Column(String, nullable=False)
    source_chunk_id = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(BigInteger, nullable=True)


####################
# Pydantic 모델
####################


class EntityModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    type: str
    canonical_name: str
    aliases: Optional[list[str]] = None
    meta: Optional[dict[str, Any]] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class EntityMentionModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    entity_id: str
    chunk_id: str
    url: Optional[str] = None
    span: Optional[str] = None
    confidence: Optional[float] = None
    created_at: Optional[int] = None


class EntityRelationModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    head_id: str
    relation_type: str
    tail_id: str
    source_chunk_id: Optional[str] = None
    confidence: Optional[float] = None
    created_at: Optional[int] = None


####################
# 데이터 액세스
####################


class _EntitiesTable:
    """엔티티 정규화 인덱스 + CRUD."""

    def upsert_by_name(
        self,
        type: str,
        canonical_name: str,
        aliases: Optional[list[str]] = None,
        meta: Optional[dict] = None,
    ) -> Optional[EntityModel]:
        """canonical_name + type 기준 upsert. 이미 있으면 aliases/meta 병합."""
        now = int(time.time())
        try:
            with get_db_context() as db:
                row = (
                    db.query(Entity)
                    .filter(
                        Entity.type == type,
                        Entity.canonical_name == canonical_name,
                    )
                    .first()
                )
                if row is None:
                    row = Entity(
                        id=str(uuid.uuid4()),
                        type=type,
                        canonical_name=canonical_name,
                        aliases=list(set(aliases or [])),
                        meta=meta or {},
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(row)
                else:
                    if aliases:
                        merged = list(set((row.aliases or []) + aliases))
                        row.aliases = merged
                    if meta:
                        merged_meta = dict(row.meta or {})
                        merged_meta.update(meta)
                        row.meta = merged_meta
                    row.updated_at = now
                db.commit()
                db.refresh(row)
                return EntityModel.model_validate(row)
        except Exception as e:
            log.exception(
                f"Entities.upsert_by_name failed "
                f"({type}={canonical_name}): {e}"
            )
            return None

    def find_by_name_or_alias(
        self, type: Optional[str], name: str, limit: int = 10
    ) -> list[EntityModel]:
        """canonical_name 부분일치 + aliases 포함 검색."""
        try:
            with get_db_context() as db:
                q = db.query(Entity)
                if type:
                    q = q.filter(Entity.type == type)
                # PostgreSQL ARRAY contains 또는 LIKE
                q = q.filter(
                    (Entity.canonical_name.ilike(f"%{name}%"))
                    | (Entity.aliases.any(name))
                )
                rows = q.limit(limit).all()
                return [EntityModel.model_validate(r) for r in rows]
        except Exception as e:
            log.exception(f"Entities.find failed: {e}")
            return []

    def get_by_id(self, entity_id: str) -> Optional[EntityModel]:
        try:
            with get_db_context() as db:
                row = db.query(Entity).filter(Entity.id == entity_id).first()
                return EntityModel.model_validate(row) if row else None
        except Exception as e:
            log.exception(f"Entities.get_by_id failed: {e}")
            return None


class _MentionsTable:
    def insert(
        self,
        entity_id: str,
        chunk_id: str,
        url: Optional[str] = None,
        span: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> Optional[EntityMentionModel]:
        try:
            with get_db_context() as db:
                row = EntityMention(
                    id=str(uuid.uuid4()),
                    entity_id=entity_id,
                    chunk_id=chunk_id,
                    url=url,
                    span=span,
                    confidence=confidence,
                    created_at=int(time.time()),
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                return EntityMentionModel.model_validate(row)
        except Exception as e:
            log.exception(f"Mentions.insert failed: {e}")
            return None

    def get_chunks_by_entity(
        self, entity_id: str, limit: int = 50
    ) -> list[str]:
        """한 엔티티가 언급된 chunk_id 목록."""
        try:
            with get_db_context() as db:
                rows = (
                    db.query(EntityMention.chunk_id)
                    .filter(EntityMention.entity_id == entity_id)
                    .limit(limit)
                    .all()
                )
                return [r[0] for r in rows]
        except Exception as e:
            log.exception(f"Mentions.get_chunks_by_entity failed: {e}")
            return []


class _RelationsTable:
    def insert(
        self,
        head_id: str,
        relation_type: str,
        tail_id: str,
        source_chunk_id: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> Optional[EntityRelationModel]:
        try:
            with get_db_context() as db:
                row = EntityRelation(
                    id=str(uuid.uuid4()),
                    head_id=head_id,
                    relation_type=relation_type,
                    tail_id=tail_id,
                    source_chunk_id=source_chunk_id,
                    confidence=confidence,
                    created_at=int(time.time()),
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                return EntityRelationModel.model_validate(row)
        except Exception as e:
            log.exception(f"Relations.insert failed: {e}")
            return None

    def neighbors(
        self,
        entity_id: str,
        relation_type: Optional[str] = None,
        direction: str = "out",  # out | in | both
        limit: int = 50,
    ) -> list[EntityRelationModel]:
        """한 엔티티의 1-hop 이웃 관계."""
        try:
            with get_db_context() as db:
                q = db.query(EntityRelation)
                if direction == "out":
                    q = q.filter(EntityRelation.head_id == entity_id)
                elif direction == "in":
                    q = q.filter(EntityRelation.tail_id == entity_id)
                else:
                    q = q.filter(
                        (EntityRelation.head_id == entity_id)
                        | (EntityRelation.tail_id == entity_id)
                    )
                if relation_type:
                    q = q.filter(EntityRelation.relation_type == relation_type)
                rows = q.limit(limit).all()
                return [EntityRelationModel.model_validate(r) for r in rows]
        except Exception as e:
            log.exception(f"Relations.neighbors failed: {e}")
            return []


Entities = _EntitiesTable()
Mentions = _MentionsTable()
Relations = _RelationsTable()
