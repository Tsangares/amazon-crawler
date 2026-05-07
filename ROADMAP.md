# Roadmap

## Done

- ✅ Forked CCC scraper from sell.applesauce.chat into a standalone repo
- ✅ Systemd unit + `deploy.sh` (matches the Applesauce `git pull` + `systemctl restart` pattern)
- ✅ Added `force_product_page` flag so callers can get real price history
- ✅ Slimmed shared.py to CCC-only primitives
- ✅ Smoke tests + stats/health endpoints

## Next: direct Amazon scraping (the high-value missing data)

Reviews and ratings are the data we don't have. CCC gives prices; Amazon
itself has the social proof. Build in this order:

1. **`GET /amazon/product/{asin}`** — single product page scrape.
   - Title, price, rating, review count, image URLs, bullet points.
   - Reuse the CCC Cloudflare/CDP browser pattern for consistency.
   - amazon.com is less strict than CCC — no Turnstile — but does fingerprint
     hard. Same Xvfb + headful + CDP setup should pass.
   - Add per-IP backoff (start at 2s between requests) and a daily cap.
   - Cache aggressively — review counts don't move fast.

2. **`GET /amazon/search?q=`** — search results page scrape.
   - Returns `[{asin, title, rating, review_count, price, sponsored}]`.
   - Lets us replace the CCC search step entirely for queries where review
     signal matters more than price history.

3. **`GET /amazon/reviews/{asin}?page=`** — paginated review text.
   - Useful for "what do people complain about" analysis downstream.
   - Heavier on bandwidth + risk of getting blocked. Last to build.

## Then: combine + enrich

4. **`GET /research?q=...`** — combined endpoint that:
   - Calls `/scrape-camel` for ASINs + price history.
   - Calls `/amazon/product/{asin}` for each ASIN to get ratings.
   - Returns a single ranked list with price + history + reviews.
   - This is the dream "data aggregator" interface for downstream LLM work.

## Possible: official APIs as backup

5. **Amazon Product Advertising API** — official, rate-limited but reliable.
   - Requires Amazon Associates account (Astron has one?).
   - Use as fallback when scraping fails or for reliable bulk fetches.

6. **Keepa API** — paid, but the gold standard for Amazon price history.
   - $20/mo for the entry tier.
   - Could replace CCC entirely for history if budget allows.

## Operational

- **Deploy target**: leaning toward mat (port 8011) since chromium + xvfb are
  already installed for the existing `ebay-scraper.service`. The systemd unit
  uses different CDP/Xvfb numbers so the two don't collide. Could split off
  to its own VPS later if it grows or starts competing for bandwidth with sell.
- **Monitoring**: `/stats` and `/crawler-health` are already there; wire
  into the Hub status grid (`hub.applesauce.chat`).
- **Caching strategy**: SQLite TTL is currently 12h. Consider shorter for
  prices, longer for review counts. Maybe per-endpoint configurable.
- **Rate-limit tuning**: hourly cap currently 500. Adjust based on real
  traffic shape once any consumers come online.

## Open questions

- Should this expose its own UI, or stay headless API-only? (Lean: API-only,
  let downstream apps build UIs.)
- One container per scraper kind, or all in one? (Current: one container.
  If amazon-direct grows heavy, split.)
- Auth? Right now it's open. Probably needs a shared API key once we have a
  public host so it doesn't get hammered.
