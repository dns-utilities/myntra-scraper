"""ScraperApp — source-agnostic pipeline: links -> scrape -> filter -> notify.

The loop is a small scheduler rather than one global cadence: every source
declares its own interval, politeness budget and health, so a slow or broken
site is parked on its own circuit breaker instead of taking the feed down or
burning the shared egress IP. Adding a source never touches this file (see
``sources.py`` and ``docs/FUTURE-SOURCES.md``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from dns_scraper.brand_registry import BrandRegistry
from dns_scraper.channels import TIER_FLOORS
from dns_scraper.config import Settings
from dns_scraper.deal_buttons import DealButtonHandler
from dns_scraper.croma_stores import CromaStoreMapper
from dns_scraper.models import Product
from dns_scraper.notifier import DealNotifier, SendLimits
from dns_scraper.sources import SourceAdapter, build_sources
from dns_scraper.stores import AutoClearPolicy, BlacklistStore
from dns_scraper.token_manager import TokenManager

log = logging.getLogger("dns_scraper.app")

# Minimum gap between two admin alerts about the same source, so a dead site
# does not turn into a notification flood.
ALERT_COOLDOWN_SECONDS = 1800

# How long shutdown waits for the send queue to empty before handing the rest
# to the on-disk outbox. Long enough that a normal queue finishes, short enough
# that a platform-wide redeploy still exits promptly.
DRAIN_SECONDS = 20.0


class ScraperApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_data_dir()

        self.blacklist = BlacklistStore(settings.blacklist_db)
        self.notifier = DealNotifier(
            settings.bot_tokens, settings.channel_plan, settings.affiliate_tag,
            limits=SendLimits(global_per_sec=settings.send_global_per_sec,
                              per_bot_per_sec=settings.send_per_bot_per_sec,
                              per_chat_per_min=settings.send_per_chat_per_min,
                              max_queue=settings.send_max_queue))
        # Anything a previous run persisted mid-shutdown is owed to the
        # channels, so it goes back in front of the workers immediately.
        restored = self.notifier.restore(settings.outbox_file)
        if restored:
            log.warning("Resumed %d unsent message(s) from the previous run", restored)
        self.croma_stores = CromaStoreMapper(settings.croma_stores_file)
        self.sources: list[SourceAdapter] = build_sources(settings)
        self._by_name = {s.name: s for s in self.sources}
        # A source with no base channel would silently drop every sub-tier deal,
        # one error line at a time. Much better to say so once, at boot.
        plan = settings.channel_plan
        homeless = [s.label for s in self.sources if not plan.base_for(s.name)]
        if homeless:
            log.warning("No base channel for %s — deals below %d%% will be dropped. "
                        "Set CHANNEL_BASE_DEFAULT, or CHANNEL_BASE_<NAME> per source.",
                        ", ".join(homeless), min(TIER_FLOORS))
        # Reporting only: sources whose seen-DB has held rows at some point this
        # boot. The baseline *decision* is the DB's emptiness at run time (see
        # _baseline_pending), so this set never suppresses a re-baseline.
        self._baselined: set[str] = set()
        self._shutdown_done = False

        # Auto-clear: which deep discounts are worth re-posting on a cooldown.
        self.brand_registry = BrandRegistry.load(settings.auto_clear_brands_file)
        self.auto_clear_policy = AutoClearPolicy(
            enabled=settings.auto_clear_enabled,
            hours=settings.auto_clear_hours,
            min_discount=settings.auto_clear_min_discount,
            max_per_hour=settings.auto_clear_max_per_hour,
            jitter_hours=settings.auto_clear_jitter_hours)

        self.controller = None            # wired by main/controller start
        self._deal_buttons: DealButtonHandler | None = None
        self._set_controller()

        self.spam_words = self._load_spam_words()
        self.affiliate_tag = self._load_affiliate_tag()
        self.notifier.set_affiliate_tag(self.affiliate_tag)
        self.scraping_enabled = True
        self.cycle_count = 0
        self.start_time = datetime.now()
        self._alerts: dict[str, float] = {}
        self._last_prune = time.monotonic()

        log.info("App ready | sources: %s | auto-clear: %s (%d brands, >=%d%%, %dh + <=%dh jitter, <=%d/h)",
                 ", ".join(s.name for s in self.sources) or "none",
                 "ON" if self.auto_clear_policy.enabled else "OFF",
                 len(self.brand_registry), self.auto_clear_policy.min_discount,
                 self.auto_clear_policy.hours, self.auto_clear_policy.jitter_hours,
                 self.auto_clear_policy.max_per_hour)
        log.info("affiliate=%s", "OFF (clean links)" if not self.affiliate_tag else "ON")

    # -- controller wiring -------------------------------------------------
    def _set_controller(self) -> None:
        from dns_scraper.controller_bot import ControllerBot

        self.controller = ControllerBot(
            self.settings.control_bot_token, self.settings.admin_chat_id, self,
            bot_tokens=self.settings.bot_tokens)
        self._deal_buttons = DealButtonHandler(
            self.blacklist,
            {s.name: s.seen() for s in self.sources},
            self.settings.admin_chat_id,
            set(self.settings.channel_plan.destinations()),
            send=self.controller.reply,
            answer=self.controller.answer_callback)

    # -- source handles (kept as attributes for the controller + /status) ---
    @property
    def _myntra(self) -> SourceAdapter | None:
        return self._by_name.get("myntra")

    @property
    def _croma(self) -> SourceAdapter | None:
        return self._by_name.get("croma")

    @property
    def tokens(self) -> TokenManager | None:
        src = self._myntra
        return getattr(src, "tokens", None) if src else None

    @property
    def scraper(self):
        src = self._croma
        return getattr(src, "_scraper", None) if src else None

    @property
    def croma_scraper(self):
        src = self._croma
        return getattr(src, "_scraper", None) if src else None

    @property
    def links(self):
        return self._myntra.links() if self._myntra else None

    @property
    def croma_links(self):
        return self._croma.links() if self._croma else None

    @property
    def seen(self):
        return self._myntra.seen() if self._myntra else None

    @property
    def croma_seen(self):
        return self._croma.seen() if self._croma else None

    def source(self, name: str) -> SourceAdapter | None:
        return self._by_name.get(name)

    def source_names(self) -> list[str]:
        return [s.name for s in self.sources]

    # -- spam words --------------------------------------------------------
    def _load_spam_words(self) -> set[str]:
        words = {w.lower() for w in self.settings.DEFAULT_SPAM_WORDS}
        try:
            if self.settings.spam_words_file.exists():
                words |= {l.strip().lower() for l in
                          self.settings.spam_words_file.read_text(encoding="utf-8").splitlines()
                          if l.strip()}
        except OSError as exc:
            log.warning("Spam file unreadable: %s", exc)
        return words

    def save_spam_words(self) -> None:
        custom = self.spam_words - {w.lower() for w in self.settings.DEFAULT_SPAM_WORDS}
        self.settings.spam_words_file.write_text("\n".join(sorted(custom)), encoding="utf-8")

    # -- affiliate tag (persisted on volume, live-updatable) ------------------
    def _load_affiliate_tag(self) -> str:
        try:
            if self.settings.affiliate_tag_file.exists():
                saved = self.settings.affiliate_tag_file.read_text(
                    encoding="utf-8").strip().lstrip("?&")
                if saved or not self.settings.affiliate_tag:
                    return saved
        except OSError as exc:
            log.warning("Affiliate tag file unreadable: %s", exc)
        return self.settings.affiliate_tag

    def set_affiliate_tag(self, tag: str) -> str:
        tag = (tag or "").strip().lstrip("?&")
        self.affiliate_tag = tag
        self.notifier.set_affiliate_tag(tag)
        try:
            self.settings.affiliate_tag_file.write_text(tag, encoding="utf-8")
        except OSError as exc:
            log.warning("Affiliate tag file unwritable: %s", exc)
        log.info("Affiliate tag %s", "cleared (clean links)" if not tag else "set")
        return tag

    # -- filtering ---------------------------------------------------------
    def _is_spam(self, product: Product) -> tuple[bool, str]:
        combined = ("%s %s" % (product.brand, product.title)).lower()
        for word in self.spam_words:
            if word in combined:
                return True, "spam word '%s'" % word
        # Priced product blocks from the "Blacklist Product" button.
        if self.blacklist.is_product_blocked(product.product_id, product.price,
                                             product.source):
            return True, "product blocked at ₹%d" % product.price
        blocked, reason = self.blacklist.is_blocked(
            product.brand, product.title, product.discount)
        return (True, reason) if blocked else (False, "")

    def _auto_clear_eligible(self, product: Product) -> bool:
        if not self.auto_clear_policy.enabled:
            return False
        return self.brand_registry.is_eligible(
            product.brand, product.title, product.discount,
            self.auto_clear_policy.min_discount)

    def _baseline_pending(self, source: SourceAdapter, seen_store) -> bool:
        """True when this run should record the catalogue instead of posting it.

        The signal is the *seen-DB's emptiness*, deliberately not a per-boot
        flag. An empty store means every qualifying product is about to look
        brand new at once, and that is true whether the emptiness came from a
        first deploy, a wiped volume, or a ``/clear_all`` sent while the bot was
        already running. A per-process flag misses that last case and re-announces
        the whole catalogue -- the exact wall of messages this exists to prevent.
        """
        if not self.settings.baseline_on_empty:
            return False
        try:
            empty = seen_store.stats().get("tracked", 0) == 0
        except Exception as exc:
            log.debug("Baseline check failed for %s: %s", source.name, exc)
            return False
        if not empty:
            self._baselined.add(source.name)      # reporting only, see /status
            return False
        if source.name in self._baselined:
            log.warning("[%s] seen-DB went empty again -- re-baselining rather "
                        "than re-announcing the catalogue", source.name)
        return True

    def _process_products(self, products, source: SourceAdapter) -> tuple[int, int]:
        """Filter + queue one source's batch. Returns (queued, skipped)."""
        seen_store = source.seen()
        baseline = self._baseline_pending(source, seen_store)
        queued, skipped, seen_keys, recorded = 0, 0, set(), 0
        for product in products:
            key = (product.product_id, product.price, product.discount)
            if key in seen_keys:
                skipped += 1
                continue
            seen_keys.add(key)
            spam, reason = self._is_spam(product)
            if spam:
                skipped += 1
                continue
            location = source.location_for(product)
            eligible = self._auto_clear_eligible(product)
            if baseline:
                # Record silently: this is the baseline, not a deal.
                seen_store.save(product.product_id, product.price, product.discount,
                                location=location,
                                store_codes=getattr(product, "store_codes", ()) or None,
                                product_name=product.title, brand=product.brand,
                                auto_clear=eligible)
                recorded += 1
                continue
            ok, why = seen_store.should_notify(
                product.product_id, product.price, product.mrp, product.discount,
                location=location,
                auto_clear=self.auto_clear_policy if eligible else None,
                brand=product.brand, title=product.title)
            if not ok:
                skipped += 1
                continue
            seen_store.save(product.product_id, product.price, product.discount,
                            location=location,
                            store_codes=getattr(product, "store_codes", ()) or None,
                            product_name=product.title, brand=product.brand,
                            auto_clear=eligible)
            if self.notifier.queue(product):
                queued += 1
            log.info("QUEUED [%s] %d%% Rs.%d %s (%s)", source.name, product.discount,
                     product.price, product.brand, why)
        if baseline and recorded:
            self._baselined.add(source.name)
            log.warning("[%s] BASELINE run: recorded %d product(s) silently, posted "
                        "nothing. Real deals start from the next cycle.",
                        source.name, recorded)
        return queued, skipped

    # -- scheduling --------------------------------------------------------
    async def run_source(self, source: SourceAdapter) -> dict:
        """Scrape one source end to end. Never raises."""
        urls = source.links().reload()
        if not urls:
            log.warning("No %s links tracked - use /add_link", source.name)
            return {"queued": 0, "skipped": 0, "found": 0}
        source.health.record_run_start()
        started = time.time()
        try:
            products = await source.scrape_all(urls)
        except Exception as exc:
            source.health.record_failure(exc="%s: %s" % (type(exc).__name__, exc))
            log.error("Source %s crashed: %s", source.name, exc, exc_info=True)
            self._alert(source.name, "crashed: %s" % exc)
            return {"queued": 0, "skipped": 0, "found": 0, "error": str(exc)}
        duration = time.time() - started
        source.health.record_run_end(duration, len(products))
        source.health.record_success()
        queued, skipped = self._process_products(products, source)
        log.info("[%s] run #%d %.1fs | found=%d queued=%d skipped=%d",
                 source.name, source.health.runs, duration,
                 len(products), queued, skipped)
        if not products:
            self._alert(source.name, "returned 0 products across %d links" % len(urls))
        return {"queued": queued, "skipped": skipped, "found": len(products)}

    async def run_due_sources(self) -> dict:
        """Run every source whose interval has elapsed and that is not parked."""
        now = time.monotonic()
        tick = max(self.settings.cycle_delay, 1)
        totals = {"queued": 0, "skipped": 0, "ran": [], "parked": [], "found": 0}
        for source in self.sources:
            if source.health.is_open:
                totals["parked"].append(source.name)
                log.info("Source %s parked (circuit open, %.0fs left, %s)",
                         source.name, source.health.cooldown_remaining(),
                         source.health.last_error)
                continue
            if not source.due(now, tick):
                continue
            result = await self.run_source(source)
            totals["queued"] += result.get("queued", 0)
            totals["skipped"] += result.get("skipped", 0)
            totals["found"] += result.get("found", 0)
            totals["ran"].append(source.name)
        self._maybe_prune()
        return totals

    async def run_cycle(self) -> dict:
        """Force one pass over every source (used by tests and /run)."""
        results = {}
        for source in self.sources:
            results[source.name] = await self.run_source(source)
        self._maybe_prune()
        return results

    # -- housekeeping ------------------------------------------------------
    def _maybe_prune(self) -> None:
        interval = max(self.settings.prune_interval_hours, 1) * 3600
        if time.monotonic() - self._last_prune < interval:
            return
        self._last_prune = time.monotonic()
        for source in self.sources:
            try:
                source.seen().prune(self.settings.seen_retention_days)
            except Exception as exc:
                log.warning("Prune failed for %s: %s", source.name, exc)

    def _alert(self, source_name: str, message: str) -> None:
        """Rate-limited admin notification for source problems."""
        if not self.controller:
            return
        now = time.monotonic()
        if now - self._alerts.get(source_name, 0) < ALERT_COOLDOWN_SECONDS:
            return
        self._alerts[source_name] = now
        try:
            self.controller.reply("⚠️ Source *%s* %s\n\nRuns continue; the circuit "
                                  "breaker will probe again automatically."
                                  % (source_name, message))
        except Exception as exc:
            log.debug("Alert send failed: %s", exc)

    # -- main loop ---------------------------------------------------------
    async def run_forever(self) -> None:
        if self.controller:
            self.controller.start()
        try:
            while True:
                if self.scraping_enabled:
                    await self.run_due_sources()
                    self.cycle_count += 1
                    if self.settings.max_cycles and self.cycle_count >= self.settings.max_cycles:
                        log.info("MAX_CYCLES=%d reached", self.settings.max_cycles)
                        break
                else:
                    log.info("Paused - send /start to resume")
                    await asyncio.sleep(5)
                    continue
                await asyncio.sleep(self.settings.cycle_delay)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    # -- reporting ---------------------------------------------------------
    def status_text(self) -> str:
        uptime = datetime.now() - self.start_time
        tg = self.notifier.stats()
        budget = self.auto_clear_policy.snapshot()
        budget_line = (
            "auto-clear: %s | brands=%d >=%d%% every=%dh (<=%dh jitter) | "
            "posted this hour=%d/%d deferred=%d" % (
                "ON" if self.auto_clear_policy.enabled else "OFF",
                len(self.brand_registry), self.auto_clear_policy.min_discount,
                self.auto_clear_policy.hours, self.auto_clear_policy.jitter_hours,
                budget["used_this_hour"], budget["cap"], budget["deferred"]))
        lines = [
            "Scraper %s | uptime %s" % ("RUNNING" if self.scraping_enabled else "PAUSED", uptime),
            "ticks: %d" % self.cycle_count,
            self.routing_text(),
        ]
        for source in self.sources:
            seen = source.seen().stats()
            st = source.stats()
            interval = source.spec.interval or self.settings.cycle_delay
            state = "PARKED %.0fs" % st.get("cooldown", 0) if st.get("open") else "ok"
            lines.append(
                "%s: links=%d tracked=%d tiered=%d every=%ds (%s)"
                % (source.spec.label, len(source.links()), seen["tracked"],
                   seen["tiered"], interval, state))
            lines.append(
                "  api req=%d ok=%d err=%d (403=%d 429=%d aborted=%d) | "
                "runs=%d items=%d empty=%d"
                % (st.get("requests", 0), st.get("success", 0), st.get("errors", 0),
                   st.get("forbidden", 0), st.get("rate_limited", 0),
                   st.get("aborted_runs", 0),
                   st.get("runs", 0), st.get("items", 0), st.get("empty_runs", 0)))
            if st.get("aborted_runs"):
                lines.append("    ⚠ %d run(s) aborted on repeated 403 — this egress IP "
                             "looks blocked by that site's WAF."
                             % st["aborted_runs"])
            if st.get("last_error"):
                lines.append("  last error: %s" % st["last_error"])
        lines += [
            "queue: sent=%d failed=%d dropped=%d pending=%d blocked-pairings=%d" % (
                tg["sent"], tg["failed"], tg.get("dropped", 0), tg["pending"],
                tg.get("blocked_pairings", 0)),
            "send pacing: %s" % self.notifier.limits.describe(),
            "baseline: %s | laid down for: %s" % (
                "ON" if self.settings.baseline_on_empty else "OFF",
                ", ".join(sorted(self._baselined)) or "none yet"),
            budget_line,
            "blacklist: %d brand rules | %d product blocks" % (
                len(self.blacklist.all()), len(self.blacklist.product_blocks())),
            "affiliate: %s" % ("OFF (clean links)" if not self.affiliate_tag
                               else "ON (%s…)" % self.affiliate_tag[:40]),
        ]
        if tg["pending"]:
            top = sorted((tg.get("pending_by_chat") or {}).items(),
                         key=lambda kv: -kv[1])[:4]
            lines.append("  pending by chat: %s"
                         % ", ".join("%s:%d" % (cid, n) for cid, n in top))
        return "\n".join(lines)

    def routing_text(self) -> str:
        """One line describing where each discount band posts."""
        return self.settings.channel_plan.describe()

    async def shutdown(self) -> None:
        # main.py and run_forever() both call this on the way out; without the
        # guard the queue is drained and the outbox written twice.
        if self._shutdown_done:
            return
        self._shutdown_done = True
        log.info("Shutting down - draining send queue (up to %.0fs)...", DRAIN_SECONDS)
        deadline = time.monotonic() + DRAIN_SECONDS
        while self.notifier.pending and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        self.notifier.stop()
        # Whatever did not make it out is written to the outbox and re-queued
        # on the next boot. Without this the deals were already marked "seen",
        # so a restart would drop them permanently.
        left = self.notifier.persist(self.settings.outbox_file)
        if left:
            log.warning("Shutdown with %d unsent message(s) — parked in %s for "
                        "the next run", left, self.settings.outbox_file)
        if self.controller:
            self.controller.stop()
        for source in self.sources:
            try:
                source.seen().close()
            except Exception as exc:
                log.debug("close %s: %s", source.name, exc)
        log.info("Goodbye after %d ticks.", self.cycle_count)
