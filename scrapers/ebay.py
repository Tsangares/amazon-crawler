"""eBay sold-listings scraper.

Ported from sell.applesauce.chat sell/scrapers/ebay.py (2026-05-06). Two
paths:

- Finding API (`findCompletedItems`, app-token auth) — cheap when it
  works, but rate-limited and currently flaky in prod, so it sits
  behind a circuit breaker.
- Playwright fallback against `ebay.com/sch/...?LH_Sold=1&LH_Complete=1`
  — slower but the actual workhorse today.

The user-facing OAuth consent flow + `.env` writing in the original
sell module is intentionally NOT ported here — that belongs to the app
that owns the user, not to a shared crawler service.
"""
import base64
import os
import threading
import time

import requests as http_requests
from fastapi import Query

from scrapers.shared import (
    log, RateLimitError,
    _API_STATS,
    _cache_get, _cache_set, _log_timing, _log_resource, _snapshot_resources,
    _INFLIGHT, _INFLIGHT_LOCK,
    _set_api_circuit_open, _is_api_circuit_open, _get_api_circuit_open_until,
    _crawler_record_success, _crawler_record_failure,
)
from scrapers.browser import (
    get_context, _increment_browser_requests, run_on_pw_thread,
)

# Dedicated semaphore so eBay PW scrapes don't contend with Mercari/etc later.
_PW_SEMAPHORE = threading.Semaphore(1)

# ── eBay Finding API credentials (optional) ──────────────────────────
EBAY_APP_ID = os.getenv("EBAY_APP_ID", "")
EBAY_CERT_ID = os.getenv("EBAY_CERT_ID", "")
EBAY_SANDBOX = os.getenv("EBAY_SANDBOX", "false").lower() == "true"
# The original sell module ALSO supports Marketplace Insights via a
# user-level refresh token. That requires the per-user OAuth flow which
# we deliberately don't host here. Sell can keep doing that itself.

_FINDING_API_PROD = "https://svcs.ebay.com/services/search/FindingService/v1"
_FINDING_API_SBX = "https://svcs.sandbox.ebay.com/services/search/FindingService/v1"


def _get_finding_api_url():
    return _FINDING_API_SBX if EBAY_SANDBOX else _FINDING_API_PROD


def _scrape_via_finding_api(query: str, per_page: int = 50) -> list[dict]:
    """Use eBay Finding API (findCompletedItems) for sold listings.

    Returns items in scraper format. Empty list on any error; raises
    RateLimitError if eBay tells us we're rate-limited (so the caller
    can record that and trip the circuit breaker).
    """
    if not EBAY_APP_ID:
        return []

    try:
        r = http_requests.get(
            _get_finding_api_url(),
            params={
                "OPERATION-NAME": "findCompletedItems",
                "SERVICE-VERSION": "1.13.0",
                "SECURITY-APPNAME": EBAY_APP_ID,
                "RESPONSE-DATA-FORMAT": "JSON",
                "keywords": query,
                "itemFilter(0).name": "SoldItemsOnly",
                "itemFilter(0).value": "true",
                "sortOrder": "EndTimeSoonest",
                "paginationInput.entriesPerPage": str(per_page),
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("Finding API HTTP %s: %s", r.status_code, r.text[:200])
            try:
                err_data = r.json()
                err_list = err_data.get("errorMessage", [{}])
                if isinstance(err_list, list) and err_list:
                    for err in err_list[0].get("error", []):
                        eid = err.get("errorId", [""])[0] if isinstance(err.get("errorId"), list) else str(err.get("errorId", ""))
                        if str(eid) in ("18", "10001"):
                            _set_api_circuit_open()
                            raise RateLimitError("eBay Finding API rate limited")
            except RateLimitError:
                raise
            except Exception:
                pass
            return []

        data = r.json()

        errors = []
        if "errorMessage" in data and "findCompletedItemsResponse" not in data:
            errors = data["errorMessage"][0].get("error", []) if isinstance(data["errorMessage"], list) else []

        resp = data.get("findCompletedItemsResponse", [{}])[0]
        ack = resp.get("ack", [None])[0]

        if not errors and ack != "Success":
            errors = resp.get("errorMessage", [{}])[0].get("error", [])

        if errors or ack != "Success":
            for err in errors:
                err_id = err.get("errorId", [""])[0] if isinstance(err.get("errorId"), list) else err.get("errorId", "")
                err_msg = err.get("message", [""])[0] if isinstance(err.get("message"), list) else err.get("message", "")
                log.warning("Finding API error %s: %s", err_id, err_msg)
            is_rate_limited = any(
                (str(err.get("errorId", [""])[0] if isinstance(err.get("errorId"), list) else err.get("errorId", "")) in ("18", "10001")
                 or "exceeded" in str(err.get("message", [""])).lower()
                 or "rate" in str(err.get("message", [""])).lower())
                for err in errors
            )
            if is_rate_limited:
                _set_api_circuit_open()
                raise RateLimitError("eBay Finding API rate limited")
            return []

        search_result = resp.get("searchResult", [{}])[0]
        raw_items = search_result.get("item", [])
        log.info("Finding API: %d items for %r", len(raw_items), query)

        items = []
        for item in raw_items:
            try:
                title = item.get("title", [""])[0]
                price_val = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0].get("__value__", "0")
                price = float(price_val)
                link = item.get("viewItemURL", [""])[0]
                item_id = item.get("itemId", [""])[0]

                end_time = item.get("listingInfo", [{}])[0].get("endTime", [""])[0]
                sold_date = ""
                if end_time:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                        sold_date = dt.strftime("%b %d, %Y")
                    except Exception:
                        sold_date = end_time[:10]

                condition = item.get("condition", [{}])[0].get("conditionDisplayName", [""])[0] if item.get("condition") else ""
                image_url = item.get("galleryURL", [""])[0]

                if title and price > 0:
                    items.append({
                        "title": title,
                        "price": price,
                        "link": link.split("?")[0] if link else f"https://www.ebay.com/itm/{item_id}",
                        "soldDate": sold_date,
                        "condition": condition,
                        "imageUrl": image_url,
                    })
            except Exception as e:
                log.debug("Skipping item: %s", e)
                continue
        return items

    except http_requests.RequestException as e:
        log.warning("Finding API request error: %s", e)
        return []


JS_EXTRACT = """() => {
    const results = [];
    const cards = document.querySelectorAll('.srp-results li, li[class*="card"]');
    for (const card of cards) {
        const heading = card.querySelector('[role="heading"]');
        const allText = card.textContent || '';
        const priceMatch = allText.match(/\\$(\\d[\\d,]*\\.?\\d*)/);
        if (!heading || !priceMatch) continue;

        let title = heading.textContent.trim()
            .replace('Opens in a new window or tab', '')
            .replace(/^New Listing/, '')
            .trim();
        if (!title || title.toLowerCase() === 'shop on ebay') continue;

        const price = parseFloat(priceMatch[1].replace(',', ''));
        const linkEl = card.querySelector('a[href*="ebay.com/itm"]');
        const link = linkEl ? linkEl.href.split('?')[0] : '';

        const imgEl = card.querySelector('img[src*="ebayimg.com"], img[data-src*="ebayimg.com"]');
        const imageUrl = imgEl ? (imgEl.src || imgEl.dataset.src || '') : '';

        let soldDate = '';
        let condition = '';
        for (const s of card.querySelectorAll('span')) {
            const t = s.textContent.trim();
            if (/Sold /i.test(t) && !/delivery/i.test(t) && !t.includes('$')) {
                soldDate = t.replace(/^Sold /i, '').trim();
            }
        }
        const condTerms = ['new', 'pre-owned', 'used', 'refurbished', 'for parts',
                           'open box', 'like new', 'very good', 'good', 'acceptable'];
        for (const s of card.querySelectorAll('span.SECONDARY_INFO, span[class*="conditi"], span[class*="SECONDARY"]')) {
            const t = s.textContent.trim().toLowerCase();
            if (condTerms.some(c => t.includes(c))) {
                condition = s.textContent.trim();
                break;
            }
        }
        if (!condition) {
            for (const s of card.querySelectorAll('span')) {
                const t = s.textContent.trim();
                if (condTerms.some(c => t.toLowerCase() === c || t.toLowerCase().startsWith(c + ' '))) {
                    condition = t;
                    break;
                }
            }
        }
        results.push({title, price, link, soldDate, condition, imageUrl});
    }
    return results;
}"""


def _wait_for_results(page, timeout_ms=8000):
    try:
        if "Pardon" in page.title() or "Checking" in page.title():
            page.wait_for_url("**/sch/**", timeout=timeout_ms)
            page.wait_for_selector("li.s-item, .srp-results, .srp-river-results", timeout=5000)
        else:
            page.wait_for_selector("li.s-item, .srp-results, .srp-river-results", timeout=timeout_ms)
    except Exception:
        page.wait_for_timeout(1500)


def _scrape_via_playwright(q: str, pages: int = 1) -> tuple[list[dict], list[dict]]:
    if not _PW_SEMAPHORE.acquire(timeout=30):
        log.warning("eBay PW semaphore timeout — too many concurrent scrapes")
        return [], [{"step": "semaphore_timeout", "ms": 30000}]
    try:
        _API_STATS["playwright_calls"] += 1
        return run_on_pw_thread(_scrape_via_playwright_inner, q, pages)
    finally:
        _PW_SEMAPHORE.release()


def _scrape_via_playwright_inner(q: str, pages: int = 1) -> tuple[list[dict], list[dict]]:
    timing_steps = []
    t0 = time.time()
    ctx = get_context()
    timing_steps.append({"step": "get_context", "ms": round((time.time() - t0) * 1000)})

    all_items = []
    _total_bytes = 0

    for pg in range(1, pages + 1):
        _increment_browser_requests()
        page = None
        try:
            page = ctx.new_page()

            def _block_resources(route):
                if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", _block_resources)

            def _track_bytes(response):
                nonlocal _total_bytes
                try:
                    _total_bytes += len(response.body())
                except Exception:
                    pass
            page.on("response", _track_bytes)

            url = (
                f"https://www.ebay.com/sch/i.html"
                f"?_nkw={q.replace(' ', '+')}"
                f"&_sop=13&LH_Complete=1&LH_Sold=1"
                f"&_pgn={pg}"
            )
            t_nav = time.time()
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            t_loaded = time.time()
            timing_steps.append({"step": f"page_{pg}_navigate", "ms": round((t_loaded - t_nav) * 1000)})

            page_title = page.title()
            challenged = "Pardon" in page_title or "Checking" in page_title
            timing_steps.append({"step": f"page_{pg}_challenge", "detected": challenged})

            _wait_for_results(page)
            t_ready = time.time()
            timing_steps.append({"step": f"page_{pg}_wait_ready", "ms": round((t_ready - t_loaded) * 1000)})

            if "Pardon" in page.title():
                timing_steps.append({"step": f"page_{pg}_blocked", "ms": 0})
                break

            items = page.evaluate(JS_EXTRACT)
            t_extract = time.time()
            timing_steps.append({"step": f"page_{pg}_extract", "ms": round((t_extract - t_ready) * 1000), "items": len(items)})

            all_items.extend(items)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception as e:
                    log.debug("Page close error: %s", e)

    timing_steps.append({"step": "bandwidth", "bytes": _total_bytes, "kb": round(_total_bytes / 1024, 1)})
    _API_STATS["bandwidth_ebay"] += _total_bytes
    _API_STATS["bandwidth_total"] += _total_bytes
    _API_STATS["bandwidth_queries"] += 1
    return all_items, timing_steps


def register_routes(app):
    @app.get("/scrape")
    def scrape(
        q: str = Query(..., description="Search query"),
        pages: int = Query(1, ge=1, le=3, description="Number of result pages to fetch"),
        skip_api: bool = Query(False, description="Skip Finding API and go straight to Playwright"),
    ):
        """Scrape eBay sold listings.

        Tries the Finding API first (cheap, when not circuit-broken or
        unconfigured), falls back to Playwright. The Playwright path is
        the production default today — see module docstring.
        """
        t0 = time.time()
        cache_key = f"ebay:{q}:{pages}"
        cached = _cache_get(cache_key)
        if cached:
            _API_STATS["ebay_cache_hits"] += 1
            log.info("Cache hit for %s", cache_key)
            cached = {**cached, "_timing": {"total_ms": round((time.time() - t0) * 1000), "cache": "hit", "source": cached.get("_timing", {}).get("source", "unknown")}}
            return cached

        with _INFLIGHT_LOCK:
            if cache_key in _INFLIGHT:
                event = _INFLIGHT[cache_key]
            else:
                event = None
                _INFLIGHT[cache_key] = threading.Event()

        if event:
            log.info("Waiting on in-flight request for %s", cache_key)
            event.wait(timeout=30)
            cached = _cache_get(cache_key)
            if cached:
                cached = {**cached, "_timing": {"total_ms": round((time.time() - t0) * 1000), "cache": "dedup", "source": cached.get("_timing", {}).get("source", "unknown")}}
                return cached

        try:
            before = _snapshot_resources()
            timing_steps = []
            _API_STATS["ebay_scrape_requests"] += 1
            _API_STATS["total_scrape_requests"] += 1

            api_items: list[dict] = []
            source = "playwright"

            if not skip_api and EBAY_APP_ID and not _is_api_circuit_open():
                try:
                    t_api = time.time()
                    api_items = _scrape_via_finding_api(q, per_page=50)
                    timing_steps.append({"step": "finding_api", "ms": round((time.time() - t_api) * 1000), "items": len(api_items)})
                    if api_items:
                        source = "finding_api"
                except RateLimitError:
                    timing_steps.append({"step": "finding_api_rate_limited", "ms": 0})
                except Exception as e:
                    log.warning("Finding API unexpected error: %s", e)
                    timing_steps.append({"step": "finding_api_error", "error": str(e)[:100]})

            if api_items:
                all_items = api_items
            else:
                _API_STATS["playwright_fallbacks"] += 1
                all_items, pw_steps = _scrape_via_playwright(q, pages)
                timing_steps.extend(pw_steps)
                source = "playwright"

            total_ms = round((time.time() - t0) * 1000)
            bw_bytes = 0
            for step in timing_steps:
                if step.get("step") == "bandwidth":
                    bw_bytes = step.get("bytes", 0)
                    break
            after = _snapshot_resources()
            _log_resource("ebay", total_ms, bw_bytes, before, after)

            result = {
                "query": q,
                "count": len(all_items),
                "items": all_items,
                "_timing": {
                    "total_ms": total_ms,
                    "cache": "miss",
                    "source": source,
                    "steps": timing_steps,
                },
            }
            _log_timing(q, result["_timing"])
            if all_items:
                _cache_set(cache_key, result)
                _crawler_record_success("ebay", q)
                _API_STATS["ebay_scrape_success"] += 1
            else:
                _crawler_record_failure("ebay", "0 items returned", q)
                _API_STATS["ebay_scrape_errors"] += 1
            return result
        except Exception as exc:
            _crawler_record_failure("ebay", str(exc)[:200], q)
            _API_STATS["ebay_scrape_errors"] += 1
            raise
        finally:
            with _INFLIGHT_LOCK:
                evt = _INFLIGHT.pop(cache_key, None)
                if evt:
                    evt.set()
