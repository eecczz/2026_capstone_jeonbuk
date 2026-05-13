"""시군구 reingest driver — PG URL list → OWI /reingest endpoint 호출.

배경: 4/13~4/28 시기 풀크롤 페이지 중 ~104K 가 Qdrant 적재 실패 (BGE M3 8192
토큰 limit). 현재는 truncate 픽스 적용돼서 재시도 시 적재됨.

흐름:
1. PG 에서 14개 사이트의 success/unchanged URL list 추출
2. CHUNK_SIZE (=10) 개씩 묶어서 POST /api/v1/crawler/reingest
3. 응답에서 ok/failed 집계

사용:
  PYTHONPATH=/app/backend python3 scripts/reingest_driver.py [--sites SITE1,SITE2] [--limit N] [--chunk 10]

JWT: ADMIN_JWT 환경변수로 전달하거나 main.py 의 default admin login 사용.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import json
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reingest")

OWI_BASE = os.environ.get("OWI_BASE_URL", "http://localhost:8080")
ADMIN_JWT = os.environ.get("ADMIN_JWT", "")

DEFAULT_TARGETS = [
    "gochang_county", "gimje_city", "jeongeup_city", "jinan_county",
    "sunchang_county", "jangsu_county", "imsil_county", "namwon_city",
    "iksan_city", "muju_county", "buan_county",
    "policy_jb", "stat_jeonbuk", "jeonju_city",
]


def get_admin_jwt() -> str:
    """ADMIN_JWT 환경변수 우선, 없으면 default admin 로그인."""
    if ADMIN_JWT:
        return ADMIN_JWT
    # default admin login
    req = urllib.request.Request(
        f"{OWI_BASE}/api/v1/auths/signin",
        data=json.dumps({"email": "sprinter@mail.go.kr", "password": "sprint26!"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["token"]


def fetch_urls(sites: list[str], limit: int | None = None) -> list[str]:
    import psycopg2

    conn = psycopg2.connect(
        os.environ.get("DATABASE_URL", "postgresql://admin:sprint26!@localhost:5432/customui")
    )
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(sites))
    q = f"""
        SELECT url FROM crawled_page
        WHERE status IN ('success','unchanged') AND site_code IN ({placeholders})
        ORDER BY site_code, url
    """
    params = list(sites)
    if limit:
        q += " LIMIT %s"
        params.append(limit)
    cur.execute(q, params)
    urls = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return urls


def post_reingest(jwt: str, urls: list[str], timeout: int = 600) -> dict:
    req = urllib.request.Request(
        f"{OWI_BASE}/api/v1/crawler/reingest",
        data=json.dumps({"urls": urls}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {jwt}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", help="comma-separated")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--chunk", type=int, default=10, help="URLs per HTTP request")
    args = ap.parse_args()

    sites = args.sites.split(",") if args.sites else DEFAULT_TARGETS
    log.info(f"Target sites: {sites}")
    log.info("Getting admin JWT...")
    jwt = get_admin_jwt()
    log.info(f"JWT acquired ({len(jwt)} chars)")

    urls = fetch_urls(sites, args.limit)
    log.info(f"PG returned {len(urls):,} URLs")

    agg = {"ok": 0, "failed": 0, "skipped_not_in_pg": 0, "errors": 0, "by_status": {}}
    t0 = time.time()
    for i in range(0, len(urls), args.chunk):
        chunk = urls[i : i + args.chunk]
        resp = post_reingest(jwt, chunk)
        if "error" in resp:
            log.warning(f"chunk {i}: HTTP error {resp['error']}")
            agg["errors"] += len(chunk)
        else:
            agg["ok"] += resp.get("ok", 0)
            agg["failed"] += resp.get("failed", 0)
            agg["skipped_not_in_pg"] += resp.get("skipped_not_in_pg", 0)
            for k, v in (resp.get("by_status") or {}).items():
                agg["by_status"][k] = agg["by_status"].get(k, 0) + v

        if (i // args.chunk + 1) % 10 == 0:
            elapsed = time.time() - t0
            rate = (i + len(chunk)) / max(elapsed, 0.01)
            eta = (len(urls) - i - len(chunk)) / max(rate, 0.01)
            log.info(
                f"progress: {i + len(chunk):,}/{len(urls):,} "
                f"ok={agg['ok']:,} failed={agg['failed']:,} "
                f"rate={rate:.1f}/s eta={eta/60:.0f}min"
            )

    log.info(f"DONE total={len(urls):,} aggregated={agg} elapsed={(time.time()-t0)/60:.1f}min")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
