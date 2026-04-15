"""
crawled_page 백필 유틸리티.

Vector DB (Qdrant) 컬렉션에 있는 기존 크롤링 청크 메타데이터를 읽어서
PostgreSQL `crawled_page` 테이블에 retroactive로 INSERT 한다.

사용 시점:
- crawled_page 마이그레이션이 첫 크롤링 이후에 적용돼서 추적 테이블이 비어있는 경우
- 어떤 사유로든 추적 테이블과 vector DB가 어긋난 경우 복구
"""

import logging
from typing import Any, Optional

from open_webui.models.crawler import CrawledPages
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT

log = logging.getLogger(__name__)


def backfill_crawled_page_from_vector_db(
    collection_name: str,
    site_code_filter: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Vector DB 컬렉션의 청크 메타데이터를 crawled_page 테이블로 백필."""
    stats: dict[str, Any] = {
        "collection": collection_name,
        "total_chunks": 0,
        "unique_urls": 0,
        "upserted": 0,
        "skipped": 0,
        "failed": 0,
        "by_site": {},
    }

    log.info(
        f"backfill START: collection={collection_name} "
        f"site_filter={site_code_filter} dry_run={dry_run}"
    )

    try:
        result = VECTOR_DB_CLIENT.get(collection_name)
    except Exception as e:
        log.exception(f"VECTOR_DB_CLIENT.get failed for {collection_name}: {e}")
        stats["error"] = str(e)
        return stats

    if result is None:
        log.warning(f"backfill: collection {collection_name} returned None")
        stats["error"] = "collection_not_found"
        return stats

    ids_all = (result.ids or [[]])[0] if result.ids else []
    metadatas_all = (result.metadatas or [[]])[0] if result.metadatas else []
    stats["total_chunks"] = len(ids_all)

    if not metadatas_all:
        log.info(f"backfill: collection {collection_name} is empty")
        return stats

    # URL 단위로 group by
    url_groups: dict[str, list[dict]] = {}
    for meta in metadatas_all:
        if not isinstance(meta, dict):
            continue
        url = meta.get("url") or meta.get("source")
        if not url:
            continue
        url_groups.setdefault(url, []).append(meta)

    stats["unique_urls"] = len(url_groups)
    log.info(
        f"backfill: {stats['total_chunks']} chunks → {stats['unique_urls']} unique URLs"
    )

    for url, metas in url_groups.items():
        head = metas[0]
        site_code = head.get("site_code") or ""
        institution = head.get("institution")
        category = head.get("category")
        title = head.get("title")

        if site_code_filter and site_code != site_code_filter:
            stats["skipped"] += 1
            continue

        bs = stats["by_site"].setdefault(
            site_code or "_unknown", {"urls": 0, "chunks": 0}
        )
        bs["urls"] += 1
        bs["chunks"] += len(metas)

        if dry_run:
            stats["upserted"] += 1
            continue

        try:
            result_row = CrawledPages.upsert(
                url=url,
                site_code=site_code,
                institution=institution,
                category=category,
                title=title,
                content_hash=head.get("content_hash") or "",
                http_etag=None,  # vector DB에 없음, 복원 불가
                http_last_modified=None,
                status="success",
                chunks_count=len(metas),
                content_changed=False,
            )
            if result_row is None:
                stats["failed"] += 1
                log.warning(f"backfill upsert returned None for {url}")
            else:
                stats["upserted"] += 1
        except Exception as e:
            stats["failed"] += 1
            log.exception(f"backfill upsert failed for {url}: {e}")

    log.info(
        f"backfill DONE: collection={collection_name} "
        f"upserted={stats['upserted']} failed={stats['failed']} "
        f"by_site={stats['by_site']}"
    )
    return stats
