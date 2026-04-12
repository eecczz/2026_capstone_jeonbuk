"""
홈페이지 크롤러 관리자 API.

매일 새벽 배치는 APScheduler가 자동 실행하지만, 관리자는 이 API로 수동 트리거·상태 조회 가능:
- GET  /api/v1/crawler/sites                — 설정된 사이트 목록
- GET  /api/v1/crawler/status               — 전체 통계 (페이지 수, 마지막 크롤링 시각 등)
- GET  /api/v1/crawler/pages                — 크롤링된 페이지 목록 (site_code 필터)
- POST /api/v1/crawler/trigger/full         — 전체 재크롤링 (백그라운드 실행)
- POST /api/v1/crawler/trigger/incremental  — 증분 크롤링 (백그라운드 실행)
- POST /api/v1/crawler/trigger/site/{code}  — 특정 사이트만 크롤링 (백그라운드)
- DELETE /api/v1/crawler/site/{code}        — 특정 사이트의 크롤링 기록·벡터 삭제

모든 엔드포인트는 get_admin_user 의존성으로 관리자 전용.
"""

import asyncio
import logging
import sys
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.crawler import CrawledPages
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
from open_webui.tasks.crawler import (
    run_full_crawl,
    run_incremental_crawl,
    run_site_crawl,
)
from open_webui.tasks.crawler_sites import SITES, get_site
from open_webui.utils.auth import get_admin_user

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

router = APIRouter()


####################
# 응답 스키마
####################


class SiteInfo(BaseModel):
    code: str
    name: str
    base_url: str
    default_category: Optional[str] = None
    max_pages: Optional[int] = None
    crawler_engine: Optional[str] = None


class CrawlerStatus(BaseModel):
    total_pages: int
    by_site: dict
    by_status: dict
    latest_crawl_at: int


####################
# 정보 조회
####################


@router.get("/sites", response_model=list[SiteInfo])
async def list_sites(user=Depends(get_admin_user)):
    """설정된 16개 사이트 목록 반환."""
    return [
        SiteInfo(
            code=s["code"],
            name=s["name"],
            base_url=s["base_url"],
            default_category=s.get("default_category"),
            max_pages=s.get("max_pages"),
            crawler_engine=s.get("crawler_engine"),
        )
        for s in SITES
    ]


@router.get("/status", response_model=CrawlerStatus)
async def get_status(user=Depends(get_admin_user)):
    """크롤링 통계 (사이트별 페이지 수, 최근 실행 시각 등)."""
    stats = CrawledPages.get_stats()
    return CrawlerStatus(
        total_pages=stats.get("total_pages", 0),
        by_site=stats.get("by_site", {}),
        by_status=stats.get("by_status", {}),
        latest_crawl_at=stats.get("latest_crawl_at", 0),
    )


@router.get("/pages")
async def list_pages(
    site_code: Optional[str] = None,
    limit: int = 100,
    user=Depends(get_admin_user),
):
    """크롤링된 페이지 목록 (site_code 필터링, limit 제한)."""
    if site_code:
        pages = CrawledPages.list_by_site(site_code, limit=limit)
    else:
        # 필터 없이 site_code별로 조금씩 가져오기
        pages = []
        for site in SITES:
            pages.extend(CrawledPages.list_by_site(site["code"], limit=max(1, limit // len(SITES))))
    return [p.model_dump() for p in pages]


####################
# 수동 트리거
####################


@router.post("/trigger/full")
async def trigger_full_crawl(
    request: Request,
    user=Depends(get_admin_user),
):
    """전체 사이트 완전 재크롤링을 백그라운드로 실행."""
    if not getattr(request.app.state.config, "CRAWLER_ENABLED", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="크롤러가 비활성화되어 있습니다. CRAWLER_ENABLED를 확인하세요.",
        )

    async def _run():
        try:
            await run_full_crawl(request)
        except Exception as e:
            log.exception(f"trigger_full_crawl background task failed: {e}")

    asyncio.create_task(_run())
    return {"status": "queued", "mode": "full", "sites": len(SITES)}


@router.post("/trigger/incremental")
async def trigger_incremental_crawl(
    request: Request,
    user=Depends(get_admin_user),
):
    """증분 크롤링을 백그라운드로 즉시 실행."""
    if not getattr(request.app.state.config, "CRAWLER_ENABLED", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="크롤러가 비활성화되어 있습니다.",
        )

    async def _run():
        try:
            await run_incremental_crawl(request)
        except Exception as e:
            log.exception(f"trigger_incremental_crawl background task failed: {e}")

    asyncio.create_task(_run())
    return {"status": "queued", "mode": "incremental", "sites": len(SITES)}


@router.post("/trigger/site/{code}")
async def trigger_site_crawl(
    code: str,
    request: Request,
    mode: str = "full",
    user=Depends(get_admin_user),
):
    """특정 사이트만 백그라운드 크롤링."""
    if get_site(code) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown site_code: {code}",
        )
    if mode not in ("full", "incremental"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be 'full' or 'incremental'",
        )

    async def _run():
        try:
            await run_site_crawl(request, code, mode=mode)
        except Exception as e:
            log.exception(f"trigger_site_crawl background failed {code}: {e}")

    asyncio.create_task(_run())
    return {"status": "queued", "site_code": code, "mode": mode}


####################
# 삭제 (위험)
####################


@router.delete("/site/{code}")
async def delete_site_data(
    code: str,
    request: Request,
    user=Depends(get_admin_user),
):
    """특정 사이트의 CrawledPage 기록과 벡터 DB 청크를 전부 삭제.

    재크롤링 전에 clean slate를 원할 때 사용. 위험한 작업이니 호출 주의.
    """
    site = get_site(code)
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown site_code: {code}",
        )

    collection_name = getattr(
        request.app.state.config, "CRAWLER_COLLECTION_NAME", "jeonbuk_gov"
    )

    try:
        VECTOR_DB_CLIENT.delete(
            collection_name=collection_name,
            filter={"site_code": code},
        )
    except Exception as e:
        log.warning(f"vector delete failed for site {code}: {e}")

    deleted = CrawledPages.delete_by_site(code)
    return {"status": "ok", "site_code": code, "deleted_records": deleted}
