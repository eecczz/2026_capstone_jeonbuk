"""Crawl4AI HTTP-only 어댑터.

도청 도메인 게시판은 SSR 정적 HTML 이라 브라우저 없이 HTTP fetch 만으로 본문이 들어온다.
AsyncHTTPCrawlerStrategy 가 LXML 기반 scraping + 마크다운 변환을 수행하므로
표/링크/제목 구조가 기존 langchain PlaywrightURLLoader 보다 잘 보존됨.

도청 환경 특이사항:
- self-signed cert 가 많으므로 verify_ssl=False 기본
- ms-playwright Chromium 다운로드가 방화벽으로 차단되어 있어 HTTP-only 전략 사용
- JS 렌더링이 필요한 페이지가 발견되면 그때 별도 브라우저 어댑터 추가
"""

import logging

from langchain_core.documents import Document

log = logging.getLogger(__name__)


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko,en;q=0.7",
}


async def load_url(url: str, *, verify_ssl: bool = False) -> list[Document]:
    """URL 을 Crawl4AI HTTP 전략으로 fetch + markdown 추출.

    실패 시 빈 리스트. 예외는 삼키고 로그만 — 기존 _load_page_sync 와 동일 정책.
    """
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
        from crawl4ai.async_crawler_strategy import (
            AsyncHTTPCrawlerStrategy,
            HTTPCrawlerConfig,
        )
    except ImportError as e:
        log.warning(f"crawl4ai not installed: {e}")
        return []

    http_cfg = HTTPCrawlerConfig(
        method="GET",
        verify_ssl=verify_ssl,
        follow_redirects=True,
        headers=_DEFAULT_HEADERS,
    )
    strategy = AsyncHTTPCrawlerStrategy(browser_config=http_cfg)
    run_cfg = CrawlerRunConfig()

    try:
        async with AsyncWebCrawler(crawler_strategy=strategy) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)
    except Exception as e:
        log.warning(f"crawl4ai exception for {url}: {e}")
        return []

    if not (result and result.success):
        log.warning(
            f"crawl4ai non-success for {url}: status={getattr(result, 'status_code', None)}"
        )
        return []

    md = (result.markdown.raw_markdown if result.markdown else "") or ""
    if not md.strip():
        log.warning(f"crawl4ai empty markdown for {url}")
        return []

    meta = result.metadata or {}
    site_title = (meta.get("title") or "").strip()
    # 도청 사이트들은 <title> 이 사이트 default 만 잡힘 ("전북특별자치도", "정읍시청"
    # 등). 진짜 게시물 제목은 markdown 본문 첫 #### 또는 ### 헤더에 있음.
    # 본문 헤더가 더 구체적이면 우선 사용. 단 너무 짧으면 (5자 미만) site_title 폴백.
    body_title = ""
    import re as _re
    m = _re.search(r"^#{2,5}\s+(.+?)$", md, _re.M)
    if m:
        cand = m.group(1).strip()
        # 마크다운 emphasis / 링크 제거
        cand = _re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cand)
        cand = _re.sub(r"[\*_`]", "", cand).strip()
        if 5 <= len(cand) <= 200:
            body_title = cand
    title = body_title if (body_title and body_title != site_title) else site_title
    return [
        Document(
            page_content=md,
            metadata={
                "source": url,
                "title": title,
                "site_title": site_title,
                "language": meta.get("language") or "ko",
                "loader": "crawl4ai",
                "status_code": result.status_code,
            },
        )
    ]
