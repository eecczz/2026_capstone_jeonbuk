"""Qdrant 적재 실패 사이트 페이지 재처리 — BFS 없이 PG URL 만 재fetch + 재적재.

배경:
- 4/13~4/28 시기 풀크롤 페이지 중 ~104K 가 PG status=success 인데 Qdrant 청크 0건
- BGE M3 8192 토큰 limit (당시 truncate 픽스 전) 으로 임베딩 실패가 원인 추정
- 현재는 truncate 적용 상태라 재시도 시 적재 성공해야 함

접근:
- BFS rediscovery 안 함 (시드 URL 결정 위험 + crawler_sites.py 변경 필요)
- PG 의 기존 URL list 만으로 fetch + 재적재 (정확한 URL 풀)
- 각 URL: Crawl4AI fetch → save_docs_to_vector_db → PG 메타 업데이트

사용:
  PYTHONPATH=/app/backend python3 scripts/reingest_failed_qdrant.py [--sites SITE1,SITE2] [--limit N] [--concurrency 5]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reingest")

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://admin:sprint26!@localhost:5432/customui"
)

DEFAULT_TARGET_SITES = [
    "gochang_county", "gimje_city", "jeongeup_city", "jinan_county",
    "sunchang_county", "jangsu_county", "imsil_county", "namwon_city",
    "iksan_city", "muju_county", "buan_county",
    "policy_jb", "stat_jeonbuk", "jeonju_city",
]


def fetch_pg_urls(sites: list[str], limit: Optional[int] = None) -> list[dict]:
    """PG 에서 target 사이트의 success/unchanged URL list 추출."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    placeholders = ",".join(["%s"] * len(sites))
    q = f"""
        SELECT url, site_code, institution, category, title
        FROM crawled_page
        WHERE status IN ('success', 'unchanged')
          AND site_code IN ({placeholders})
        ORDER BY site_code, url
    """
    params = list(sites)
    if limit:
        q += " LIMIT %s"
        params.append(limit)
    cur.execute(q, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


async def process_url(row: dict, sem: asyncio.Semaphore, stats: dict, request, save_fn):
    """단일 URL: fetch → chunks → save_docs_to_vector_db → PG upsert."""
    from langchain_core.documents import Document
    from open_webui.models.crawler import CrawledPages
    from open_webui.tasks.crawler import _load_page, _compute_content_hash

    url = row["url"]
    site_code = row["site_code"]

    async with sem:
        try:
            docs = await _load_page(url, site_code)
        except Exception as e:
            log.debug(f"load failed {url}: {e}")
            stats["fetch_failed"] += 1
            return

        if not docs:
            stats["empty_docs"] += 1
            return

        # save_docs_to_vector_db: metadata 구성
        metadata = {
            "url": url,
            "source": url,
            "title": row.get("title") or "",
            "institution": row.get("institution") or "",
            "category": row.get("category") or "",
            "site_code": site_code,
            "crawled_at": int(time.time()),
            "name": row.get("title") or url,
        }

        try:
            await asyncio.to_thread(
                save_fn,
                request,
                docs,
                "jeonbuk_gov",
                metadata,
                False,  # overwrite
                True,   # split
                True,   # add
                None,   # user
            )
        except Exception as e:
            log.warning(f"save failed {url}: {e}")
            stats["save_failed"] += 1
            return

        # PG 메타 갱신 (status=success 유지, content_hash 새로 계산)
        try:
            content_hash = _compute_content_hash(docs)
            CrawledPages.upsert(
                url=url,
                site_code=site_code,
                institution=row.get("institution") or "",
                category=row.get("category") or "",
                title=row.get("title") or "",
                content_hash=content_hash,
                http_etag=None,
                http_last_modified=None,
                status="success",
                chunks_count=len(docs),
                content_changed=True,
            )
        except Exception as e:
            log.debug(f"pg update failed {url}: {e}")

        stats["reingested"] += 1
        stats["chunks_total"] += len(docs)


async def amain():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", help="comma-separated site codes (default: target list)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=5)
    args = ap.parse_args()

    sites = args.sites.split(",") if args.sites else DEFAULT_TARGET_SITES
    log.info(f"Target sites: {sites}")

    sys.path.insert(0, "/app/backend")

    # OWI request 객체를 흉내내야 함 — config 와 state.main_loop 가 필요
    # 실제 main app 의 state 를 가져오는 fake request 만들기
    import open_webui.main as owi_main
    from types import SimpleNamespace
    fake_request = SimpleNamespace(
        app=owi_main.app,
        state=SimpleNamespace(),
    )

    from open_webui.routers.retrieval import save_docs_to_vector_db

    rows = fetch_pg_urls(sites, args.limit)
    log.info(f"PG returned {len(rows):,} URLs to reingest")

    sem = asyncio.Semaphore(args.concurrency)
    stats = {
        "reingested": 0, "chunks_total": 0,
        "fetch_failed": 0, "empty_docs": 0, "save_failed": 0,
    }
    t0 = time.time()
    BATCH = 100
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        await asyncio.gather(*(
            process_url(r, sem, stats, fake_request, save_docs_to_vector_db)
            for r in batch
        ))
        elapsed = time.time() - t0
        rate = (i + len(batch)) / max(elapsed, 0.01)
        eta = (len(rows) - i - len(batch)) / max(rate, 0.01)
        log.info(
            f"progress: total={i + len(batch):,}/{len(rows):,} "
            f"ok={stats['reingested']:,} chunks={stats['chunks_total']:,} "
            f"fetch_fail={stats['fetch_failed']} save_fail={stats['save_failed']} "
            f"rate={rate:.1f}/s eta={eta/60:.0f}min"
        )

    log.info(f"DONE total stats={stats} elapsed={(time.time()-t0)/60:.1f}min")
    return 0


def main():
    sys.exit(asyncio.run(amain()) or 0)


if __name__ == "__main__":
    main()
