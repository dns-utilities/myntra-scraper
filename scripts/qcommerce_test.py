"""Blinkit + BigBasket plumbing, the empty-DB baseline, and the send gate.

Everything here is offline: no network, no Telegram, no real scrapers. What is
being pinned down is the logic that is easy to get subtly wrong and expensive to
discover in production:

* delivery-point parsing and pincode-cache resolution (both entry forms),
* the two payload parsers, including the sponsored / out-of-stock drops,
* per-source base routing with exclusive 70/80/90 tiers (no duplicate posts),
* the empty-DB baseline (a wiped database must not become a wall of messages),
* the three-bucket send gate that keeps bursts inside Telegram's limits,
  plus the queue ceiling and the shutdown outbox.

Run: python scripts/qcommerce_test.py
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
    print("%s %-48s %r" % ("PASS" if ok else "FAIL", label, got))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def check_range(label: str, got, low, high) -> None:
    ok = low <= got <= high
    if not ok:
        FAILS.append("%s: got %r want %r..%r" % (label, got, low, high))
    print("%s %-48s %r" % ("PASS" if ok else "FAIL", label, got))


# --------------------------------------------------------------------------
# Blinkit payload
# --------------------------------------------------------------------------
def blinkit_card(pid=98765, price="₹250", mrp="₹500", offer="50% OFF",
                 sold_out=False, sponsored=False):
    card = {
        "product_id": pid,
        "name": {"text": "Amul Butter"},
        "brand_name": {"text": "Amul"},
        "variant": {"text": "500 g"},
        "normal_price": {"text": price},
        "mrp": {"text": mrp},
        "image": {"url": "https://cdn.blinkit.com/butter.png"},
    }
    if offer:
        card["offer_tag"] = {"title": {"text": offer}}
    if sold_out:
        card["is_sold_out"] = True
    if sponsored:
        card["is_sponsored"] = True
    return card


def bigbasket_item(pid=55501, desc="Tata Salt", mrp=60.0, sp=39.0,
                   status="001", button="Add"):
    return {
        "id": pid,
        "desc": desc,
        "brand": {"name": "Tata"},
        "w": "1 kg",
        "absolute_url": "/pd/%d/tata-salt/" % pid,
        "image": "https://cdn.bigbasket.com/salt.png",
        "pricing": {"discount": {"mrp": mrp, "prim_price": {"sp": sp}}},
        "availability": {"avail_status": status, "button": button},
    }


def test_parsers() -> None:
    print("\n-- models: text helpers --")
    from dns_scraper.models import (Product, node_text, parse_money,
                                       parse_percent)

    check("parse_money rupee glyph", parse_money("₹250"), 250)
    check("parse_money thousands", parse_money("1,299"), 1299)
    check("parse_money empty", parse_money(""), 0)
    check("parse_money None", parse_money(None), 0)
    check("parse_percent spacing", parse_percent("50% OFF"), 50)
    check("parse_percent newline", parse_percent("20%\nOFF"), 20)
    check("parse_percent absent", parse_percent(""), 0)
    check("node_text dict", node_text({"text": "hi"}), "hi")
    check("node_text str", node_text("hi"), "hi")
    check("node_text None", node_text(None), "")

    print("\n-- models: blinkit parser --")
    p = Product.from_blinkit_item(blinkit_card(), location="29.3,76.9",
                                 location_label="Panipat")
    check("blinkit source", p.source, "blinkit")
    check("blinkit price", p.price, 250)
    check("blinkit mrp", p.mrp, 500)
    check("blinkit discount from offer tag", p.discount, 50)
    check("blinkit url", p.url, "https://blinkit.com/prn/x/prid/98765")
    check("blinkit location scoped", p.location, "29.3,76.9")
    check("blinkit label kept", p.location_label, "Panipat")
    check("blinkit title brand stripped", p.title, "Butter 500 g")
    check("blinkit image unwrapped", p.image, "https://cdn.blinkit.com/butter.png")

    # No offer tag -> discount is computed from price vs mrp.
    p2 = Product.from_blinkit_item(blinkit_card(offer=None))
    check("blinkit computed discount", p2.discount, 50)
    check("blinkit sold out dropped",
          Product.from_blinkit_item(blinkit_card(sold_out=True)), None)
    check("blinkit zero price dropped",
          Product.from_blinkit_item(blinkit_card(price="₹0")), None)
    check("blinkit no id dropped",
          Product.from_blinkit_item(blinkit_card(pid=None)), None)

    print("\n-- models: bigbasket parser --")
    b = Product.from_bigbasket_item(bigbasket_item(), location="10:0",
                                    location_label="Hyderabad-bbnow")
    check("bigbasket source", b.source, "bigbasket")
    check("bigbasket price", b.price, 39)
    check("bigbasket mrp", b.mrp, 60)
    check("bigbasket discount", b.discount, 35)
    check("bigbasket url absolute", b.url,
          "https://www.bigbasket.com/pd/55501/tata-salt/")
    check("bigbasket title brand stripped", b.title, "Salt 1 kg")
    check("bigbasket label kept", b.location_label, "Hyderabad-bbnow")
    check("bigbasket oos status dropped",
          Product.from_bigbasket_item(bigbasket_item(status="002")), None)
    check("bigbasket notify-me dropped",
          Product.from_bigbasket_item(bigbasket_item(button="Notify Me")), None)
    check("bigbasket zero sp dropped",
          Product.from_bigbasket_item(bigbasket_item(sp=0)), None)

    print("\n-- message shape --")
    msg = p.format_message()
    lines = msg.splitlines()
    check("blinkit head", lines[0], "50% Off - Amul Butter 500 g")
    check("blinkit link is the product url", lines[4],
          "https://blinkit.com/prn/x/prid/98765")
    check("blinkit app line", lines[-1], "App: Blinkit (Panipat)")
    bmsg = b.format_message()
    check("bigbasket app line", bmsg.splitlines()[-1],
          "App: BigBasket (Hyderabad-bbnow)")


# --------------------------------------------------------------------------
# API request building
# --------------------------------------------------------------------------
def test_api_modules() -> None:
    print("\n-- blinkit api --")
    from dns_scraper import blinkit_api

    check("term bare", blinkit_api.term("atta"), "atta")
    check("term from url", blinkit_api.term("https://blinkit.com/s/?q=atta%20flour"),
          "atta flour")
    # LinkStore keeps '#' comments so the files stay readable; a keyword source
    # must not turn those into searches.
    check("comment is not a term", blinkit_api.term("# --- Atta ---"), "")
    check("comment is not usable", blinkit_api.is_usable("# --- Atta ---"), False)
    check("blank is not usable", blinkit_api.is_usable("   "), False)
    check("real term is usable", blinkit_api.is_usable("atta"), True)
    h = blinkit_api.headers({"lat": "29.3", "lon": "76.9"})
    check("blinkit lat header", h["lat"], "29.3")
    check("blinkit lon header", h["lon"], "76.9")
    check("blinkit app_client", h["app_client"], "consumer_web")
    check("blinkit platform", h["platform"], "desktop_web")

    data = {"response": {"snippets": [
        {"widget_type": blinkit_api.PRODUCT_CARD, "data": {"product_id": 1}},
        {"widget_type": "banner", "data": {"product_id": 2}},
        {"widget_type": blinkit_api.PRODUCT_CARD, "data": {"product_id": 3}},
    ]}}
    check("iter_cards only product cards",
          [c["product_id"] for c in blinkit_api.iter_cards(data)], [1, 3])
    check("iter_cards survives junk", list(blinkit_api.iter_cards({"x": 1})), [])
    check("sponsored flagged", blinkit_api.is_sponsored({"is_sponsored": True}), True)
    check("sponsored absent", blinkit_api.is_sponsored({"product_id": 1}), False)

    # Blinkit answers a refused fingerprint with a flat 403 on the search call.
    # chrome124/chrome131 are refused by its edge (measured live), so the
    # default must be a fingerprint that answers, and the chain must not repeat.
    check("default fingerprint is a candidate",
          blinkit_api.DEFAULT_IMPERSONATE in blinkit_api.IMPERSONATIONS, True)
    check("default is not a refused fingerprint",
          blinkit_api.DEFAULT_IMPERSONATE in
          ("chrome119", "chrome123", "chrome124", "chrome131"), False)
    from dns_scraper.blinkit_scraper import BlinkitScraper
    chain = BlinkitScraper()._fingerprints()
    check("fingerprint chain leads with the default",
          chain[0], blinkit_api.DEFAULT_IMPERSONATE)
    check("fingerprint chain covers every candidate",
          len(chain), len(blinkit_api.IMPERSONATIONS))
    override = BlinkitScraper(impersonate="chrome124")._fingerprints()
    check("operator override still leads", override[0], "chrome124")
    check("override is not repeated in the fallbacks", override.count("chrome124"), 1)

    print("\n-- bigbasket api --")
    from dns_scraper import bigbasket_api

    check("parse search", bigbasket_api.parse_link("search:atta"), ("ps", "atta"))
    check("parse slug", bigbasket_api.parse_link("headphones"), ("pc", "headphones"))
    check("parse pc url",
          bigbasket_api.parse_link("https://www.bigbasket.com/pc/1/fruits-vegetables/"),
          ("pc", "fruits-vegetables"))
    check("comment is not a slug", bigbasket_api.parse_link("# --- Audio ---"), ("", ""))
    check("comment is not usable", bigbasket_api.is_usable("# --- Audio ---"), False)
    check("real slug is usable", bigbasket_api.is_usable("headphones"), True)
    hb = bigbasket_api.headers({"ec_id": "10", "sa_id": "0", "city_id": "1",
                                "ec": "bbnow"}, "UA")
    check("bb entry context id", hb["x-entry-context-id"], "10")
    check("bb channel", hb["x-channel"], "BB-WEB")
    check("bb caller", hb["x-caller"], "UIKIRK")
    check("bb sa omitted when zero", "x-sa-id" in hb, False)
    check("bb city sent", hb["x-city-id"], "1")
    hb2 = bigbasket_api.headers({"ec_id": "10", "sa_id": "42", "city_id": "7"}, "UA")
    check("bb sa sent when set", hb2["x-sa-id"], "42")
    check("bb tracker is per-request", hb["x-tracker"] != hb2["x-tracker"], True)

    flat = {"products": [{"id": 1}, {"id": 2}]}
    check("iter_products flat", [p["id"] for p in bigbasket_api.iter_products(flat)], [1, 2])
    tabbed = {"tabs": [{"product_info": {"products": [{"id": 3}]}},
                       {"product_info": {"products": [{"id": 4}]}}]}
    check("iter_products tabbed",
          [p["id"] for p in bigbasket_api.iter_products(tabbed)], [3, 4])


# --------------------------------------------------------------------------
# Delivery points
# --------------------------------------------------------------------------
def test_locations() -> None:
    print("\n-- locations: parsing --")
    from dns_scraper.locations import (PincodeCache, build_location_set,
                                          parse_lines)

    points, pending = parse_lines(
        ["# comment", "", "17.4027233 | 78.3773127 | HYD-Manikonda",
         "pin:132103 | Panipat"], "blinkit")
    check("blinkit raw parsed", points[0].key, "17.4027233,78.3773127")
    check("blinkit raw label", points[0].label, "HYD-Manikonda")
    check("blinkit pin pending", pending, ["132103"])
    check("blinkit pin has no key yet", points[1].key, "")

    points, _ = parse_lines(["17.4027233 | 78.3773127 | HYD"], "blinkit")
    check("blinkit comma separator", points[0].key, "17.4027233,78.3773127")
    points, _ = parse_lines(["garbage line"], "blinkit")
    check("blinkit junk skipped", len(points), 0)

    points, _ = parse_lines(["10 | 0 | 1 | Hyderabad-bbnow"], "bigbasket")
    check("bigbasket raw key", points[0].key, "10:0")
    check("bigbasket raw payload", points[0].payload["city_id"], "1")
    points, _ = parse_lines(["10 | 42 | 7 | Ambala"], "bigbasket")
    check("bigbasket sa in key", points[0].key, "10:42")
    points, _ = parse_lines(["nonsense"], "bigbasket")
    check("bigbasket junk skipped", len(points), 0)

    print("\n-- locations: cache resolution --")
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PincodeCache(Path(tmpdir) / "pincode_cache.json")
        cache.put("blinkit", "132103",
                  {"key": "29.39,76.96", "label": "Panipat",
                   "lat": "29.39", "lon": "76.96"})
        resolved = build_location_set("blinkit", ["pin:132103 | Panipat"],
                                      cache, geocode=False)
        check("blinkit pin resolved from cache", len(resolved), 1)
        check("blinkit resolved key", resolved.all()[0].key, "29.39,76.96")
        check("blinkit resolved payload", resolved.all()[0].payload["lat"], "29.39")

        # Cache survives a fresh instance (it is persisted, not in-memory).
        again = build_location_set("blinkit", ["pin:132103 | Panipat"],
                                   PincodeCache(Path(tmpdir) / "pincode_cache.json"),
                                   geocode=False)
        check("cache persists on disk", len(again), 1)
        # The file's label wins over whatever the geocoder stored, so the deal
        # footer does not change wording between restarts.
        check("file label wins over cached label", again.all()[0].label, "Panipat")
        relabelled = build_location_set("blinkit", ["pin:132103 | Panipat-132103"],
                                        PincodeCache(Path(tmpdir) / "pincode_cache.json"),
                                        geocode=False)
        check("file label edits are picked up", relabelled.all()[0].label,
              "Panipat-132103")
        check("key is unchanged by a relabel", relabelled.all()[0].key, "29.39,76.96")

        # An unresolved pin with geocoding disabled is dropped, not guessed.
        empty = build_location_set("blinkit", ["pin:999999 | Nowhere"],
                                   cache, geocode=False)
        check("unresolved pin dropped, not guessed", len(empty), 0)

        cache.put("bigbasket", "134114",
                  {"key": "10:42", "label": "Ambala", "ec_id": "10",
                   "sa_id": "42", "city_id": "7"})
        bb = build_location_set("bigbasket", ["pin:134114 | Ambala"],
                                cache, geocode=False)
        check("bigbasket pin resolved from cache", len(bb), 1)
        check("bigbasket pin key", bb.all()[0].key, "10:42")


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------
def test_routing() -> None:
    print("\n-- routing: per-source base, exclusive tiers --")
    from dns_scraper.channels import ChannelPlan
    from dns_scraper.notifier import DealNotifier

    plan = ChannelPlan(
        bases={"default": 0, "myntra": -1001, "croma": -1002,
               "blinkit": -1003, "bigbasket": -1004},
        tiers={70: -1005, 80: -1006, 90: -1007})
    note = DealNotifier([], plan, "")     # no tokens -> no worker threads

    check("myntra base", note.route(10, "myntra"), [-1001])
    check("croma base", note.route(10, "croma"), [-1002])
    check("blinkit base", note.route(10, "blinkit"), [-1003])
    check("bigbasket base", note.route(10, "bigbasket"), [-1004])
    check("70 goes to shared tier only", note.route(70, "blinkit"), [-1005])
    check("85 goes to shared tier only", note.route(85, "bigbasket"), [-1006])
    check("95 goes to shared tier only", note.route(95, "myntra"), [-1007])
    check("69 stays in base", note.route(69, "blinkit"), [-1003])

    # No duplicates: a tier deal never also lands in the source base channel.
    for source in ("myntra", "croma", "blinkit", "bigbasket"):
        for discount in (70, 80, 90):
            routed = note.route(discount, source)
            check_true("%s @%d%% posts exactly once" % (source, discount),
                       len(routed) == 1 and routed[0] not in
                       (plan.bases[source], plan.bases["default"]))

    # A source with no base of its own inherits the default base; with no
    # default either, its sub-tier deals are dropped rather than misposted.
    fallback = ChannelPlan(bases={"default": -2001},
                           tiers={70: -2005, 80: -2006, 90: -2007})
    check("unset source falls back to the default base",
          fallback.route(10, "blinkit"), [-2001])
    check("no base at all drops the deal",
          ChannelPlan(tiers={70: -1}).route(10, "blinkit"), [])

    # A band with no channel of its own falls back to the *dealing source's*
    # base, so a "bases only" deployment still delivers deep discounts instead
    # of losing the best deals to a variable nobody set.
    bases_only = ChannelPlan(bases={"default": -3000, "myntra": -3001, "croma": -3002},
                             tiers={})
    check("unset tier falls back to myntra base",
          bases_only.route(85, "myntra"), [-3001])
    check("unset tier falls back to croma base",
          bases_only.route(95, "croma"), [-3002])
    check("source with no base of its own inherits the default",
          bases_only.route(70, "blinkit"), [-3000])
    check("one-channel deployment still routes tiers to the default",
          ChannelPlan(bases={"default": -4001}, tiers={}).route(95, "myntra"),
          [-4001])
    check("band set wins over the source base",
          ChannelPlan(bases={"myntra": -3001}, tiers={70: -5005}).route(75, "myntra"),
          [-5005])


# --------------------------------------------------------------------------
# Send gate + queue ceiling + outbox
# --------------------------------------------------------------------------
def test_send_gate() -> None:
    print("\n-- send gate: per-chat ceiling is the binding one --")
    from dns_scraper.notifier import SendGate, SendLimits

    # 60/min per chat = 1/s. The other two buckets are pushed far out of the
    # way, so anything that waits is waiting on the per-chat bucket.
    FREE = 1_000_000.0

    gate = SendGate(SendLimits(global_per_sec=FREE, per_bot_per_sec=FREE,
                               per_chat_per_min=60, burst=1))
    check("first send admitted", gate.acquire("tok", -1001), 0.0)
    check_range("second send to same chat waits ~1s",
                gate.acquire("tok", -1001), 0.9, 1.15)
    # "Unaffected" means no *meaningful* wait. A token bucket always needs some
    # refill time, so the bar is microseconds, not exactly zero.
    check_range("a different chat is unaffected", gate.acquire("tok", -1002), 0.0, 0.01)

    print("\n-- send gate: global ceiling --")
    g2 = SendGate(SendLimits(global_per_sec=1, per_bot_per_sec=FREE,
                             per_chat_per_min=FREE, burst=1))
    check("first global slot taken", g2.acquire("tok", -1), 0.0)
    check_range("global exhaustion throttles across chats",
                g2.acquire("tok", -2), 0.9, 1.15)

    print("\n-- send gate: per-bot ceiling --")
    g3 = SendGate(SendLimits(global_per_sec=FREE, per_bot_per_sec=1,
                             per_chat_per_min=FREE, burst=1))
    check("first bot slot taken", g3.acquire("botA", -1), 0.0)
    check_range("bot exhaustion throttles that bot",
                g3.acquire("botA", -2), 0.9, 1.15)
    check_range("another bot is unaffected", g3.acquire("botB", -3), 0.0, 0.01)

    print("\n-- send gate: a rejected send consumes nothing --")
    g4 = SendGate(SendLimits(global_per_sec=FREE, per_bot_per_sec=FREE,
                             per_chat_per_min=60, burst=1))
    g4.acquire("tok", -1)                    # chat -1 now empty
    blocked = g4.acquire("tok", -1)          # rejected
    check_range("rejection reports the wait", blocked, 0.9, 1.15)
    # The rejection must not have eaten global/bot capacity: a different chat
    # still goes through immediately.
    check_range("capacity preserved after rejection",
                g4.acquire("tok", -9), 0.0, 0.01)

    print("\n-- 429 penalty closes the chat for retry_after --")
    g5 = SendGate(SendLimits(global_per_sec=FREE, per_bot_per_sec=FREE,
                             per_chat_per_min=6000, burst=1))
    g5.penalise(-7, 5)
    check_range("penalised chat waits ~5s", g5.acquire("tok", -7), 4.5, 5.5)
    check_range("penalty is per chat", g5.acquire("tok", -8), 0.0, 0.01)


def test_queue_and_outbox() -> None:
    print("\n-- queue ceiling --")
    from dns_scraper.channels import ChannelPlan
    from dns_scraper.models import Product
    from dns_scraper.notifier import DealNotifier, SendLimits

    plan = ChannelPlan(bases={"myntra": -1001},
                       tiers={70: -1005, 80: -1006, 90: -1007})

    def deal(pid, discount=10):
        return Product(product_id=pid, brand="B", title="T", price=100,
                       mrp=200, discount=discount, source="myntra",
                       url="https://www.myntra.com/%s" % pid)

    with tempfile.TemporaryDirectory() as tmpdir:
        note = DealNotifier([], plan, "",
                            limits=SendLimits(max_queue=2))
        check("first accepted", note.queue(deal("A")), True)
        check("second accepted", note.queue(deal("B")), True)
        check("third dropped at ceiling", note.queue(deal("C")), False)
        check("dropped is counted", note.stats()["dropped"], 1)
        check("queue holds the accepted two", note.pending, 2)

        print("\n-- shutdown outbox --")
        outbox = Path(tmpdir) / "outbox.jsonl"
        written = note.persist(outbox)
        check("persisted pending", written, 2)
        check("outbox file exists", outbox.exists(), True)
        check("queue drained to disk", note.pending, 2)   # persist is a copy

        fresh = DealNotifier([], plan, "",
                             limits=SendLimits(max_queue=10))
        restored = fresh.restore(outbox)
        check("restored on next boot", restored, 2)
        check("restored into the queue", fresh.pending, 2)
        check("outbox consumed", outbox.exists(), False)

        # Persisting an empty queue clears the file rather than leaving a stale
        # one behind to be replayed forever.
        empty = DealNotifier([], plan, "")
        check("persist with empty queue is a no-op", empty.persist(outbox), 0)


# --------------------------------------------------------------------------
# Empty-DB baseline (needs a real app)
# --------------------------------------------------------------------------
def test_baseline() -> None:
    print("\n-- empty-DB baseline --")
    from dns_scraper.models import Product

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ.update({
            "BOT_TOKENS": "123456:AA-fake-token-for-tests",
            "ADMIN_CHAT_ID": "42",
            "CHANNEL_BASE_DEFAULT": "-1001",
            "CHANNEL_TIER_70": "-1002",
            "CHANNEL_TIER_80": "-1003",
            "CHANNEL_TIER_90": "-1004",
            "DATA_DIR": tmpdir,
            "SOURCE_CROMA_ENABLED": "false",
            "SOURCE_BLINKIT_ENABLED": "false",
            "SOURCE_BIGBASKET_ENABLED": "false",
            "BASELINE_ON_EMPTY": "true",
        })
        from dns_scraper.app import ScraperApp
        from dns_scraper.config import Settings

        settings = Settings.load()
        app = ScraperApp(settings)
        app.notifier.stop()          # no real sends; counts must be deterministic

        real_seen = app._by_name["myntra"].seen()
        batch = [
            Product(product_id="P1", brand="Titan", title="Watch", price=1000,
                    mrp=5000, discount=80, source="myntra",
                    url="https://www.myntra.com/P1"),
            Product(product_id="P2", brand="Nike", title="Shoes", price=2000,
                    mrp=4000, discount=50, source="myntra",
                    url="https://www.myntra.com/P2"),
        ]
        fake = FakeSource(app, "myntra", batch, real_seen)
        app.sources = [fake]
        app._by_name = {"myntra": fake}
        app._deal_buttons._seen = {"myntra": real_seen}

        # Cycle 1 over an empty DB: record everything, post nothing.
        res = asyncio.run(app.run_source(fake))
        check("baseline posts nothing", res["queued"], 0)
        check("baseline found the products", res["found"], 2)
        check("baseline recorded them", real_seen.stats()["tracked"], 2)
        check("baseline marked as laid down", "myntra" in app._baselined, True)

        # Cycle 2, nothing changed: still nothing posted.
        res = asyncio.run(app.run_source(fake))
        check("second cycle quiet", res["queued"], 0)

        # A genuinely new deal now posts.
        batch.append(Product(product_id="P3", brand="Adidas", title="Tee",
                             price=300, mrp=3000, discount=90, source="myntra",
                             url="https://www.myntra.com/P3"))
        res = asyncio.run(app.run_source(fake))
        check("new deal after baseline posts", res["queued"], 1)

        # Regression: /clear_all sent while the bot is already running. The next
        # cycle must re-baseline and post nothing — the baseline signal is the
        # store's emptiness, not a per-boot flag. With a per-boot flag this run
        # would re-announce the whole catalogue (19k messages in the field).
        check("clear_all wiped the store", real_seen.clear(), 3)
        res = asyncio.run(app.run_source(fake))
        check("cycle after a mid-run clear posts nothing", res["queued"], 0)
        check("cycle after a mid-run clear re-recorded it",
              real_seen.stats()["tracked"], 3)
        res = asyncio.run(app.run_source(fake))
        check("cycle after the re-baseline is back to quiet diffing",
              res["queued"], 0)

        # main.py and run_forever() both call shutdown(); the second call must be
        # a no-op, or the queue is drained and the outbox written twice.
        asyncio.run(app.shutdown())
        check("shutdown ran once", app._shutdown_done, True)
        asyncio.run(app.shutdown())          # must not raise or repeat work
        check("shutdown is idempotent", app._shutdown_done, True)

        app.notifier.stop()
        for source in app.sources:
            try:
                source.seen().close()
            except Exception:
                pass
        # The other sources' stores were opened by the deal-button wiring.
        for name in ("croma", "blinkit", "bigbasket"):
            src = app.source(name)
            if src is not None:
                try:
                    src.seen().close()
                except Exception:
                    pass


class FakeSource:
    """Minimal SourceAdapter stand-in that reuses the app's real SeenStore."""

    def __init__(self, app, name, products, seen_store):
        from dns_scraper.sources import SourceSpec

        self.name = name
        self.spec = SourceSpec(
            name=name, label=name.title(),
            links_file=Path(app.settings.data_dir) / ("%s_links.txt" % name),
            seen_db=Path(app.settings.data_dir) / ("%s_seen.db" % name),
            default_location="387001")
        self._seen = seen_store
        self._products = products
        self._links = type("L", (), {"all": lambda s: ["u"], "reload": lambda s: ["u"],
                                     "__len__": lambda s: 1})()
        self.health = type("H", (), {
            "is_open": False, "last_run": 0.0, "runs": 1,
            "cooldown_remaining": lambda s: 0,
            "record_run_start": lambda s: None,
            "record_run_end": lambda s, d, i: None,
            "record_success": lambda s: None,
            "record_failure": lambda s, exc="", status=None: None,
            "snapshot": lambda s: {"runs": 1, "items": 0, "open": False,
                                   "cooldown": 0, "last_duration": 0.0,
                                   "last_error": "", "empty_runs": 0,
                                   "run_failures": 0},
        })()

    def links(self):
        return self._links

    def seen(self):
        return self._seen

    def location_for(self, product):
        return self.spec.default_location

    async def scrape_all(self, urls):
        return list(self._products)

    def scraper_stats(self):
        return {}

    def stats(self):
        out = dict(self.scraper_stats())
        out.update(self.health.snapshot())
        return out


def test_source_overrides() -> None:
    print("\n-- per-source env overrides --")
    from dns_scraper.config import _parse_source_overrides

    parsed = _parse_source_overrides({
        "SOURCE_MYNTRA_RPS": "3.5",
        "SOURCE_MYNTRA_MAX_PAGES": "2",
        "SOURCE_MYNTRA_MIN_DISCOUNT_NEW": "0",
        "SOURCE_MYNTRA_MIN_DISCOUNT_EXISTING": "20",
        "SOURCE_MYNTRA_IMPERSONATE": "chrome120",
        "SOURCE_CROMA_CHANNEL_CODE": "400049",
        "SOURCE_BLINKIT_ENABLED": "false",
        "SOURCE_BLINKIT_WARM_COOKIES": "false",
        "SOURCE_BIGBASKET_INTERVAL": "1800",
        "SOURCE_BOGUS_NOTAFIELD": "1",
        "UNRELATED_VAR": "x",
    })
    check("override float rps", parsed["myntra"]["rps"], 3.5)
    check("override max_pages", parsed["myntra"]["max_pages"], 2)
    # These two names contain an underscore, which a naive split mis-reads.
    check("override min_discount_new", parsed["myntra"]["min_discount_new"], 0)
    check("override min_discount_existing", parsed["myntra"]["min_discount_existing"], 20)
    check("override text impersonate", parsed["myntra"]["impersonate"], "chrome120")
    check("override text channel_code",
          parsed["croma"]["channel_code"], "400049")
    check("override enabled bool", parsed["blinkit"]["enabled"], False)
    check("override warm_cookies bool",
          parsed["blinkit"]["warm_cookies"], False)
    check("override interval float", parsed["bigbasket"]["interval"], 1800.0)
    # A mis-split on the last underscore would invent sources like "blinkit_warm".
    check("source keys are exactly the four sources",
          sorted(parsed), ["bigbasket", "blinkit", "croma", "myntra"])
    check("unknown field ignored", "bogus" in parsed, False)
    check("unrelated vars ignored", len(parsed), 4)
    check("unparseable value ignored",
          _parse_source_overrides({"SOURCE_MYNTRA_RPS": "fast"}), {})

    # And they must actually reach the spec the scraper is built from.
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ.update({
            "BOT_TOKENS": "123456:AA-fake-token-for-tests",
            "ADMIN_CHAT_ID": "42",
            "CHANNEL_BASE_DEFAULT": "-1001",
            "DATA_DIR": tmpdir,
            "SOURCE_CROMA_ENABLED": "false",
            "SOURCE_BLINKIT_ENABLED": "false",
            "SOURCE_BIGBASKET_ENABLED": "false",
            "SOURCE_MYNTRA_MAX_PAGES": "2",
            "SOURCE_MYNTRA_RPS": "1.5",
        })
        from dns_scraper.config import Settings
        from dns_scraper.sources import build_sources

        settings = Settings.load()
        sources = build_sources(settings)
        spec = sources[0].spec
        check("override reached the spec (pages)", spec.max_pages, 2)
        check("override reached the spec (rps)", spec.rps, 1.5)
        check("site default lives on the spec, not Settings",
              spec.impersonate, "chrome131_android")
        check("the plan is read from the env",
              settings.channel_plan.base_for("myntra"), -1001)


def main() -> int:
    test_parsers()
    test_api_modules()
    test_locations()
    test_source_overrides()
    test_routing()
    test_send_gate()
    test_queue_and_outbox()
    test_baseline()

    print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES",
                                    len(FAILS), "" if len(FAILS) == 1 else "s"))
    for failure in FAILS:
        print("  - " + failure)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
