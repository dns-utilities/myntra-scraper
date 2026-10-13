"""Myntra search API client — async bulk scrape with curl_cffi impersonation."""

from __future__ import annotations

import asyncio
import logging
import random
import threading

from curl_cffi.requests import AsyncSession

from myntra_scraper.models import Product
from myntra_scraper.search_api import ROWS_PER_PAGE, build_api_request, parse_search_url
from myntra_scraper.token_manager import TokenManager

log = logging.getLogger("myntra_scraper.scraper")


class MyntraScraper:
    def __init__(self, token_manager: TokenManager, impersonate: str,
                 pincode: str, concurrency: int, max_pages: int,
                 rate_limiter=None) -> None:
        self._tokens = token_manager
        self._impersonate = impersonate
        self._pincode = pincode
        self._concurrency = concurrency
        self._max_pages = max_pages
        # Per-source politeness budget. Concurrency alone still lets a batch
        # burst the host; the bucket spaces requests out.
        self._limiter = rate_limiter
        self._lock = threading.Lock()
        self.requests = 0
        self.successes = 0
        self.errors = 0
        self.forbidden = 0
        self.rate_limited = 0
        log.info("Scraper ready | pincode=%s concurrency=%d pages=%d impersonate=%s rps=%s",
                 pincode, concurrency, max_pages, token_manager.impersonate,
                 getattr(rate_limiter, "_rate", "unbounded"))

    def _headers(self, token: str, referer: str) -> dict:
        # MUST match the token session: same device, same sessionID,
        # same UA, same abtest, same impersonation family. Any mismatch
        # (old bug: random device + random abtest per request) => WAF 403.
        device = self._tokens.device
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US,en;q=0.5",
            "at": token,
            "Connection": "Keep-Alive",
            "IGNORE-REFRESH": "true",
            "User-Agent": self._tokens.user_agent,
            "x-device-state": "connectionClass=UNKNOWN; connectionType=WIFI; bandwidth=-1.0; deviceYear=2015; deviceYearType=2;",
            "X-MYNTRA-ABTEST": self._tokens.abtest,
            "X-MYNTRA-APP": "appFamily=MyntraRetailAndroid; appVersion=3.33.3; appBuild=80110249; deviceCategory=Phone; osVersion=%s; sdkVersion=%s; deviceID=%s; installationID=%s; sessionID=%s;advertisingID=0;advertisingIDKeySpace=GAID;bundleVersion=1.0.0;" % (device["os"], device["os"], device["device_id"], device["install_id"], self._tokens.session_id),
            "X-MYNTRA-PINCODE": self._pincode,
            "X-SLOT-MODE": "disabled",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    async def scrape_all(self, urls: list[str]) -> list[Product]:
        import orjson as json_lib
        found: list[Product] = []
        lock = asyncio.Lock()

        async def fetch_one(session: AsyncSession, url: str) -> None:
            info = parse_search_url(url)
            results: list[Product] = []
            page = 0
            # Retry budget per URL: 403 -> force token refresh + retry once,
            # instead of old behaviour (invalidate + abandon page silently).
            retries_403 = 0
            while page < self._max_pages:
                token = self._tokens.get_token()
                if not token:
                    log.warning("No token available — skipping %s", url[:80])
                    break
                api_url, params = build_api_request(info, page)
                try:
                    if self._limiter is not None:
                        await self._limiter.acquire()
                    resp = await session.get(api_url, params=params,
                                             headers=self._headers(token, url), timeout=20)
                    with self._lock:
                        self.requests += 1
                    if resp.status_code == 200:
                        with self._lock:
                            self.successes += 1
                        try:
                            items = json_lib.loads(resp.content).get("products", [])
                        except Exception as exc:
                            log.warning("Bad JSON for %s: %s", url[:80], exc)
                            with self._lock:
                                self.errors += 1
                            break
                        batch = [p for raw in items if (p := Product.from_api_item(raw))]
                        results.extend(batch)
                        if len(items) < ROWS_PER_PAGE:
                            break
                        page += 1
                    elif resp.status_code in (401, 403):
                        with self._lock:
                            self.errors += 1
                            self.forbidden += 1
                        body = (resp.text or "")[:200]
                        log.warning("Search HTTP %d for %s (try %d): %s",
                                    resp.status_code, url[:80], retries_403 + 1, body)
                        self._tokens.invalidate()
                        if retries_403 < 1:
                            retries_403 += 1
                            fresh = self._tokens.get_token(force_refresh=True)
                            if fresh:
                                await asyncio.sleep(2)
                                continue  # retry SAME page with fresh token
                        await asyncio.sleep(0.5)
                        break
                    elif resp.status_code == 429:
                        with self._lock:
                            self.errors += 1
                            self.rate_limited += 1
                        log.warning("Search 429 for %s — backing off", url[:80])
                        await asyncio.sleep(5)
                        break
                    else:
                        with self._lock:
                            self.errors += 1
                        log.warning("Search HTTP %d for %s: %s",
                                    resp.status_code, url[:80], (resp.text or "")[:200])
                        break
                except Exception as exc:
                    with self._lock:
                        self.errors += 1
                    log.warning("Search error for %s: %s", url[:80], exc)
                    break
            async with lock:
                found.extend(results)

        # Seed the async session with the token session's cookies (mxeccomid,
        # _myntra_* etc.). The old code used a FRESH cookie jar here, so every
        # search looked like a different client than the token call => 403.
        seed_cookies = self._tokens.cookies_snapshot()
        async with AsyncSession(impersonate=self._tokens.impersonate,
                                cookies=seed_cookies or None) as session:
            sem = asyncio.Semaphore(min(self._concurrency, 8))

            async def limited(url: str) -> None:
                async with sem:
                    await fetch_one(session, url)
                    # Gentle pacing: 20 parallel search hits = WAF bait.
                    await asyncio.sleep(random.uniform(0.3, 0.9))

            # Stagger batch start so all URLs don't fire at once.
            async def staggered(urls_chunk: list[str]) -> None:
                await asyncio.gather(*[limited(u) for u in urls_chunk], return_exceptions=True)

            batch_size = min(len(urls), 8)
            for i in range(0, len(urls), batch_size):
                await staggered(urls[i:i + batch_size])
                if i + batch_size < len(urls):
                    await asyncio.sleep(1.0)
        return found

    def stats(self) -> dict:
        total = max(self.requests, 1)
        return {"requests": self.requests, "success": self.successes,
                "errors": self.errors, "forbidden": self.forbidden,
                "rate_limited": self.rate_limited,
                "error_rate": round(self.errors / total * 100, 1)}
