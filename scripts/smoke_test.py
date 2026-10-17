"""Offline smoke test for the new behaviour. No network, no Telegram.

Run: python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode the deal emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dns_scraper.brand_registry import BrandRegistry, normalize
from dns_scraper.channels import ChannelPlan
from dns_scraper.deal_buttons import (deal_keyboard, confirm_keyboard,
                                         extract_product_id, parse_deal_message)
from dns_scraper.models import DiscountTier, Product
from dns_scraper.notifier import DealNotifier
from dns_scraper.stores import AutoClearPolicy, BlacklistStore, SeenStore

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILS.append("%s: got %r want %r" % (label, got, want))
    print("%s %-46s %r" % ("PASS" if ok else "FAIL", label, got))


# -- tiers ------------------------------------------------------------------
check("tier 65", DiscountTier.for_discount(65).name, "BASE")
check("tier 70", DiscountTier.for_discount(70).name, "TIER_70")
check("tier 79", DiscountTier.for_discount(79).name, "TIER_70")
check("tier 80", DiscountTier.for_discount(80).name, "TIER_80")
check("tier 89", DiscountTier.for_discount(89).name, "TIER_80")
check("tier 90", DiscountTier.for_discount(90).name, "TIER_90")
check("tier 100", DiscountTier.for_discount(100).name, "TIER_90")

plan = ChannelPlan(bases={"default": -100, "myntra": -200, "croma": -300},
                   tiers={70: -170, 80: -180, 90: -190})

n = DealNotifier([], plan, "")     # no tokens -> no worker threads
check("route 65 myntra", n.route(65, "myntra"), [-200])       # base, per source
check("route 65 croma", n.route(65, "croma"), [-300])
check("route 65 unknown source", n.route(65, "blinkit"), [-100])  # default base
check("route 75", n.route(75, "myntra"), [-170])               # tier only
check("route 85", n.route(85, "myntra"), [-180])
check("route 95", n.route(95, "croma"), [-190])

# the whole point: a tier deal must never also hit a base channel
for d in range(0, 101):
    got = n.route(d, "myntra")
    if d >= 70:
        check_ok = (got == [-170] or got == [-180] or got == [-190]) and -200 not in got
    else:
        check_ok = got == [-200]
    if not check_ok:
        FAILS.append("route(%d, myntra) leaked base or wrong tier: %r" % (d, got))
check("no tier/base leakage 0-100", [f for f in FAILS if "leaked" in f], [])

# A one-channel deployment (everything inheriting the default base) must not
# post twice to that channel.
single = ChannelPlan(bases={"default": -100, "myntra": -100, "croma": -100},
                     tiers={70: -100, 80: -100, 90: -100})
m = DealNotifier([], single, "")
check("no dup when every destination is one chat", m.route(85, "myntra"), [-100])
check("base dedup myntra", m.route(65, "myntra"), [-100])
check("distinct destinations", single.destinations(), [-100])
check("labels collapse to one", single.labels(), {-100: "base:default"})

# -- message format ---------------------------------------------------------
p = Product(product_id="ABC123", brand="Titan", title="Raga Watch For Women",
            price=1299, mrp=6495, discount=80, source="myntra")
msg = p.format_message()
print("\n--- sample message ---\n%s\n---\n" % msg)
check("no app emoji in head", "🛍️" in msg or "🏬" in msg, False)
check("head order", msg.splitlines()[0], "🚨 80% Off - Titan Raga Watch For Women")
check("app line present", msg.splitlines()[-1], "App: Myntra")
check("price line", "💰 Price: ₹1299 (MRP: ₹6495)" in msg, True)
croma = Product(product_id="265123", brand="Sony", title="Bravia 55 inch",
                price=49990, mrp=89990, discount=44, source="croma",
                url="https://www.croma.com/x/y/265123", location="SA-1234")
check("croma app line", croma.format_message().splitlines()[-1], "App: Croma (SA-1234)")

# -- brand registry ---------------------------------------------------------
registry_payload = {"brands": [
    {"name": "Titan", "mentions": 3118, "category": "watches_accessories",
     "aliases": ["titan"]},
    {"name": "Lacoste", "mentions": 604, "category": "fashion_apparel",
     "aliases": ["lacoste"]},
    {"name": "Apple", "mentions": 4654, "category": "smartphones",
     "aliases": ["apple", "iphone"]},
    {"name": "NoDeals", "mentions": 0, "category": "luxury", "aliases": ["nodeals"]},
]}
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "brands.json"
    path.write_text(json.dumps(registry_payload), encoding="utf-8")
    reg = BrandRegistry.load(path)

    check("resolve exact", reg.resolve("Titan", "Raga Watch"), "Titan")
    check("resolve title lead", reg.resolve("", "Titan Raga Watch For Women"), "Titan")
    check("resolve alias", reg.resolve("Apple", "iPhone 15"), "Apple")
    check("no false positive", reg.resolve("Pineapple", "Pineapple Slice"), None)
    # 0-mention entries stay in the data but callers must filter on evidence
    check("zero-mention still resolves", reg.resolve("NoDeals", "x"), "NoDeals")
    check("eligible 80%", reg.is_eligible("Titan", "Raga", 80, 50), True)
    check("ineligible below min", reg.is_eligible("Titan", "Raga", 40, 50), False)
    check("ineligible unknown brand", reg.is_eligible("Wobble", "x", 90, 50), False)
    check("registry size", len(reg), 4)
    reg.remove("titan")
    check("after remove", reg.resolve("Titan", "Raga Watch"), None)
    check("after remove size", len(reg), 3)

# -- seen store: location keying + migration --------------------------------
with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "seen.db"

    # legacy schema: PK was product_id alone
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE seen (product_id TEXT PRIMARY KEY, last_price TEXT,
        last_discount INTEGER, first_seen TEXT, last_notified TEXT,
        notification_count INTEGER DEFAULT 0)""")
    conn.execute("INSERT INTO seen VALUES ('OLD1','999',80,'t','t',1)")
    conn.commit(); conn.close()

    store = SeenStore(db, source="myntra", default_location="387001",
                      min_new=0, min_existing=15, min_change=1)
    check("migration adopted row", store.get_product("OLD1")["discount"], 80)
    check("migration keeps location", store.get_product("OLD1")["location"], "387001")
    # old key must not double-alert
    check("legacy row not re-alerted",
          store.should_notify("OLD1", 999, 4995, 80, location="387001")[0], False)
    # different location = different observation
    check("other location alerts",
          store.should_notify("OLD1", 999, 4995, 80, location="560001")[0], True)

    # up-only unchanged
    store.save("P1", 500, 70, location="387001", brand="Titan")
    check("same price dup", store.should_notify("P1", 500, 1600, 70, location="387001")[0], False)
    check("discount up", store.should_notify("P1", 500, 1800, 75, location="387001")[0], True)
    check("discount down", store.should_notify("P1", 400, 1600, 65, location="387001")[0], False)

    # auto-clear: cooldown not elapsed -> still blocked
    policy = AutoClearPolicy(enabled=True, hours=24, min_discount=50)
    store.save("P2", 300, 80, location="387001", brand="Titan")
    check("auto-clear waits", store.should_notify("P2", 300, 1500, 80, location="387001",
                                                  auto_clear=policy)[0], False)
    # simulate an old last_notified
    with store._lock:
        store._conn.execute("UPDATE seen SET last_notified='2020-01-01T00:00:00'"
                            " WHERE product_id='P2'")
        store._conn.commit()
        store._cache["%s|%s|%s" % ("myntra", "387001", "P2")] = ("300", 80, "2020-01-01T00:00:00")
    ok, why = store.should_notify("P2", 300, 1500, 80, location="387001",
                                  auto_clear=policy, brand="Titan", title="Raga")
    check("auto-clear fires after cooldown", ok, True)
    check("auto-clear reason", "AUTO-CLEAR" in why, True)
    check("no auto-clear without policy",
          store.should_notify("P2", 300, 1500, 80, location="387001")[0], False)

    # -- auto-clear budget + jitter guards --------------------------------
    import time as _time
    capped = AutoClearPolicy(enabled=True, hours=24, min_discount=50, max_per_hour=3)
    check("budget starts empty", capped.snapshot()["used_this_hour"], 0)
    spent = [capped.try_spend() for _ in range(6)]
    check("budget caps at 3", spent, [True, True, True, False, False, False])
    check("budget deferred counted", capped.snapshot()["deferred"], 3)
    check("budget zero disables", AutoClearPolicy(enabled=True, max_per_hour=0).try_spend(), False)
    # jitter must be deterministic across processes (crc32, not hash())
    jit = AutoClearPolicy(enabled=True, hours=24, jitter_hours=6)
    check("cooldown >= base", jit.cooldown_for("TITAN1") >= 24.0, True)
    check("cooldown <= base+jitter", jit.cooldown_for("TITAN1") <= 30.0, True)
    check("cooldown stable", jit.cooldown_for("TITAN1"), jit.cooldown_for("TITAN1"))
    offsets = {jit.cooldown_for("P%d" % i) for i in range(200)}
    check("jitter spreads products", len(offsets) > 20, True)
    check("no jitter when disabled",
          AutoClearPolicy(enabled=True, hours=24, jitter_hours=0).cooldown_for("X"), 24.0)

    # a capped budget must suppress even a cooldown-eligible product
    store.save("P3", 100, 80, location="387001", brand="Titan")
    with store._lock:
        store._conn.execute("UPDATE seen SET last_notified='2020-01-01T00:00:00'"
                            " WHERE product_id='P3'")
        store._conn.commit()
    store._warm_cache(store._conn)
    zero = AutoClearPolicy(enabled=True, hours=24, min_discount=50, max_per_hour=0)
    check("capped budget blocks re-post",
          store.should_notify("P3", 100, 500, 80, location="387001",
                              auto_clear=zero)[0], False)

    # pruning: P2, P3 and DEAD were all backdated past the 30d window.
    store.save("DEAD", 100, 10, location="387001")
    with store._lock:
        store._conn.execute("UPDATE seen SET last_notified='2020-01-01T00:00:00'"
                            " WHERE product_id IN ('P2','P3','DEAD')")
        store._conn.commit()
    check("prune removed", store.prune(30), 3)
    check("pruned DEAD gone", store.get_product("DEAD") is None, True)
    check("pruned P2 gone", store.get_product("P2") is None, True)
    check("pruned P3 gone", store.get_product("P3") is None, True)
    store.close()

# -- blacklist: brand rules + priced product blocks -------------------------
with tempfile.TemporaryDirectory() as tmp:
    bl = BlacklistStore(Path(tmp) / "blacklist.db")
    bl.add("Nike", 70)
    check("brand blocked", bl.is_blocked("Nike", "Air Max", 65)[0], True)
    check("brand allowed", bl.is_blocked("Nike", "Air Max", 75)[0], False)
    bl.add_product("MYNTRA123", 2499, source="myntra", brand="LG", title="Fridge 260L")
    check("product blocked at price",
          bl.is_product_blocked("MYNTRA123", 2499, "myntra"), True)
    check("product NOT blocked at other price",
          bl.is_product_blocked("MYNTRA123", 1999, "myntra"), False)
    check("product not blocked cross-source",
          bl.is_product_blocked("MYNTRA123", 2499, "croma"), False)
    check("undo block", bl.remove_product("MYNTRA123", 2499, "myntra"), True)
    check("undo gone", bl.is_product_blocked("MYNTRA123", 2499, "myntra"), False)
    check("block count", len(bl.product_blocks()), 0)

# -- buttons -----------------------------------------------------------------
kb = deal_keyboard("MYNTRA123456789", 2499, "myntra")
row = kb["inline_keyboard"][0]
for btn in row:
    print("button callback_data len=%d :: %s" % (len(btn["callback_data"]), btn["callback_data"]))
    if len(btn["callback_data"]) > 64:
        FAILS.append("callback_data too long: %s" % btn["callback_data"])
# longest realistic payload: 25-char id, 7-digit price
long_kb = deal_keyboard("A" * 25, 1234567, "croma")
for btn in long_kb["inline_keyboard"][0]:
    check("long callback <=64", len(btn["callback_data"]) <= 64, True)
    print("  worst case len=%d" % len(btn["callback_data"]))

parsed = parse_deal_message(msg)
check("parsed discount", parsed["discount"], 80)
check("parsed brand", parsed["brand"], "Titan")
check("parsed title", parsed["title"], "Raga Watch For Women")
check("parsed price", parsed["price"], 1299)
check("parsed mrp", parsed["mrp"], 6495)
check("parsed app", parsed["app"], "Myntra")
check("parsed pid", parsed["product_id"], "ABC123")
check("extract pid", extract_product_id("https://www.myntra.com/ABC123"), "ABC123")
check("extract pid buy", extract_product_id("https://www.myntra.com/buy/ABC123?pid=XYZ1"), "XYZ1")
check("confirm keyboard", confirm_keyboard(60)["inline_keyboard"][0][0]["callback_data"], "blk|c|60")

# -- normalize ---------------------------------------------------------------
check("normalize punct", normalize("L'Oréal  Paris-2024!"), "l or al paris 2024")

print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES",
                                len(FAILS), "" if len(FAILS) == 1 else "s"))
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
