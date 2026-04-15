"""
크롤링된 홈페이지 페이지 메타데이터 테이블.

각 URL별로:
- 마지막 크롤링 시각
- 콘텐츠 해시 (SHA-256) — 변경 감지용
- HTTP ETag / Last-Modified — 증분 크롤링 판단용
- 소속 기관 / 카테고리 / 제목 — 사람이 읽기 위함
- 벡터 DB 청크 수 — 통계/디버깅용
- 상태 (success/error/skipped)

일별 증분 배치 실행 시:
1. CrawledPages.get_by_url(url)로 기존 레코드 조회
2. 있으면 HTTP HEAD → etag/last_modified 비교 → 같으면 skip
3. 다르면 페이지 재로드 → content_hash 비교 → 실제 변경시 재인덱싱
4. 새 URL이면 처음부터 인덱싱

기존 벡터 DB (ChromaDB 컬렉션 "jeonbuk_gov") 와 병행 사용.
"""

import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Integer, String, Text, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from open_webui.internal.db import Base, get_db, get_db_context

log = logging.getLogger(__name__)


####################
# CrawledPage DB Schema
####################


class CrawledPage(Base):
    __tablename__ = "crawled_page"

    id = Column(String, primary_key=True, unique=True)
    url = Column(Text, unique=True, nullable=False, index=True)

    site_code = Column(String, index=True)  # "jeonbuk_main", "hrd_jeonbuk" 등
    institution = Column(String)  # "전북특별자치도 인재개발원"
    category = Column(String, index=True)  # "교육", "공지", "행사", "지원사업" 등
    title = Column(Text, nullable=True)

    content_hash = Column(String, nullable=True)  # SHA-256 of page content
    http_etag = Column(String, nullable=True)
    http_last_modified = Column(String, nullable=True)

    published_at = Column(BigInteger, nullable=True)  # 게시일 (epoch)
    first_crawled_at = Column(BigInteger)
    last_crawled_at = Column(BigInteger)
    last_changed_at = Column(BigInteger, nullable=True)

    status = Column(String, default="success")  # success / error / skipped / unchanged
    error_message = Column(Text, nullable=True)
    chunks_count = Column(Integer, default=0)


class CrawledPageModel(BaseModel):
    id: str
    url: str
    site_code: Optional[str] = None
    institution: Optional[str] = None
    category: Optional[str] = None
    title: Optional[str] = None
    content_hash: Optional[str] = None
    http_etag: Optional[str] = None
    http_last_modified: Optional[str] = None
    published_at: Optional[int] = None
    first_crawled_at: Optional[int] = None
    last_crawled_at: Optional[int] = None
    last_changed_at: Optional[int] = None
    status: Optional[str] = "success"
    error_message: Optional[str] = None
    chunks_count: Optional[int] = 0

    model_config = ConfigDict(from_attributes=True)


####################
# Data Access
####################


class CrawledPagesTable:
    def get_by_url(self, url: str) -> Optional[CrawledPageModel]:
        try:
            with get_db_context() as db:
                row = db.query(CrawledPage).filter(CrawledPage.url == url).first()
                if not row:
                    return None
                return CrawledPageModel.model_validate(row)
        except Exception as e:
            log.exception(f"CrawledPagesTable.get_by_url failed: {e}")
            return None

    def list_by_site(
        self, site_code: str, limit: int = 1000
    ) -> list[CrawledPageModel]:
        try:
            with get_db_context() as db:
                rows = (
                    db.query(CrawledPage)
                    .filter(CrawledPage.site_code == site_code)
                    .limit(limit)
                    .all()
                )
                return [CrawledPageModel.model_validate(r) for r in rows]
        except Exception as e:
            log.exception(f"CrawledPagesTable.list_by_site failed: {e}")
            return []

    def upsert(
        self,
        url: str,
        site_code: str,
        institution: Optional[str] = None,
        category: Optional[str] = None,
        title: Optional[str] = None,
        content_hash: Optional[str] = None,
        http_etag: Optional[str] = None,
        http_last_modified: Optional[str] = None,
        published_at: Optional[int] = None,
        status: str = "success",
        error_message: Optional[str] = None,
        chunks_count: int = 0,
        content_changed: bool = True,
    ) -> Optional[CrawledPageModel]:
        """URL 기준 upsert. content_changed=True면 last_changed_at 갱신."""
        now = int(time.time())
        # PostgreSQL text 컬럼은 NUL 바이트(0x00) 를 거부한다.
        # 바이너리 파일(.hwpx, .pdf 등)을 크롤링할 때 title 에 NUL이 섞일 수 있음.
        def _sanitize(s: Optional[str]) -> Optional[str]:
            if s is None:
                return None
            return s.replace("\x00", "")

        url = _sanitize(url) or ""
        institution = _sanitize(institution)
        category = _sanitize(category)
        title = _sanitize(title)
        content_hash = _sanitize(content_hash)
        http_etag = _sanitize(http_etag)
        http_last_modified = _sanitize(http_last_modified)
        error_message = _sanitize(error_message)
        try:
            with get_db_context() as db:
                row = db.query(CrawledPage).filter(CrawledPage.url == url).first()
                if row is None:
                    row = CrawledPage(
                        id=str(uuid.uuid4()),
                        url=url,
                        site_code=site_code,
                        institution=institution,
                        category=category,
                        title=title,
                        content_hash=content_hash,
                        http_etag=http_etag,
                        http_last_modified=http_last_modified,
                        published_at=published_at,
                        first_crawled_at=now,
                        last_crawled_at=now,
                        last_changed_at=now,
                        status=status,
                        error_message=error_message,
                        chunks_count=chunks_count,
                    )
                    db.add(row)
                else:
                    row.site_code = site_code or row.site_code
                    if institution is not None:
                        row.institution = institution
                    if category is not None:
                        row.category = category
                    if title is not None:
                        row.title = title
                    if content_hash is not None:
                        row.content_hash = content_hash
                    if http_etag is not None:
                        row.http_etag = http_etag
                    if http_last_modified is not None:
                        row.http_last_modified = http_last_modified
                    if published_at is not None:
                        row.published_at = published_at
                    row.last_crawled_at = now
                    if content_changed:
                        row.last_changed_at = now
                    row.status = status
                    row.error_message = error_message
                    if chunks_count:
                        row.chunks_count = chunks_count
                db.commit()
                db.refresh(row)
                return CrawledPageModel.model_validate(row)
        except OperationalError as e:
            # 테이블 자체가 없거나 스키마 불일치. 마이그레이션 미적용 의심.
            log.error(
                f"CrawledPagesTable.upsert: operational error for {url} "
                f"(likely missing table or schema mismatch). "
                f"Run 'alembic upgrade head'. Detail: {e}"
            )
            return None
        except Exception as e:
            log.exception(f"CrawledPagesTable.upsert failed for {url}: {e}")
            return None

    def mark_unchanged(self, url: str) -> None:
        try:
            with get_db_context() as db:
                row = db.query(CrawledPage).filter(CrawledPage.url == url).first()
                if row:
                    row.last_crawled_at = int(time.time())
                    row.status = "unchanged"
                    db.commit()
        except Exception as e:
            log.exception(f"CrawledPagesTable.mark_unchanged failed for {url}: {e}")

    def mark_error(self, url: str, site_code: str, error_message: str) -> None:
        try:
            with get_db_context() as db:
                row = db.query(CrawledPage).filter(CrawledPage.url == url).first()
                now = int(time.time())
                if row is None:
                    row = CrawledPage(
                        id=str(uuid.uuid4()),
                        url=url,
                        site_code=site_code,
                        first_crawled_at=now,
                        last_crawled_at=now,
                        status="error",
                        error_message=error_message[:2000],
                    )
                    db.add(row)
                else:
                    row.last_crawled_at = now
                    row.status = "error"
                    row.error_message = error_message[:2000]
                db.commit()
        except Exception as e:
            log.exception(f"CrawledPagesTable.mark_error failed for {url}: {e}")

    def get_stats(self) -> dict:
        """사이트별/카테고리별 통계."""
        try:
            with get_db_context() as db:
                total = db.query(func.count(CrawledPage.id)).scalar() or 0
                site_counts = (
                    db.query(CrawledPage.site_code, func.count(CrawledPage.id))
                    .group_by(CrawledPage.site_code)
                    .all()
                )
                status_counts = (
                    db.query(CrawledPage.status, func.count(CrawledPage.id))
                    .group_by(CrawledPage.status)
                    .all()
                )
                latest_crawl = (
                    db.query(func.max(CrawledPage.last_crawled_at)).scalar() or 0
                )
                return {
                    "total_pages": total,
                    "by_site": {k or "unknown": v for k, v in site_counts},
                    "by_status": {k or "unknown": v for k, v in status_counts},
                    "latest_crawl_at": latest_crawl,
                }
        except Exception as e:
            log.exception(f"CrawledPagesTable.get_stats failed: {e}")
            return {}

    def delete_by_site(self, site_code: str) -> int:
        """특정 사이트 레코드 전부 삭제 (재크롤링 전 초기화용)."""
        try:
            with get_db_context() as db:
                deleted = (
                    db.query(CrawledPage)
                    .filter(CrawledPage.site_code == site_code)
                    .delete(synchronize_session=False)
                )
                db.commit()
                return deleted or 0
        except Exception as e:
            log.exception(f"CrawledPagesTable.delete_by_site failed: {e}")
            return 0


CrawledPages = CrawledPagesTable()
