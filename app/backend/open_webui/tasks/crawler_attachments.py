"""
크롤링된 페이지의 첨부 파일 + 인라인 이미지 처리 파이프라인.

흐름:
1. 페이지 HTML 에서 attachment 링크와 inline `<img>` 추출
2. 각 자원을 임시 파일로 다운로드
3. 적절한 Loader (Mineru/DeepSeekOCR/HWPLoader/PyPDF/...) 통과 → Document 생성
4. save_docs_to_vector_db 로 같은 collection 에 저장
5. CrawledAttachments 테이블에 상태 기록

이미지 처리:
- PDF/HWP/HWPX 안의 이미지/표는 MinerU 혹은 DeepSeekOCR 가 OCR 처리해서 텍스트로 변환
- 페이지 인라인 `<img>` 는 DeepSeekOCR 로 단일 이미지 처리 (로컬 OCR 서버 필요)
- OCR 서버가 응답하지 않으면 기본 alt/caption fallback 으로 처리하고 status='error' 기록
"""

import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from langchain_core.documents import Document

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.crawler import CrawledAttachments
from open_webui.retrieval.loaders.main import Loader as DocLoader

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)


_DEFAULT_HEADERS = {
    "User-Agent": "JeonbukBot/1.0 (+https://www.jeonbuk.go.kr)",
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# 처리 가능한 첨부 확장자
_DOC_EXTS = {
    "pdf",
    "hwp",
    "hwpx",
    "docx",
    "doc",
    "xlsx",
    "xls",
    "pptx",
    "ppt",
    "txt",
    "md",
    "csv",
}
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif"}
# 무시할 도메인/링크 (사이트 외부 광고 등)
_SKIP_PATTERNS = re.compile(
    r"(googletagmanager|google-analytics|adservice|facebook\.com|twitter\.com)",
    re.IGNORECASE,
)
# 이미지 URL 중 명백히 UI 자원인 경우 (작은 아이콘 등) 무시
_IMG_SKIP_HINTS = re.compile(
    r"(icon|button|btn_|sprite|spinner|loader|favicon|/ui/|/css/|emoji)",
    re.IGNORECASE,
)
# 요청 timeout / 다운로드 한도
_DOWNLOAD_TIMEOUT = 30
_MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024  # 60 MB
_PER_PAGE_ATTACHMENT_LIMIT = 30
_PER_PAGE_IMAGE_LIMIT = 15


####################
# Link extraction
####################


def _ext_from_url(url: str) -> Optional[str]:
    """URL 에서 파일 확장자 추출. 쿼리스트링 제거 후 마지막 . 이후 토큰."""
    path = urlparse(url).path
    path = unquote(path)
    if "." not in path:
        return None
    ext = path.rsplit(".", 1)[-1].lower()
    return ext if 0 < len(ext) <= 6 else None


def _ext_from_content_type(ct: Optional[str]) -> Optional[str]:
    if not ct:
        return None
    ct = ct.split(";")[0].strip().lower()
    if "pdf" in ct:
        return "pdf"
    if "hwp" in ct:
        return "hwp"
    if "msword" in ct or "wordprocessingml" in ct:
        return "docx"
    if "ms-excel" in ct or "spreadsheetml" in ct:
        return "xlsx"
    if "powerpoint" in ct or "presentationml" in ct:
        return "pptx"
    if ct.startswith("image/"):
        sub = ct.split("/", 1)[1]
        if sub in {"png", "jpeg", "jpg", "gif", "webp", "bmp", "tiff"}:
            return "jpg" if sub == "jpeg" else sub
    if ct == "text/plain":
        return "txt"
    return None


def extract_attachment_links(html: str, base_url: str) -> list[dict[str, Any]]:
    """페이지 HTML 에서 첨부 가능한 링크 추출.

    반환: [{"url": ..., "ext": "pdf", "anchor_text": ...}]
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        if _SKIP_PATTERNS.search(href):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue

        ext = _ext_from_url(absolute)
        # 다운로드 라우터처럼 확장자가 없는 경우 anchor 의 hint 로 추정
        anchor_text = (a.get_text() or "").strip()[:200]
        if not ext:
            # 정부 사이트의 download.* 패턴 + 데이터에 .pdf/.hwp 표기가 있으면 pdf 로 가정
            hint = (
                href
                + " "
                + (a.get("title") or "")
                + " "
                + anchor_text
            ).lower()
            if "download" in hint or "atchfileuid" in hint or "filesid" in hint:
                m = re.search(r"\.(pdf|hwpx?|docx?|xlsx?|pptx?)\b", hint)
                if m:
                    ext = m.group(1)
                    if ext == "doc":
                        ext = "doc"
                    elif ext == "xls":
                        ext = "xls"
                    elif ext == "ppt":
                        ext = "ppt"
                    elif ext == "hwp":
                        ext = "hwp"
                    elif ext == "hwpx":
                        ext = "hwpx"
                else:
                    # 알 수 없으면 일단 pdf 로 시도 (정부 첨부 대부분 pdf/hwp)
                    ext = "unknown"

        if not ext:
            continue
        if ext in _DOC_EXTS or ext in _IMAGE_EXTS or ext == "unknown":
            seen.add(absolute)
            results.append(
                {
                    "url": absolute,
                    "ext": ext,
                    "anchor_text": anchor_text,
                }
            )
        if len(results) >= _PER_PAGE_ATTACHMENT_LIMIT:
            break
    return results


def extract_image_links(
    html: str, base_url: str, min_size_hint: int = 80
) -> list[dict[str, Any]]:
    """페이지 인라인 `<img>` 추출.

    - UI 자원/아이콘으로 보이는 패턴은 제외
    - width/height 속성이 있고 너무 작은 것은 제외
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src:
            continue
        if _IMG_SKIP_HINTS.search(src):
            continue
        absolute = urljoin(base_url, src)
        if absolute in seen:
            continue

        # 작은 이미지 거르기
        try:
            w = int(img.get("width", "0").rstrip("px") or 0)
            h = int(img.get("height", "0").rstrip("px") or 0)
            if (w and w < min_size_hint) or (h and h < min_size_hint):
                continue
        except ValueError:
            pass

        ext = _ext_from_url(absolute)
        if ext and ext not in _IMAGE_EXTS:
            continue
        alt = (img.get("alt") or "").strip()[:200]
        seen.add(absolute)
        results.append(
            {
                "url": absolute,
                "ext": ext or "img",
                "alt": alt,
            }
        )
        if len(results) >= _PER_PAGE_IMAGE_LIMIT:
            break
    return results


####################
# Download
####################


async def _download(
    session: aiohttp.ClientSession, url: str
) -> Optional[tuple[bytes, str, str]]:
    """URL 다운로드.

    반환: (bytes, content_type, suggested_filename) 또는 None.
    """
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                log.debug(f"download {url} -> HTTP {resp.status}")
                return None
            ctype = resp.headers.get("Content-Type", "")
            cd = resp.headers.get("Content-Disposition", "")
            # 파일명 추출
            fname = None
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
            if m:
                fname = unquote(m.group(1).strip())
            if not fname:
                fname = unquote(urlparse(url).path.rsplit("/", 1)[-1] or "download")

            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > _MAX_DOWNLOAD_BYTES:
                    log.warning(f"download {url} exceeded size limit, truncating")
                    return None
            return bytes(data), ctype, fname
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        log.debug(f"download error {url}: {e}")
        return None


####################
# Loader dispatch
####################


def _build_loader(request) -> DocLoader:
    """요청 컨텍스트의 RAG 설정으로 Loader 인스턴스 생성.

    crawler 호출 컨텍스트에서는 routers.retrieval 의 분기 코드를 그대로 재사용한다.
    OCR 서버 URL 등은 환경변수에서 자동 로드된다.
    """
    from open_webui.env import OCR_SERVER_URL  # late import

    cfg = getattr(request.app.state, "config", None)
    kwargs = {}
    if cfg:
        # mineru / docling / external 등 활성 엔진을 그대로 따라간다
        engine = (
            getattr(cfg, "CONTENT_EXTRACTION_ENGINE", "")
            or getattr(cfg, "RAG_CONTENT_EXTRACTION_ENGINE", "")
            or ""
        )
        kwargs["MINERU_API_URL"] = getattr(cfg, "MINERU_API_URL", "") or ""
        kwargs["MINERU_API_MODE"] = getattr(cfg, "MINERU_API_MODE", "local") or "local"
        kwargs["MINERU_API_KEY"] = getattr(cfg, "MINERU_API_KEY", "") or ""
        kwargs["MINERU_API_TIMEOUT"] = getattr(cfg, "MINERU_API_TIMEOUT", 300) or 300
        kwargs["MINERU_PARAMS"] = getattr(cfg, "MINERU_PARAMS", {}) or {}
        kwargs["TIKA_SERVER_URL"] = getattr(cfg, "TIKA_SERVER_URL", "") or ""
        kwargs["DOCLING_SERVER_URL"] = getattr(cfg, "DOCLING_SERVER_URL", "") or ""
        kwargs["DOCUMENT_INTELLIGENCE_ENDPOINT"] = (
            getattr(cfg, "DOCUMENT_INTELLIGENCE_ENDPOINT", "") or ""
        )
        kwargs["DOCUMENT_INTELLIGENCE_KEY"] = (
            getattr(cfg, "DOCUMENT_INTELLIGENCE_KEY", "") or ""
        )
        kwargs["DOCUMENT_INTELLIGENCE_MODEL"] = (
            getattr(cfg, "DOCUMENT_INTELLIGENCE_MODEL", "") or ""
        )
        kwargs["DATALAB_MARKER_API_KEY"] = (
            getattr(cfg, "DATALAB_MARKER_API_KEY", "") or ""
        )
        kwargs["DATALAB_MARKER_API_BASE_URL"] = (
            getattr(cfg, "DATALAB_MARKER_API_BASE_URL", "") or ""
        )
        kwargs["EXTERNAL_DOCUMENT_LOADER_URL"] = (
            getattr(cfg, "EXTERNAL_DOCUMENT_LOADER_URL", "") or ""
        )
        kwargs["EXTERNAL_DOCUMENT_LOADER_API_KEY"] = (
            getattr(cfg, "EXTERNAL_DOCUMENT_LOADER_API_KEY", "") or ""
        )
        kwargs["MISTRAL_OCR_API_KEY"] = (
            getattr(cfg, "MISTRAL_OCR_API_KEY", "") or ""
        )
        kwargs["MISTRAL_OCR_API_BASE_URL"] = (
            getattr(cfg, "MISTRAL_OCR_API_BASE_URL", "") or ""
        )
        kwargs["PDF_EXTRACT_IMAGES"] = bool(
            getattr(cfg, "PDF_EXTRACT_IMAGES", True)
        )
        kwargs["HWP_JAR_PATH"] = getattr(
            cfg,
            "HWP_JAR_PATH",
            "/app/backend/python-hwplib/hwplib-1.1.8.jar",
        )
        kwargs["HWPX_JAR_PATH"] = getattr(
            cfg,
            "HWPX_JAR_PATH",
            "/app/backend/python-hwpxlib/hwpxlib-1.0.5.jar",
        )
        return DocLoader(engine=engine, **kwargs)

    # config 가 없는 컨텍스트 — 최소 기본값
    return DocLoader(engine="")


def _ocr_image_file(file_path: str) -> list[Document]:
    """이미지 단일 파일 → DeepSeekOCRLoader 로 OCR.

    이미지 분기는 Loader._get_loader 가 PDF 만 지원하므로 직접 호출.
    """
    from open_webui.env import OCR_SERVER_URL  # late import
    from open_webui.retrieval.loaders.deepseek_ocr_loader import DeepSeekOCRLoader

    if not OCR_SERVER_URL:
        return []
    try:
        loader = DeepSeekOCRLoader(
            file_path=file_path,
            ocr_server_url=str(OCR_SERVER_URL),
            extract_images=True,
        )
        return loader.load()
    except Exception as e:
        log.debug(f"OCR loader failed for {file_path}: {e}")
        return []


def _content_type_for_ext(ext: str) -> str:
    return mimetypes.types_map.get(f".{ext}", "application/octet-stream")


####################
# Per-attachment processing
####################


def _doc_chunks_via_loader(
    request, file_path: str, filename: str, ext: str
) -> list[Document]:
    """파일을 Loader 로 처리해서 Document 리스트 반환.

    이미지는 OCR 로 분기. 그 외는 표준 디스패치.
    """
    if ext in _IMAGE_EXTS or ext == "img":
        return _ocr_image_file(file_path)

    loader = _build_loader(request)
    content_type = _content_type_for_ext(ext)
    try:
        return loader.load(filename, content_type, file_path)
    except Exception as e:
        log.debug(f"primary loader failed for {filename}: {e}; trying fallback")
        # PDF/HWP 가 OCR 의존인데 서버 죽었을 수 있음 — 기본 PyPDF/Text 시도
        try:
            fb = DocLoader(engine="pypdf")
            return fb.load(filename, content_type, file_path)
        except Exception as e2:
            log.warning(f"fallback loader also failed for {filename}: {e2}")
            return []


async def process_one_attachment(
    request,
    *,
    session: aiohttp.ClientSession,
    page_url: str,
    page_metadata: dict[str, Any],
    site_code: str,
    collection_name: str,
    attachment_url: str,
    ext: Optional[str],
    anchor_text: str = "",
    is_image: bool = False,
) -> str:
    """단일 첨부/이미지 처리. 반환: "processed" | "skipped" | "error" """
    # 이미 처리된 경우 skip
    existing = CrawledAttachments.get_by_url(attachment_url)
    if existing and existing.status == "processed":
        return "skipped"

    download = await _download(session, attachment_url)
    if download is None:
        CrawledAttachments.upsert(
            url=attachment_url,
            source_page_url=page_url,
            site_code=site_code,
            file_type=ext,
            status="error",
            error_message="download failed",
        )
        return "error"

    data, ctype, fname = download
    # ext 보정
    file_ext = (
        ext
        or _ext_from_url(attachment_url)
        or _ext_from_content_type(ctype)
        or ("png" if is_image else "bin")
    )
    if file_ext == "unknown":
        file_ext = _ext_from_content_type(ctype) or "pdf"

    content_hash = hashlib.sha256(data).hexdigest()
    if existing and existing.content_hash == content_hash:
        CrawledAttachments.upsert(
            url=attachment_url,
            source_page_url=page_url,
            site_code=site_code,
            file_type=file_ext,
            file_size=len(data),
            content_hash=content_hash,
            status="processed",
        )
        return "skipped"

    # 임시 파일에 저장
    safe_fname = re.sub(r"[\\/:*?\"<>|]", "_", fname)[:120]
    if "." not in safe_fname:
        safe_fname = f"{safe_fname}.{file_ext}"
    tmp_dir = tempfile.mkdtemp(prefix="crawl_att_")
    file_path = os.path.join(tmp_dir, safe_fname)
    try:
        with open(file_path, "wb") as f:
            f.write(data)

        # Loader → Document 리스트
        docs = await asyncio.to_thread(
            _doc_chunks_via_loader, request, file_path, safe_fname, file_ext
        )
        if not docs:
            CrawledAttachments.upsert(
                url=attachment_url,
                source_page_url=page_url,
                site_code=site_code,
                file_type=file_ext,
                file_size=len(data),
                content_hash=content_hash,
                status="error",
                error_message="loader returned empty",
            )
            return "error"

        # 메타데이터 합성: 페이지 metadata + attachment 정보
        att_metadata = {
            **page_metadata,
            "url": attachment_url,
            "source_page_url": page_url,
            "site_code": site_code,
            "attachment": True,
            "is_image": is_image,
            "anchor_text": anchor_text or page_metadata.get("title", ""),
            "file_type": file_ext,
            "file_name": safe_fname,
        }

        # 기존 청크 삭제 후 재저장
        from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
        from open_webui.routers.retrieval import save_docs_to_vector_db

        try:
            VECTOR_DB_CLIENT.delete(
                collection_name=collection_name,
                filter={"url": attachment_url},
            )
        except Exception as e:
            log.debug(f"delete pre-update for {attachment_url}: {e}")

        try:
            ok = await asyncio.to_thread(
                save_docs_to_vector_db,
                request,
                docs,
                collection_name,
                att_metadata,
                False,
                True,
                True,
                None,
            )
        except Exception as e:
            CrawledAttachments.upsert(
                url=attachment_url,
                source_page_url=page_url,
                site_code=site_code,
                file_type=file_ext,
                file_size=len(data),
                content_hash=content_hash,
                status="error",
                error_message=f"save_docs_to_vector_db failed: {e}",
            )
            return "error"

        if not ok:
            CrawledAttachments.upsert(
                url=attachment_url,
                source_page_url=page_url,
                site_code=site_code,
                file_type=file_ext,
                file_size=len(data),
                content_hash=content_hash,
                status="error",
                error_message="save_docs_to_vector_db returned False",
            )
            return "error"

        CrawledAttachments.upsert(
            url=attachment_url,
            source_page_url=page_url,
            site_code=site_code,
            file_type=file_ext,
            file_size=len(data),
            content_hash=content_hash,
            chunks_count=len(docs),
            status="processed",
        )
        return "processed"
    finally:
        try:
            os.remove(file_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


async def process_page_attachments(
    request,
    *,
    page_url: str,
    page_html: str,
    page_metadata: dict[str, Any],
    site_code: str,
    collection_name: str,
    include_images: bool = True,
) -> dict[str, int]:
    """페이지의 첨부 + 인라인 이미지를 모두 처리.

    반환: {"processed": int, "skipped": int, "error": int, "discovered": int}
    """
    counts = {"processed": 0, "skipped": 0, "error": 0, "discovered": 0}

    attachments = extract_attachment_links(page_html, page_url)
    images = extract_image_links(page_html, page_url) if include_images else []
    counts["discovered"] = len(attachments) + len(images)
    if counts["discovered"] == 0:
        return counts

    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=4, ssl=False)
    async with aiohttp.ClientSession(
        headers=_DEFAULT_HEADERS, timeout=timeout, connector=connector
    ) as session:
        # 첨부 (직렬: OCR/loader 부하 제어)
        for att in attachments:
            try:
                r = await process_one_attachment(
                    request,
                    session=session,
                    page_url=page_url,
                    page_metadata=page_metadata,
                    site_code=site_code,
                    collection_name=collection_name,
                    attachment_url=att["url"],
                    ext=att.get("ext"),
                    anchor_text=att.get("anchor_text", ""),
                    is_image=att.get("ext") in _IMAGE_EXTS,
                )
                counts[r] = counts.get(r, 0) + 1
            except Exception as e:
                log.exception(f"attachment {att['url']} failed: {e}")
                counts["error"] += 1
        # 인라인 이미지
        for im in images:
            try:
                r = await process_one_attachment(
                    request,
                    session=session,
                    page_url=page_url,
                    page_metadata=page_metadata,
                    site_code=site_code,
                    collection_name=collection_name,
                    attachment_url=im["url"],
                    ext=im.get("ext"),
                    anchor_text=im.get("alt", ""),
                    is_image=True,
                )
                counts[r] = counts.get(r, 0) + 1
            except Exception as e:
                log.exception(f"image {im['url']} failed: {e}")
                counts["error"] += 1
    return counts
