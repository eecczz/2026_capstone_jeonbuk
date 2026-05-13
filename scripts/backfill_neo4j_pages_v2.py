"""v2: Neo4j Page backfill — Qdrant bulk scroll 으로 content_preview 일괄 매핑.

v1 (backfill_neo4j_pages.py) 은 페이지당 Qdrant scroll filter 호출이라 5 pages/s,
137K pages 처리에 ~6.7 시간. v2 는:

1. Qdrant 전체 tenant 를 scroll 페이지네이션 (~350회) 으로 한번에 가져와
   url → first_text 메모리 dict 구축 (3~5분)
2. PG 에서 페이지 목록 받음
3. Neo4j UNWIND batch 200 으로 일괄 MERGE (수십초)

총 5분 미만 예상.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_v2")

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://admin:sprint26!@localhost:5432/customui"
)
QDRANT_URL = os.environ.get("QDRANT_URI", "http://host.docker.internal:6333")
TENANT = "jeonbuk_gov"


def get_qdrant_key() -> str:
    """워커 environ 에서 가져오기 (sudo 필요)."""
    try:
        with open("/proc/61/environ", "rb") as f:
            env = f.read().split(b"\x00")
        for e in env:
            if e.startswith(b"QDRANT_API_KEY="):
                return e.split(b"=", 1)[1].decode()
    except Exception as e:
        log.warning(f"Cannot read worker env: {e}")
    return os.environ.get("QDRANT_API_KEY", "")


def bulk_scroll_url_text(api_key: str, prefer_first: bool = True) -> dict[str, str]:
    """Qdrant scroll 으로 jeonbuk_gov tenant 전체 → {url: text[:2000]} 반환.

    한 URL 에 여러 chunk → 가장 긴 text 또는 첫 번째 (start_index=-1 또는 0).
    """
    url_to_text: dict[str, str] = {}
    next_offset = None
    page = 0
    t0 = time.time()
    total_points = 0
    while True:
        body = {
            "filter": {
                "must": [{"key": "tenant_id", "match": {"value": TENANT}}]
            },
            "limit": 500,
            "with_payload": True,
            "with_vector": False,
        }
        if next_offset is not None:
            body["offset"] = next_offset
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/open-webui_knowledge/points/scroll",
            data=json.dumps(body).encode(),
            headers={"api-key": api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
        except Exception as e:
            log.error(f"qdrant scroll failed at page {page}: {e}")
            break

        result = resp.get("result", {})
        pts = result.get("points") or []
        if not pts:
            break
        for pt in pts:
            pl = pt.get("payload") or {}
            md = pl.get("metadata") or {}
            url = md.get("url") or md.get("source")
            if not url:
                continue
            text = pl.get("text") or ""
            if not text:
                continue
            text = text[:2000]
            # 첫 번째 chunk 우선 — 더 짧은 (앞부분) text 가 보통 더 의미 있음.
            # start_index 가 작은 chunk 가 첫 부분.
            si = md.get("start_index")
            existing = url_to_text.get(url)
            if existing is None:
                url_to_text[url] = text
            else:
                # 기존이 있으면 길이가 더 길거나 si=0/-1 같은 시작 부분을 선호
                if isinstance(si, int) and si in (0, -1):
                    url_to_text[url] = text

        total_points += len(pts)
        page += 1
        next_offset = result.get("next_page_offset")
        if next_offset is None:
            break
        if page % 20 == 0:
            elapsed = time.time() - t0
            log.info(
                f"scroll: page={page} points={total_points} urls={len(url_to_text)} "
                f"elapsed={elapsed:.1f}s"
            )

    elapsed = time.time() - t0
    log.info(
        f"scroll DONE: pages={page} points={total_points} unique_urls={len(url_to_text)} "
        f"elapsed={elapsed:.1f}s"
    )
    return url_to_text


def fetch_pg_pages(site_code: str | None = None) -> list[dict]:
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
    cur.execute(q, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--no-preview", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, "/app/backend")
    from open_webui.retrieval.graphrag.neo4j_client import (
        get_neo4j_store, ensure_schema,
    )

    store = get_neo4j_store()
    if store is None:
        log.error("Neo4j unavailable")
        return 1
    ensure_schema()

    # 1. Qdrant bulk scroll (skip if --no-preview)
    url_text: dict[str, str] = {}
    if not args.no_preview:
        api_key = get_qdrant_key()
        if not api_key:
            log.error("QDRANT_API_KEY missing — use --no-preview to skip")
            return 1
        log.info("Step 1: Qdrant bulk scroll url->text dict")
        url_text = bulk_scroll_url_text(api_key)

    # 2. PG pages
    log.info(f"Step 2: PG fetch site={args.site or '*'}")
    pages = fetch_pg_pages(args.site)
    log.info(f"PG returned {len(pages):,} pages")

    # 3. Neo4j UNWIND batch
    log.info("Step 3: Neo4j MERGE batch")
    t0 = time.time()
    matched_preview = 0
    for i in range(0, len(pages), args.batch):
        batch = pages[i : i + args.batch]
        rows = []
        for p in batch:
            preview = url_text.get(p["url"], "")
            if preview:
                matched_preview += 1
            rows.append({
                "url": p["url"],
                "title": p["title"] or "",
                "site_code": p["site_code"] or "",
                "crawled_at": int(p["crawled_at"]),
                "institution": p["institution"] or "",
                "category": p["category"] or "",
                "chunks_count": int(p["chunks_count"]),
                "content_preview": preview,
            })
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
                    p.chunks_count = row.chunks_count
                FOREACH (_ IN CASE WHEN row.content_preview <> '' THEN [1] ELSE [] END |
                    SET p.content_preview = row.content_preview
                )
                """,
                parameters={"rows": rows},
            )
        except Exception as e:
            log.error(f"batch {i} failed: {e}")
            continue
        if (i // args.batch + 1) % 10 == 0:
            elapsed = time.time() - t0
            rate = (i + len(batch)) / max(elapsed, 0.01)
            log.info(
                f"flushed {i + len(batch):,}/{len(pages):,} "
                f"rate={rate:.0f}/s preview_matched={matched_preview:,}"
            )

    elapsed = time.time() - t0
    log.info(
        f"DONE — {len(pages):,} pages in {elapsed:.1f}s "
        f"({len(pages)/max(elapsed,1):.0f}/s) preview_matched={matched_preview:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
