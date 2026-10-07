"""ScraperApp — scrape-loop orchestration: links -> scrape -> filter -> notify."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from myntra_scraper.config import Settings
from myntra_scraper.controller_bot import ControllerBot
from myntra_scraper.models import Product
from myntra_scraper.notifier import DealNotifier
from myntra_scraper.scraper import MyntraScraper
from myntra_scraper.stores import BlacklistStore, LinkStore, SeenStore
from myntra_scraper.token_manager import TokenManager

log = logging.getLogger("myntra_scraper.app")


class ScraperApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_data_dir()
        self.links = LinkStore(settings.links_file)
        self.seen = SeenStore(settings.seen_db, settings.min_discount_new,
                              settings.min_discount_existing, settings.min_change_percent)
        self.blacklist = BlacklistStore(settings.blacklist_db)
        self.tokens = TokenManager(settings.curl_impersonate)
        self.scraper = MyntraScraper(self.tokens, settings.curl_impersonate,
                                     settings.pincode, settings.scrape_concurrency,
                                     settings.max_pages_per_url)
        self.notifier = DealNotifier(settings.bot_tokens, settings.channels,
                                     settings.affiliate_tag)
        self.controller = ControllerBot(settings.control_bot_token,
                                        settings.admin_chat_id, self)
        self.spam_words = self._load_spam_words()
        self.affiliate_tag = self._load_affiliate_tag()
        self.notifier.set_affiliate_tag(self.affiliate_tag)
        self.scraping_enabled = True
        self.cycle_count = 0
        self.start_time = datetime.now()
        log.info("App ready | %d links | spam=%d | blacklist=%d | affiliate=%s",
                 len(self.links), len(self.spam_words), len(self.blacklist.all()),
                 "OFF (clean links)" if not self.affiliate_tag else "ON")

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

    def _is_spam(self, product: Product) -> tuple[bool, str]:
        combined = ("%s %s" % (product.brand, product.title)).lower()
        for word in self.spam_words:
            if word in combined:
                return True, "spam word '%s'" % word
        blocked, reason = self.blacklist.is_blocked(
            product.brand, product.title, product.discount)
        return (True, reason) if blocked else (False, "")

    async def run_cycle(self) -> dict:
        self.cycle_count += 1
        started = time.time()
        log.info("CYCLE #%d started", self.cycle_count)
        urls = self.links.reload()
        if not urls:
            log.warning("No links tracked - use /add_link")
            return {"error": "no_links"}
        products = await self.scraper.scrape_all(urls)
        log.info("Found %d products across %d links", len(products), len(urls))
        queued, skipped, seen_keys = 0, 0, set()
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
            ok, why = self.seen.should_notify(
                product.product_id, product.price, product.mrp, product.discount)
            if not ok:
                skipped += 1
                continue
            self.seen.save(product.product_id, product.price, product.discount)
            self.notifier.queue(product)
            queued += 1
            log.info("QUEUED %d%% Rs.%d %s (%s)", product.discount,
                     product.price, product.brand, why)
        elapsed = time.time() - started
        log.info("CYCLE #%d done in %.1fs | queued=%d skipped=%d pending=%d",
                 self.cycle_count, elapsed, queued, skipped, self.notifier.pending)
        return {"cycle": self.cycle_count, "time": elapsed,
                "queued": queued, "skipped": skipped}


    async def run_forever(self) -> None:
        self.controller.start()
        try:
            while True:
                if self.scraping_enabled:
                    await self.run_cycle()
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

    def status_text(self) -> str:
        uptime = datetime.now() - self.start_time
        seen = self.seen.stats()
        tg = self.notifier.stats()
        sc = self.scraper.stats()
        return "\n".join([
            "Scraper %s | uptime %s" % ("RUNNING" if self.scraping_enabled else "PAUSED", uptime),
            "cycles: %d | links: %d" % (self.cycle_count, len(self.links)),
            "tracked: %d | hot70+: %d | sent total: %d" % (
                seen["tracked"], seen["hot_70plus"], seen["notifications"]),
            "queue: sent=%d failed=%d pending=%d" % (tg["sent"], tg["failed"], tg["pending"]),
            "api: req=%d ok=%d err=%d (403=%d 429=%d)" % (
                sc["requests"], sc["success"], sc["errors"],
                sc.get("forbidden", 0), sc.get("rate_limited", 0)),
            "blacklist: %d rules | spam words: %d" % (
                len(self.blacklist.all()), len(self.spam_words)),
            "affiliate: %s" % ("OFF (clean links)" if not self.affiliate_tag
                               else "ON (%s…)" % self.affiliate_tag[:40]),
        ])

    async def shutdown(self) -> None:
        log.info("Shutting down - draining queue...")
        for _ in range(100):
            if self.notifier.pending == 0:
                break
            await asyncio.sleep(0.1)
        self.notifier.stop()
        self.controller.stop()
        self.seen.close()
        log.info("Goodbye after %d cycles.", self.cycle_count)
