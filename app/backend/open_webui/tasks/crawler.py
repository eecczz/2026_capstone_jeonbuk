"""
전북도청 + 직속기관 홈페이지 일별 배치 크롤러.

사용 흐름:
1. APScheduler가 매일 02:00 KST에 run_incremental_crawl()을 호출 (main.py lifespan에서 등록)
2. SITES 목록 순회, 각 사이트별로 URL 수집 → HTTP HEAD로 변경 확인 → 변경된 페이지만 재인덱싱
3. 각 청크에 institution / contact_phone / homepage_url 등 metadata 주입 (save_docs_to_vector_db 재사용)
4. CrawledPage 테이블에 상태 upsert

관리자 API (routers/crawler.py)에서 수동 트리거 가능:
- /api/v1/crawler/trigger/full — 전체 재크롤링
- /api/v1/crawler/trigger/incremental — 증분 크롤링 즉시 실행
- /api/v1/crawler/trigger/site/{code} — 특정 사이트만
"""

import asyncio
import hashlib
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from langchain_core.documents import Document

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.crawler import CrawledPages
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
from open_webui.retrieval.web.utils import get_web_loader
from open_webui.routers.retrieval import save_docs_to_vector_db
from open_webui.tasks.crawler_sites import SITES, infer_sub_institution, get_site

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

# 크롤러가 HTTP 요청 시 보낼 기본 헤더
_DEFAULT_HEADERS = {
    "User-Agent": "JeonbukBot/1.0 (+https://www.jeonbuk.go.kr)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# 페이지별 HTTP HEAD 요청 타임아웃 (초)
_HEAD_TIMEOUT = 10
# 페이지 로드 간 딜레이 (ms) — 사이트 부하 배려
_DEFAULT_DELAY_MS = 500
# 전화번호 추출 정규식 (보조 수단)
_PHONE_REGEX = re.compile(r"0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}")


####################
# URL 수집
####################


async def _fetch_sitemap_urls(sitemap_url: str) -> list[str]:
    """sitemap.xml에서 URL 목록 추출. sitemap index도 재귀 처리."""
    urls: list[str] = []
    try:
        async with aiohttp.ClientSession(headers=_DEFAULT_HEADERS) as session:
            async with session.get(
                sitemap_url, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        f"sitemap fetch failed: {sitemap_url} status={resp.status}"
                    )
                    return []
                text = await resp.text()
    except Exception as e:
        log.warning(f"sitemap fetch exception: {sitemap_url}: {e}")
        return []

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        log.warning(f"sitemap parse error: {sitemap_url}: {e}")
        return []

    # XML namespaces 처리
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    # sitemapindex 형태면 하위 sitemap들을 재귀 수집
    for sm in root.findall(".//sm:sitemap/sm:loc", ns):
        if sm.text:
            urls.extend(await _fetch_sitemap_urls(sm.text.strip()))
    # urlset 형태면 loc 태그에서 URL 추출
    for loc in root.findall(".//sm:url/sm:loc", ns):
        if loc.text:
            urls.append(loc.text.strip())
    # namespace 없는 경우 대응
    if not urls:
        for loc in root.findall(".//loc"):
            if loc.text:
                urls.append(loc.text.strip())

    return urls


async def _fetch_links_from_page(
    url: str, base_url: str
) -> tuple[list[str], Optional[str]]:
    """페이지에서 내부 링크 추출. (links, html_content) 반환."""
    try:
        async with aiohttp.ClientSession(headers=_DEFAULT_HEADERS) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return [], None
                html = await resp.text()
    except Exception as e:
        log.debug(f"fetch_links failed: {url}: {e}")
        return [], None

    # 단순 정규식 기반 <a href="..."> 추출 (BS4 사용하면 더 정확하지만 의존성 증가)
    href_pattern = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)
    links: set[str] = set()
    base_parsed = urlparse(base_url)

    for match in href_pattern.findall(html):
        href = match.strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = urljoin(url, href)
        parsed = urlparse(abs_url)
        # 같은 호스트만 포함
        if parsed.netloc and parsed.netloc == base_parsed.netloc:
            # fragment 제거
            clean = abs_url.split("#", 1)[0]
            links.add(clean)

    return sorted(links), html


def _url_matches_patterns(url: str, allowed: list[str], excluded: list[str]) -> bool:
    path = urlparse(url).path + (
        "?" + urlparse(url).query if urlparse(url).query else ""
    )
    for pat in excluded:
        if re.search(pat, url) or re.search(pat, path):
            return False
    if not allowed:
        return True
    for pat in allowed:
        if re.search(pat, path) or re.search(pat, url):
            return True
    return False


async def discover_urls(site_config: dict[str, Any]) -> list[str]:
    """사이트 설정에 따라 크롤링할 URL 목록을 수집.

    순서:
    1. sitemap_urls가 있으면 sitemap에서 URL 추출
    2. 그 외에 priority_paths는 항상 시작점으로 추가
    3. max_pages 제한 적용
    4. allowed/excluded 패턴으로 필터링
    """
    base_url = site_config["base_url"].rstrip("/")
    max_pages = int(site_config.get("max_pages", 500))
    max_depth = int(site_config.get("max_depth", 3))

    allowed = site_config.get("allowed_path_patterns") or []
    excluded = site_config.get("excluded_path_patterns") or []

    collected: set[str] = set()

    # 1. sitemap
    for sm_url in site_config.get("sitemap_urls", []) or []:
        sm_urls = await _fetch_sitemap_urls(sm_url)
        for u in sm_urls:
            if _url_matches_patterns(u, allowed, excluded):
                collected.add(u)
            if len(collected) >= max_pages:
                break
        if len(collected) >= max_pages:
            break

    # 2. priority_paths을 큐에 넣고 BFS로 링크 탐색 (max_depth 제한)
    if len(collected) < max_pages:
        start_urls = [
            urljoin(base_url + "/", p.lstrip("/"))
            for p in (site_config.get("priority_paths") or ["/"])
        ]
        queue: list[tuple[str, int]] = [(u, 0) for u in start_urls]
        visited: set[str] = set()

        while queue and len(collected) < max_pages:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if _url_matches_patterns(current, allowed, excluded):
                collected.add(current)

            if depth < max_depth:
                try:
                    links, _ = await _fetch_links_from_page(current, base_url)
                except Exception:
                    links = []
                for link in links:
                    if link not in visited and len(collected) < max_pages:
                        queue.append((link, depth + 1))

    # 결과 (정렬된 리스트)
    result = sorted(collected)[:max_pages]
    log.info(
        f"discover_urls: site={site_config['code']} collected={len(result)} "
        f"(max={max_pages})"
    )
    return result


####################
# HTTP HEAD (증분 확인)
####################


async def _http_head(url: str) -> dict[str, Optional[str]]:
    """HTTP HEAD로 ETag, Last-Modified, status 조회."""
    try:
        async with aiohttp.ClientSession(headers=_DEFAULT_HEADERS) as session:
            async with session.head(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=_HEAD_TIMEOUT),
            ) as resp:
                return {
                    "status": str(resp.status),
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                }
    except Exception as e:
        log.debug(f"http_head failed: {url}: {e}")
        return {"status": None, "etag": None, "last_modified": None}


####################
# 페이지 로드 (langchain Document 반환)
####################


def _load_page_sync(url: str) -> list[Document]:
    """기존 get_web_loader로 페이지를 로드하여 langchain Document 반환.

    주의: get_web_loader는 전역 WEB_LOADER_ENGINE 설정을 사용한다.
    사이트별 엔진 지정은 현재 지원하지 않음 (향후 개선 여지).
    """
    loader = get_web_loader(url)
    try:
        docs = loader.load()
        return docs or []
    except Exception as e:
        log.warning(f"load_page failed: {url}: {e}")
        return []


async def _load_page(url: str) -> list[Document]:
    return await asyncio.to_thread(_load_page_sync, url)


####################
# 메타데이터 추출
####################


def _extract_phone(text: str, default: Optional[str]) -> Optional[str]:
    """페이지 내 전화번호 추출 (정규식 기반, 보조 수단)."""
    if not text:
        return default
    m = _PHONE_REGEX.search(text)
    if m:
        return m.group(0)
    return default


def _build_metadata(
    url: str,
    site_config: dict[str, Any],
    docs: list[Document],
) -> dict[str, Any]:
    """청크에 부착할 metadata 구성.

    기본적으로 site_config의 contact 정보를 그대로 사용하고,
    URL 경로로 하위 기관 추론, 페이지 본문에서 전화번호 보조 추출.
    """
    institution = site_config["name"]
    sub_inst = infer_sub_institution(url, site_config["code"])
    if sub_inst:
        institution = sub_inst

    contact = site_config.get("contact", {}) or {}
    phone = contact.get("phone")
    page_text = "\n".join([d.page_content for d in docs[:1]]) if docs else ""
    phone = _extract_phone(page_text, phone)

    title = None
    if docs and docs[0].metadata:
        title = docs[0].metadata.get("title")
    if not title and docs:
        first_line = docs[0].page_content.strip().splitlines()[:1]
        if first_line:
            title = first_line[0][:200]

    return {
        "url": url,
        "source": url,  # 기존 RAG 파이프라인이 source 필드를 표준으로 사용
        "name": title or institution,
        "title": title,
        "institution": institution,
        "site_code": site_config["code"],
        "category": site_config.get("default_category") or "기타",
        "contact_phone": phone,
        "contact_email": contact.get("email"),
        "contact_address": contact.get("address"),
        "homepage_url": contact.get("homepage") or site_config["base_url"],
        "crawled_at": int(time.time()),
    }


def _compute_content_hash(docs: list[Document]) -> str:
    h = hashlib.sha256()
    for d in docs:
        h.update((d.page_content or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()


####################
# 단일 페이지 처리
####################


async def _process_url(
    request, site_config: dict[str, Any], url: str, mode: str
) -> str:
    """한 URL에 대해: HEAD 체크 → 로드 → 해시 비교 → 저장.

    반환값: "new" | "updated" | "unchanged" | "skipped" | "error"
    """
    collection_name = getattr(
        request.app.state.config, "CRAWLER_COLLECTION_NAME", "jeonbuk_gov"
    )

    existing = CrawledPages.get_by_url(url)
    head = None

    if mode == "incremental" and existing:
        head = await _http_head(url)
        # ETag 우선 비교
        if (
            existing.http_etag
            and head.get("etag")
            and existing.http_etag == head["etag"]
        ):
            CrawledPages.mark_unchanged(url)
            return "unchanged"
        if (
            existing.http_last_modified
            and head.get("last_modified")
            and existing.http_last_modified == head["last_modified"]
        ):
            CrawledPages.mark_unchanged(url)
            return "unchanged"

    # 페이지 로드
    docs = await _load_page(url)
    if not docs:
        CrawledPages.mark_error(url, site_config["code"], "page load returned empty")
        return "error"

    # 콘텐츠 해시 계산 → 기존과 비교
    content_hash = _compute_content_hash(docs)
    if existing and existing.content_hash == content_hash:
        CrawledPages.mark_unchanged(url)
        return "unchanged"

    # 메타데이터 구성
    metadata = _build_metadata(url, site_config, docs)

    # 기존 청크 삭제 (같은 URL에 대한 이전 버전 제거)
    if existing:
        try:
            VECTOR_DB_CLIENT.delete(
                collection_name=collection_name,
                filter={"url": url},
            )
        except Exception as e:
            log.debug(f"vector delete (pre-update) failed for {url}: {e}")

    # 벡터 DB에 저장 (sync 함수, to_thread로 오프로딩)
    try:
        await asyncio.to_thread(
            save_docs_to_vector_db,
            request,
            docs,
            collection_name,
            metadata,
            False,  # overwrite=False (우리는 url 필터로 직접 delete함)
            True,   # split=True (자동 청킹)
            True,   # add=True (기존 컬렉션에 추가)
            None,   # user=None
        )
    except Exception as e:
        log.warning(f"save_docs_to_vector_db failed for {url}: {e}")
        CrawledPages.mark_error(url, site_config["code"], str(e))
        return "error"

    # CrawledPage upsert (PostgreSQL 추적 테이블)
    # 주의: vector DB 저장은 이미 성공했으므로, upsert 실패 시 raise는 하지 않고
    # 별도 "tracking_missing" 상태로 표시하여 stats에서 집계한다.
    upsert_result = CrawledPages.upsert(
        url=url,
        site_code=site_config["code"],
        institution=metadata["institution"],
        category=metadata["category"],
        title=metadata.get("title"),
        content_hash=content_hash,
        http_etag=(head or {}).get("etag"),
        http_last_modified=(head or {}).get("last_modified"),
        status="success",
        chunks_count=len(docs),
        content_changed=True,
    )
    if upsert_result is None:
        log.error(
            f"crawled_page upsert returned None for {url} "
            f"(site={site_config['code']}) — vector DB saved but tracking row MISSING. "
            f"Check alembic migrations (head should be e7f8a9b0c1d2)."
        )
        return "tracking_missing"

    # GraphRAG 엔티티 추출 (feature flag로 gate, 실패해도 크롤링은 성공 유지)
    if getattr(
        request.app.state.config, "ENABLE_GRAPH_RAG_EXTRACTION", False
    ):
        try:
            await _extract_entities_for_page(request, docs, url, metadata)
        except Exception as e:
            log.warning(
                f"graph entity extraction failed for {url}: {e} "
                "(crawling continues)"
            )

    return "updated" if existing else "new"


async def _extract_entities_for_page(
    request, docs: list[Document], url: str, metadata: dict
) -> None:
    """
    한 페이지의 docs(청크)에서 엔티티/관계 트리플을 LLM으로 추출한 뒤
    entity/entity_mention/entity_relation 테이블에 저장.

    LLM 호출 횟수: URL 당 1회 (docs 합본 텍스트 사용).
    실패는 호출자가 try/except로 처리.
    """
    # 지연 import — 그래프 모듈이 없어도 크롤러 자체는 import 가능해야 함
    from open_webui.retrieval.graph.extractor import (
        build_extraction_messages,
        parse_and_store,
    )
    from open_webui.routers.public_chatbot import _get_public_user
    from open_webui.utils.chat import generate_chat_completion

    if not docs:
        return

    # 페이지 본문 합본 (첫 3청크, 최대 3000자)
    combined = "\n\n".join((d.page_content or "")[:1500] for d in docs[:3])
    if not combined.strip():
        return

    base_model = getattr(
        request.app.state.config, "PUBLIC_CHATBOT_BASE_MODEL", "gpt-5.4-mini"
    )
    form_data = {
        "model": base_model,
        "messages": build_extraction_messages(combined),
        "stream": False,
        "temperature": 0.0,
    }
    user = _get_public_user(request)
    try:
        resp = await generate_chat_completion(
            request, form_data, user=user, bypass_filter=True
        )
    except Exception as e:
        log.warning(f"LLM call failed in entity extraction for {url}: {e}")
        return

    # resp 형태: dict with 'choices' or JSONResponse
    raw_text = _extract_response_text(resp)
    if not raw_text:
        return

    stats = parse_and_store(
        llm_raw_output=raw_text,
        chunk_id=url,  # URL을 chunk_id로 사용 (retriever가 url로 vector filter)
        url=url,
        confidence=0.8,
    )
    log.info(f"graph extraction stored for {url}: {stats}")


def _extract_response_text(resp: Any) -> str:
    """generate_chat_completion 응답에서 text 추출."""
    if isinstance(resp, dict):
        try:
            return resp["choices"][0]["message"]["content"] or ""
        except Exception:
            return ""
    # JSONResponse 등
    try:
        body = getattr(resp, "body", None) or b""
        if body:
            import json as _json
            d = _json.loads(body.decode("utf-8"))
            return d["choices"][0]["message"]["content"] or ""
    except Exception:
        pass
    return ""


####################
# 사이트 단위 크롤링
####################


async def crawl_site(
    request, site_config: dict[str, Any], mode: str = "incremental"
) -> dict[str, Any]:
    """한 사이트를 크롤링.

    mode="full": 기존 상태 무시하고 전부 재수집
    mode="incremental": HEAD + hash로 변경분만 재수집
    """
    start = time.time()
    log.info(f"crawl_site START: {site_config['code']} mode={mode}")

    urls = await discover_urls(site_config)
    delay_ms = int(
        getattr(request.app.state.config, "CRAWLER_REQUEST_DELAY_MS", _DEFAULT_DELAY_MS)
    )
    delay_sec = max(0.0, delay_ms / 1000.0)

    stats = {
        "site_code": site_config["code"],
        "site_name": site_config["name"],
        "total_urls": len(urls),
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "error": 0,
        "tracking_missing": 0,  # vector DB 저장은 됐으나 crawled_page upsert 실패
        "elapsed_sec": 0.0,
    }

    for idx, url in enumerate(urls):
        try:
            result = await _process_url(request, site_config, url, mode)
            stats[result] = stats.get(result, 0) + 1
        except Exception as e:
            log.exception(f"_process_url unexpected error {url}: {e}")
            stats["error"] += 1
            CrawledPages.mark_error(url, site_config["code"], str(e))

        if delay_sec and idx < len(urls) - 1:
            await asyncio.sleep(delay_sec)

    stats["elapsed_sec"] = round(time.time() - start, 2)
    log.info(f"crawl_site DONE: {site_config['code']} stats={stats}")
    return stats


####################
# 전체 크롤링 (배치 진입점)
####################


async def run_full_crawl(request) -> list[dict[str, Any]]:
    """모든 사이트 전체 크롤링."""
    log.info("run_full_crawl START")
    all_stats: list[dict[str, Any]] = []
    for site in SITES:
        try:
            stats = await crawl_site(request, site, mode="full")
            all_stats.append(stats)
        except Exception as e:
            log.exception(f"crawl_site failed for {site['code']}: {e}")
            all_stats.append(
                {"site_code": site["code"], "error_message": str(e), "error": 1}
            )
    log.info(f"run_full_crawl DONE: sites={len(all_stats)}")
    return all_stats


async def run_incremental_crawl(request) -> list[dict[str, Any]]:
    """일별 배치: 변경분만 재수집."""
    log.info("run_incremental_crawl START")
    all_stats: list[dict[str, Any]] = []
    for site in SITES:
        try:
            stats = await crawl_site(request, site, mode="incremental")
            all_stats.append(stats)
        except Exception as e:
            log.exception(f"crawl_site failed for {site['code']}: {e}")
            all_stats.append(
                {"site_code": site["code"], "error_message": str(e), "error": 1}
            )
    log.info(f"run_incremental_crawl DONE: sites={len(all_stats)}")
    return all_stats


async def run_site_crawl(
    request, site_code: str, mode: str = "full"
) -> dict[str, Any]:
    """특정 사이트 하나만 크롤링."""
    site = get_site(site_code)
    if site is None:
        raise ValueError(f"Unknown site_code: {site_code}")
    return await crawl_site(request, site, mode=mode)
