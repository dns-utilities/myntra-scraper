# dns-scraper — Myntra + Croma + Blinkit + BigBasket Telegram deal bot

Clean rebuild of the deals scraper (with a full Telegram control bot), now
running **four sources as siblings in one deployment**: Myntra and Croma
priced nationally, Blinkit and BigBasket priced *per delivery point*. One
service, zero hardcoded secrets. All sources share the same bot tokens, deal
channels, up-only duplicate filter, spam/blacklist, and notifier — and the
notifier paces every send to Telegram's real limits, so a burst (or a cleared
database) drains steadily instead of flooding a channel.

## Architecture

```text
dns-scraper/
├── main.py                      # entry point — wires config + app + shutdown
├── requirements.txt
├── .env.example                 # copy to .env, fill secrets
├── railway.json / .python-version
├── docs/
│   ├── SCRAPING-GUIDELINES.md   # politeness, circuit breakers, retention, why
│   └── FUTURE-SOURCES.md        # how to add a source + Zepto/Instamart notes
├── config/
│   ├── links.txt                # Myntra seed URLs (copied to DATA_DIR on first boot)
│   ├── croma_links.txt          # Croma seed category URLs (same seeding)
│   ├── blinkit_links.txt        # Blinkit seed search terms (grocery)
│   ├── bigbasket_links.txt      # BigBasket seed category slugs + searches
│   ├── blinkit_locations.txt    # Blinkit delivery points (lat|lon, or pin:)
│   ├── bigbasket_locations.txt  # BigBasket delivery points (ec|sa|city, or pin:)
│   └── auto_clear_brands.json   # 617 evidenced brands (generated; see scripts/)
└── src/dns_scraper/
    ├── __init__.py
    ├── config.py                # Settings dataclass — ALL secrets/env in one place
    ├── channels.py              # ChannelPlan — base-vs-tier routing value object
    ├── logging_setup.py         # logging factory (no global side effects)
    ├── models.py                # Product + DiscountTier + per-source parsers
    ├── token_manager.py         # Myntra /auth/v1/token (STABLE device + cookies)
    ├── search_api.py            # Myntra search URL parsing + request building
    ├── scraper.py               # Myntra API client (async bulk scrape)
    ├── croma_api.py             # Croma category URL parsing + request building
    ├── croma_scraper.py         # Croma API client (async bulk scrape)
    ├── croma_stores.py          # CromaStoreMapper — SA-XXXX → store name/city
    ├── blinkit_api.py           # Blinkit layout/search request building + cards
    ├── blinkit_scraper.py       # Blinkit client (per-point cookies, 429/403 gate)
    ├── bigbasket_api.py         # BigBasket listing request building + products
    ├── bigbasket_scraper.py     # BigBasket client (cookie TTL, 429/403 gate)
    ├── locations.py             # delivery points + pincode cache/geocoding
    ├── sources.py               # SourceAdapter / SourceSpec / SourceHealth / limiter
    ├── brand_registry.py        # auto-clear brand matching (aliases, longest-first)
    ├── deal_buttons.py          # inline keyboard, message parser, brand-prompt flow
    ├── stores.py                # LinkStore / SeenStore / BlacklistStore (SQLite)
    ├── notifier.py              # multi-bot sender + 3-bucket send gate + outbox
    ├── controller_bot.py        # control bot: /start /stop /status /diag + pollers
    └── app.py                   # ScraperApp — per-source scheduler + pipeline
scripts/
    ├── build_auto_clear_brands.py  # regenerate the auto-clear list from the registry
    ├── smoke_test.py               # offline unit checks (tiers, seen, blacklist, buttons)
    ├── integration_test.py         # fake source through the real pipeline
    ├── poller_test.py              # per-bot Telegram cursor isolation
    ├── migration_check.py          # seen-DB schema migration from v1
    ├── button_test.py              # simulated Telegram callback round trip
    └── qcommerce_test.py           # Blinkit/BigBasket parsers, routing, send gate
```

### Naming conventions used
| Element | Convention | Example |
|---|---|---|
| Packages / modules / files | `snake_case` | `controller_bot.py`, `token_manager.py` |
| Classes | `PascalCase` | `ScraperApp`, `ControllerBot`, `CromaScraper` |
| Functions / methods / vars | `snake_case` | `run_cycle()`, `should_notify()` |
| Constants | `UPPER_SNAKE_CASE` | `DEFAULT_CYCLE_DELAY` |
| Env vars | `UPPER_SNAKE_CASE` | `BOT_TOKENS`, `DATA_DIR` |

## Telegram control commands

Sent to the **control bot** (token #1) in the admin chat:

| Command | Effect |
|---|---|
| `/start` | start the scrape loop |
| `/stop` | stop the scrape loop (sender keeps draining) |
| `/status` | per-source cycles, uptime, queue, tracked, circuit state, 403/429 split |
| `/diag [source]` | live probe + per-source health, request/error/empty counters |
| `/run` | force one pass over every source now |
| `/add_link <url>` | add a Myntra / Croma / Blinkit / BigBasket URL (auto-detected by domain, persists to volume) |
| `/add_link <source>:<term>` | add a bare search term, e.g. `/add_link blinkit:atta` |
| `/remove_link <url-or-index>` | remove by URL or `LIST#` index |
| `/list_links` | show tracked URLs/terms, grouped per source |
| `/add_location <source> <line>` | add a delivery point, e.g. `/add_location blinkit pin:132103\|Panipat` |
| `/list_locations` | delivery points each location-scoped source is priced at |
| `/croma_stores` | refresh the Croma store-name map (for `/stock`) |
| `/stock <croma_product_id>` | which Croma stores carry it (resolves SA-XXXX codes) |
| `/add_blacklist <brand> <min_discount>` | e.g. `/add_blacklist Nike 60` — skip Nike below 60% |
| `/remove_blacklist <brand>` | lift a brand rule |
| `/list_blacklist` | show brand rules |
| `/blacklist_product <product_id> <price>` | block one product at one price |
| `/unblock_product <product_id> <price>` | lift that priced block |
| `/blacklist_products` | list priced product blocks |
| `/clear_product <product_id>` | forget a product (will re-alert as new) |
| `/clear_all` | wipe seen DB |
| `/autoclear` | auto-clear config + eligible brand count |
| `/autoclear_list` | show auto-clear brands |
| `/autoclear_add <brand>` / `/autoclear_remove <brand>` | edit the list (persists) |
| `/verify_bots` | probe every bot × every channel, print a pass/fail matrix |
| `/reset_bots` | clear retired (bot, chat) pairings after promoting a bot |
| `/prune` | force the seen-DB retention sweep |
| `/affiliate` / `/set_affiliate` / `/clear_affiliate` | link tag |
| `/help` | command list |

Every deal message also carries two buttons:

| Button | Behaviour |
|---|---|
| 🚫 **Blacklist Product** | one tap; blocks that `(source, product_id, price)` only. A later price drop on the same product still alerts. Comes with an ↩️ Undo button. |
| 🏷️ **Blacklist Brand** | prompts for a percentage, then confirms with ✅/❌. Brand is resolved from the stored brand field, falling back to the message headline, then the first word of the title — so you see what will be blocked before you commit. |

## Message format

```
🚨 80% Off - Titan Raga Watch For Women

💰 Price: ₹1299 (MRP: ₹6495)

https://www.myntra.com/ABCDEF1234

App: Myntra
```

Discount badge first, then title, then price/MRP, then link, then the source
as a plain `App:` line at the bottom (with the location in parentheses for
location-scoped sources). No source branding in the headline.

Blinkit and BigBasket deals name the delivery point they were priced at:

```
🚨 80% Off - Raga Watch For Women

💰 Price: ₹1299 (MRP: ₹6495)

https://blinkit.com/prn/x/prid/98765

App: Blinkit (Panipat)
```
## Tier channels

Every deal posts to **exactly one** channel family. Tier deals never also land
in a base channel — an 85% deal appearing in both D80 and Myntra-base is what
makes a base feed feel spammy.

| Discount | Channel |
|---|---|
| `< 70%` | the deal's own base: `CHANNEL_BASE_MYNTRA` / `CHANNEL_BASE_CROMA` / `CHANNEL_BASE_BLINKIT` / `CHANNEL_BASE_BIGBASKET` (falls back to `CHANNEL_BASE_DEFAULT`) |
| 70–79% | `CHANNEL_TIER_70` (shared by all sources) |
| 80–89% | `CHANNEL_TIER_80` (shared by all sources) |
| 90%+ | `CHANNEL_TIER_90` (shared by all sources) |

So a base channel carries everything *below* 70% for that one source, and the
tier channels stay combined across all sources. `/status` prints the live
routing map.

Configuring **only the bases** is already a complete setup: a band with no
channel of its own falls back to the dealing source's own base, so you never
lose a 95% deal to a tier variable you forgot to set. `CHANNEL_BASE_DEFAULT` is
therefore only needed for a source with no base at all — or for a one-channel
deployment, where every base and band inherits it.

Badges in the headline: 95+ 💎✨ · 90+ 🔥 · 80+ 🚨.

## Hyper-local sources (Blinkit, BigBasket)

Unlike Myntra/Croma, these price per **delivery point**, not nationally: the
same SKU at two dark stores is two different observations, so `location` is
part of the seen-key. Each source has a list of points:

* **Blinkit** — a coordinate. Write `lat | lon | name`, or `pin:132103 | Panipat`
  and it is geocoded once and cached in `data/pincode_cache.json`.
* **BigBasket** — a service-area triple, `ec_id | sa_id | city_id | name`.
  There is no reliable public pincode → service-area lookup, so `pin:` lines
  are **not** auto-resolved for BigBasket; the file header shows exactly how to
  read the three ids from DevTools, and a hand-written `pincode_cache.json`
  entry makes a `pin:` line work.

Both files are seeded from `config/` on first boot and are live-editable with
`/add_location` (no restart; the file is re-read and the point list reloaded).

A WAF 403 is treated as a block, not a flake: after a few refusals in a row the
run is **abandoned** rather than finishing the sweep, and the per-source circuit
breaker parks the source. `/status` shows the `403=` count and an
`aborted=` line, because feeding such a block only deepens it.

Q-commerce discounts are shallow (mostly 10–35%), so a grocery base feed only
makes sense with the empty-DB baseline below, or `SOURCE_BLINKIT_MIN_DISCOUNT_NEW=0`
for a targeted single-store test.

## Send pacing (the anti-flood gate)

The bot posts through a three-bucket gate — global, per-bot, and **per-chat** —
because Telegram's per-*chat* ceiling (~20 messages/minute) is the one that
actually bites, whatever the overall throughput is. Defaults:

| Setting | Default | Why |
|---|---|---|
| `SEND_GLOBAL_PER_SEC` | `20` | Telegram's overall tolerance |
| `SEND_PER_BOT_PER_SEC` | `15` | headroom under one bot's share |
| `SEND_PER_CHAT_PER_MIN` | `18` | just under Telegram's ~20/min per channel |
| `SEND_MAX_QUEUE` | `5000` | memory ceiling; overflow is dropped and counted, not buffered forever |

A burst therefore drains at Telegram's pace, never faster. If the gate says wait,
the worker rotates to another message instead of sleeping — so one busy channel
cannot stall the others. A `429 retry_after` penalises that chat for exactly the
duration Telegram asked for.

Nothing is lost across a restart: unsent messages are written to
`data/outbox.jsonl` on shutdown and re-queued on the next boot. This matters
because the pipeline marks a product seen *before* it is sent — without the
outbox, a restart mid-drain would drop those deals permanently.

## Empty-DB baseline

With `BASELINE_ON_EMPTY=true` (default), a source whose seen-DB is empty records
the current catalogue state and **posts nothing** on that first cycle. Only
changes from the next cycle onwards are announced.

That is what makes "clear the database and redeploy" safe: without it, an empty
DB means every qualifying product is *new* at once, and the first cycle is a
wall of messages. The baseline is per source, so adding a source never re-posts
the others.

The trigger is the seen-DB's **emptiness at run time**, not a flag set once per
boot. So `/clear_all` while the bot is already running is safe too: the next
cycle re-baselines instead of re-announcing the catalogue.

## Multiple bots

`BOT_TOKENS` takes several comma/newline-separated tokens. Token #1 is the
control bot; the rest are extra senders, and the notifier round-robins deal
posts across all of them so one bot's rate limit is not the ceiling.

Each bot gets **its own long-poll thread**, for two reasons:

1. A button callback is delivered to whichever bot *posted* the message, so
   every token must be listening or those buttons do nothing.
2. Telegram's `update_id` sequence is **per bot**. Sharing one cursor across
   poller threads makes every bot but the busiest silently skip its own
   updates — commands sent to those bots just never arrive. (This was a real
   bug; see `scripts/poller_test.py`.)

Every bot must be an **admin with post permission in every channel** it may
post to. Telegram does not let a bot add another bot to a group, so that part
is manual — but `/verify_bots` probes every (bot, channel) pairing and prints a
matrix, so the manual work is verifiable instead of hopeful. `/reset_bots`
clears retired pairings after you promote a bot.

The notifier also self-heals: if a (bot, chat) pairing starts failing with
403/400, that pairing is retired and another bot takes over the message, and
`/status` shows `blocked-pairings`.

## Auto-clear

Long-running deals used to post once and then go silent, because the up-only
duplicate filter suppressed every repeat at the same price. Auto-clear gives
*worthy* deals a second life:

* **Eligible** when the brand is in the curated registry **with real deal
  evidence** (`mentions > 0` — 617 of the 653 curated brands) **and** the
  discount is at least `AUTO_CLEAR_MIN_DISCOUNT` (default 50%).
* Once `AUTO_CLEAR_HOURS` (default 24 h) plus a deterministic per-product
  offset of up to `AUTO_CLEAR_JITTER_HOURS` (default 6 h) has passed since the
  last post, the deal re-posts even at an identical price, then the cooldown
  restarts.
* **Capped at `AUTO_CLEAR_MAX_PER_HOUR` re-posts per hour** (default 25).
  "Evidenced brand at 50%+" matches hundreds of live products at once, so
  without a cap a restart drains the whole backlog into the channel in one
  cycle. With the cap, a backlog drains at a steady rate.
* Everything else keeps the strict up-only behaviour.

The brand list is generated from `research/Brands Analysis` by
`scripts/build_auto_clear_brands.py`, shipped as `config/auto_clear_brands.json`,
copied to the volume on first boot, and live-editable with
`/autoclear_add` + `/autoclear_remove` (which persist across redeploys).

Tune the volume if the channels feel busy: `AUTO_CLEAR_MAX_PER_HOUR=0`
disables re-posting entirely while keeping eligibility tracking,
`AUTO_CLEAR_MIN_DISCOUNT=70` restricts cycling to the deepest deals only.

A product blocked with 🚫 Blacklist Product never auto-clears, at any discount
— that is the point of the priced block.

## Blacklist semantics

Two independent rule kinds, both in `blacklist.db`:

**Brand rules** — `/add_blacklist <brand> <percent>` means *skip this brand
unless the deal is at least `<percent>`% off*. Match is a case-insensitive
substring on `brand + title`.

- `/add_blacklist nike 70` → Nike at 65% skipped, at 75% sent
- `/add_blacklist priority 90` → Priority spam filtered unless 90%+

**Product rules** — `/blacklist_product <product_id> <price>` (or the button)
means *never post this product at this exact price*. Deliberately priced:
blocking a 20% fridge must not silence the same fridge at 80%, and must not
survive a genuine price drop into range.

## Environment variables (ALL secrets here — nothing hardcoded)

| Key | Required | Default | Notes |
|---|---|---|---|
| `BOT_TOKENS` | **yes** | — | comma/newline-separated bot tokens, token #1 = control bot |
| `ADMIN_CHAT_ID` | **yes** | — | your Telegram user/chat id — only this id may control |
| `CHANNEL_BASE_DEFAULT` | no\* | — | fallback base feed — \*set this (or any base/tier), else startup fails |
| `CHANNEL_BASE_MYNTRA` | no | `CHANNEL_BASE_DEFAULT` | Myntra base feed (below 70%) |
| `CHANNEL_BASE_CROMA` | no | `CHANNEL_BASE_DEFAULT` | Croma base feed (below 70%) |
| `CHANNEL_BASE_BLINKIT` | no | `CHANNEL_BASE_DEFAULT` | Blinkit base feed (below 70%) |
| `CHANNEL_BASE_BIGBASKET` | no | `CHANNEL_BASE_DEFAULT` | BigBasket base feed (below 70%) |
| `CHANNEL_TIER_70` | no | source's own base | 70–79% deals (shared by all sources) |
| `CHANNEL_TIER_80` | no | source's own base | 80–89% deals (shared by all sources) |
| `CHANNEL_TIER_90` | no | source's own base | 90%+ deals (shared by all sources) |
| `SEND_GLOBAL_PER_SEC` | no | `20` | overall send ceiling, all bots + chats |
| `SEND_PER_BOT_PER_SEC` | no | `15` | per-bot send ceiling |
| `SEND_PER_CHAT_PER_MIN` | no | `18` | per-channel ceiling (Telegram's is ~20) |
| `SEND_MAX_QUEUE` | no | `5000` | queue must never grow without bound |
| `BASELINE_ON_EMPTY` | no | `true` | a cleared DB records silently, never floods |
| `SOURCE_BLINKIT_*` | no | spec default | `ENABLED=true`, `RPS=3`, `IMPERSONATE=chrome124`, `PAGE_LIMIT=15`, `MAX_PAGES=6`, `CONCURRENCY=5`, `COOLDOWN=20`, `WARM_COOKIES=true` |
| `SOURCE_BIGBASKET_*` | no | spec default | `ENABLED=true`, `RPS=4`, `IMPERSONATE=chrome`, `MAX_PAGES=5`, `CONCURRENCY=8`, `COOLDOWN=25`, `COOKIE_TTL=600` |
| `GEOCODE_ENABLED` | no | `true` | resolve Blinkit `pin:` lines via a public geocoder |
| `GEOCODE_URL` | no | Nominatim | geocoder endpoint (results cached on the volume) |
| `AUTO_CLEAR_ENABLED` | no | `true` | re-post eligible deals on a cooldown |
| `AUTO_CLEAR_HOURS` | no | `24` | cooldown before an eligible deal may re-post |
| `AUTO_CLEAR_MAX_PER_HOUR` | no | `25` | hard cap on auto-clear re-posts per hour |
| `AUTO_CLEAR_JITTER_HOURS` | no | `6` | per-product cooldown spread, so re-posts don't cluster |
| `AUTO_CLEAR_MIN_DISCOUNT` | no | `50` | min discount to be an auto-clear candidate |
| `SEEN_RETENTION_DAYS` | no | `45` | prune seen rows not notified within this |
| `PRUNE_INTERVAL_HOURS` | no | `24` | how often the prune sweep runs |
| `SOURCE_<NAME>_RPS` | no | spec default | per-source request budget (polite pacing) |
| `SOURCE_<NAME>_INTERVAL` | no | `0` | seconds between runs; `0` = every tick |
| `SOURCE_<NAME>_ENABLED` | no | `true` | switch one source off without touching the rest |
| `SOURCE_<NAME>_IMPERSONATE` | no | spec default | curl_cffi TLS profile for that source |
| `SOURCE_<NAME>_MAX_PAGES` / `_CONCURRENCY` / `_MIN_DISCOUNT_*` | no | spec default | per-source overrides |
| `SOURCE_MYNTRA_*` | no | spec default | `RPS=5`, `IMPERSONATE=chrome131_android`, `MAX_PAGES=5`, `CONCURRENCY=20` |
| `SOURCE_CROMA_*` | no | spec default | `ENABLED=true`, `RPS=5`, `IMPERSONATE=chrome120`, `CHANNEL_CODE=400049`, `MAX_PAGES=10`, `CONCURRENCY=6` |
| `DATA_DIR` | no | `./data` | `/data` on Railway volume |
| `PINCODE` | no | `387001` | delivery pincode (Myntra) |
| `CYCLE_DELAY` | no | `20` | seconds between cycles |
| `SCRAPE_CONCURRENCY` | no | `20` | parallel requests |
| `MAX_PAGES_PER_URL` | no | `5` | pages per URL |
| `MIN_DISCOUNT_NEW` | no | `0` | min % for unseen products |
| `MIN_DISCOUNT_EXISTING` | no | `15` | min % for seen products |
| `MIN_CHANGE_PERCENT` | no | `1` | re-alert only if discount rose by this |
| `MAX_CYCLES` | no | `0` | `0` = forever, `1` = single test cycle |
| `PORT` | no | — | Railway injects; health server answers `OK` |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` shows per-request 403/429 detail |

Get your Telegram id: message `@userinfobot`, copy the number.

## Running unattended

The bot is designed to be left alone for weeks. See
[`docs/SCRAPING-GUIDELINES.md`](docs/SCRAPING-GUIDELINES.md) for the full
reasoning; the short version:

* **Per-source politeness** — concurrency cap *and* a token-bucket request
  budget per source, because a burst at one host is what gets an IP throttled.
* **Per-source circuit breaker** — 5 consecutive run failures (or 3 empty
  runs) parks that source with exponential backoff, so one broken site cannot
  take the feed down or burn the shared egress IP.
* **Silent zeros are treated as failures** — the original Myntra 403 bug
  produced "0 products" with no error; empty runs now trip the breaker and
  alert the admin (rate-limited to one alert per source per 30 min).
* **Deal re-posts are budgeted** — anything that can loop forever (like
  auto-clear recycling a long-running deal) needs a hard per-hour cap and
  jittered spacing, not just a cooldown. A cooldown alone is satisfied by
  hundreds of products simultaneously the moment the process restarts.
* **Retention** — the seen DB is pruned so a permanent deployment does not grow
  without bound on a finite volume.
* **Sends are paced, not batched** — the three-bucket send gate caps global,
  per-bot and per-channel throughput, so a large batch (or a cleared database)
  cannot arrive as a flood. Unsent messages survive a restart via the outbox.
* **A cleared DB posts nothing on the first cycle** — the per-source baseline
  records state silently, so wiping the database is a safe operation.
* **Everything is env-tunable** — a site that starts 429-ing can be slowed down
  without a redeploy.

Adding another source is a new adapter plus env vars; see
[`docs/FUTURE-SOURCES.md`](docs/FUTURE-SOURCES.md).

## Tests

```powershell
python scripts/smoke_test.py        # tiers, seen store, blacklist, parsing
python scripts/integration_test.py  # fake source through the real pipeline
python scripts/button_test.py      # simulated Telegram button round trip
python scripts/poller_test.py      # per-bot Telegram cursor isolation
python scripts/migration_check.py  # seen-DB schema migration from v1
python scripts/qcommerce_test.py   # Blinkit/BigBasket parsers, routing, send gate
```

All six run offline — no network, no Telegram.

## 403 fix (Oct 2026 — verified live: search HTTP 200, 287k products)

The continuous-403 had FOUR compounding causes, all fixed:

1. **Rotating device fingerprint** (`token_manager.py`): every token fetch
   cycled `device_id / install_id / client_id` across 3 profiles. Myntra
   binds the `at` token to the device — the next search (with a DIFFERENT
   device) failed the WAF check. **Fix:** one stable `DEVICE`, one stable
   `sessionID` for the process lifetime.
2. **Fresh cookie jar per search batch** (`scraper.py`): the token call set
   cookies (`ak_bmsc` etc.) on the sync session, but searches ran on a NEW
   `AsyncSession` with zero cookies — different client => 403.
   **Fix:** `cookies_snapshot()` seeds the async session.
3. **TLS-vs-UA mismatch**: impersonation `chrome110` (desktop TLS) with an
   Android UA string. **Fix:** default `chrome131_android` (env-overridable).
4. **Silent failure**: 403 did `invalidate() + break` with no log, no counter,
   no retry — `Found 0 products` with zero explanation. **Fix:** warn-log with
   status+body, `forbidden`/`rate_limited` counters in `/status`, one forced
   token refresh + same-page retry per URL, gentler pacing (≤8 concurrent,
   batched + jittered).

Also fixed: `controller_bot._post` was indented into `stop()` (so
`reply()`/`verify()` would `AttributeError`); now a real method. Added
`/diag` live probe bot command.

If 403s return later, it is IP-rate-limiting (Railway egress), not headers:
`/diag` distinguishes — 200 = fine, persistent 403 after 10-min pause =
rotate egress IP or lower `SCRAPE_CONCURRENCY`.

## Run locally

```powershell
cd C:\Users\bhati\OneDrive\Desktop\DnS\dns-utilities\dns-scraper
copy .env.example .env   # fill BOT_TOKENS, ADMIN_CHAT_ID, and the CHANNEL_BASE_* / CHANNEL_TIER_* ids
pip install -r requirements.txt
python main.py
# single test cycle:
$env:MAX_CYCLES=1; python main.py
```

## Deploy on Railway — steps

1. **Push to GitHub** (fresh repo, e.g. `dns-utilities/dns-scraper`):
   ```powershell
   cd C:\Users\bhati\OneDrive\Desktop\DnS\dns-utilities\dns-scraper
   git init; git add -A; git commit -m "dns-scraper: clean rebuild with control bot"
   git remote add origin https://github.com/YOUR-ORG/dns-scraper.git
   git branch -M main; git push -u origin main
   ```
   `.env`, `data/`, `*.db*` are gitignored — secrets never committed.
2. **Railway → New Project → Deploy from GitHub** → pick the repo.
3. **Settings → Deploy**: Start `python main.py`, Restart `ON_FAILURE` ×10, Healthcheck OFF (worker; built-in server answers `$PORT` anyway).
4. **Volumes → New Volume**, mount `/data`. **Variables** → `DATA_DIR=/data`.
5. **Variables** — set `BOT_TOKENS`, `ADMIN_CHAT_ID`, and the
   `CHANNEL_BASE_*` / `CHANNEL_TIER_*` ids (per-source bases plus the shared
   70/80/90 tiers). Get tokens from your old `bot_tokens_for_railway.txt`.
6. **Redeploy** → logs should show `control bot polling as @...`, `Loaded N urls`,
   `CYCLE #1`. Send `/status` to the control bot → it replies.
7. **Clearing the database**: delete `*.db` (and, if you want a hard reset,
   `outbox.jsonl`) from the volume and redeploy. The per-source baseline records
   the first cycle silently, so the cleared DB does not flood the channels.
8. Seed links: repo `config/*.txt` auto-copy to `/data/` on first boot; afterwards
   manage with `/add_link` and `/add_location` live (they persist on the volume).

## Cost note

24/7 worker ≈ $5–15/month usage pricing. Trial $5 credit covers testing.
