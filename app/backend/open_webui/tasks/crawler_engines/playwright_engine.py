"""Playwright (Chromium) 어댑터 — JS-rendered SPA 페이지 fallback.

기본 _load_page 는 crawl4ai HTTP-only 로 SSR 페이지를 빠르게 처리.
SPA / JS 로 콘텐츠 렌더링하는 site (jbwelfare, jcid, kogl, lsns,
archives.jb 등) 는 HTTP fetch 만으론 빈 markdown 만 받음 — 이때
이 모듈의 load_url_with_browser 가 fallback 으로 chromium 띄워 렌더된
DOM 의 text 를 추출.

성능 주의:
- 매 URL 마다 browser launch ≈ 2~4초 → BFS 안 처리량 낮음.
- 따라서 SPA site 의 max_pages 는 작게 잡거나 향후 site 단위 browser
  pool 로 개선해야 함. 지금은 simplicity 우선.

환경 가정:
- /root/.cache/ms-playwright/chromium-1217 사전 설치 확인됨.
- /data/root-home/.cache/ms-playwright 영구 볼륨에 보존.
"""

from __future__ import annotations

import logging

from langchain_core.documents import Document

log = logging.getLogger(__name__)


# crawl4ai_engine.py 와 동일한 User-Agent — 동일 site 가 동일 식별자로 다룸.
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
)

# SPA 응답 검증: 추출된 text 가 이 길이 미만이면 의미 있는 콘텐츠 아니라고 본다.
_MIN_TEXT_LEN = 50


async def load_url_with_browser(
    url: str,
    *,
    timeout_ms: int = 25000,
    wait_until: str = "networkidle",
) -> list[Document]:
    """SPA / JS-rendered page 를 chromium 으로 렌더 후 text 추출.

    crawl4ai 가 빈 markdown 반환할 때 호출되는 fallback. 실패 시 빈 리스트.
    예외는 삼키고 로그만 — _load_page 의 정책과 동일.

    Args:
        url: fetch 대상 URL.
        timeout_ms: page.goto / wait 의 timeout (ms). 일부 site 는 networkidle
            안 도달함 → timeout 후에도 page.content() 시도.
        wait_until: playwright wait_until 옵션. "networkidle" 가 SPA 에 최적.
    """
    try:
        from playwright.async_api import async_playwright
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except ImportError as e:
        log.warning(f"playwright not installed: {e}")
        return []

    title = ""
    html = ""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=_DEFAULT_UA,
                    locale="ko-KR",
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                page.set_default_timeout(timeout_ms)
                # 일부 site 는 alert/dialog 띄우면 진행 멈춤 — 자동 dismiss.
                page.on("dialog", lambda d: d.dismiss())
                try:
                    await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    # networkidle 못 도달해도 진행 — 부분 렌더 결과라도 사용
                    log.debug(f"playwright timeout (continue): {url}")
                try:
                    title = (await page.title()).strip()
                except Exception:
                    title = ""
                html = await page.content()
            finally:
                await browser.close()
    except Exception as e:
        log.warning(f"playwright exception for {url}: {e}")
        return []

    if not html:
        return []

    # HTML → text. BeautifulSoup get_text(separator="\n") 가 RAG 청크화에 충분.
    # script/style/noscript 등은 시각 콘텐츠 아니므로 제거.
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
    except Exception as e:
        log.warning(f"BS parse failed for {url}: {e}")
        return []

    if len(text) < _MIN_TEXT_LEN:
        log.info(f"playwright empty/tiny content for {url}: {len(text)} chars")
        return []

    return [
        Document(
            page_content=text,
            metadata={
                "source": url,
                "title": title,
                "language": "ko",
                "loader": "playwright",
            },
        )
    ]
