"""Smoke tests against a running amazon-crawler instance.

Default target is http://localhost:8010 (the docker-compose port). Override
with AMAZON_CRAWLER_URL.

Run:
    pytest tests/test_smoke.py -v
or:
    AMAZON_CRAWLER_URL=http://crawler.example.com:8010 pytest tests/test_smoke.py -v
"""
import os
import urllib.request
import json

BASE = os.environ.get("AMAZON_CRAWLER_URL", "http://localhost:8010").rstrip("/")


def _get(path: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def test_health():
    body = _get("/health")
    assert body["status"] == "ok"


def test_index():
    body = _get("/")
    assert body["service"] == "amazon-crawler"
    assert "/scrape-camel" in body["endpoints"]


def test_stats():
    body = _get("/stats")
    assert "api_stats" in body
    assert "camel" in body
    assert "uptime_s" in body


def test_crawler_health_shape():
    body = _get("/crawler-health")
    assert isinstance(body, dict)


def test_amazon_endpoints_return_501():
    """Until direct Amazon scraping is implemented, these should 501."""
    import urllib.error
    for path in ("/amazon/search?q=test", "/amazon/product/B00TEST123", "/amazon/reviews/B00TEST123"):
        try:
            _get(path)
            assert False, f"expected 501 from {path}"
        except urllib.error.HTTPError as e:
            assert e.code == 501, f"{path} returned {e.code}"
