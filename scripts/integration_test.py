"""End-to-end wiring test: fake source -> real pipeline -> real notifier.

Validates the pieces that unit tests cannot: source discovery, location
scoping, spam/blacklist/auto-clear interaction, seen-keying and tier routing —
without touching the network. Telegram sends fail harmlessly (bogus token).

Run: python scripts/integration_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILS.append("%s: got %r want %r" % (label, got, want))
    print("%s %-44s %r" % ("PASS" if ok else "FAIL", label, got))


class FakeSource:
    """Stands in for a real SourceAdapter, reusing the app's real SeenStore."""

    def __init__(self, app, name, products, links_count=3, seen_store=None):
        from dns_scraper.sources import SourceSpec

        self.name = name
        self._real_seen = seen_store
        self.spec = SourceSpec(
            name=name, label=name.title(),
            links_file=Path(app.settings.data_dir) / ("%s_links.txt" % name),
            seen_db=Path(app.settings.data_dir) / ("%s_seen.db" % name),
            default_location="387001")
        self._links = type("L", (), {"all": lambda s: ["u"] * links_count,
                                     "reload": lambda s: ["u"] * links_count,
                                     "__len__": lambda s: links_count})()
        self._products = products
        self.health = type("H", (), {
            "is_open": False, "cooldown_remaining": lambda s: 0,
            "record_run_start": lambda s: None,
            "record_run_end": lambda s, d, i: None,
            "record_success": lambda s: None,
            "record_failure": lambda s, exc="", status=None: None,
            "last_run": 0.0, "runs": 0,
            "snapshot": lambda s: {"runs": 1, "items": len(self._products),
                                   "open": False, "cooldown": 0,
                                   "last_duration": 0.1, "last_error": "",
                                   "empty_runs": 0, "run_failures": 0},
            "stats": lambda s: s.snapshot(),
        })()

    def links(self):
        return self._links

    def seen(self):
        return self._real_seen

    def location_for(self, product):
        return self.spec.default_location

    async def scrape_all(self, urls):
        return list(self._products)

    def scraper_stats(self):
        return {"requests": 3, "success": 3, "errors": 0,
                "forbidden": 0, "rate_limited": 0}

    def stats(self):
        out = dict(self.scraper_stats())
        out.update(self.health.snapshot())
        return out


def build_app(tmp: Path):
    os.environ.update({
        "BOT_TOKENS": "123456:AA-fake-token-for-tests",
        "ADMIN_CHAT_ID": "42",
        "CHANNEL_BASE_DEFAULT": "-1001",
        "CHANNEL_TIER_70": "-1002",
        "CHANNEL_TIER_80": "-1003",
        "CHANNEL_TIER_90": "-1004",
        "DATA_DIR": str(tmp),
        "SOURCE_CROMA_ENABLED": "false",
        # Blinkit/BigBasket need the network and a location file; they have
        # their own suite (qcommerce_test.py). Keeping them out here holds this
        # test offline and to the pipeline it is actually about.
        "SOURCE_BLINKIT_ENABLED": "false",
        "SOURCE_BIGBASKET_ENABLED": "false",
        # This suite asserts the *posting* path, so the empty-DB baseline
        # (which deliberately posts nothing on the first cycle) is off.
        # Baseline behaviour is covered by qcommerce_test.py.
        "BASELINE_ON_EMPTY": "false",
        "AUTO_CLEAR_HOURS": "24",
        "AUTO_CLEAR_MIN_DISCOUNT": "50",
    })
    for key in ("SOURCE_MYNTRA_RPS", "SOURCE_CROMA_RPS"):
        os.environ.pop(key, None)
    from dns_scraper.app import ScraperApp
    from dns_scraper.config import Settings

    settings = Settings.load()
    return ScraperApp(settings), settings


def product(pid, brand, title, price, mrp, discount, source="myntra"):
    from dns_scraper.models import Product
    return Product(product_id=pid, brand=brand, title=title, price=price,
                   mrp=mrp, discount=discount, source=source,
                   url="https://www.myntra.com/%s" % pid)


async def main() -> int:
    from dns_scraper.models import Product

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        app, settings = build_app(tmp)

        # Swap the real sources for a fake one that reuses the real SeenStore.
        real_seen = app._by_name["myntra"].seen()
        batch = [
            product("TITAN1", "Titan", "Raga Watch For Women", 1299, 6495, 80),
            product("TITAN2", "Titan", "Neo Watch", 1499, 2998, 50),
            product("LOWDISC", "Nike", "Air Max", 4000, 5000, 20),
            product("SPAMMY", "Roadster", "Pack of 3 T-shirts", 500, 2500, 80),
            product("JUNKBRAND", "Wobble", "Mystery Box", 100, 5000, 98),
        ]
        fake = FakeSource(app, "myntra", batch, seen_store=real_seen)
        app.sources = [fake]
        app._by_name = {"myntra": fake}
        app._deal_buttons._seen = {"myntra": real_seen}

        print("\n-- pass 1 (all new) --")
        res = await app.run_source(fake)
        check("queued pass1", res["queued"], 4)      # junk brand + spam excluded
        check("found pass1", res["found"], 5)

        print("\n-- pass 2 (immediate repeat = duplicates) --")
        fake._products = list(batch)
        res = await app.run_source(fake)
        check("queued pass2 (dupes)", res["queued"], 0)

        print("\n-- pass 3 (Blacklist Product at read price) --")
        app.blacklist.add_product("TITAN1", 1299, source="myntra", brand="Titan")
        ti = [p for p in batch if p.product_id == "TITAN1"][0]
        res = await app.run_source(fake)
        check("blocked product not re-queued", res["queued"], 0)
        # a price drop on the same product is a different observation
        fake._products = [product("TITAN1", "Titan", "Raga Watch For Women",
                                  999, 6495, 85)]
        res = await app.run_source(fake)
        check("price drop still alerts", res["queued"], 1)

        print("\n-- pass 4 (discount rise re-alerts) --")
        fake._products = [product("LOWDISC", "Nike", "Air Max", 3000, 6000, 50)]
        res = await app.run_source(fake)
        check("discount up re-queued", res["queued"], 1)

        print("\n-- pass 5 (auto-clear after cooldown) --")
        # Lift the block from pass 3 — this is what /unblock_product does.
        app.blacklist.remove_product("TITAN1", 1299, "myntra")
        # Titan at 80% is an evidenced brand at >=50%: eligible.
        fake._products = [product("TITAN1", "Titan", "Raga Watch For Women",
                                  1299, 6495, 80)]
        res = await app.run_source(fake)
        check("cooldown not elapsed -> 0", res["queued"], 0)
        import sqlite3
        conn = sqlite3.connect(str(settings.seen_db))
        conn.execute("UPDATE seen SET last_notified='2020-01-01T00:00:00'")
        conn.commit(); conn.close()
        # refresh the warm cache the same way save() does
        real_seen._cache.clear()
        real_seen._warm_cache(real_seen._conn)
        res = await app.run_source(fake)
        check("cooldown elapsed -> re-queued", res["queued"], 1)

        print("\n-- status / diag render --")
        text = app.status_text()
        check("status mentions auto-clear", "auto-clear:" in text, True)
        check("status mentions brands", "brands=%d" % len(app.brand_registry) in text, True)
        check("registry loaded", len(app.brand_registry) > 500, True)

        print("\n-- message shape --")
        msg = batch[0].format_message()
        check("head clean", msg.splitlines()[0], "🚨 80% Off - Titan Raga Watch For Women")
        check("app at bottom", msg.splitlines()[-1], "App: Myntra")

        app.notifier.stop()
        for source in app.sources:
            try:
                source.seen().close()
            except Exception:
                pass

    print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES",
                                    len(FAILS), "" if len(FAILS) == 1 else "s"))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
