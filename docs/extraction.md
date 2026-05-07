# Sell → applesauce-crawlers extraction plan

The goal: move the eBay/CCC scrapers out of sell.applesauce.chat into
this standalone service so other apps (pickl, hub, future research
tools) can share them, and so the scrapers can be redeployed without
touching the user-facing app.

## Phase A — service running side-by-side (DONE)

`applesauce-crawlers.service` runs on **mat** at port `8014` with
`/scrape-camel` and `/scrape` (eBay) endpoints. CDP/Xvfb numbers
(9260 / :96) chosen to not collide with `ebay-scraper.service`
(9250 / :98) or `tiktok-scraper.service` (:99) on the same host.

Both services run at the same time. Sell still talks to its own
`ebay-scraper.service` at 8002. Nothing in sell has changed yet.

Smoke test:

```bash
curl -s http://mat:8014/health
APPLESAUCE_CRAWLERS_URL=http://mat:8014 pytest tests/test_smoke.py -v
```

## Phase B — per-scraper feature flag in sell

Add per-scraper backend URLs to `sell/app.py` with the same default as
today, so unset env vars are a strict no-op. Each scraper can be
flipped to applesauce-crawlers individually — no big-bang switchover.

Patch:

```diff
--- a/sell/app.py
+++ b/sell/app.py
@@ -2283,6 +2283,13 @@ SCRAPE_HEADERS = {
 EBAY_SCRAPER_URL = os.getenv("EBAY_SCRAPER_URL", "http://localhost:8002")
 POSTER_URL = os.getenv("POSTER_URL", "http://localhost:8003")

+# Per-scraper backend URLs. Default each to the legacy ebay-scraper service
+# (current production). Override individually to migrate a single crawler to
+# the new applesauce-crawlers service (mat:8014) without touching the others.
+# Rollback is just unsetting the override.
+EBAY_BACKEND_URL = os.getenv("EBAY_BACKEND_URL", EBAY_SCRAPER_URL)
+CAMEL_BACKEND_URL = os.getenv("CAMEL_BACKEND_URL", EBAY_SCRAPER_URL)
+
 # Discogs API (for vinyl/music pricing)

@@ -2307,10 +2314,11 @@ def scrape_ebay(query: str, pages: int = 1) -> dict:
     """Call the eBay scraper service.
     Returns dict with 'items', 'source' (api|playwright_fallback), 'cache', '_timing'.
+    Routes via EBAY_BACKEND_URL so we can flip to applesauce-crawlers per-scraper.
     """
     try:
         resp = requests.get(
-            f"{EBAY_SCRAPER_URL}/scrape",
+            f"{EBAY_BACKEND_URL}/scrape",
             params={"q": query, "pages": pages},
             timeout=30,
         )

@@ -2348,10 +2356,12 @@ def scrape_camelcamelcamel(query: str, max_results: int = 8) -> list[dict]:
     """Call the CamelCamelCamel scraper service.
     Returns list of dicts with: name, current_price, lowest_price, highest_price,
     average_price, product_url.
+    Routes via CAMEL_BACKEND_URL so we can flip to applesauce-crawlers
+    independently of eBay/Mercari/Google.
     """
     try:
         resp = requests.get(
-            f"{EBAY_SCRAPER_URL}/scrape-camel",
+            f"{CAMEL_BACKEND_URL}/scrape-camel",
             params={"q": query, "max_results": max_results},
             timeout=60,  # CCC scraping is slow (visits multiple product pages)
         )
```

Other scrapers (`scrape_mercari`, `scrape_google_shopping`, `scrape_swapshop`,
`/system-stats`, `/google-quota`, etc.) intentionally still point at
`EBAY_SCRAPER_URL`. They'll move when we port those endpoints into this
service.

## Phase C — flip the flags one at a time

After deploying the patch with no env vars set (sell's behavior is
unchanged, every test should still pass), enable per scraper in
`/opt/sell/.env`:

```bash
# Move CCC first — it's the lower-traffic of the two and easier to roll back.
CAMEL_BACKEND_URL=http://localhost:8014

# Then eBay once CCC has been stable for a while.
EBAY_BACKEND_URL=http://localhost:8014
```

`systemctl restart sell` after each change. Watch
`/admin/analytics` and the smoke tests to confirm price research +
analyze still work end-to-end.

## Phase D — port the rest

Mercari, Google Shopping, Swapshop, the monitoring routes
(`/system-stats`, `/bandwidth`, etc.). Each follows the same pattern:

1. Port the scraper module into `scrapers/`.
2. Wire it into `main.py`.
3. Add a smoke test that confirms route registration.
4. Add a corresponding `*_BACKEND_URL` env var in sell.

Once everything is migrated, `sell/scrapers/` can be deleted and
sell's `EBAY_SCRAPER_URL` env can default to the new service.

## Phase E — retire `ebay-scraper.service`

When sell no longer references it for any endpoint, stop and disable
the unit on mat. Free up CDP port 9250, Xvfb display :98, and the
~400 MB of resident memory it holds.
