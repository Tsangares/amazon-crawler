"""CamelCamelCamel (Amazon price history) scraper — CDP browser + Turnstile solver.

Forked from sell.applesauce.chat sell/scrapers/camel.py — kept the production
proven Cloudflare-bypass setup, dropped the cross-scraper imports.

Threading strategy:
    A persistent daemon thread owns the Playwright instance so the greenlet
    dispatcher fiber stays alive. connect_over_cdp() is called through it.
"""
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote_plus

import requests as http_requests
from fastapi import Query
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from scrapers.shared import (
    log, _API_STATS,
    _cache_get, _cache_set, _log_timing, _log_resource, _snapshot_resources,
    _camel_db_get_search, _camel_db_set_search,
    _camel_db_get_product, _camel_db_set_product,
    _camel_record_fail, _camel_record_success,
    _is_camel_circuit_open,
    _camel_check_hourly_limit, _camel_increment_hourly,
    _crawler_record_success, _crawler_record_failure,
    _increment_browser_requests,
)

# Dedicated semaphore so CCC scrapes don't contend with future scrapers.
_PW_SEMAPHORE = threading.Semaphore(1)
_PW_STEALTH = Stealth()

# ── Persistent Playwright thread for CCC (work queue pattern) ───────
_CAMEL_PW = None
_CAMEL_PW_READY = threading.Event()
_CAMEL_PW_WORK_QUEUE = __import__("queue").Queue()


def _camel_pw_thread_worker():
    """Persistent thread: start CCC PW, then process work items."""
    global _CAMEL_PW
    try:
        _CAMEL_PW = sync_playwright().start()
        log.info("CCC Playwright instance started on dedicated thread")
    except Exception as e:
        log.error("Failed to start CCC Playwright: %s", e)
    _CAMEL_PW_READY.set()

    while True:
        work_item = _CAMEL_PW_WORK_QUEUE.get()
        if work_item is None:
            break
        fn, args, kwargs, result_event, result_holder = work_item
        try:
            result_holder[0] = fn(*args, **kwargs)
        except Exception as e:
            result_holder[1] = e
        result_event.set()


_CAMEL_PW_THREAD = threading.Thread(
    target=_camel_pw_thread_worker, daemon=True, name="pw-camel"
)
_CAMEL_PW_THREAD.start()
_CAMEL_PW_READY.wait(timeout=10)
if _CAMEL_PW is None:
    log.error("CCC Playwright instance failed to initialize!")


def run_on_camel_pw_thread(fn, *args, **kwargs):
    """Execute fn on the CCC Playwright thread and wait for result."""
    result_event = threading.Event()
    result_holder = [None, None]
    _CAMEL_PW_WORK_QUEUE.put((fn, args, kwargs, result_event, result_holder))
    result_event.wait()
    if result_holder[1] is not None:
        raise result_holder[1]
    return result_holder[0]


# ── CCC constants ────────────────────────────────────────────────────
_CAMEL_SEARCH_URL = "https://camelcamelcamel.com/search?q={query}"

_CAMEL_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]

# ── CDP browser state (paths configurable for containerized deploys) ─
_CAMEL_PROFILE_DIR = os.environ.get("CAMEL_PROFILE_DIR", "/tmp/camel-chrome-profile")
_CAMEL_CDP_PORT = int(os.environ.get("CAMEL_CDP_PORT", "9250"))
_CAMEL_CHROMIUM_BIN = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium")
_CAMEL_XVFB_DISPLAY = os.environ.get("CAMEL_XVFB_DISPLAY", ":98")
_CAMEL_CDP_BROWSER = None
_CAMEL_CDP_PROC = None
_CAMEL_XVFB_PROC = None
_CAMEL_CDP_LOCK = threading.Lock()


def _get_camel_cdp_browser():
    """Get or create a CDP-connected Chromium for CCC.

    Launches headful Chromium on Xvfb and connects via CDP to bypass
    Cloudflare Turnstile (which detects headless / Playwright automation).
    """
    global _CAMEL_CDP_BROWSER, _CAMEL_CDP_PROC, _CAMEL_XVFB_PROC

    with _CAMEL_CDP_LOCK:
        if _CAMEL_CDP_PROC and _CAMEL_CDP_PROC.poll() is not None:
            log.info("CCC CDP browser died (exit=%s), will restart", _CAMEL_CDP_PROC.returncode)
            _CAMEL_CDP_BROWSER = None
            _CAMEL_CDP_PROC = None

        if _CAMEL_CDP_BROWSER is not None:
            return _CAMEL_CDP_BROWSER

        os.system(f"pkill -f 'remote-debugging-port={_CAMEL_CDP_PORT}' 2>/dev/null")
        time.sleep(0.5)

        # Stale Singleton lock files cause Chromium to exit 21 after a crash.
        os.makedirs(_CAMEL_PROFILE_DIR, exist_ok=True)
        for _sf in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(_CAMEL_PROFILE_DIR, _sf))
                log.info("CCC: removed stale %s", _sf)
            except FileNotFoundError:
                pass
            except Exception as e:
                log.warning("CCC: failed to remove %s: %s", _sf, e)

        # llvmpipe + --ignore-gpu-blocklist makes WebGL "available" without a
        # real GPU, which is what Turnstile fingerprints on.
        if _CAMEL_XVFB_PROC and _CAMEL_XVFB_PROC.poll() is None:
            _CAMEL_XVFB_PROC.terminate()
            try:
                _CAMEL_XVFB_PROC.wait(timeout=3)
            except Exception:
                pass
        os.system(f"pkill -f 'Xvfb {_CAMEL_XVFB_DISPLAY}' 2>/dev/null")
        time.sleep(0.3)
        _CAMEL_XVFB_PROC = subprocess.Popen(
            ["Xvfb", _CAMEL_XVFB_DISPLAY, "-screen", "0", "1280x900x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

        env = os.environ.copy()
        env["DISPLAY"] = _CAMEL_XVFB_DISPLAY
        env.pop("WAYLAND_DISPLAY", None)
        _CAMEL_CDP_PROC = subprocess.Popen(
            [
                _CAMEL_CHROMIUM_BIN,
                f"--remote-debugging-port={_CAMEL_CDP_PORT}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={_CAMEL_PROFILE_DIR}",
                "--window-size=1280,900",
                "--ignore-gpu-blocklist",
                "--enable-features=OverrideSoftwareRenderingList",
                "--ozone-platform=x11",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(10):
            time.sleep(0.5)
            try:
                http_requests.get(f"http://127.0.0.1:{_CAMEL_CDP_PORT}/json/version", timeout=1)
                break
            except Exception:
                continue
        else:
            log.warning("CCC CDP browser failed to start")
            return None

        _CAMEL_CDP_BROWSER = _CAMEL_PW.chromium.connect_over_cdp(
            f"http://127.0.0.1:{_CAMEL_CDP_PORT}"
        )
        log.warning("CCC CDP browser connected on port %d (pid %d)",
                    _CAMEL_CDP_PORT, _CAMEL_CDP_PROC.pid)
        return _CAMEL_CDP_BROWSER


def close_camel_cdp():
    """Tear down CDP browser + Xvfb. Keep _CAMEL_PW alive for reuse."""
    global _CAMEL_CDP_BROWSER, _CAMEL_CDP_PROC, _CAMEL_XVFB_PROC
    with _CAMEL_CDP_LOCK:
        if _CAMEL_CDP_BROWSER:
            try:
                _CAMEL_CDP_BROWSER.close()
            except Exception:
                pass
            _CAMEL_CDP_BROWSER = None
        if _CAMEL_CDP_PROC:
            _CAMEL_CDP_PROC.terminate()
            try:
                _CAMEL_CDP_PROC.wait(timeout=5)
            except Exception:
                pass
            _CAMEL_CDP_PROC = None
        if _CAMEL_XVFB_PROC:
            _CAMEL_XVFB_PROC.terminate()
            try:
                _CAMEL_XVFB_PROC.wait(timeout=3)
            except Exception:
                pass
            _CAMEL_XVFB_PROC = None
        for _sf in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(_CAMEL_PROFILE_DIR, _sf))
            except FileNotFoundError:
                pass


def stop_camel_playwright():
    """Stop the CCC Playwright instance. Call ONLY on app shutdown."""
    global _CAMEL_PW
    if _CAMEL_PW is not None:
        try:
            _CAMEL_PW.stop()
        except Exception as e:
            log.debug("CCC Playwright stop error: %s", e)
        _CAMEL_PW = None


# ── CCC proactive health-check thread ────────────────────────────────
_CAMEL_HEALTH_CHECK_INTERVAL = 30


def _camel_health_check_loop():
    """Background daemon: detect dead CDP browser/Xvfb and clear state.

    If Chromium or Xvfb has died, null the state so the next
    _get_camel_cdp_browser() call relaunches cleanly. Doesn't actively
    relaunch (avoids burning resources when idle).
    """
    global _CAMEL_CDP_BROWSER, _CAMEL_CDP_PROC, _CAMEL_XVFB_PROC
    while True:
        time.sleep(_CAMEL_HEALTH_CHECK_INTERVAL)
        with _CAMEL_CDP_LOCK:
            if _CAMEL_XVFB_PROC and _CAMEL_XVFB_PROC.poll() is not None:
                log.warning("CCC health: Xvfb died (exit=%s), clearing CDP state",
                            _CAMEL_XVFB_PROC.returncode)
                _CAMEL_XVFB_PROC = None
                _CAMEL_CDP_BROWSER = None
                _CAMEL_CDP_PROC = None
                continue
            if _CAMEL_CDP_PROC and _CAMEL_CDP_PROC.poll() is not None:
                log.warning("CCC health: Chromium died (exit=%s), clearing CDP state",
                            _CAMEL_CDP_PROC.returncode)
                _CAMEL_CDP_BROWSER = None
                _CAMEL_CDP_PROC = None


threading.Thread(
    target=_camel_health_check_loop, daemon=True, name="camel-health",
).start()


def _get_camel_context():
    """Get the persistent CDP browser context. Cookies (cf_clearance) persist."""
    browser = _get_camel_cdp_browser()
    if browser is None:
        log.warning("CCC CDP browser unavailable, cannot scrape CamelCamelCamel")
        return None
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context(viewport={"width": 1280, "height": 900})


def _solve_cloudflare_turnstile(page, label: str = "page") -> bool:
    """Attempt to solve Cloudflare Turnstile.

    Managed mode usually auto-clears in ~7s on this CPU-only setup. Falls
    back to clicking the checkbox iframe if interactive.
    """
    log.warning("CCC %s: Turnstile challenge detected (url=%s)", label, page.url[:80])

    for wait_i in range(3):
        page.wait_for_timeout(2000)
        try:
            title = page.title()
        except Exception:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            log.warning("CCC %s: Turnstile auto-cleared via redirect (%ds)", label, (wait_i + 1) * 2)
            return True
        if "just a moment" not in title.lower():
            log.warning("CCC %s: Turnstile auto-cleared (%ds)", label, (wait_i + 1) * 2)
            return True

    no_iframe_count = 0
    for attempt in range(8):
        try:
            title = page.title()
        except Exception:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            log.warning("CCC %s: Turnstile cleared via redirect (click attempt %d)", label, attempt + 1)
            return True

        if "just a moment" not in title.lower():
            log.warning("CCC %s: Turnstile cleared after click attempt %d", label, attempt + 1)
            return True

        try:
            iframes = page.query_selector_all("iframe")
            log.warning("CCC %s: %d iframes visible (attempt %d)", label, len(iframes), attempt + 1)
            clicked = False

            for iframe in iframes:
                src = iframe.get_attribute("src") or ""
                box = iframe.bounding_box()
                if not box or box["width"] < 10 or box["height"] < 10:
                    continue

                is_turnstile = any(k in src.lower() for k in ("challenge", "turnstile", "cloudflare"))
                if is_turnstile or (len(iframes) <= 3 and box["width"] > 20):
                    click_x = box["x"] + min(box["width"] / 2, 30)
                    click_y = box["y"] + box["height"] / 2
                    page.mouse.click(click_x, click_y)
                    log.warning("CCC %s: clicked iframe %dx%d at (%.0f,%.0f) src=%s",
                                label, int(box["width"]), int(box["height"]),
                                click_x, click_y, src[:60])
                    clicked = True
                    break

            if not clicked:
                no_iframe_count += 1
                if no_iframe_count >= 3:
                    log.warning("CCC %s: no Turnstile iframe after %d attempts, giving up", label, no_iframe_count)
                    break
                page.mouse.click(640, 350)
                log.warning("CCC %s: no iframe found, blind-clicked center", label)

        except Exception as e:
            log.warning("CCC %s: Turnstile click error: %s", label, e)

        page.wait_for_timeout(2500)

    log.warning("CCC %s: Turnstile did NOT clear after ~26s", label)
    return False


def _parse_camel_price(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"\$([0-9,]+\.?\d*)", text.strip())
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _scrape_camel_product_page(ctx, url: str) -> dict | None:
    """Visit a CCC product page and extract price stats including history."""
    page = None
    _total_bytes = 0
    try:
        page = ctx.new_page()

        def _track_bytes(response):
            nonlocal _total_bytes
            try:
                _total_bytes += len(response.body())
            except Exception:
                pass
        page.on("response", _track_bytes)

        if _CAMEL_CDP_BROWSER is None:
            _PW_STEALTH.apply_stealth_sync(page)
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('table, .price_section, [class*="price"]', timeout=3000)
            page.wait_for_timeout(500)
        except Exception:
            page.wait_for_timeout(1500)

        page_title = page.title()
        if "just a moment" in page_title.lower():
            log.warning("CCC product: Cloudflare challenge detected, attempting to solve...")
            _solve_cloudflare_turnstile(page, "product")

        page_url = page.url
        page_title = page.title()
        if "captcha" in page_url.lower() or "challenge" in page_url.lower():
            log.warning("CCC product: CAPTCHA/challenge redirect — url=%s", page_url)
            return None
        block_keywords = ["just a moment", "attention required", "access denied", "are you a robot", "verify you are human"]
        if any(kw in page_title.lower() for kw in block_keywords):
            log.warning("CCC product: blocked page — title=%r  url=%s", page_title, page_url)
            return None

        title = page.evaluate("""() => {
            const el = document.querySelector('h2.title, h1.title, .product_title, h2 a, h1');
            return el ? el.textContent.trim() : '';
        }""")

        # CCC product pages show Amazon / 3rd Party New / 3rd Party Used with
        # Current / Lowest / Highest / Average. We extract the Amazon row.
        price_data = page.evaluate("""() => {
            const result = {current: null, lowest: null, highest: null, average: null, strategy: null};

            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const headerRow = table.querySelector('tr');
                if (!headerRow) continue;
                const headers = [...headerRow.querySelectorAll('th, td')].map(h => h.textContent.trim().toLowerCase());

                if (headers.some(h => h.includes('current')) && headers.some(h => h.includes('lowest'))) {
                    const rows = table.querySelectorAll('tr');
                    for (let i = 1; i < rows.length; i++) {
                        const cells = rows[i].querySelectorAll('td');
                        const rowLabel = cells.length > 0 ? cells[0].textContent.trim().toLowerCase() : '';
                        if (rowLabel.includes('amazon') || i === 1) {
                            for (let j = 0; j < cells.length; j++) {
                                const cellText = cells[j].textContent.trim();
                                const headerLabel = headers[j] || '';
                                const priceMatch = cellText.match(/\\$(\\d[\\d,]*\\.?\\d*)/);
                                if (priceMatch) {
                                    const price = parseFloat(priceMatch[1].replace(',', ''));
                                    if (headerLabel.includes('current')) result.current = price;
                                    else if (headerLabel.includes('lowest')) result.lowest = price;
                                    else if (headerLabel.includes('highest')) result.highest = price;
                                    else if (headerLabel.includes('average')) result.average = price;
                                }
                            }
                            if (result.current || result.lowest) {
                                result.strategy = 'table';
                                break;
                            }
                        }
                    }
                }
                if (result.strategy) break;
            }

            if (!result.current && !result.lowest) {
                const allText = document.body.innerText;
                const patterns = [
                    {key: 'current', regex: /current[:\\s]*\\$(\\d[\\d,]*\\.?\\d*)/i},
                    {key: 'lowest', regex: /lowest[:\\s]*\\$(\\d[\\d,]*\\.?\\d*)/i},
                    {key: 'highest', regex: /highest[:\\s]*\\$(\\d[\\d,]*\\.?\\d*)/i},
                    {key: 'average', regex: /average[:\\s]*\\$(\\d[\\d,]*\\.?\\d*)/i},
                ];
                for (const {key, regex} of patterns) {
                    const match = allText.match(regex);
                    if (match) result[key] = parseFloat(match[1].replace(',', ''));
                }
                if (result.current || result.lowest) result.strategy = 'text_regex';
            }

            if (!result.current && !result.lowest) {
                const elements = document.querySelectorAll('[class*="price"], [class*="stat"], [class*="value"]');
                const prices = [];
                for (const el of elements) {
                    const m = el.textContent.match(/\\$(\\d[\\d,]*\\.?\\d*)/);
                    if (m) prices.push(parseFloat(m[1].replace(',', '')));
                }
                if (prices.length >= 2) {
                    prices.sort((a, b) => a - b);
                    result.lowest = prices[0];
                    result.highest = prices[prices.length - 1];
                    result.current = prices[prices.length > 2 ? Math.floor(prices.length / 2) : 0];
                    result.average = prices.reduce((a, b) => a + b, 0) / prices.length;
                    result.strategy = 'element_scan';
                }
            }

            return result;
        }""")

        _CCC_TITLE_SUFFIXES = [
            " | Amazon price tracker / tracking, Amazon price history charts, Amazon price watches, Amazon price drop alerts",
            " | camelcamelcamel.com",
        ]
        for suffix in _CCC_TITLE_SUFFIXES:
            if title and title.endswith(suffix):
                title = title[:-len(suffix)]
        if not title:
            raw_title = page.title()
            for suffix in _CCC_TITLE_SUFFIXES:
                raw_title = raw_title.replace(suffix, "")
            title = raw_title.strip()

        if not title and not price_data.get("current") and not price_data.get("lowest"):
            return None

        strategy = price_data.get("strategy", "none")
        log.warning("CCC product extracted via strategy '%s': %s (current=$%s)",
                    strategy, (title or "?")[:60], price_data.get("current"))

        return {
            "name": title or "Unknown Product",
            "current_price": price_data.get("current"),
            "lowest_price": price_data.get("lowest"),
            "highest_price": price_data.get("highest"),
            "average_price": price_data.get("average"),
            "product_url": url,
            "strategy_used": strategy,
        }

    except Exception as e:
        log.warning("CamelCamelCamel product page error (%s): %s", url, e)
        return None
    finally:
        _API_STATS["bandwidth_camel"] += _total_bytes
        _API_STATS["bandwidth_total"] += _total_bytes
        if page:
            try:
                page.close()
            except Exception:
                pass


def _scrape_camel_search(ctx, query: str, max_results: int = 8) -> list[dict]:
    """Search CCC and return product page URLs + basic info."""
    page = None
    _total_bytes = 0
    try:
        page = ctx.new_page()

        def _track_bytes(response):
            nonlocal _total_bytes
            try:
                _total_bytes += len(response.body())
            except Exception:
                pass
        page.on("response", _track_bytes)

        if _CAMEL_CDP_BROWSER is None:
            _PW_STEALTH.apply_stealth_sync(page)
        search_url = _CAMEL_SEARCH_URL.format(query=quote_plus(query))
        page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        page_title = page.title()
        if "just a moment" in page_title.lower():
            log.warning("CCC search: Cloudflare challenge detected, attempting to solve...")
            _solve_cloudflare_turnstile(page, "search")

        page_url = page.url
        page_title = page.title()
        body_len = page.evaluate("() => document.body.innerText.length")
        log.warning("CCC search: title=%r  url=%r  body_len=%d", page_title, page_url, body_len)

        if "captcha" in page_url.lower() or "challenge" in page_url.lower():
            log.warning("CCC search: CAPTCHA/challenge redirect — url=%s", page_url)
            return []
        block_keywords = ["just a moment", "attention required", "access denied", "are you a robot", "verify you are human"]
        if any(kw in page_title.lower() for kw in block_keywords):
            log.warning("CCC search: blocked page — title=%r  url=%s", page_title, page_url)
            return []

        # CCC search pages have .search-result blocks; product links are
        # ASIN-based (/product/B...). The /product/<id>/go variant is an
        # Amazon redirect and we skip it.
        products = page.evaluate("""(maxResults) => {
            const results = [];
            const seen = new Set();

            const blocks = document.querySelectorAll('.search-result');
            for (const block of blocks) {
                if (results.length >= maxResults) break;

                let productUrl = null;
                let asin = null;
                let title = '';

                const links = block.querySelectorAll('a[href*="/product/"]');
                for (const link of links) {
                    const href = link.getAttribute('href') || '';
                    if (href.includes('/go')) continue;
                    const m = href.match(/\\/product\\/([A-Z0-9]{10})/);
                    if (!m) continue;
                    asin = m[1];
                    if (seen.has(asin)) { asin = null; break; }
                    productUrl = 'https://camelcamelcamel.com/product/' + asin;
                    const txt = link.textContent.trim();
                    if (txt.length > 3 && !title) title = txt;
                }
                if (!asin) continue;
                seen.add(asin);

                if (!title) {
                    const el = block.querySelector('.product-title a, .product-title');
                    if (el) title = el.textContent.trim();
                }

                let amazonPrice = null, tpNew = null, tpUsed = null;
                for (const row of block.querySelectorAll('.watch_row')) {
                    const typeEl = row.querySelector('.price-type a, .price-type');
                    const priceEl = row.querySelector('.cur-price');
                    if (!typeEl || !priceEl) continue;
                    const t = typeEl.textContent.trim().toLowerCase();
                    const pm = priceEl.textContent.match(/\\$(\\d[\\d,]*\\.?\\d*)/);
                    const p = pm ? parseFloat(pm[1].replace(',', '')) : null;
                    if (t.includes('amazon')) amazonPrice = p;
                    else if (t.includes('3rd') && t.includes('new')) tpNew = p;
                    else if (t.includes('3rd') && t.includes('used')) tpUsed = p;
                }

                results.push({
                    title: (title || '').substring(0, 200),
                    url: productUrl,
                    asin: asin,
                    search_price: amazonPrice || tpNew || tpUsed,
                    amazon_price: amazonPrice,
                    third_party_new: tpNew,
                    third_party_used: tpUsed,
                    strategy_used: 1,
                });
            }

            // Fallback: scan all links for ASIN patterns
            if (results.length === 0) {
                const allLinks = document.querySelectorAll('a[href*="/product/"]');
                for (const link of allLinks) {
                    if (results.length >= maxResults) break;
                    const href = link.getAttribute('href') || '';
                    if (href.includes('/go')) continue;
                    const m = href.match(/\\/product\\/([A-Z0-9]{10})/);
                    if (!m) continue;
                    const candidateAsin = m[1];
                    if (seen.has(candidateAsin)) continue;
                    seen.add(candidateAsin);

                    const title = link.textContent.trim();
                    if (!title || title.length < 3) continue;

                    results.push({
                        title: title.substring(0, 200),
                        url: 'https://camelcamelcamel.com/product/' + candidateAsin,
                        asin: candidateAsin,
                        search_price: null,
                        amazon_price: null,
                        third_party_new: null,
                        third_party_used: null,
                        strategy_used: 2,
                    });
                }
            }

            return results;
        }""", max_results)

        log.warning("CCC search: found %d product links", len(products) if products else 0)
        if not products:
            snippet = page.evaluate("() => document.body.innerText.substring(0, 300)")
            log.warning("CCC search: 0 results — first 300 chars: %s", snippet)

        return products or []

    except Exception as e:
        log.warning("CamelCamelCamel search error: %s", e)
        return []
    finally:
        _API_STATS["bandwidth_camel"] += _total_bytes
        _API_STATS["bandwidth_total"] += _total_bytes
        if page:
            try:
                page.close()
            except Exception:
                pass


def _scrape_camel_product_with_retry(ctx, url: str, max_retries: int = 2) -> dict | None:
    """Retry wrapper for product page scraping with L2 cache check."""
    cached_product = _camel_db_get_product(url)
    if cached_product:
        log.info("CCC product cache hit (SQLite): %s", url[:80])
        return cached_product

    for attempt in range(1 + max_retries):
        if _is_camel_circuit_open():
            _API_STATS["camel_circuit_breaker_skips"] += 1
            return None
        result = _scrape_camel_product_page(ctx, url)
        if result:
            _camel_record_success()
            _camel_db_set_product(url, result)
            return result
        _camel_record_fail()
        if attempt < max_retries:
            backoff = (attempt + 1)
            log.info("CCC product retry %d/%d for %s (backoff %ds)", attempt + 1, max_retries, url[:80], backoff)
            time.sleep(backoff)
    return None


# 75s budget per /scrape-camel call — CPU-only Xvfb needs ~7s per Turnstile.
_CAMEL_DEADLINE = 75


def _scrape_camel_inner(query: str, max_results: int = 8, force_product_page: bool = False) -> list[dict]:
    """Full CCC scrape: search + (optional) product page details.

    `force_product_page=True` skips the search-page fast path and visits
    each product page individually so price-history fields get populated.
    Slower (~7s/product) but returns lowest/highest/average prices.
    """
    ctx = _get_camel_context()
    if ctx is None:
        return []
    try:
        _increment_browser_requests()
        deadline = time.time() + _CAMEL_DEADLINE

        # Short-query retry: long product names sometimes return 0 results.
        search_results = _scrape_camel_search(ctx, query, max_results)
        words = query.split()
        while not search_results and len(words) > 2:
            words = words[:-1]
            short_q = " ".join(words)
            log.info("CCC: retrying shorter query '%s'", short_q)
            search_results = _scrape_camel_search(ctx, short_q, max_results)
        if not search_results:
            log.warning("CamelCamelCamel: no search results for '%s'", query)
            return []

        items = []
        for result in search_results[:max_results]:
            if time.time() > deadline:
                log.warning("CCC time cap reached (%ds), returning %d items", _CAMEL_DEADLINE, len(items))
                break

            if _is_camel_circuit_open():
                log.warning("CCC circuit breaker open, returning %d items", len(items))
                _API_STATS["camel_circuit_breaker_skips"] += 1
                break

            product_url = result["url"]
            search_price = result.get("amazon_price") or result.get("search_price")

            if search_price and not force_product_page:
                items.append({
                    "name": result.get("title") or "Unknown Product",
                    "current_price": search_price,
                    "lowest_price": None,
                    "highest_price": None,
                    "average_price": None,
                    "product_url": product_url,
                    "strategy_used": "search_page",
                    "third_party_new": result.get("third_party_new"),
                    "third_party_used": result.get("third_party_used"),
                })
            else:
                product_data = _scrape_camel_product_with_retry(ctx, product_url)
                if product_data and (product_data.get("current_price") or product_data.get("lowest_price")):
                    if product_data["name"] == "Unknown Product" and result.get("title"):
                        product_data["name"] = result["title"]
                    # Carry forward third-party prices from search if product page didn't get them.
                    product_data.setdefault("third_party_new", result.get("third_party_new"))
                    product_data.setdefault("third_party_used", result.get("third_party_used"))
                    items.append(product_data)
                else:
                    items.append({
                        "name": result.get("title") or "Unknown Product",
                        "current_price": search_price,
                        "lowest_price": None,
                        "highest_price": None,
                        "average_price": None,
                        "product_url": product_url,
                        "strategy_used": "search_page_fallback" if search_price else "search_page_no_price",
                        "third_party_new": result.get("third_party_new"),
                        "third_party_used": result.get("third_party_used"),
                    })
            _API_STATS["camel_products_scraped"] += 1

        return items

    finally:
        # Don't close the CDP default context — cookies must persist across requests.
        if _CAMEL_CDP_BROWSER is None or ctx not in (_CAMEL_CDP_BROWSER.contexts if _CAMEL_CDP_BROWSER else []):
            try:
                ctx.close()
            except Exception:
                pass


def register_routes(app):
    @app.get("/scrape-camel")
    def scrape_camel(
        q: str = Query(..., description="Search query"),
        max_results: int = Query(8, ge=1, le=15),
        force_product_page: bool = Query(False, description="Skip search-page fast path; visit each product page for full price history (slower)"),
    ):
        """Scrape CamelCamelCamel for Amazon price history data."""
        _API_STATS["camel_scrape_requests"] += 1
        t0 = time.time()
        cache_key = f"camel:{q}:{max_results}:{int(force_product_page)}"

        cached = _cache_get(cache_key)
        if cached:
            cached["_timing"] = {"total_ms": round((time.time() - t0) * 1000), "cache": "hit", "source": "camelcamelcamel"}
            _API_STATS["camel_cache_hits"] += 1
            return cached

        db_cached = _camel_db_get_search(cache_key)
        if db_cached:
            db_cached["_timing"] = {"total_ms": round((time.time() - t0) * 1000), "cache": "hit_db", "source": "camelcamelcamel"}
            _cache_set(cache_key, db_cached)
            _API_STATS["camel_cache_hits"] += 1
            return db_cached

        if _is_camel_circuit_open():
            _API_STATS["camel_circuit_breaker_skips"] += 1
            return {"query": q, "count": 0, "items": [], "error": "circuit_breaker_open",
                    "_timing": {"total_ms": round((time.time() - t0) * 1000), "cache": "miss", "source": "camelcamelcamel"}}

        if not _camel_check_hourly_limit():
            stale = _camel_db_get_search(cache_key, ignore_ttl=True)
            if stale:
                stale["_timing"] = {"total_ms": round((time.time() - t0) * 1000), "cache": "stale_db", "source": "camelcamelcamel"}
                return stale
            return {"query": q, "count": 0, "items": [], "error": "rate_limited_hourly",
                    "_timing": {"total_ms": round((time.time() - t0) * 1000), "cache": "miss", "source": "camelcamelcamel"}}

        if not _PW_SEMAPHORE.acquire(timeout=30):
            log.warning("Playwright semaphore timeout for CamelCamelCamel scrape")
            return {"query": q, "count": 0, "items": [], "error": "too_many_concurrent"}
        before = _snapshot_resources()
        bw_before = _API_STATS["bandwidth_camel"]
        try:
            _camel_increment_hourly()
            items = run_on_camel_pw_thread(_scrape_camel_inner, q, max_results, force_product_page)
            total_ms = round((time.time() - t0) * 1000)

            bw_delta = _API_STATS["bandwidth_camel"] - bw_before
            after = _snapshot_resources()
            _log_resource("camel", total_ms, bw_delta, before, after)

            result = {
                "query": q,
                "count": len(items),
                "items": items,
                "_timing": {"total_ms": total_ms, "cache": "miss", "source": "camelcamelcamel"},
            }
            if items:
                _cache_set(cache_key, result)
                _camel_db_set_search(cache_key, q, result)
                _API_STATS["camel_scrape_success"] += 1
                _crawler_record_success("camelcamelcamel", q)
            else:
                _API_STATS["camel_scrape_errors"] += 1
                _crawler_record_failure("camelcamelcamel", "0 items returned", q)
            _log_timing(q, result["_timing"])
            _API_STATS["total_scrape_requests"] += 1
            _API_STATS["bandwidth_queries"] += 1
            return result
        except Exception as exc:
            _crawler_record_failure("camelcamelcamel", str(exc)[:200], q)
            raise
        finally:
            _PW_SEMAPHORE.release()
