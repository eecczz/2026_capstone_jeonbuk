"""본청 게시판 전체 view URL enumerate.

본청 BFS 가 list 페이지 paging 끝까지 안 따라가서 (BFS time budget + max_depth=5
한계) view 게시물이 boardId 당 100~500개만 발견됨. 실제론 boardId 당 수천 건.

이 스크립트:
1. PG 에 있는 본청 boardId list 추출
2. 각 boardId 의 list page 1 → max startPage 파싱
3. startPage=1..N 까지 모두 fetch → view URL 추출
4. URL list 파일 저장 (reingest_driver 의 --urls-file 로 사용)

부하 관리: concurrency=10, 사이트 친화 지연.
"""

from __future__ import annotations

import asyncio, os, re, sys, time
import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
BASE = "https://www.jeonbuk.go.kr"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/jeonbuk_main_view_urls.txt"
MAX_BOARD_PAGES = int(os.environ.get("MAX_BOARD_PAGES", "700"))  # cap per board


def fetch_board_list() -> list[str]:
    """PG 에서 본청 boardId list 추출."""
    import psycopg2
    conn = psycopg2.connect(
        os.environ.get("DATABASE_URL", "postgresql://admin:sprint26!@localhost:5432/customui")
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT regexp_replace(url, '.*boardId=([^&]+).*', '\\1') AS bid
        FROM crawled_page
        WHERE site_code='jeonbuk_main'
          AND url LIKE '%/board/list.jeonbuk%'
          AND status IN ('success','unchanged')
    """)
    bids = [r[0] for r in cur.fetchall() if r[0] and r[0].startswith("BBS_")]
    cur.close(); conn.close()
    return sorted(set(bids))


async def fetch(client, url):
    try:
        r = await client.get(url, headers={"User-Agent": UA}, timeout=15.0,
                              follow_redirects=True)
        if r.status_code < 400:
            return r.text
    except Exception:
        pass
    return ""


async def discover_max_page(client, board_id: str) -> int:
    url = f"{BASE}/board/list.jeonbuk?boardId={board_id}&paging=ok&startPage=1"
    html = await fetch(client, url)
    if not html:
        return 0
    pages = re.findall(r"startPage=(\d+)", html)
    if not pages:
        return 1
    return min(MAX_BOARD_PAGES, max(int(p) for p in pages))


async def enumerate_board(client, board_id: str, max_page: int, all_urls: set[str], sem: asyncio.Semaphore):
    async def one(page_num: int):
        async with sem:
            url = f"{BASE}/board/list.jeonbuk?boardId={board_id}&paging=ok&startPage={page_num}"
            html = await fetch(client, url)
            if not html:
                return 0
            views = re.findall(r"/board/view\.jeonbuk\?[^\"'<>\s)]+", html)
            cnt = 0
            for v in views:
                # 절대 URL 로
                full = f"{BASE}{v}" if v.startswith("/") else v
                # decode amp
                full = full.replace("&amp;", "&")
                if full not in all_urls:
                    all_urls.add(full)
                    cnt += 1
            return cnt

    tasks = [one(p) for p in range(1, max_page + 1)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    total = sum(r for r in results if isinstance(r, int))
    return total


async def main():
    boards = fetch_board_list()
    print(f"discovered {len(boards)} boardIds from PG")
    sem = asyncio.Semaphore(10)
    all_urls: set[str] = set()
    t0 = time.time()

    async with httpx.AsyncClient(verify=False) as client:
        for i, bid in enumerate(boards):
            mp = await discover_max_page(client, bid)
            if mp == 0:
                print(f"  [{i+1}/{len(boards)}] {bid}: list page fetch fail, skip")
                continue
            added = await enumerate_board(client, bid, mp, all_urls, sem)
            print(f"  [{i+1}/{len(boards)}] {bid}: max_page={mp} new_urls={added} cumulative={len(all_urls)}")

    elapsed = time.time() - t0
    print(f"\n=== DONE. {len(all_urls):,} view URLs in {elapsed/60:.1f}min")
    with open(OUT, "w") as f:
        for u in sorted(all_urls):
            f.write(u + "\n")
    print(f"written to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
