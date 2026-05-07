"""Shared state, caching, stats, circuit breakers, and utilities.

Slimmed-down fork of sell.applesauce.chat's scrapers/shared.py — kept only
the CCC-related primitives and renamed the data path to be configurable
via DATA_DIR for containerized deploys.
"""
import json
import logging
import os
import sqlite3
import threading
import time

import psutil

# ── Logging ──────────────────────────────────────────────────────────
log = logging.getLogger("applesauce-crawlers")


# ── Exceptions ───────────────────────────────────────────────────────
class RateLimitError(Exception):
    """Generic upstream rate-limit signal."""


class QuotaExhaustedError(Exception):
    """Generic quota-exhausted signal."""


# ── Playwright semaphore (CCC has its own — see camel.py) ────────────
_PW_SEMAPHORE = threading.Semaphore(1)


# ── Request stats (extend as new scrapers come online) ───────────────
_API_STATS: dict = {
    "started_at": time.time(),

    # CamelCamelCamel
    "camel_scrape_requests": 0,
    "camel_scrape_success": 0,
    "camel_scrape_errors": 0,
    "camel_circuit_breaker_skips": 0,
    "camel_cache_hits": 0,
    "camel_products_scraped": 0,

    # eBay
    "ebay_scrape_requests": 0,
    "ebay_scrape_success": 0,
    "ebay_scrape_errors": 0,
    "ebay_cache_hits": 0,
    "playwright_calls": 0,
    "playwright_fallbacks": 0,

    # Direct Amazon (planned)
    "amazon_scrape_requests": 0,
    "amazon_scrape_success": 0,
    "amazon_scrape_errors": 0,

    # Aggregate
    "total_scrape_requests": 0,
    "bandwidth_camel": 0,
    "bandwidth_ebay": 0,
    "bandwidth_amazon": 0,
    "bandwidth_total": 0,
    "bandwidth_queries": 0,
}


# ── In-flight request dedup (thundering-herd guard) ──────────────────
_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_LOCK = threading.Lock()


def _snapshot_resources():
    """Snapshot CPU times and memory across the process tree.

    Walks live children directly because cpu_times().children_* only counts
    waited-on (exited) children, and Chromium is long-lived.
    """
    proc = psutil.Process(os.getpid())
    cpu = proc.cpu_times()
    total_cpu = cpu.user + cpu.system
    mem_rss = proc.memory_info().rss
    for child in proc.children(recursive=True):
        try:
            child_cpu = child.cpu_times()
            total_cpu += child_cpu.user + child_cpu.system
            mem_rss += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return {"cpu_seconds": total_cpu, "memory_bytes": mem_rss}


# ── In-memory L1 cache ────────────────────────────────────────────────
_SCRAPE_CACHE: dict = {}
CACHE_TTL = 7200  # 2 hours


def _cache_get(key: str):
    entry = _SCRAPE_CACHE.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value):
    _SCRAPE_CACHE[key] = (time.time(), value)
    if len(_SCRAPE_CACHE) > 200:
        cutoff = time.time() - CACHE_TTL
        for k in list(_SCRAPE_CACHE):
            if _SCRAPE_CACHE[k][0] < cutoff:
                del _SCRAPE_CACHE[k]


# ── SQLite L2 cache (CCC search + product) ───────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
_CAMEL_DB_PATH = os.path.join(DATA_DIR, "camel_cache.db")
_CAMEL_DB_TTL = 43200  # 12 hours


def _camel_db_init():
    con = sqlite3.connect(_CAMEL_DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS camel_search_cache (
        query_key TEXT PRIMARY KEY,
        query TEXT,
        result_json TEXT,
        item_count INTEGER,
        cached_at REAL,
        ttl_hours REAL DEFAULT 12
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS camel_product_cache (
        url TEXT PRIMARY KEY,
        product_json TEXT,
        cached_at REAL,
        ttl_hours REAL DEFAULT 12
    )""")
    con.commit()
    con.close()


try:
    _camel_db_init()
except Exception as e:
    log.warning("Failed to init camel_cache.db at %s: %s", _CAMEL_DB_PATH, e)


def _camel_db_get_search(key: str, ignore_ttl: bool = False) -> dict | None:
    try:
        con = sqlite3.connect(_CAMEL_DB_PATH)
        row = con.execute(
            "SELECT result_json, cached_at, ttl_hours FROM camel_search_cache WHERE query_key = ?",
            (key,),
        ).fetchone()
        con.close()
        if not row:
            return None
        result_json, cached_at, ttl_hours = row
        if not ignore_ttl and (time.time() - cached_at) > (ttl_hours * 3600):
            return None
        return json.loads(result_json)
    except Exception as e:
        log.warning("CCC DB search get error: %s", e)
        return None


def _camel_db_set_search(key: str, query: str, result: dict):
    try:
        con = sqlite3.connect(_CAMEL_DB_PATH)
        con.execute(
            "INSERT OR REPLACE INTO camel_search_cache "
            "(query_key, query, result_json, item_count, cached_at) VALUES (?, ?, ?, ?, ?)",
            (key, query, json.dumps(result), result.get("count", 0), time.time()),
        )
        con.commit()
        con.close()
    except Exception as e:
        log.warning("CCC DB search set error: %s", e)


def _camel_db_get_product(url: str) -> dict | None:
    try:
        con = sqlite3.connect(_CAMEL_DB_PATH)
        row = con.execute(
            "SELECT product_json, cached_at, ttl_hours FROM camel_product_cache WHERE url = ?",
            (url,),
        ).fetchone()
        con.close()
        if not row:
            return None
        product_json, cached_at, ttl_hours = row
        if (time.time() - cached_at) > (ttl_hours * 3600):
            return None
        return json.loads(product_json)
    except Exception as e:
        log.warning("CCC DB product get error: %s", e)
        return None


def _camel_db_set_product(url: str, product: dict):
    try:
        con = sqlite3.connect(_CAMEL_DB_PATH)
        con.execute(
            "INSERT OR REPLACE INTO camel_product_cache (url, product_json, cached_at) VALUES (?, ?, ?)",
            (url, json.dumps(product), time.time()),
        )
        con.commit()
        con.close()
    except Exception as e:
        log.warning("CCC DB product set error: %s", e)


def _camel_db_count() -> int:
    try:
        con = sqlite3.connect(_CAMEL_DB_PATH)
        s = con.execute("SELECT COUNT(*) FROM camel_search_cache").fetchone()[0]
        p = con.execute("SELECT COUNT(*) FROM camel_product_cache").fetchone()[0]
        con.close()
        return s + p
    except Exception:
        return 0


# ── CCC circuit breaker ──────────────────────────────────────────────
_CAMEL_CONSECUTIVE_FAILS = 0
_CAMEL_CIRCUIT_OPEN_UNTIL = 0.0
_CAMEL_CIRCUIT_COOLDOWN = 1800  # 30 min


def _camel_record_fail():
    global _CAMEL_CONSECUTIVE_FAILS, _CAMEL_CIRCUIT_OPEN_UNTIL
    _CAMEL_CONSECUTIVE_FAILS += 1
    if _CAMEL_CONSECUTIVE_FAILS >= 3:
        _CAMEL_CIRCUIT_OPEN_UNTIL = time.time() + _CAMEL_CIRCUIT_COOLDOWN
        log.warning(
            "CCC circuit breaker OPEN — skipping CCC for %ds after %d consecutive failures",
            _CAMEL_CIRCUIT_COOLDOWN, _CAMEL_CONSECUTIVE_FAILS,
        )


def _camel_record_success():
    global _CAMEL_CONSECUTIVE_FAILS
    _CAMEL_CONSECUTIVE_FAILS = 0


def _is_camel_circuit_open():
    return time.time() < _CAMEL_CIRCUIT_OPEN_UNTIL


def _get_camel_consecutive_fails():
    return _CAMEL_CONSECUTIVE_FAILS


def _get_camel_circuit_open_until():
    return _CAMEL_CIRCUIT_OPEN_UNTIL


# ── CCC hourly rate limit ────────────────────────────────────────────
_CAMEL_HOURLY_CAP = 500
_CAMEL_HOURLY_COUNT = 0
_CAMEL_HOUR_RESET = 0.0


def _camel_check_hourly_limit() -> bool:
    global _CAMEL_HOURLY_COUNT, _CAMEL_HOUR_RESET
    now = time.time()
    if now - _CAMEL_HOUR_RESET > 3600:
        _CAMEL_HOURLY_COUNT = 0
        _CAMEL_HOUR_RESET = now
    return _CAMEL_HOURLY_COUNT < _CAMEL_HOURLY_CAP


def _camel_increment_hourly():
    global _CAMEL_HOURLY_COUNT
    _CAMEL_HOURLY_COUNT += 1


def _get_camel_hourly_count():
    return _CAMEL_HOURLY_COUNT


def _get_camel_hourly_cap():
    return _CAMEL_HOURLY_CAP


# ── Recent timing log ────────────────────────────────────────────────
_TIMING_LOG: list = []
MAX_TIMING_LOG = 100


def _log_timing(query: str, timing: dict):
    _TIMING_LOG.append({"query": query, "ts": time.time(), **timing})
    if len(_TIMING_LOG) > MAX_TIMING_LOG:
        _TIMING_LOG.pop(0)


# ── Per-request resource profiling ───────────────────────────────────
_RESOURCE_LOG: list = []
MAX_RESOURCE_LOG = 500


def _log_resource(scraper_type: str, wall_ms: float, bandwidth_bytes: int, before: dict, after: dict):
    cpu_delta = after["cpu_seconds"] - before["cpu_seconds"]
    mem_peak = max(before["memory_bytes"], after["memory_bytes"])
    entry = {
        "ts": time.time(),
        "type": scraper_type,
        "wall_ms": round(wall_ms),
        "cpu_seconds": round(cpu_delta, 3),
        "memory_mb_peak": round(mem_peak / (1024 * 1024), 1),
        "bandwidth_bytes": bandwidth_bytes,
    }
    _RESOURCE_LOG.append(entry)
    if len(_RESOURCE_LOG) > MAX_RESOURCE_LOG:
        _RESOURCE_LOG.pop(0)


# ── Per-crawler health tracking ──────────────────────────────────────
_CRAWLER_HEALTH: dict[str, dict] = {}


def _empty_health() -> dict:
    return {
        "total_success": 0, "total_fail": 0,
        "last_success_at": None, "last_fail_at": None,
        "last_success_query": "", "last_error": "",
        "consecutive_fails": 0, "recent_errors": [],
    }


def _crawler_record_success(crawler: str, query: str = ""):
    h = _CRAWLER_HEALTH.setdefault(crawler, _empty_health())
    h["total_success"] += 1
    h["last_success_at"] = time.time()
    h["last_success_query"] = query
    h["consecutive_fails"] = 0


def _crawler_record_failure(crawler: str, error: str, query: str = ""):
    h = _CRAWLER_HEALTH.setdefault(crawler, _empty_health())
    h["total_fail"] += 1
    h["last_fail_at"] = time.time()
    h["last_error"] = error[:200]
    h["consecutive_fails"] += 1
    h["recent_errors"].append({"ts": time.time(), "error": error[:200], "query": query})
    if len(h["recent_errors"]) > 10:
        h["recent_errors"].pop(0)


def _get_crawler_health() -> dict:
    now = time.time()
    result = {}
    for name, h in _CRAWLER_HEALTH.items():
        total = h["total_success"] + h["total_fail"]
        success_rate = round(h["total_success"] / max(total, 1) * 100, 1)
        if h["consecutive_fails"] >= 5:
            status = "down"
        elif h["consecutive_fails"] >= 2 or success_rate < 50:
            status = "degraded"
        else:
            status = "healthy"
        result[name] = {
            "status": status,
            "total_success": h["total_success"],
            "total_fail": h["total_fail"],
            "success_rate": success_rate,
            "consecutive_fails": h["consecutive_fails"],
            "last_success_ago": round(now - h["last_success_at"]) if h["last_success_at"] else None,
            "last_fail_ago": round(now - h["last_fail_at"]) if h["last_fail_at"] else None,
            "last_error": h["last_error"],
            "recent_errors": h["recent_errors"][-3:],
        }
    return result


# ── Lightweight browser-request counter (used by CCC for restart hints) ─
_BROWSER_REQUEST_COUNT = 0


def _increment_browser_requests():
    global _BROWSER_REQUEST_COUNT
    _BROWSER_REQUEST_COUNT += 1


# ── Generic API circuit breaker (used by eBay Finding API) ───────────
_API_CIRCUIT_OPEN_UNTIL = 0.0
_API_CIRCUIT_COOLDOWN = 1800  # 30 min — match the CCC value


def _set_api_circuit_open():
    global _API_CIRCUIT_OPEN_UNTIL
    _API_CIRCUIT_OPEN_UNTIL = time.time() + _API_CIRCUIT_COOLDOWN
    log.warning("API circuit breaker OPEN — backing off API calls for %ds", _API_CIRCUIT_COOLDOWN)


def _is_api_circuit_open() -> bool:
    return time.time() < _API_CIRCUIT_OPEN_UNTIL


def _get_api_circuit_open_until() -> float:
    return _API_CIRCUIT_OPEN_UNTIL
