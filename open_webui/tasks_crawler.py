"""
Scheduled crawler task — runs periodically to crawl due targets.
Uses APScheduler BackgroundScheduler.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler = None


async def _run_due_crawls(app):
    """Crawl all targets that are due based on their interval."""
    from starlette.datastructures import Headers
    from starlette.requests import Request
    from open_webui.models.crawl_targets import CrawlTargets
    from open_webui.routers.crawler import _crawl_and_index_target

    targets = CrawlTargets.get_targets_due_for_crawl()
    if not targets:
        log.debug("No crawl targets due")
        return

    log.info(f"Scheduled crawl: {len(targets)} target(s) due")

    # Create mock request with app state for retrieval functions
    mock_request = Request(
        {
            "type": "http",
            "asgi.version": "3.0",
            "asgi.spec_version": "2.0",
            "method": "GET",
            "path": "/internal/crawl",
            "query_string": b"",
            "headers": Headers({}).raw,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
            "scheme": "http",
            "app": app,
        }
    )

    for target in targets:
        try:
            result = await _crawl_and_index_target(mock_request, target)
            log.info(f"Scheduled crawl result for '{target.label}': {result}")
        except Exception as e:
            log.exception(f"Scheduled crawl failed for '{target.label}': {e}")


def start_crawl_scheduler(app, interval_minutes: int = 30):
    """Start the background scheduler that checks for due crawl targets."""
    global _scheduler

    if _scheduler is not None:
        log.warning("Crawl scheduler already running")
        return

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _run_due_crawls,
        "interval",
        minutes=interval_minutes,
        args=[app],
        id="crawl_due_targets",
        name="Crawl due homepage targets",
        misfire_grace_time=300,
    )
    _scheduler.start()
    log.info(f"Crawl scheduler started (checking every {interval_minutes} minutes)")


def stop_crawl_scheduler():
    """Stop the background scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("Crawl scheduler stopped")
