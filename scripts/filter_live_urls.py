"""PG URL list 에서 HTTP HEAD 로 살아있는 URL 만 필터링.

배경: 시군구 92K URL 중 first 100 reingest 결과 87% failed (4xx/timeout). 시군구
사이트 구조가 4/24 이후 바뀌었거나 처음부터 broken 인 URL 이 많음. reingest 비용
절감을 위해 HEAD check 로 200/301/302 응답만 큐로.

흐름:
1. PG 에서 14개 사이트 URL list 추출
2. asyncio + httpx, concurrency 50 으로 HEAD check
3. 살아있는 URL 만 파일에 저장 (line-separated)
4. reingest_driver.py 가 그 파일을 큐로 사용

사용:
  python3 scripts/filter_live_urls.py --out /tmp/live_urls.txt [--concurrency 50] [--timeout 5]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

import httpx
import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("filter")

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://admin:sprint26!@localhost:5432/customui"
)

DEFAULT_TARGETS = [
    "gochang_county", "gimje_city", "jeongeup_city", "jinan_county",
    "sunchang_county", "jangsu_county", "imsil_county", "namwon_city",
    "iksan_city", "muju_county", "buan_county",
    "policy_jb", "stat_jeonbuk", "jeonju_city",
]


def fetch_urls(sites: list[str]) -> list[str]:
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(sites))
    cur.execute(
        f"""
        SELECT url FROM crawled_page
        WHERE status IN ('success','unchanged') AND site_code IN ({placeholders})
        ORDER BY site_code, url
        """,
        sites,
    )
    urls = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return urls


async def check_one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore, stats: dict) -> str | None:
    async with sem:
        try:
            # HEAD 일부 서버 차단 → GET 으로 첫 byte 만 요청 (Range header)
            r = await client.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True)
            if r.status_code < 400:
                stats["alive"] += 1
                return url
            stats["dead"] += 1
        except Exception:
            stats["error"] += 1
        return None


async def amain():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/live_urls.txt")
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--timeout", type=int, default=8)
    ap.add_argument("--sites", help="comma-separated")
    args = ap.parse_args()

    sites = args.sites.split(",") if args.sites else DEFAULT_TARGETS
    urls = fetch_urls(sites)
    log.info(f"PG returned {len(urls):,} URLs to check")

    sem = asyncio.Semaphore(args.concurrency)
    stats = {"alive": 0, "dead": 0, "error": 0}
    t0 = time.time()
    live_urls: list[str] = []

    async with httpx.AsyncClient(timeout=args.timeout, verify=False) as client:
        BATCH = 500
        for i in range(0, len(urls), BATCH):
            chunk = urls[i : i + BATCH]
            results = await asyncio.gather(*(check_one(client, u, sem, stats) for u in chunk))
            for r in results:
                if r:
                    live_urls.append(r)
            if (i // BATCH + 1) % 10 == 0:
                elapsed = time.time() - t0
                rate = (i + len(chunk)) / max(elapsed, 0.01)
                eta = (len(urls) - i - len(chunk)) / max(rate, 0.01)
                log.info(
                    f"progress: {i + len(chunk):,}/{len(urls):,} "
                    f"alive={stats['alive']:,} dead={stats['dead']:,} error={stats['error']:,} "
                    f"rate={rate:.0f}/s eta={eta/60:.0f}min"
                )

    with open(args.out, "w") as f:
        for u in live_urls:
            f.write(u + "\n")
    log.info(
        f"DONE — alive={stats['alive']:,} ({stats['alive']/len(urls)*100:.1f}%) "
        f"dead={stats['dead']:,} error={stats['error']:,} "
        f"elapsed={(time.time()-t0)/60:.1f}min written to {args.out}"
    )
    return 0


def main():
    sys.exit(asyncio.run(amain()) or 0)


if __name__ == "__main__":
    main()
