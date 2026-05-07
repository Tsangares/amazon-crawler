"""applesauce-crawlers service entrypoint.

Start: uvicorn main:app --host 0.0.0.0 --port 8014
"""
import logging
import time

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="applesauce-crawlers", version="0.2.0")


@app.get("/")
def index():
    return {
        "service": "applesauce-crawlers",
        "version": "0.2.0",
        "docs": "/docs",
        "endpoints": [
            "/scrape-camel",
            "/scrape",
            "/amazon/search (501)",
            "/amazon/product/{asin} (501)",
            "/amazon/reviews/{asin} (501)",
            "/health",
            "/stats",
            "/crawler-health",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "ts": time.time()}


# Register scraper routes
from scrapers.camel import register_routes as register_camel
from scrapers.amazon import register_routes as register_amazon
from scrapers.ebay import register_routes as register_ebay

register_camel(app)
register_amazon(app)
register_ebay(app)


# Monitoring routes (lightweight; pulls from shared.py state)
from scrapers.shared import (
    _API_STATS, _get_crawler_health, _get_camel_consecutive_fails,
    _get_camel_circuit_open_until, _get_camel_hourly_count, _get_camel_hourly_cap,
    _camel_db_count, _get_api_circuit_open_until,
)


@app.get("/stats")
def stats():
    return {
        "api_stats": _API_STATS,
        "uptime_s": round(time.time() - _API_STATS["started_at"]),
        "camel": {
            "consecutive_fails": _get_camel_consecutive_fails(),
            "circuit_open_until": _get_camel_circuit_open_until(),
            "circuit_open": time.time() < _get_camel_circuit_open_until(),
            "hourly_count": _get_camel_hourly_count(),
            "hourly_cap": _get_camel_hourly_cap(),
            "db_rows": _camel_db_count(),
        },
        "ebay": {
            "api_circuit_open_until": _get_api_circuit_open_until(),
            "api_circuit_open": time.time() < _get_api_circuit_open_until(),
        },
    }


@app.get("/crawler-health")
def crawler_health():
    return _get_crawler_health()


@app.on_event("shutdown")
def _shutdown():
    """Tear down browsers + Playwright cleanly on stop/restart."""
    from scrapers.camel import close_camel_cdp, stop_camel_playwright
    from scrapers.browser import close_browser, stop_playwright
    close_camel_cdp()
    stop_camel_playwright()
    close_browser()
    stop_playwright()
