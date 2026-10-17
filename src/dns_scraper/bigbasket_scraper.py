"""BigBasket listing scraper — async, location-bound, with cookie re-warm.

Same contract as the other scrapers: fetch politely, hand raw results to
``Product.from_bigbasket_item``, never decide what a deal is.

Two BigBasket-specific behaviours:

* **Anti-bot cookies with a TTL.** BigBasket issues cookies from the homepage
  and they expire; when they go stale every listing answers 403. So the cookie
  is refreshed on a timer *and* immediately on a 403, which is the single
  change that keeps a 24/7 run alive instead of dying overnight.
* **204 means "no more pages".** A successful but empty page is normal
  termination, not an error, so it must not trip the circuit breaker.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from curl_cffi.requests import AsyncSession

from dns_scraper import bigbasket_api
from dns_scraper.models import Product

log = logging.getLogger("dns_scraper.bigbasket")


class BigBasketScraper:
    def __init__(self, impersonate: str = "chrome", concurrency: int = 8,
                 max_pages: int = 6, rate_limiter=None, cooldown_seconds: int = 25,
                 max_attempts: int = 6, min_products: int = 5,
                 cookie_ttl: int = 600, proxy: str = "") -> None:
        self._impersonate = impersonate
        self._proxy = proxy or ""
        self._concurrency = max(1, concurrency)
        self._max_pages = max(1, max_pages)
        self._limiter = rate_limiter
        self._cooldown_seconds = max(1, cooldown_seconds)
        self._max_attempts = max(1, max_attempts)
        self._min_products = max(1, min_products)
        self._cookie_ttl = max(60, cookie_ttl)
        self._cooldown_until = 0.0
        self._warned = False
        self._cookie_ready = False
        self._cookie_at = 0.0
        # A WAF 403 is not a flake: repeating it deepens the block, so after a
        # few in a row the sweep is abandoned rather than finished against a wall.
        self._consecutive_forbidden = 0
        self._abort_run = False
        self._abort_after = 3
        # One UA for the whole session -- the anti-bot cookie is bound to it.
        self._ua = bigbasket_api.pick_user_agent()
        self._lock = asyncio.Lock()
        self.requests = 0
        self.successes = 0
        self.errors = 0
        self.forbidden = 0
        self.rate_limited = 0
        self.aborted_runs = 0
        log.info("BigBasket scraper ready | concurrency=%d pages=%d ua=%s "
                 "egress=%s rps=%s",
                 self._concurrency, self._max_pages, self._ua[-24:],
                 "proxy" if self._proxy else "direct",
                 getattr(rate_limiter, "_rate", "unbounded"))

    def _proxies(self) -> dict | None:
        """curl_cffi proxy mapping, or None to connect directly."""
        if not self._proxy:
            return None
        return {"http": self._proxy, "https": self._proxy}

    # -- politeness --------------------------------------------------------
    async def _wait_cooldown(self) -> None:
        while True:
            remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(2.0, remaining))

    async def _warm(self, session: AsyncSession, point_now, force: bool = False) -> None:
        """(Re)collect anti-bot cookies; they expire, so this is on a timer."""
        if not force and self._cookie_ready and \
                (time.monotonic() - self._cookie_at) < self._cookie_ttl:
            return
        try:
            await session.get(bigbasket_api.BASE + "/",
                              headers=bigbasket_api.home_headers(point_now.payload, self._ua),
                              timeout=30)
            self._cookie_ready = True
            self._cookie_at = time.monotonic()
        except Exception as exc:
            log.debug("BigBasket cookie warm failed: %s", exc)

    # -- fetching ----------------------------------------------------------
    async def _page(self, session: AsyncSession, point_now, kind: str, slug: str,
                    page: int) -> list[Product]:
        params = bigbasket_api.search_params(kind, slug, page)
        url = bigbasket_api.BASE + bigbasket_api.LIST_PATH
        headers = bigbasket_api.headers(point_now.payload, self._ua)
        if self._abort_run:
            return []
        for attempt in range(self._max_attempts):
            await self._wait_cooldown()
            try:
                if self._limiter is not None:
                    await self._limiter.acquire()
                resp = await session.get(url, params=params, headers=headers, timeout=30)
            except Exception as exc:
                async with self._lock:
                    self.errors += 1
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(min(8.0, 1.0 * 2 ** attempt) + random.random())
                    continue
                log.warning("BigBasket error for '%s' p%d: %s", slug, page, exc)
                return []

            async with self._lock:
                self.requests += 1

            if resp.status_code == 200:
                async with self._lock:
                    self.successes += 1
                    self._consecutive_forbidden = 0
                try:
                    data = resp.json()
                except Exception as exc:
                    log.warning("BigBasket bad JSON for '%s' p%d: %s", slug, page, exc)
                    return []
                out = []
                for raw in bigbasket_api.iter_products(data):
                    product = Product.from_bigbasket_item(
                        raw, location=point_now.key, location_label=point_now.label)
                    if product is not None:
                        out.append(product)
                return out

            if resp.status_code == 204:
                # No more products on this page: normal termination.
                async with self._lock:
                    self.successes += 1
                return []

            if resp.status_code in (429, 403):
                async with self._lock:
                    self.errors += 1
                    self.rate_limited += 1
                    if resp.status_code == 403:
                        self.forbidden += 1
                        self._consecutive_forbidden += 1
                        blocked = self._consecutive_forbidden
                    else:
                        blocked = 0
                self._cooldown_until = time.monotonic() + self._cooldown_seconds
                if blocked >= self._abort_after:
                    if not self._abort_run:
                        self._abort_run = True
                        log.error("BigBasket refused %d requests in a row (HTTP 403) "
                                  "— this egress IP looks blocked. Aborting the run "
                                  "rather than finishing the sweep against a wall.",
                                  blocked)
                    return []
                if not self._warned:
                    self._warned = True
                    log.warning("BigBasket %d — cooling down %ds and refreshing "
                                "cookies", resp.status_code, self._cooldown_seconds)
                await self._wait_cooldown()
                if resp.status_code == 403:
                    # Stale/blocked cookies are the usual cause; get fresh ones.
                    await self._warm(session, point_now, force=True)
                continue

            if resp.status_code in (500, 502, 503) and attempt < self._max_attempts - 1:
                await asyncio.sleep(min(8.0, 1.0 * 2 ** attempt) + random.random())
                continue

            async with self._lock:
                self.errors += 1
            log.warning("BigBasket HTTP %d for '%s' p%d: %s", resp.status_code,
                        slug, page, (resp.text or "")[:160])
            return []
        return []

    async def _scrape_point_link(self, session: AsyncSession, point_now,
                                 kind: str, slug: str) -> list[Product]:
        if not kind or not slug:
            return []
        first = await self._page(session, point_now, kind, slug, 1)
        if not first:
            return []
        collected = list(first)
        if len(first) < self._min_products:
            return collected
        seen = {p.product_id for p in first}
        page, batch = 2, 8
        while page <= self._max_pages:
            pages = list(range(page, min(page + batch, self._max_pages + 1)))
            results = await asyncio.gather(
                *[self._page(session, point_now, kind, slug, pg) for pg in pages],
                return_exceptions=True)
            empty = 0
            valid = 0
            for res in results:
                if isinstance(res, Exception):
                    continue          # transient, not an empty page
                if isinstance(res, list):
                    valid += 1
                    if res:
                        fresh = [p for p in res if p.product_id not in seen]
                        if not fresh:
                            empty += 1
                        for product in fresh:
                            seen.add(product.product_id)
                            collected.append(product)
                    else:
                        empty += 1
            if valid > 0 and empty == valid:
                break
            page += batch
            await asyncio.sleep(0.1)
        return collected

    # -- entry point -------------------------------------------------------
    async def scrape_all(self, urls: list[str], points: list) -> list[Product]:
        if not points:
            log.warning("BigBasket: no delivery points resolved — nothing to scrape")
            return []
        # Drop blank/comment lines — see blinkit_scraper for the same reasoning:
        # a bare line here is a category slug, so a '#' heading would be fetched.
        urls = [u for u in urls if bigbasket_api.is_usable(u)]
        if not urls:
            log.warning("BigBasket: no usable categories/search terms in the links file")
            return []
        self._abort_run = False
        self._consecutive_forbidden = 0
        found: list[Product] = []
        lock = asyncio.Lock()
        async with AsyncSession(impersonate=self._impersonate, timeout=30,
                                proxies=self._proxies()) as session:
            sem = asyncio.Semaphore(self._concurrency)

            async def job(point_now, url: str) -> None:
                if self._abort_run:
                    return
                kind, slug = bigbasket_api.parse_link(url)
                if not kind or not slug:
                    return
                async with sem:
                    products = await self._scrape_point_link(session, point_now, kind, slug)
                    if products:
                        async with lock:
                            found.extend(products)
                    await asyncio.sleep(random.uniform(0.2, 0.6))

            for point_now in points:
                if self._abort_run:
                    break
                await self._warm(session, point_now)
                batch = 8
                for i in range(0, len(urls), batch):
                    await asyncio.gather(
                        *[job(point_now, u) for u in urls[i:i + batch]],
                        return_exceptions=True)
                    if self._abort_run:
                        break
                    if i + batch < len(urls):
                        await asyncio.sleep(0.5)
        if self._abort_run:
            self.aborted_runs += 1
        self._warned = False
        return found

    def stats(self) -> dict:
        total = max(self.requests, 1)
        return {"requests": self.requests, "success": self.successes,
                "errors": self.errors, "forbidden": self.forbidden,
                "rate_limited": self.rate_limited,
                "aborted_runs": self.aborted_runs,
                "error_rate": round(self.errors / total * 100, 1),
                "points": 1 if self._cookie_ready else 0}
