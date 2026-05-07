"""Smoke tests against a running applesauce-crawlers instance.

Default target is http://localhost:8011 (the systemd unit's port). Override
with APPLESAUCE_CRAWLERS_URL.

Run:
    pytest tests/test_smoke.py -v
or:
    APPLESAUCE_CRAWLERS_URL=http://crawler.example.com:8011 pytest tests/test_smoke.py -v
"""
import os
import urllib.request
import json

BASE = os.environ.get("APPLESAUCE_CRAWLERS_URL", "http://localhost:8011").rstrip("/")


def _get(path: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def test_health():
    body = _get("/health")
    assert body["status"] == "ok"


def test_index():
    body = _get("/")
    assert body["service"] == "applesauce-crawlers"
    assert "/scrape-camel" in body["endpoints"]
    assert "/scrape" in body["endpoints"]


def test_stats():
    body = _get("/stats")
    assert "api_stats" in body
    assert "camel" in body
    assert "ebay" in body
    assert "uptime_s" in body
    assert "ebay_scrape_requests" in body["api_stats"]


def test_crawler_health_shape():
    body = _get("/crawler-health")
    assert isinstance(body, dict)


def test_scrape_endpoint_registered():
    """/scrape (eBay) must require ?q= — confirms the route is wired up
    without actually hitting live eBay."""
    import urllib.error
    try:
        _get("/scrape")
        assert False, "expected 422 when ?q is missing"
    except urllib.error.HTTPError as e:
        assert e.code == 422, f"/scrape (no q) returned {e.code}, expected 422"


def test_amazon_endpoints_return_501():
    """Until direct Amazon scraping is implemented, these should 501."""
    import urllib.error
    for path in ("/amazon/search?q=test", "/amazon/product/B00TEST123", "/amazon/reviews/B00TEST123"):
        try:
            _get(path)
            assert False, f"expected 501 from {path}"
        except urllib.error.HTTPError as e:
            assert e.code == 501, f"{path} returned {e.code}"
