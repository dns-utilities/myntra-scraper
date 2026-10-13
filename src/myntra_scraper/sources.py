"""Source abstraction — one pipeline, many sites.

Everything site-specific lives behind a ``SourceAdapter``: how URLs are turned
into ``Product`` objects, where the links/seen-state live, how fast it is
allowed to talk, and how often. The pipeline in ``app.py`` is source-agnostic,
so adding Blinkit/Zepto/Instamart later is a new adapter plus env vars — not a
rewrite (see ``docs/FUTURE-SOURCES.md``).

Two safeguards exist because the pipeline runs unattended for weeks:

* **``SourceHealth``** — a per-source circuit breaker. A source that keeps
  failing is parked with exponential backoff and probed occasionally, so one
  broken site cannot burn the shared IP reputation or flood the logs.
* **``AsyncRateLimiter``** — a per-source token bucket. Politeness is a
  per-host constraint, never a global one: a burst aimed at one site is what
  gets an IP rate-limited, and a 403 takes down the whole feed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from myntra_scraper.config import Settings
from myntra_scraper.models import Product
from myntra_scraper.stores import LinkStore, SeenStore

log = logging.getLogger("myntra_scraper.sources")


@dataclass
class SourceSpec:
    """Declarative description of one source. No behaviour, no secrets."""

    name: str
    label: str
    links_file: Path
    seen_db: Path
    # Location the prices are observed at. "" for a single national price;
    # a pincode/store id makes the seen-key location-scoped.
    default_location: str = ""
    # 0 = run every scheduler tick (legacy behaviour). Otherwise minimum
    # seconds between runs, so slow-moving sources can be polled daily.
    interval: int = 0
    rps: float = 5.0            # per-source request budget
    timeout: int = 20
    enabled: bool = True
    min_discount_new: int = 0
    min_discount_existing: int = 15
    max_pages: int = 5
    concurrency: int = 8

    def apply_overrides(self, overrides: dict) -> "SourceSpec":
        for key, value in (overrides or {}).items():
            if hasattr(self, key):
                setattr(self, key, value)
        return self


class AsyncRateLimiter:
    """Token bucket shared by every request of one source."""

    def __init__(self, rate: float, burst: int = 1) -> None:
        self._rate = max(float(rate), 0.05)
        self._capacity = max(1, int(burst))
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


class SourceHealth:
    """Per-source circuit breaker + run-level numbers.

    Deliberately *run*-focused: HTTP-level counters (requests/403/429/...) stay
    with the scraper, so /status and /diag show one number per metric.
    """

    OPEN_AFTER_FAILURES = 5
    MAX_OPEN_SECONDS = 3600
    EMPTY_AFTER = 3            # consecutive zero-product runs before opening

    def __init__(self, name: str) -> None:
        self.name = name
        self.runs = 0
        self.items = 0
        self.run_failures = 0
        self.empty_runs = 0
        self.consecutive_failures = 0
        self.consecutive_empty = 0
        self.open_until = 0.0
        self.open_seconds = 300.0
        self.last_ok = 0.0
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_run = 0.0
        self.last_duration = 0.0

    # -- breaker -----------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def should_probe(self, now: float | None = None) -> bool:
        """Called when open: True when the cooldown has elapsed (half-open)."""
        return not self.is_open

    def _trip(self, reason: str) -> None:
        self.open_seconds = min(self.open_seconds * 2, self.MAX_OPEN_SECONDS)
        self.open_until = time.monotonic() + self.open_seconds
        log.warning("Source %s circuit OPEN for %.0fs (%s)",
                    self.name, self.open_seconds, reason)

    def _reset(self) -> None:
        self.consecutive_failures = 0
        self.consecutive_empty = 0
        self.open_seconds = 300.0
        self.open_until = 0.0

    # -- recording ---------------------------------------------------------
    def record_run_start(self) -> None:
        self.runs += 1
        self.last_run = time.monotonic()

    def record_run_end(self, duration: float, items: int) -> None:
        self.last_duration = duration
        self.items += items
        if items > 0:
            self.consecutive_empty = 0
            self.empty_runs = 0
        else:
            self.consecutive_empty += 1
            self.empty_runs += 1
            if self.consecutive_empty >= self.EMPTY_AFTER and not self.is_open:
                self._trip("%d consecutive empty runs" % self.consecutive_empty)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_ok = time.monotonic()

    def record_failure(self, exc: str = "", status: int | None = None) -> None:
        self.run_failures += 1
        self.consecutive_failures += 1
        self.last_error = ("HTTP %s" % status) if status else (exc or "error")
        self.last_error_at = time.monotonic()
        if self.consecutive_failures >= self.OPEN_AFTER_FAILURES and not self.is_open:
            self._trip("%d consecutive run failures (%s)"
                       % (self.consecutive_failures, self.last_error))

    def cooldown_remaining(self) -> float:
        return max(0.0, self.open_until - time.monotonic())

    def snapshot(self) -> dict:
        return {
            "runs": self.runs, "items": self.items, "run_failures": self.run_failures,
            "empty_runs": self.empty_runs,
            "open": self.is_open, "cooldown": round(self.cooldown_remaining()),
            "last_duration": round(self.last_duration, 1),
            "last_error": self.last_error,
        }


class SourceAdapter(ABC):
    """One site. Owns its scraper, its link store and its seen DB."""

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec
        self.health = SourceHealth(spec.name)
        self._limiter = AsyncRateLimiter(spec.rps)
        self._links: LinkStore | None = None
        self._seen: SeenStore | None = None

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def rate_limiter(self) -> AsyncRateLimiter:
        return self._limiter

    # -- state -------------------------------------------------------------
    def links(self) -> LinkStore:
        if self._links is None:
            self._links = LinkStore(self.spec.links_file)
        return self._links

    def seen(self) -> SeenStore:
        if self._seen is None:
            self._seen = SeenStore(self.spec.seen_db, source=self.spec.name,
                                   default_location=self.spec.default_location,
                                   min_new=self.spec.min_discount_new,
                                   min_existing=self.spec.min_discount_existing)
        return self._seen

    def location_for(self, product: Product) -> str:
        """Scope a product's price observation. Empty for national pricing."""
        return product.location or self.spec.default_location

    # -- work --------------------------------------------------------------
    @abstractmethod
    async def scrape_all(self, urls: list[str]) -> list[Product]:
        """Fetch every tracked URL for this source and normalise to Product."""

    def scraper_stats(self) -> dict:
        """HTTP-level counters, if the underlying client keeps any."""
        return {}

    def stats(self) -> dict:
        out = dict(self.scraper_stats())
        out.update(self.health.snapshot())
        return out

    def due(self, now: float, tick: float) -> bool:
        interval = self.spec.interval or tick
        return (now - self.health.last_run) >= interval


# -- concrete adapters ------------------------------------------------------
class MyntraSource(SourceAdapter):
    """Myntra search API, behind the token manager."""

    def __init__(self, spec: SourceSpec, settings: Settings) -> None:
        super().__init__(spec)
        from myntra_scraper.scraper import MyntraScraper
        from myntra_scraper.token_manager import TokenManager

        self._tokens = TokenManager(settings.curl_impersonate)
        self._scraper = MyntraScraper(
            self._tokens, settings.curl_impersonate, settings.pincode,
            self.spec.concurrency, self.spec.max_pages, rate_limiter=self._limiter)
        self._pincode = settings.pincode

    @property
    def tokens(self):
        return self._tokens

    async def scrape_all(self, urls: list[str]) -> list[Product]:
        return await self._scraper.scrape_all(urls)

    def location_for(self, product: Product) -> str:
        # Myntra pricing follows the delivery pincode.
        return product.location or self._pincode

    def diag(self) -> str:
        from myntra_scraper.search_api import build_api_request, parse_search_url
        token = self._tokens.get_token(force_refresh=True)
        if not token:
            return "diag[myntra]: token fetch FAILED (network / impersonation)"
        urls = self.links().all()
        if not urls:
            return "diag[myntra]: token OK (%s…), no links tracked" % token[:8]
        info = parse_search_url(urls[0])
        api_url, params = build_api_request(info, 0)
        return "diag[myntra]: token OK | probe %s" % api_url[:90]

    def scraper_stats(self) -> dict:
        return dict(self._scraper.stats())


class CromaSource(SourceAdapter):
    """Croma category API. No auth token, just browser impersonation."""

    def __init__(self, spec: SourceSpec, settings: Settings) -> None:
        super().__init__(spec)
        from myntra_scraper.croma_scraper import CromaScraper

        self._scraper = CromaScraper(settings.croma_impersonate,
                                     self.spec.concurrency, self.spec.max_pages,
                                     settings.croma_channel_code,
                                     rate_limiter=self._limiter)

    async def scrape_all(self, urls: list[str]) -> list[Product]:
        return await self._scraper.scrape_all(urls)

    def scraper_stats(self) -> dict:
        return dict(self._scraper.stats())


# -- assembly ---------------------------------------------------------------
def build_sources(settings: Settings) -> list[SourceAdapter]:
    """Instantiate the enabled sources declared for this deployment."""
    overrides = settings.source_overrides or {}
    specs = [
        SourceSpec(
            name="myntra", label="Myntra",
            links_file=settings.links_file, seen_db=settings.seen_db,
            default_location=settings.pincode,
            interval=0, rps=5.0, max_pages=settings.max_pages_per_url,
            concurrency=settings.scrape_concurrency,
            min_discount_new=settings.min_discount_new,
            min_discount_existing=settings.min_discount_existing,
        ).apply_overrides(overrides.get("myntra")),
        SourceSpec(
            name="croma", label="Croma",
            links_file=settings.croma_links_file, seen_db=settings.croma_seen_db,
            default_location="",
            interval=0, rps=5.0, max_pages=settings.croma_max_pages,
            concurrency=settings.croma_concurrency,
            min_discount_new=settings.min_discount_new,
            min_discount_existing=settings.min_discount_existing,
        ).apply_overrides(overrides.get("croma")),
    ]
    if not settings.croma_enabled:
        specs = [s for s in specs if s.name != "croma"]
    for spec in specs:
        if spec.enabled:
            log.info("Source %s: interval=%s rps=%s pages=%s conc=%s location=%r",
                     spec.name, spec.interval or "per-tick", spec.rps,
                     spec.max_pages, spec.concurrency, spec.default_location)
    return [_ADAPTERS[s.name](s, settings) for s in specs if s.enabled]


_ADAPTERS = {"myntra": MyntraSource, "croma": CromaSource}
