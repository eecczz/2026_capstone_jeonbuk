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
from open_webui.tasks.crawler_backfill import backfill_crawled_page_from_vector_db
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


####################
# 복구 (백필)
####################


@router.post("/backfill")
async def backfill(
    request: Request,
    collection_name: Optional[str] = None,
    site_code: Optional[str] = None,
    dry_run: bool = False,
    user=Depends(get_admin_user),
):
    """Vector DB 컬렉션에 있는 기존 청크 메타데이터를 crawled_page 테이블로 retroactive 백필.

    Query params:
    - collection_name: Qdrant 컬렉션 이름 (None이면 CRAWLER_COLLECTION_NAME 사용)
    - site_code: 특정 site_code만 처리 (None이면 전체)
    - dry_run: true면 실제 INSERT 하지 않고 집계만 반환
    """
    target_collection = collection_name or getattr(
        request.app.state.config, "CRAWLER_COLLECTION_NAME", "jeonbuk_gov"
    )
    stats = await asyncio.to_thread(
        backfill_crawled_page_from_vector_db,
        target_collection,
        site_code,
        dry_run,
    )
    return stats


####################
# GraphRAG — retroactive 엔티티 추출
####################


@router.post("/extract/site/{code}")
async def extract_entities_for_site(
    code: str,
    request: Request,
    limit: int = 20,
    user=Depends(get_admin_user),
):
    """이미 크롤된 특정 사이트의 첫 N개 URL에 대해 LLM 엔티티 추출을 실행.

    재크롤링하지 않고 vector DB에 있는 텍스트를 읽어서 LLM에 바로 넘긴다.
    entity/entity_mention/entity_relation 테이블이 채워지는지 검증용.
    """
    from open_webui.models.crawler import CrawledPages
    from open_webui.retrieval.graph.extractor import (
        build_extraction_messages,
        parse_and_store,
    )
    from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
    from open_webui.routers.public_chatbot import _get_public_user
    from open_webui.utils.chat import generate_chat_completion

    pages = CrawledPages.list_by_site(code, limit=limit)
    if not pages:
        raise HTTPException(
            status_code=404, detail=f"No crawled_page rows for site_code={code}"
        )

    collection = getattr(
        request.app.state.config, "CRAWLER_COLLECTION_NAME", "jeonbuk_gov"
    )
    base_model = getattr(
        request.app.state.config, "PUBLIC_CHATBOT_BASE_MODEL", "gpt-5.4-mini"
    )
    public_user = _get_public_user(request)

    stats = {
        "site_code": code,
        "total_pages": len(pages),
        "processed": 0,
        "entities": 0,
        "mentions": 0,
        "relations": 0,
        "llm_failed": 0,
        "parse_failed": 0,
        "empty_text": 0,
    }

    for p in pages:
        # vector DB에서 해당 URL의 청크 텍스트 조회
        try:
            vq = VECTOR_DB_CLIENT.query(
                collection_name=collection,
                filter={"url": p.url},
                limit=3,
            )
        except Exception as e:
            log.warning(f"vector query failed for {p.url}: {e}")
            continue
        if vq is None:
            stats["empty_text"] += 1
            continue
        docs = (vq.documents or [[]])[0] if vq.documents else []
        combined = "\n\n".join((d or "")[:1500] for d in docs[:3])
        if not combined.strip():
            stats["empty_text"] += 1
            continue

        form_data = {
            "model": base_model,
            "messages": build_extraction_messages(combined),
            "stream": False,
            "temperature": 0.0,
        }
        try:
            resp = await generate_chat_completion(
                request, form_data, user=public_user, bypass_filter=True
            )
        except Exception as e:
            log.warning(f"LLM extraction failed for {p.url}: {e}")
            stats["llm_failed"] += 1
            continue

        raw = ""
        if isinstance(resp, dict):
            try:
                raw = resp["choices"][0]["message"]["content"] or ""
            except Exception:
                raw = ""
        if not raw:
            try:
                body = getattr(resp, "body", None) or b""
                if body:
                    import json as _json
                    d = _json.loads(body.decode("utf-8"))
                    raw = d["choices"][0]["message"]["content"] or ""
            except Exception:
                pass

        if not raw:
            stats["llm_failed"] += 1
            continue

        sub = parse_and_store(
            llm_raw_output=raw,
            chunk_id=p.url,
            url=p.url,
            confidence=0.8,
            seed_institution=p.institution,
            seed_category=p.category,
        )
        stats["processed"] += 1
        stats["entities"] += sub.get("entities", 0)
        stats["mentions"] += sub.get("mentions", 0)
        stats["relations"] += sub.get("relations", 0)
        if sub.get("parse_failed"):
            stats["parse_failed"] += 1

    return stats


@router.post("/extract/all")
async def extract_entities_all_sites(
    request: Request,
    limit_per_site: int = 1000,
    user=Depends(get_admin_user),
):
    """모든 사이트의 크롤된 페이지에 대해 순차 엔티티 추출을 백그라운드로 실행.

    즉시 응답하며 실제 작업은 asyncio.create_task 로 돌아간다. 진행 상황은
    entity / entity_mention / entity_relation 테이블을 SELECT 해서 확인.
    """
    from open_webui.models.crawler import CrawledPages
    from open_webui.retrieval.graph.extractor import (
        build_extraction_messages,
        parse_and_store,
    )
    from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
    from open_webui.routers.public_chatbot import _get_public_user
    from open_webui.utils.chat import generate_chat_completion

    collection = getattr(
        request.app.state.config, "CRAWLER_COLLECTION_NAME", "jeonbuk_gov"
    )
    base_model = getattr(
        request.app.state.config, "PUBLIC_CHATBOT_BASE_MODEL", "gpt-5.4-mini"
    )
    public_user = _get_public_user(request)

    # 이미 처리된 URL 스킵 집합 (재시작 대응)
    from open_webui.internal.db import get_db_context
    from open_webui.models.entity import EntityMention as _EM
    processed_urls: set[str] = set()
    try:
        with get_db_context() as db:
            rows = db.query(_EM.url).filter(_EM.url.isnot(None)).distinct().all()
            processed_urls = {r[0] for r in rows}
    except Exception as e:
        log.warning(f"could not load processed URL set: {e}")

    async def _run_all():
        all_stats: dict[str, dict] = {}
        for site in SITES:
            code = site["code"]
            pages = CrawledPages.list_by_site(code, limit=limit_per_site)
            if not pages:
                continue
            s = {
                "total_pages": len(pages),
                "processed": 0,
                "skipped_already": 0,
                "entities": 0,
                "mentions": 0,
                "relations": 0,
                "llm_failed": 0,
                "empty_text": 0,
            }
            log.info(
                f"extract_all START site={code} pages={len(pages)} "
                f"already_processed={sum(1 for p in pages if p.url in processed_urls)}"
            )
            for p in pages:
                if p.url in processed_urls:
                    s["skipped_already"] += 1
                    continue
                try:
                    vq = VECTOR_DB_CLIENT.query(
                        collection_name=collection,
                        filter={"url": p.url},
                        limit=3,
                    )
                except Exception as e:
                    log.warning(f"vector query failed {p.url}: {e}")
                    continue
                if vq is None:
                    s["empty_text"] += 1
                    continue
                docs = (vq.documents or [[]])[0] if vq.documents else []
                combined = "\n\n".join((d or "")[:1500] for d in docs[:3])
                if not combined.strip():
                    s["empty_text"] += 1
                    continue
                form_data = {
                    "model": base_model,
                    "messages": build_extraction_messages(combined),
                    "stream": False,
                    "temperature": 0.0,
                }
                try:
                    resp = await generate_chat_completion(
                        request, form_data, user=public_user, bypass_filter=True
                    )
                except Exception as e:
                    log.warning(f"LLM extraction failed for {p.url}: {e}")
                    s["llm_failed"] += 1
                    continue
                raw = ""
                if isinstance(resp, dict):
                    try:
                        raw = resp["choices"][0]["message"]["content"] or ""
                    except Exception:
                        raw = ""
                if not raw:
                    try:
                        body = getattr(resp, "body", None) or b""
                        if body:
                            import json as _json
                            d = _json.loads(body.decode("utf-8"))
                            raw = d["choices"][0]["message"]["content"] or ""
                    except Exception:
                        pass
                if not raw:
                    s["llm_failed"] += 1
                    continue
                sub = parse_and_store(
                    llm_raw_output=raw,
                    chunk_id=p.url,
                    url=p.url,
                    confidence=0.8,
                    seed_institution=p.institution,
                    seed_category=p.category,
                )
                s["processed"] += 1
                s["entities"] += sub.get("entities", 0)
                s["mentions"] += sub.get("mentions", 0)
                s["relations"] += sub.get("relations", 0)
                processed_urls.add(p.url)
            log.info(f"extract_all DONE site={code} stats={s}")
            all_stats[code] = s
        log.info(f"extract_all FULL COMPLETE: {all_stats}")

    asyncio.create_task(_run_all())
    return {
        "status": "queued",
        "mode": "background",
        "sites": len(SITES),
        "limit_per_site": limit_per_site,
    }
