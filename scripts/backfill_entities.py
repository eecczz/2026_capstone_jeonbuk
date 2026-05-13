"""LLM 기반 entity/relation backfill — Neo4j Page 노드에 entity 추출 적용.

전제: scripts/backfill_neo4j_pages.py 로 Page 노드가 적재된 상태.

흐름:
1. Neo4j 에서 Entity 미연결 Page 조회 (LIMIT 으로 batch)
2. content_preview 가져옴 (Page property)
3. extract_entities_llm 호출 (LLM)
4. upsert_entities 로 적재
5. concurrent 처리 (asyncio.gather, default 5)

비용 가드:
- 페이지당 ~1500 tokens input, ~500 tokens output
- gpt-4o-mini: $0.0005/page → 100K 페이지 ~$50
- 더 비싼 모델 쓰지 마라

사용:
  PYTHONPATH=/app/backend python3 scripts/backfill_entities.py --limit 100  # 샘플
  PYTHONPATH=/app/backend python3 scripts/backfill_entities.py              # 전체
  PYTHONPATH=/app/backend python3 scripts/backfill_entities.py --site jeonbuk_main  # 본청만
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
log = logging.getLogger("backfill_ent")


async def process_page(page: dict, sem: asyncio.Semaphore, stats: dict):
    """단일 페이지: entity 추출 + Neo4j upsert."""
    from open_webui.retrieval.graphrag.entity_extractor import extract_entities_llm
    from open_webui.retrieval.graphrag.neo4j_client import upsert_entities

    async with sem:
        url = page["url"]
        title = page["title"] or ""
        text = page["content_preview"] or ""
        if not text.strip() or len(text) < 50:
            stats["skipped_short"] += 1
            return

        t0 = time.time()
        try:
            extracted = await extract_entities_llm(title=title, text=text)
        except Exception as e:
            log.warning(f"extract failed {url}: {e}")
            stats["failed"] += 1
            return

        if not extracted or not extracted.get("entities"):
            stats["skipped_empty"] += 1
            return

        try:
            r = await asyncio.to_thread(
                upsert_entities,
                page_url=url,
                entities=extracted["entities"],
                relations=extracted.get("relations") or [],
            )
            if r:
                stats["entities_added"] += r["entities"]
                stats["relations_added"] += r["relations"]
                stats["pages_done"] += 1
            else:
                stats["failed"] += 1
        except Exception as e:
            log.warning(f"upsert failed {url}: {e}")
            stats["failed"] += 1
        finally:
            elapsed = time.time() - t0
            stats["total_seconds"] += elapsed


async def amain():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", help="filter site_code")
    ap.add_argument("--limit", type=int, help="max pages")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    sys.path.insert(0, "/app/backend")
    from open_webui.retrieval.graphrag.neo4j_client import get_neo4j_store, ensure_schema

    store = get_neo4j_store()
    if store is None:
        log.error("Neo4j unavailable")
        return 1
    ensure_schema()

    # Entity 가 연결되지 않은 Page 조회 (MENTIONS edge 없는 것).
    where_site = ""
    params: dict = {"limit": args.batch}
    if args.site:
        where_site = "AND p.site_code = $site"
        params["site"] = args.site

    sem = asyncio.Semaphore(args.concurrency)
    stats = {
        "pages_done": 0,
        "entities_added": 0,
        "relations_added": 0,
        "skipped_short": 0,
        "skipped_empty": 0,
        "failed": 0,
        "total_seconds": 0.0,
    }
    overall_t0 = time.time()
    processed_total = 0
    max_total = args.limit

    while True:
        # batch 마다 Neo4j 에서 다음 페이지 set 가져옴 (Entity 미연결 + content_preview 있는 것)
        q = f"""
            MATCH (p:Page)
            WHERE NOT (p)-[:MENTIONS]->()
              AND p.content_preview IS NOT NULL
              AND size(p.content_preview) > 100
              {where_site}
            RETURN p.url AS url, p.title AS title, p.content_preview AS content_preview
            LIMIT $limit
        """
        res = store.execute_query(q, parameters=params)
        records = res.get("records") if isinstance(res, dict) else (res or [])
        pages = []
        for r in records:
            if isinstance(r, dict) and r.get("url"):
                pages.append(r)
        if not pages:
            log.info("No more pages to process.")
            break

        log.info(f"Processing batch of {len(pages)} pages (concurrency={args.concurrency})")
        await asyncio.gather(*(process_page(p, sem, stats) for p in pages))
        processed_total += len(pages)

        elapsed = time.time() - overall_t0
        rate = processed_total / max(elapsed, 0.01)
        log.info(
            f"progress: total={processed_total} "
            f"done={stats['pages_done']} ents={stats['entities_added']} "
            f"rels={stats['relations_added']} "
            f"skipped_short={stats['skipped_short']} skipped_empty={stats['skipped_empty']} "
            f"failed={stats['failed']} rate={rate:.2f}/s elapsed={elapsed/60:.1f}min"
        )
        if max_total and processed_total >= max_total:
            break

    log.info(f"DONE total_processed={processed_total} stats={stats}")
    return 0


def main():
    sys.exit(asyncio.run(amain()) or 0)


if __name__ == "__main__":
    main()
