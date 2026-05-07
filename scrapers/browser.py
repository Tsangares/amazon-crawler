"""Shared headless Chromium for non-Cloudflare scrapers (eBay, future Mercari).

CamelCamelCamel has its OWN browser stack — see camel.py. CCC needs the
Xvfb + CDP + headful setup to pass Turnstile. eBay search results pages
are open enough that plain headless Playwright works fine.

Threading: like camel.py, we own the Playwright instance on a dedicated
daemon thread so the greenlet dispatcher stays alive across requests.
"""
import os
import queue
import signal
import subprocess
import threading
import time

from playwright.sync_api import sync_playwright

from scrapers.shared import log

_PW_INSTANCE = None
_PW_READY = threading.Event()
_PW_WORK_QUEUE = queue.Queue()


def _pw_thread_worker():
    global _PW_INSTANCE
    try:
        _PW_INSTANCE = sync_playwright().start()
        log.info("Shared headless Playwright started on dedicated thread")
    except Exception as e:
        log.error("Failed to start shared Playwright: %s", e)
    _PW_READY.set()

    while True:
        work_item = _PW_WORK_QUEUE.get()
        if work_item is None:
            break
        fn, args, kwargs, result_event, result_holder = work_item
        try:
            result_holder[0] = fn(*args, **kwargs)
        except Exception as e:
            result_holder[1] = e
        result_event.set()


_PW_THREAD = threading.Thread(target=_pw_thread_worker, daemon=True, name="pw-shared")
_PW_THREAD.start()
_PW_READY.wait(timeout=10)
if _PW_INSTANCE is None:
    log.error("Shared Playwright instance failed to initialize!")


def run_on_pw_thread(fn, *args, **kwargs):
    result_event = threading.Event()
    result_holder = [None, None]
    _PW_WORK_QUEUE.put((fn, args, kwargs, result_event, result_holder))
    result_event.wait()
    if result_holder[1] is not None:
        raise result_holder[1]
    return result_holder[0]


BROWSER = None
CONTEXT = None

_BROWSER_REQUEST_COUNT = 0
_BROWSER_RESTART_EVERY = 8
_BROWSER_MAX_AGE = 120
_BROWSER_CREATED_AT = 0.0
_BROWSER_LOCK = threading.Lock()

CHROMIUM_BIN = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium")


def _kill_orphan_chromium():
    """Kill orphan Chromium processes — but spare the CCC CDP browser tree.

    CCC's headful Chromium runs as a long-lived subprocess we must not touch;
    its PID is read lazily so this module doesn't hard-import camel.py at
    startup.
    """
    try:
        our_pid = os.getpid()
        protected = {our_pid}

        camel_pid = None
        try:
            from scrapers.camel import _CAMEL_CDP_PROC
            if _CAMEL_CDP_PROC and _CAMEL_CDP_PROC.poll() is None:
                camel_pid = _CAMEL_CDP_PROC.pid
        except Exception:
            pass

        if camel_pid:
            protected.add(camel_pid)
            try:
                children = subprocess.run(
                    ["pgrep", "-P", str(camel_pid)],
                    capture_output=True, text=True, timeout=5,
                )
                if children.returncode == 0:
                    for p in children.stdout.strip().split("\n"):
                        if p.strip():
                            protected.add(int(p.strip()))
            except Exception:
                pass

        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", "chromium"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
            killed = 0
            for pid in pids:
                if pid in protected:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
            if killed:
                log.info("Killed %d orphan Chromium processes (spared CCC pid %s)", killed, camel_pid)
    except Exception as e:
        log.debug("Orphan cleanup error: %s", e)


def close_browser():
    global BROWSER, CONTEXT, _BROWSER_REQUEST_COUNT, _BROWSER_CREATED_AT

    log.info(
        "Closing browser (requests served: %d, uptime: %ds)",
        _BROWSER_REQUEST_COUNT,
        int(time.time() - _BROWSER_CREATED_AT) if _BROWSER_CREATED_AT else 0,
    )
    try:
        if CONTEXT is not None:
            CONTEXT.close()
    except Exception as e:
        log.debug("Context close error: %s", e)
    try:
        if BROWSER is not None:
            BROWSER.close()
    except Exception as e:
        log.debug("Browser close error: %s", e)

    BROWSER = None
    CONTEXT = None
    _BROWSER_REQUEST_COUNT = 0
    _BROWSER_CREATED_AT = 0.0

    _kill_orphan_chromium()


def stop_playwright():
    global _PW_INSTANCE
    if _PW_INSTANCE is not None:
        try:
            _PW_INSTANCE.stop()
        except Exception as e:
            log.debug("Playwright stop error: %s", e)
        _PW_INSTANCE = None


def _maybe_restart_browser():
    with _BROWSER_LOCK:
        if BROWSER is None:
            return
        age = time.time() - _BROWSER_CREATED_AT if _BROWSER_CREATED_AT else 0
        if _BROWSER_REQUEST_COUNT >= _BROWSER_RESTART_EVERY:
            log.info("Browser restart: request threshold (%d), recycling", _BROWSER_REQUEST_COUNT)
            close_browser()
        elif age > _BROWSER_MAX_AGE:
            log.info("Browser restart: max age %ds, recycling", int(age))
            close_browser()


def _increment_browser_requests():
    global _BROWSER_REQUEST_COUNT
    _BROWSER_REQUEST_COUNT += 1


def _get_or_create_browser():
    global BROWSER, _BROWSER_CREATED_AT

    with _BROWSER_LOCK:
        if BROWSER is None:
            BROWSER = _PW_INSTANCE.chromium.launch(
                headless=True,
                executable_path=CHROMIUM_BIN,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-translate",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--js-flags=--max-old-space-size=256",
                ],
            )
            _BROWSER_CREATED_AT = time.time()
            log.info("Launched headless Chromium for shared scrapers")

    return BROWSER


def get_context():
    """Get/create the default headless context (used by eBay)."""
    global CONTEXT
    _maybe_restart_browser()
    _get_or_create_browser()

    if CONTEXT is None:
        CONTEXT = BROWSER.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
    return CONTEXT
