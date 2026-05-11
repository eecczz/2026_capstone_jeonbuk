"""크롤러 전용 독립 워커 프로세스.

uvicorn 워커 안에서 asyncio.create_task 로 띄우면 SIGHUP reload 마다 죽는다.
이 스크립트는 별도 프로세스로 실행되어 OWI uvicorn 과 라이프사이클이 분리된다.

사용:
    nohup python3 -m open_webui.tasks.crawler_worker --site jeonbuk_main --mode full \\
        > /tmp/crawler_worker.log 2>&1 &

필요한 환경변수 (OWI 와 동일):
    DATABASE_URL=postgresql://admin:sprint26!@localhost:5432/customui
    PYTHONPATH=/app/backend

uvicorn 와 같은 Postgres / Qdrant / 30100 OCR / BGE M3 endpoint 를 그대로 쓰므로
DB·임베딩 인프라는 공유한다. 단지 크롤 작업 자체만 분리되어 실행된다.
"""

import argparse
import asyncio
import logging
import os
import sys
from types import SimpleNamespace
from typing import Any

# OWI config 모듈은 import 시 DB 접근하므로 PYTHONPATH 보정.
if "/app/backend" not in sys.path:
    sys.path.insert(0, "/app/backend")

logging.basicConfig(
    level=os.environ.get("CRAWLER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("crawler_worker")


def _build_request_proxy() -> Any:
    """OWI 의 진짜 app.state 를 그대로 활용하는 Request-like 프록시.

    `open_webui.main` import 시:
    - module 최상위 코드 (app.state.config.X = Y 같은 alias 매핑) 실행됨
    - 라우터 등록·DB 마이그레이션 진입 등 startup 부수효과 실행됨

    lifespan 안에서만 설정되는 attribute (예: app.state.main_loop) 는 import 만으로
    누락되므로 본 워커가 사용 직전 보강해 준다.
    """
    import asyncio as _asyncio

    from open_webui.main import app  # 이 import 자체가 module-level alias 매핑 실행

    # lifespan 안에서 보통 setup 되는 것들을 워커 컨텍스트에 보강
    if not hasattr(app.state, "main_loop"):
        try:
            app.state.main_loop = _asyncio.get_event_loop()
        except Exception:
            pass

    return SimpleNamespace(app=app)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--site",
        default="jeonbuk_main",
        help="site_code (crawler_sites.SITES 의 code). 'all' 이면 전체 사이트.",
    )
    parser.add_argument(
        "--mode",
        default="full",
        choices=["full", "incremental"],
        help="full = 처음부터, incremental = 변경 페이지만",
    )
    args = parser.parse_args()

    request = _build_request_proxy()

    from open_webui.tasks.crawler import (
        run_full_crawl,
        run_incremental_crawl,
        run_site_crawl,
    )

    log.info(f"crawler_worker start: site={args.site} mode={args.mode}")
    try:
        if args.site == "all":
            if args.mode == "full":
                await run_full_crawl(request)
            else:
                await run_incremental_crawl(request)
        else:
            await run_site_crawl(request, args.site, mode=args.mode)
    except Exception as e:
        log.exception(f"crawler_worker failed: {e}")
        return 1
    log.info("crawler_worker done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
