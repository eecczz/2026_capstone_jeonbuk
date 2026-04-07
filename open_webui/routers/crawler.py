import asyncio
import hashlib
import logging
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from open_webui.constants import ERROR_MESSAGES
from open_webui.models.crawl_targets import (
    CrawlTargetForm,
    CrawlTargetModel,
    CrawlTargets,
    CrawlTargetUpdateForm,
)
from open_webui.utils.auth import get_admin_user

log = logging.getLogger(__name__)

router = APIRouter()


############################
# CRUD Endpoints
############################


@router.get("/targets", response_model=list[CrawlTargetModel])
async def get_crawl_targets(user=Depends(get_admin_user)):
    return CrawlTargets.get_targets()


@router.post("/targets/create", response_model=Optional[CrawlTargetModel])
async def create_crawl_target(
    form_data: CrawlTargetForm,
    user=Depends(get_admin_user),
):
    target = CrawlTargets.insert_new_target(user.id, form_data)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create crawl target. URL may already exist.",
        )
    return target


@router.get("/targets/{target_id}", response_model=Optional[CrawlTargetModel])
async def get_crawl_target(target_id: str, user=Depends(get_admin_user)):
    target = CrawlTargets.get_target_by_id(target_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crawl target not found.",
        )
    return target


@router.post("/targets/{target_id}/update", response_model=Optional[CrawlTargetModel])
async def update_crawl_target(
    target_id: str,
    form_data: CrawlTargetUpdateForm,
    user=Depends(get_admin_user),
):
    target = CrawlTargets.update_target_by_id(target_id, form_data)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crawl target not found.",
        )
    return target


@router.delete("/targets/{target_id}")
async def delete_crawl_target(
    request: Request,
    target_id: str,
    user=Depends(get_admin_user),
):
    target = CrawlTargets.get_target_by_id(target_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crawl target not found.",
        )

    # Delete vector DB collection
    if target.collection_name:
        try:
            from open_webui.retrieval.vector.main import VECTOR_DB_CLIENT

            VECTOR_DB_CLIENT.delete_collection(target.collection_name)
        except Exception as e:
            log.warning(f"Failed to delete collection {target.collection_name}: {e}")

    CrawlTargets.delete_target_by_id(target_id)
    return {"status": True}


############################
# Crawl Execution
############################


def _discover_urls(base_url: str, max_depth: int, verify_ssl: bool = False) -> list[str]:
    """Discover URLs from a site by following links up to max_depth."""
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc
    visited = set()
    to_visit = [(base_url, 0)]
    discovered = []

    while to_visit:
        url, depth = to_visit.pop(0)

        # Normalize
        url = url.split("#")[0].split("?")[0].rstrip("/")
        if url in visited:
            continue
        if depth > max_depth:
            continue

        visited.add(url)

        try:
            resp = requests.get(url, timeout=15, verify=verify_ssl, headers={
                "User-Agent": "Mozilla/5.0 (compatible; JeonbukAI-Crawler/1.0)"
            })
            if resp.status_code != 200:
                continue
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type:
                continue
        except Exception as e:
            log.debug(f"Failed to fetch {url}: {e}")
            continue

        discovered.append(url)

        # Don't follow links beyond max_depth
        if depth >= max_depth:
            continue

        # Parse links
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                full_url = urljoin(url, href)
                parsed = urlparse(full_url)

                # Same domain only
                if parsed.netloc != base_domain:
                    continue
                # HTTP(S) only
                if parsed.scheme not in ("http", "https"):
                    continue

                clean_url = full_url.split("#")[0].split("?")[0].rstrip("/")
                if clean_url not in visited:
                    to_visit.append((clean_url, depth + 1))
        except Exception as e:
            log.debug(f"Failed to parse links from {url}: {e}")

    return discovered


def _try_sitemap(base_url: str) -> list[str]:
    """Try to discover URLs from sitemap.xml."""
    sitemap_urls_to_try = [
        f"{base_url.rstrip('/')}/sitemap.xml",
        f"{base_url.rstrip('/')}/sitemap_index.xml",
    ]
    discovered = []

    for sitemap_url in sitemap_urls_to_try:
        try:
            resp = requests.get(sitemap_url, timeout=15, verify=False, headers={
                "User-Agent": "Mozilla/5.0 (compatible; JeonbukAI-Crawler/1.0)"
            })
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "xml")

            # Check for sitemap index (contains other sitemaps)
            sitemaps = soup.find_all("sitemap")
            if sitemaps:
                for sm in sitemaps:
                    loc = sm.find("loc")
                    if loc:
                        try:
                            sub_resp = requests.get(loc.text.strip(), timeout=15, verify=False)
                            sub_soup = BeautifulSoup(sub_resp.text, "xml")
                            for url_tag in sub_soup.find_all("url"):
                                loc_tag = url_tag.find("loc")
                                if loc_tag:
                                    discovered.append(loc_tag.text.strip())
                        except Exception:
                            pass

            # Direct URL entries
            for url_tag in soup.find_all("url"):
                loc = url_tag.find("loc")
                if loc:
                    discovered.append(loc.text.strip())

            if discovered:
                break
        except Exception as e:
            log.debug(f"Sitemap fetch failed for {sitemap_url}: {e}")

    return discovered


async def _crawl_and_index_target(request: Request, target: CrawlTargetModel):
    """Crawl a target site and save content to vector DB."""
    from open_webui.retrieval.utils import get_content_from_url
    from open_webui.routers.retrieval import save_docs_to_vector_db

    log.info(f"Starting crawl for '{target.label}' ({target.url})")
    CrawlTargets.update_crawl_status(target.id, "in_progress")

    try:
        # Step 1: Discover URLs (try sitemap first, fallback to link crawl)
        urls = await run_in_threadpool(_try_sitemap, target.url)
        if not urls:
            log.info(f"No sitemap found for {target.url}, falling back to link crawl")
            urls = await run_in_threadpool(
                _discover_urls,
                target.url,
                target.max_depth,
            )

        log.info(f"Discovered {len(urls)} URLs for '{target.label}'")

        if not urls:
            CrawlTargets.update_crawl_status(target.id, "success", page_count=0)
            return {"status": True, "pages": 0}

        # Step 2: Fetch content and save to vector DB
        all_docs = []
        success_count = 0

        for url in urls:
            try:
                content, docs = await run_in_threadpool(
                    get_content_from_url, request, url
                )
                if docs:
                    # Add source URL metadata
                    for doc in docs:
                        doc.metadata["source"] = url
                        doc.metadata["crawl_target_id"] = target.id
                        doc.metadata["crawl_target_label"] = target.label
                    all_docs.extend(docs)
                    success_count += 1
            except Exception as e:
                log.debug(f"Failed to fetch {url}: {e}")
                continue

        # Step 3: Save all docs to vector DB in one batch
        if all_docs and target.collection_name:
            await run_in_threadpool(
                save_docs_to_vector_db,
                request,
                all_docs,
                target.collection_name,
                overwrite=True,
            )

        CrawlTargets.update_crawl_status(
            target.id, "success", page_count=success_count
        )
        log.info(
            f"Crawl complete for '{target.label}': "
            f"{success_count}/{len(urls)} pages indexed"
        )
        return {"status": True, "pages": success_count, "total_urls": len(urls)}

    except Exception as e:
        log.exception(f"Crawl failed for '{target.label}': {e}")
        CrawlTargets.update_crawl_status(target.id, "failed")
        return {"status": False, "error": str(e)}


@router.post("/targets/{target_id}/crawl")
async def trigger_crawl(
    request: Request,
    target_id: str,
    user=Depends(get_admin_user),
):
    """Manually trigger a crawl for a specific target."""
    target = CrawlTargets.get_target_by_id(target_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crawl target not found.",
        )

    result = await _crawl_and_index_target(request, target)
    return result


@router.post("/crawl/all")
async def trigger_crawl_all(
    request: Request,
    user=Depends(get_admin_user),
):
    """Manually trigger crawl for all active targets."""
    targets = CrawlTargets.get_active_targets()
    results = []

    for target in targets:
        result = await _crawl_and_index_target(request, target)
        results.append({"target_id": target.id, "label": target.label, **result})

    return {"status": True, "results": results}


@router.post("/crawl/due")
async def trigger_crawl_due(
    request: Request,
    user=Depends(get_admin_user),
):
    """Trigger crawl for targets that are due based on their interval."""
    targets = CrawlTargets.get_targets_due_for_crawl()
    results = []

    for target in targets:
        result = await _crawl_and_index_target(request, target)
        results.append({"target_id": target.id, "label": target.label, **result})

    return {"status": True, "results": results}
