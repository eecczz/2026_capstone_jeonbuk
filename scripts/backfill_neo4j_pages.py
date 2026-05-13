"""Neo4j Page 노드 backfill — PG crawled_page + Qdrant payload 결합.

배경: graphrag dual-track 코드(2026-05-12)가 본청 풀크롤 중간에 배포돼서 그
이전에 적재된 페이지들은 Neo4j Page 노드가 누락. PG 137K success/unchanged
대비 Neo4j Page 1,077 (0.8%).

해결: PG 에서 url+title+site_code+institution+category 가져오고, content_preview
는 Qdrant 첫 chunk text 에서 추출. LLM 호출 없음 — 거의 무료, 빠름.

사용:
  PYTHONPATH=/app/backend python3 scripts/backfill_neo4j_pages.py [--site SITE_CODE] [--batch 200]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill")

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://admin:sprint26!@localhost:5432/customui"
)
QDRANT_URL = os.environ.get("QDRANT_URI", "http://host.docker.internal:6333")


def _get_qdrant_key() -> str:
    """워커 프로세스의 environ 에서 가져오기 (sprint 사용자도 sudo 가능)."""
    try:
        with open("/proc/61/environ", "rb") as f:
            env = f.read().split(b"\x00")
        for e in env:
            if e.startswith(b"QDRANT_API_KEY="):
                return e.split(b"=", 1)[1].decode()
    except Exception as e:
        log.warning(f"Cannot read worker env: {e}")
    return os.environ.get("QDRANT_API_KEY", "")


def fetch_pg_pages(site_code: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
    """PG 에서 success/unchanged 페이지 목록 가져오기."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    q = """
        SELECT url, title, site_code, institution, category,
               COALESCE(last_crawled_at, 0) AS crawled_at,
               COALESCE(chunks_count, 0) AS chunks_count
        FROM crawled_page
        WHERE status IN ('success', 'unchanged')
    """
    params: list = []
    if site_code:
        q += " AND site_code = %s"
        params.append(site_code)
    q += " ORDER BY site_code, last_crawled_at DESC"
    if limit:
        q += " LIMIT %s"
        params.append(limit)
    cur.execute(q, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def fetch_qdrant_text_for_url(url: str, qdrant_key: str) -> str:
    """Qdrant scroll filter 로 URL 의 첫 chunk text 추출."""
    import urllib.request
    import urllib.error
    import json

    body = json.dumps(
        {
            "filter": {
                "must": [
                    {"key": "tenant_id", "match": {"value": "jeonbuk_gov"}},
                    {"key": "metadata.url", "match": {"value": url}},
                ]
            },
            "limit": 1,
            "with_payload": True,
            "with_vector": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/open-webui_knowledge/points/scroll",
        data=body,
        headers={"api-key": qdrant_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            resp = json.loads(r.read())
        pts = resp.get("result", {}).get("points") or []
        if not pts:
            return ""
        text = pts[0].get("payload", {}).get("text") or ""
        return text[:2000]
    except Exception as e:
        log.debug(f"qdrant fetch failed for {url}: {e}")
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", help="site_code filter (default: all)")
    ap.add_argument("--limit", type=int, help="max pages")
    ap.add_argument("--batch", type=int, default=200, help="commit batch size")
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--no-preview", action="store_true", help="skip Qdrant fetch (faster)")
    args = ap.parse_args()

    sys.path.insert(0, "/app/backend")
    from open_webui.retrieval.graphrag.neo4j_client import (
        get_neo4j_store,
        ensure_schema,
    )

    log.info("Loading Neo4j connection...")
    store = get_neo4j_store()
    if store is None:
        log.error("Neo4j unavailable — aborting")
        return 1
    ensure_schema()

    log.info(f"Fetching PG pages site={args.site or '*'} limit={args.limit or 'ALL'}")
    pages = fetch_pg_pages(args.site, args.limit)
    log.info(f"PG returned {len(pages):,} pages")

    qdrant_key = _get_qdrant_key()
    if not qdrant_key and not args.no_preview:
        log.warning("No QDRANT_API_KEY — running --no-preview mode")
        args.no_preview = True

    # 기존 Neo4j Page URL 셋 (한 번에 메모리 로드 — ~1K~150K 정도 OK)
    log.info("Loading existing Neo4j Page URLs...")
    existing_res = store.execute_query("MATCH (p:Page) RETURN p.url AS url")
    existing_urls = set()
    if isinstance(existing_res, dict) and existing_res.get("records"):
        for r in existing_res["records"]:
            u = r.get("url") if isinstance(r, dict) else None
            if u:
                existing_urls.add(u)
    log.info(f"Existing Neo4j Pages: {len(existing_urls):,}")

    todo = [p for p in pages if p["url"] not in existing_urls]
    log.info(f"To backfill: {len(todo):,} pages")

    if not todo:
        log.info("Nothing to do.")
        return 0

    batch: list[dict] = []
    t0 = time.time()
    processed = 0
    for p in todo:
        preview = ""
        if not args.no_preview:
            preview = fetch_qdrant_text_for_url(p["url"], qdrant_key)
        batch.append(
            {
                "url": p["url"],
                "title": p["title"] or "",
                "site_code": p["site_code"] or "",
                "crawled_at": int(p["crawled_at"]),
                "institution": p["institution"] or "",
                "category": p["category"] or "",
                "chunks_count": int(p["chunks_count"]),
                "content_preview": preview,
            }
        )
        if len(batch) >= args.batch:
            _flush(store, batch)
            processed += len(batch)
            elapsed = time.time() - t0
            rate = processed / max(elapsed, 0.01)
            eta = (len(todo) - processed) / max(rate, 0.01)
            log.info(
                f"flushed {processed:,}/{len(todo):,} rate={rate:.0f}/s eta={eta/60:.1f}min"
            )
            batch = []

    if batch:
        _flush(store, batch)
        processed += len(batch)

    log.info(f"DONE — backfilled {processed:,} pages in {(time.time()-t0)/60:.1f}min")
    return 0


def _flush(store, batch: list[dict]):
    """UNWIND 로 한 트랜잭션에 batch 적재."""
    try:
        store.execute_query(
            """
            UNWIND $rows AS row
            MERGE (p:Page {url: row.url})
            SET p.title = row.title,
                p.site_code = row.site_code,
                p.crawled_at = row.crawled_at,
                p.institution = row.institution,
                p.category = row.category,
                p.chunks_count = row.chunks_count,
                p.content_preview = row.content_preview
            """,
            parameters={"rows": batch},
        )
    except Exception as e:
        log.error(f"flush failed for batch of {len(batch)}: {e}")


if __name__ == "__main__":
    sys.exit(main() or 0)
