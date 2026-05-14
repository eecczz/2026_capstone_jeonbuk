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
from open_webui.models.crawler import CrawledAttachments, CrawledPages
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
from open_webui.tasks.crawler import (
    _fetch_html,
    _build_metadata,
    _load_page,
    run_full_crawl,
    run_incremental_crawl,
    run_site_crawl,
)
from open_webui.tasks.crawler_attachments import process_page_attachments
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


####################
# Reingest — URL list 받아서 BFS 없이 강제 재처리
####################


class ReingestRequest(BaseModel):
    urls: list[str]
    skip_entity: bool = True
    skip_attachment: bool = True


@router.post("/reingest")
async def reingest_urls(
    body: ReingestRequest,
    request: Request,
    user=Depends(get_admin_user),
):
    """URL list 를 받아 BFS 없이 _process_url 강제 재호출.

    배경: 4/13~4/28 풀크롤 시기 BGE M3 8192 토큰 limit 으로 Qdrant 적재 실패한
    페이지들 (PG status=success 인데 Qdrant 청크 0). content_hash 가 PG 에 남아
    있어서 mode=full 로 다시 돌려도 unchanged 처리됨. 그러므로 content_hash 를
    NULL reset 후 _process_url 강제 호출.

    crawler_sites.py 에 site_code 정의 없으면 (시군구 등) PG 의 메타 (institution,
    category) 기반 stub site_config 구성.

    skip_entity=True (default): _process_url 의 LLM 엔티티 추출 단계 건너뜀
    (entity backfill 이 따로 도므로 중복 호출 회피, 속도 ~10배 향상).
    skip_attachment=True (default): 첨부 OCR 처리 건너뜀 (별도 작업 가능).
    """
    from urllib.parse import urlparse
    from open_webui.tasks.crawler import _process_url
    from open_webui.tasks.crawler_sites import get_site
    import os as _os

    results = {"ok": 0, "failed": 0, "skipped_not_in_pg": 0, "by_status": {}}

    # 스킵 플래그 — process_url 안에서 환경변수 / 설정 체크 우회용으로 사용.
    # process-local 만 영향 (sub-call) — async task 끝나면 원복.
    if body.skip_entity:
        _os.environ["CRAWL_GRAPH_ENTITY_EXTRACT"] = "0"
    if body.skip_attachment:
        # request.app.state.config.CRAWL_ATTACHMENTS_ENABLED 를 임시 끄고 finally 에서 복원.
        cfg = getattr(request.app.state, "config", None)
        prev_attach = getattr(cfg, "CRAWL_ATTACHMENTS_ENABLED", True) if cfg else True
        if cfg is not None:
            try:
                cfg.CRAWL_ATTACHMENTS_ENABLED = False
            except Exception:
                pass

    sem = asyncio.Semaphore(15)  # SQL session pool + Qdrant insert 부하 균형

    import html as _html

    async def _one(url: str):
        async with sem:
            # PG 일부 시군구 URL 은 &amp; HTML entity 로 저장됨 — driver 는 PG 의
            # 인코딩된 URL 그대로 보내고, 여기서 fetch 시점에 unescape.
            existing = CrawledPages.get_by_url(url)
            if not existing:
                # PG 미등록 URL — 도메인으로 site_code 추론 후 minimal stub row 생성.
                from urllib.parse import urlparse as _up
                host = _up(url).netloc.lower()
                inferred_site = None
                if "jeonbuk.go.kr" in host and "tour" not in host and "policy" not in host and "stat" not in host:
                    inferred_site = "jeonbuk_main"
                if inferred_site is None:
                    results["skipped_not_in_pg"] += 1
                    return
                try:
                    CrawledPages.upsert(
                        url=url, site_code=inferred_site,
                        institution="전북특별자치도",
                        category="행정",
                        title="",
                        content_hash=None,
                        http_etag=None,
                        http_last_modified=None,
                        status="pending",
                        chunks_count=0,
                        content_changed=False,
                    )
                    existing = CrawledPages.get_by_url(url)
                except Exception as e:
                    log.warning(f"reingest stub create failed {url}: {e}")
                    results["skipped_not_in_pg"] += 1
                    return
            # fetch + metadata 에 사용할 URL — HTML entity 풀어진 형태.
            url_for_fetch = _html.unescape(url)

            # site_config — 정의된 것 우선, 없으면 PG 메타 기반 stub.
            site_config = get_site(existing.site_code)
            if site_config is None:
                parsed = urlparse(url)
                base = f"{parsed.scheme}://{parsed.netloc}"
                site_config = {
                    "code": existing.site_code,
                    "name": existing.institution or existing.site_code,
                    "base_url": base,
                    "contact": {},
                    "default_category": existing.category or "기타",
                }

            # content_hash NULL reset → _process_url 이 changed 로 인식하도록.
            # CrawledPages.upsert 는 content_hash=None 일 때 skip 하니 직접 SQL.
            # encoded URL (PG 원본 row) 와 decoded URL (_process_url lookup row) 둘 다.
            try:
                from open_webui.internal.db import get_db_context
                from open_webui.models.crawler import CrawledPage
                with get_db_context() as _db:
                    _db.query(CrawledPage).filter(
                        CrawledPage.url.in_(list({url, url_for_fetch}))
                    ).update(
                        {
                            CrawledPage.content_hash: None,
                            CrawledPage.http_etag: None,
                            CrawledPage.http_last_modified: None,
                        },
                        synchronize_session=False,
                    )
                    _db.commit()
            except Exception as e:
                log.warning(f"reingest hash reset failed {url}: {e}")

            try:
                # _process_url 에 url_for_fetch (decoded) 전달 — Crawl4AI fetch +
                # metadata 의 url 필드 도 decoded 형태로. PG row 의 url 은 그대로 유지.
                status_str = await _process_url(request, site_config, url_for_fetch, mode="full")
            except Exception as e:
                log.warning(f"reingest _process_url failed {url}: {e}")
                results["failed"] += 1
                return

            results["by_status"][status_str] = results["by_status"].get(status_str, 0) + 1
            if status_str in ("new", "updated"):
                results["ok"] += 1
            else:
                results["failed"] += 1

    try:
        await asyncio.gather(*(_one(u) for u in body.urls))
    finally:
        # 스킵 플래그 원복 — 다음 reingest 호출 / 정상 크롤 동작 보호.
        if body.skip_attachment:
            cfg = getattr(request.app.state, "config", None)
            if cfg is not None:
                try:
                    cfg.CRAWL_ATTACHMENTS_ENABLED = prev_attach
                except Exception:
                    pass
        # entity extract 환경변수도 원복 (다음 페이지 정상 크롤 영향 안 가도록).
        if body.skip_entity:
            _os.environ.pop("CRAWL_GRAPH_ENTITY_EXTRACT", None)
    return results


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


####################
# 첨부 처리
####################


@router.get("/attachments/status")
async def attachments_status(user=Depends(get_admin_user)):
    """첨부 처리 통계 (status 별 / type 별)."""
    return CrawledAttachments.get_stats()


class TestPageRequest(BaseModel):
    url: str
    site_code: str = "jeonbuk_main"
    include_images: bool = True


@router.post("/test-page")
async def test_single_page(
    request: Request,
    body: TestPageRequest,
    user=Depends(get_admin_user),
):
    """단일 URL 로 페이지 + 첨부 + 인라인 이미지 처리를 즉시 실행 (디버깅/테스트 용).

    크롤 큐 없이 동기로 끝까지 돌리며 결과를 그대로 반환한다.
    """
    site = get_site(body.site_code) or {
        "code": body.site_code,
        "name": body.site_code,
        "base_url": body.url,
    }
    collection_name = getattr(
        request.app.state.config, "CRAWLER_COLLECTION_NAME", "jeonbuk_gov"
    )

    import asyncio as _asyncio
    from open_webui.routers.retrieval import save_docs_to_vector_db
    from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
    from open_webui.models.crawler import CrawledPages

    docs = await _load_page(body.url)
    if not docs:
        return {"status": "error", "stage": "page_load", "message": "load returned empty"}

    metadata = _build_metadata(body.url, site, docs)

    # 페이지 본문도 collection 에 저장 (덮어쓰기)
    try:
        VECTOR_DB_CLIENT.delete(
            collection_name=collection_name,
            filter={"url": body.url},
        )
    except Exception as e:
        log.debug(f"page vector delete failed: {e}")
    page_saved = False
    try:
        page_saved = bool(
            await _asyncio.to_thread(
                save_docs_to_vector_db,
                request, docs, collection_name, metadata,
                False, True, True, None,
            )
        )
        CrawledPages.upsert(
            url=body.url,
            site_code=body.site_code,
            institution=metadata.get("institution"),
            category=metadata.get("category"),
            title=metadata.get("title"),
            chunks_count=len(docs),
            status="success",
        )
    except Exception as e:
        log.warning(f"page save failed: {e}")

    html = await _fetch_html(body.url)
    att_stats: dict = {}
    if html:
        att_stats = await process_page_attachments(
            request,
            page_url=body.url,
            page_html=html,
            page_metadata=metadata,
            site_code=body.site_code,
            collection_name=collection_name,
            include_images=body.include_images,
        )
    return {
        "status": "ok",
        "page_saved": page_saved,
        "collection_name": collection_name,
        "url": body.url,
        "page_chunks": len(docs),
        "metadata": {k: v for k, v in metadata.items() if k != "_orig_metadata"},
        "attachments": att_stats,
    }
