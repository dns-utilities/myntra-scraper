"""Blinkit search scraper — async, location-bound, with a shared 429 gate.

Mirrors ``croma_scraper.py`` so all sources share one pipeline, but Blinkit has
two behaviours the others do not need:

* **Per-point cookie warm-up.** Blinkit sets serviceability cookies
  (city, ``cf_bm``, device id) on the homepage and they are tied to the
  coordinate hint. Each new delivery point is warmed once before its first
  search so results are priced for that store, not the previous one.
* **A shared rate-limit gate.** Blinkit throttles on a sliding window and
  answers 429. When that happens *every* in-flight request pauses together for
  a cooldown instead of each retrying on its own -- hammering a throttled host
  is what turns a short cooldown into a longer one.

Products are parsed by ``Product.from_blinkit_item``; this module knows how to
fetch and how to be polite, not what a deal looks like.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from curl_cffi.requests import AsyncSession

from dns_scraper import blinkit_api
from dns_scraper.models import Product

log = logging.getLogger("dns_scraper.blinkit")


class BlinkitScraper:
    def __init__(self, impersonate: str = blinkit_api.DEFAULT_IMPERSONATE,
                 concurrency: int = 5,
                 max_pages: int = 6, page_limit: int = blinkit_api.PAGE_LIMIT,
                 rate_limiter=None, cooldown_seconds: int = 20,
                 max_attempts: int = 6, min_products: int = 5,
                 warm_cookies: bool = True) -> None:
        self._impersonate = impersonate
        self._concurrency = max(1, concurrency)
        self._max_pages = max(1, max_pages)
        self._page_limit = max(1, page_limit)
        self._limiter = rate_limiter
        self._cooldown_seconds = max(1, cooldown_seconds)
        self._max_attempts = max(1, max_attempts)
        self._min_products = max(1, min_products)
        self._warm_cookies = warm_cookies
        self._cooldown_until = 0.0
        self._warned = False
        self._warmed: set[str] = set()
        # A WAF 403 is different from an error: repeating it deepens the block.
        # After a few in a row the run is abandoned outright rather than
        # finishing the (expensive) sweep against a wall.
        self._consecutive_forbidden = 0
        self._abort_run = False
        self._abort_after = 3
        self._lock = asyncio.Lock()
        self.requests = 0
        self.successes = 0
        self.errors = 0
        self.forbidden = 0
        self.rate_limited = 0
        self.aborted_runs = 0
        log.info("Blinkit scraper ready | concurrency=%d pages=%d impersonate=%s rps=%s",
                 self._concurrency, self._max_pages, impersonate,
                 getattr(rate_limiter, "_rate", "unbounded"))

    # -- politeness --------------------------------------------------------
    async def _wait_cooldown(self) -> None:
        """Block while a 429 cooldown is active so every request backs off together."""
        while True:
            remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(2.0, remaining))

    async def _warm(self, session: AsyncSession, point_now) -> None:
        """Prime serviceability cookies for one delivery point (once each)."""
        if not self._warm_cookies or point_now.key in self._warmed:
            return
        self._warmed.add(point_now.key)
        try:
            await session.get(blinkit_api.HOME_URL,
                              headers=blinkit_api.home_headers(point_now.payload),
                              timeout=25)
        except Exception as exc:
            # Search usually still works without the warm-up; never fatal.
            log.debug("Blinkit warm-up failed for %s: %s", point_now.key, exc)

    # -- fetching ----------------------------------------------------------
    async def _page(self, session: AsyncSession, point_now, query: str,
                    offset: int) -> list[Product]:
        params = blinkit_api.search_params(query, offset, self._page_limit)
        if self._abort_run:
            return []
        for attempt in range(self._max_attempts):
            await self._wait_cooldown()
            try:
                if self._limiter is not None:
                    await self._limiter.acquire()
                resp = await session.post(
                    blinkit_api.SEARCH_URL, params=params, json={},
                    headers=blinkit_api.headers(point_now.payload), timeout=25)
            except Exception as exc:
                async with self._lock:
                    self.errors += 1
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                log.warning("Blinkit error for '%s': %s", query, exc)
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
                    log.warning("Blinkit bad JSON for '%s': %s", query, exc)
                    return []
                out = []
                for card in blinkit_api.iter_cards(data):
                    if blinkit_api.is_sponsored(card):
                        continue
                    product = Product.from_blinkit_item(
                        card, location=point_now.key, location_label=point_now.label)
                    if product is not None:
                        out.append(product)
                return out

            if resp.status_code == 429:
                async with self._lock:
                    self.errors += 1
                    self.rate_limited += 1
                self._cooldown_until = time.monotonic() + self._cooldown_seconds
                if not self._warned:
                    self._warned = True
                    log.warning("Blinkit rate-limited (429) — cooling down %ds for "
                                "every request, then resuming", self._cooldown_seconds)
                await self._wait_cooldown()
                continue

            if resp.status_code in (401, 403):
                async with self._lock:
                    self.errors += 1
                    self.forbidden += 1
                    self._consecutive_forbidden += 1
                    blocked = self._consecutive_forbidden
                self._cooldown_until = time.monotonic() + self._cooldown_seconds
                if blocked >= self._abort_after:
                    # Repeated refusal is not a flake. Finishing the sweep would
                    # mean hundreds more requests at a wall that is already
                    # saying no, so stop and let the circuit breaker park us.
                    if not self._abort_run:
                        self._abort_run = True
                        log.error("Blinkit refused %d requests in a row (HTTP %d) — "
                                  "this egress IP looks blocked. Aborting the run and "
                                  "backing off instead of hammering it.",
                                  blocked, resp.status_code)
                    return []
                log.warning("Blinkit HTTP %d for '%s' — cooling down %ds",
                            resp.status_code, query, self._cooldown_seconds)
                await self._wait_cooldown()
                continue

            if resp.status_code in (500, 502, 503) and attempt < self._max_attempts - 1:
                await asyncio.sleep(1.0 + attempt * 1.5)
                continue

            async with self._lock:
                self.errors += 1
            log.warning("Blinkit HTTP %d for '%s': %s", resp.status_code, query,
                        (resp.text or "")[:160])
            return []
        return []

    async def _scrape_point_term(self, session: AsyncSession, point_now,
                                 query: str) -> list[Product]:
        """Paginate one term at one delivery point until the pages run dry."""
        if not query or not query.strip():
            return []
        first = await self._page(session, point_now, query, 0)
        if not first:
            return []
        collected = list(first)
        if len(first) < self._min_products:
            return collected
        seen = {p.product_id for p in first}
        batch = 8
        for start in range(1, self._max_pages, batch):
            offsets = [pg * self._page_limit
                       for pg in range(start, min(start + batch, self._max_pages))]
            results = await asyncio.gather(
                *[self._page(session, point_now, query, off) for off in offsets],
                return_exceptions=True)
            empty = 0
            for res in results:
                if isinstance(res, list) and res:
                    fresh = [p for p in res if p.product_id not in seen]
                    if not fresh:
                        empty += 1
                    for product in fresh:
                        seen.add(product.product_id)
                        collected.append(product)
                else:
                    empty += 1
            if empty == len(results):
                break
            await asyncio.sleep(0.1)
        return collected

    # -- entry point -------------------------------------------------------
    def _fingerprints(self) -> tuple[str, ...]:
        """The impersonation to try first, then the known-good fallbacks.

        A refused fingerprint is a hard 403, so a single hard-coded value is a
        single point of failure. The configured value leads (an operator
        override still wins) and ``blinkit_api.IMPERSONATIONS`` follows.
        """
        primary = self._impersonate or blinkit_api.DEFAULT_IMPERSONATE
        return tuple(dict.fromkeys([primary] + [
            candidate for candidate in blinkit_api.IMPERSONATIONS
            if candidate != primary]))

    async def scrape_all(self, urls: list[str], points: list) -> list[Product]:
        if not points:
            log.warning("Blinkit: no delivery points resolved — nothing to scrape")
            return []
        # Drop blank/comment lines: this source treats a bare line as a search
        # term, so the links file's own headings would otherwise be requested.
        urls = [u for u in urls if blinkit_api.is_usable(u)]
        if not urls:
            log.warning("Blinkit: no usable search terms in the links file")
            return []
        candidates = self._fingerprints()
        found: list[Product] = []
        for index, fingerprint in enumerate(candidates):
            found = await self._sweep(fingerprint, urls, points)
            if not self._abort_run:
                return found
            # Every request from this fingerprint was refused: not a flake, and
            # not worth finishing the sweep. Try the next one before giving up.
            self.aborted_runs += 1
            self._abort_run = False
            if index + 1 < len(candidates):
                log.warning("Blinkit: %s was refused for every request — retrying "
                            "the run as %s", fingerprint, candidates[index + 1])
        return found

    async def _sweep(self, fingerprint: str, urls: list[str],
                     points: list) -> list[Product]:
        """One full pass with a single impersonation profile."""
        self._abort_run = False
        self._consecutive_forbidden = 0
        self._warmed.clear()      # a fresh session has no warmed cookies
        found: list[Product] = []
        lock = asyncio.Lock()
        async with AsyncSession(impersonate=fingerprint) as session:
            sem = asyncio.Semaphore(self._concurrency)

            async def job(point_now, url: str) -> None:
                if self._abort_run:
                    return
                async with sem:
                    products = await self._scrape_point_term(
                        session, point_now, blinkit_api.term(url))
                    if products:
                        async with lock:
                            found.extend(products)
                    await asyncio.sleep(random.uniform(0.2, 0.6))

            # Point-major: cookies stay warm for one store while its terms run,
            # and the host sees one store's traffic at a time.
            for point_now in points:
                if self._abort_run:
                    break
                await self._warm(session, point_now)
                batch = 6
                for i in range(0, len(urls), batch):
                    await asyncio.gather(
                        *[job(point_now, u) for u in urls[i:i + batch]],
                        return_exceptions=True)
                    if self._abort_run:
                        break
                    if i + batch < len(urls):
                        await asyncio.sleep(0.5)
        self._warned = False
        return found

    def stats(self) -> dict:
        total = max(self.requests, 1)
        return {"requests": self.requests, "success": self.successes,
                "errors": self.errors, "forbidden": self.forbidden,
                "rate_limited": self.rate_limited,
                "aborted_runs": self.aborted_runs,
                "error_rate": round(self.errors / total * 100, 1),
                "points": len(self._warmed)}
