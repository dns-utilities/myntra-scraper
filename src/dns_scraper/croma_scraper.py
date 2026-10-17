"""Croma category scraper — async bulk scrape with curl_cffi impersonation.

Mirrors scraper.py (MyntraScraper) but for Croma's category API. No auth
token: just browser impersonation + rotating headers. Returns Product models
(via Product.from_croma_item) so both Myntra and Croma share the same
filter/notify pipeline in ScraperApp.
"""

from __future__ import annotations

import asyncio
import logging
import random

from curl_cffi.requests import AsyncSession

from dns_scraper.croma_api import ROWS_PER_PAGE, api_headers, build_api_url, parse_category_url
from dns_scraper.models import Product

log = logging.getLogger("dns_scraper.croma")


class CromaScraper:
    def __init__(self, impersonate: str, concurrency: int, max_pages: int,
                 channel_code: str = "400049", rate_limiter=None) -> None:
        self._impersonate = impersonate
        self._concurrency = concurrency
        self._max_pages = max_pages
        self._channel_code = channel_code
        # Per-source politeness budget — see sources.AsyncRateLimiter.
        self._limiter = rate_limiter
        self.requests = 0
        self.successes = 0
        self.errors = 0
        self.forbidden = 0
        self.rate_limited = 0
        self._lock = asyncio.Lock()
        log.info("Croma scraper ready | concurrency=%d pages=%d impersonate=%s rps=%s",
                 concurrency, max_pages, impersonate,
                 getattr(rate_limiter, "_rate", "unbounded"))

    async def scrape_all(self, urls: list[str]) -> list[Product]:
        found: list[Product] = []
        found_lock = asyncio.Lock()

        async def fetch_one(session: AsyncSession, url: str) -> None:
            info = parse_category_url(url)
            results: list[Product] = []
            if not info["category_id"]:
                log.warning("Croma: no category id in %s — skipping", url[:80])
                return
            page = 0
            while page < self._max_pages:
                api_url = build_api_url(info, page, self._channel_code)
                try:
                    if self._limiter is not None:
                        await self._limiter.acquire()
                    resp = await session.get(api_url, headers=api_headers(info["referer"]),
                                             impersonate=self._impersonate, timeout=20)
                except Exception as exc:
                    async with self._lock:
                        self.errors += 1
                    log.warning("Croma error for %s: %s", url[:80], exc)
                    break

                async with self._lock:
                    self.requests += 1

                if resp.status_code == 200:
                    async with self._lock:
                        self.successes += 1
                    data = resp.json()
                    raw_products = data.get("products", [])
                    pagination = data.get("pagination", {})
                    total_pages = pagination.get("totalPages", 1)
                    for raw in raw_products:
                        product = Product.from_croma_item(raw)
                        if product:
                            results.append(product)
                    if not raw_products or (page + 1) >= min(self._max_pages, total_pages):
                        break
                    page += 1
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                elif resp.status_code == 429:
                    async with self._lock:
                        self.errors += 1
                        self.rate_limited += 1
                    log.warning("Croma 429 for %s — backing off", url[:80])
                    await asyncio.sleep(random.uniform(5, 10))
                    break
                elif resp.status_code == 403:
                    async with self._lock:
                        self.errors += 1
                        self.forbidden += 1
                    log.warning("Croma 403 for %s: %s", url[:80], (resp.text or "")[:200])
                    break
                else:
                    async with self._lock:
                        self.errors += 1
                    log.warning("Croma HTTP %d for %s: %s", resp.status_code,
                                url[:80], (resp.text or "")[:200])
                    break
            async with found_lock:
                found.extend(results)

        async with AsyncSession(impersonate=self._impersonate) as session:
            sem = asyncio.Semaphore(min(self._concurrency, 6))

            async def limited(url: str) -> None:
                async with sem:
                    await fetch_one(session, url)
                    await asyncio.sleep(random.uniform(0.3, 0.9))

            batch_size = min(len(urls), 6)
            for i in range(0, len(urls), batch_size):
                await asyncio.gather(*[limited(u) for u in urls[i:i + batch_size]],
                                     return_exceptions=True)
                if i + batch_size < len(urls):
                    await asyncio.sleep(1.0)
        return found

    def stats(self) -> dict:
        total = max(self.requests, 1)
        return {"requests": self.requests, "success": self.successes,
                "errors": self.errors, "forbidden": self.forbidden,
                "rate_limited": self.rate_limited,
                "error_rate": round(self.errors / total * 100, 1)}
