"""크롤러 엔진 어댑터 — 페이지 URL → langchain Document 리스트.

각 엔진은 동일한 시그니처를 제공한다:
  async def load_url(url, *, verify_ssl=False) -> list[Document]

차후 Firecrawl/Trafilatura 비교 도입 시 같은 인터페이스로 어댑터만 추가.
"""

from .crawl4ai_engine import load_url as crawl4ai_load_url

__all__ = ["crawl4ai_load_url"]
