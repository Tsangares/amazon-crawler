"""Direct Amazon product/search scraping — STUB.

Not implemented yet. Planned endpoints:

    GET /amazon/search?q=...        list ASINs + titles + ratings
    GET /amazon/product/{asin}      title, price, rating, review count, images
    GET /amazon/reviews/{asin}      paginated review text + ratings

Implementation notes:
- Amazon detects Playwright headless trivially; will need the same
  Xvfb + CDP + system Chromium pattern the CCC scraper uses.
- amazon.com aggressively rate-limits by IP. Either rotate via residential
  proxy pool, or add aggressive backoff + per-IP daily caps.
- Selectors break monthly. Plan for selector fallbacks like camel.py does.
- Consider adding the official Product Advertising API as an alternate
  path for users who can authenticate (requires Amazon Associates account).
"""
from fastapi import HTTPException, Query


def register_routes(app):
    @app.get("/amazon/search")
    def amazon_search(q: str = Query(..., description="Search query")):
        raise HTTPException(status_code=501, detail="amazon search not implemented yet — see scrapers/amazon.py")

    @app.get("/amazon/product/{asin}")
    def amazon_product(asin: str):
        raise HTTPException(status_code=501, detail="amazon product not implemented yet — see scrapers/amazon.py")

    @app.get("/amazon/reviews/{asin}")
    def amazon_reviews(asin: str):
        raise HTTPException(status_code=501, detail="amazon reviews not implemented yet — see scrapers/amazon.py")
