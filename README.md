# Myntra + Croma Scraper — Telegram-controlled deal bot

Clean rebuild of the Myntra deals scraper (with a full Telegram control bot),
now running **both Myntra and Croma** as sibling sources in one deployment.
One service, zero hardcoded secrets. Both sources share the same bot tokens,
deal channels, up-only duplicate filter, spam/blacklist, and notifier.

## Architecture

```text
myntra-scraper/
├── main.py                      # entry point — wires config + app + shutdown
├── requirements.txt
├── .env.example                 # copy to .env, fill secrets
├── railway.json / .python-version
├── docs/
│   ├── SCRAPING-GUIDELINES.md   # politeness, circuit breakers, retention, why
│   └── FUTURE-SOURCES.md        # Blinkit/Zepto/Instamart plan + research
├── config/
│   ├── links.txt                # Myntra seed URLs (copied to DATA_DIR on first boot)
│   ├── croma_links.txt          # Croma seed category URLs (same seeding)
│   └── auto_clear_brands.json   # 617 evidenced brands (generated; see scripts/)
└── src/myntra_scraper/
    ├── __init__.py
    ├── config.py                # Settings dataclass — ALL secrets/env in one place
    ├── logging_setup.py         # logging factory (no global side effects)
    ├── models.py                # Product dataclass + DiscountTier (myntra | croma)
    ├── token_manager.py         # Myntra /auth/v1/token (STABLE device + cookies)
    ├── search_api.py            # Myntra search URL parsing + request building
    ├── scraper.py               # Myntra API client (async bulk scrape)
    ├── croma_api.py             # Croma category URL parsing + request building + headers
    ├── croma_scraper.py         # Croma API client (async bulk scrape)
    ├── croma_stores.py          # CromaStoreMapper — SA-XXXX → store name/city (STOCK#)
    ├── sources.py               # SourceAdapter / SourceSpec / SourceHealth / rate limiter
    ├── brand_registry.py        # auto-clear brand matching (aliases, longest-first)
    ├── deal_buttons.py          # inline keyboard, message parser, brand-prompt flow
    ├── stores.py                # LinkStore / SeenStore / BlacklistStore (SQLite)
    ├── notifier.py              # multi-bot Telegram sender w/ rate limiting
    ├── controller_bot.py        # control bot: /start /stop /status /diag + button pollers
    └── app.py                   # ScraperApp — per-source scheduler + pipeline
scripts/
    ├── build_auto_clear_brands.py  # regenerate the auto-clear list from the registry
    ├── smoke_test.py               # offline unit checks (tiers, seen, blacklist, buttons)
    ├── integration_test.py         # fake source through the real pipeline
    └── button_test.py              # simulated Telegram callback round trip
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
| `/add_link <url>` | add a Myntra OR Croma URL (auto-detected by domain, persists to volume) |
| `/remove_link <url-or-index>` | remove by URL or `LIST#` index |
| `/list_links` | show tracked URLs (Myntra + Croma sections) |
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

## Tier channels

Bands are **non-overlapping**, so a deal posts once per channel family:

| Discount | Channels |
|---|---|
| `< 70%` | `all` |
| 70–79% | `all`, `70plus` |
| 80–89% | `all`, `80plus` |
| 90%+ | `all`, `90plus` |

Badges in the headline: 95+ 💎✨ · 90+ 🔥 · 80+ 🚨.

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
| `CHANNEL_ALL` | **yes** | — | deal channel id (all deals) |
| `CHANNEL_70PLUS` | no | same as ALL | 70–79% deals |
| `CHANNEL_80PLUS` | no | same as ALL | 80–89% deals |
| `CHANNEL_90PLUS` | no | same as ALL | 90%+ deals |
| `AUTO_CLEAR_ENABLED` | no | `true` | re-post eligible deals on a cooldown |
| `AUTO_CLEAR_HOURS` | no | `24` | cooldown before an eligible deal may re-post |
| `AUTO_CLEAR_MAX_PER_HOUR` | no | `25` | hard cap on auto-clear re-posts per hour |
| `AUTO_CLEAR_JITTER_HOURS` | no | `6` | per-product cooldown spread, so re-posts don't cluster |
| `AUTO_CLEAR_MIN_DISCOUNT` | no | `50` | min discount to be an auto-clear candidate |
| `SEEN_RETENTION_DAYS` | no | `45` | prune seen rows not notified within this |
| `PRUNE_INTERVAL_HOURS` | no | `24` | how often the prune sweep runs |
| `SOURCE_<NAME>_RPS` | no | `5` | per-source request budget (polite pacing) |
| `SOURCE_<NAME>_INTERVAL` | no | `0` | seconds between runs; `0` = every tick |
| `SOURCE_<NAME>_MAX_PAGES` / `_CONCURRENCY` / `_MIN_DISCOUNT_*` | no | spec default | per-source overrides |
| `DATA_DIR` | no | `./data` | `/data` on Railway volume |
| `PINCODE` | no | `387001` | delivery pincode |
| `CYCLE_DELAY` | no | `20` | seconds between cycles |
| `SCRAPE_CONCURRENCY` | no | `20` | parallel requests |
| `MAX_WORKERS` | no | `10` | legacy compat |
| `MAX_PAGES_PER_URL` | no | `5` | pages per URL |
| `MIN_DISCOUNT_NEW` | no | `0` | min % for unseen products |
| `MIN_DISCOUNT_EXISTING` | no | `15` | min % for seen products |
| `MIN_CHANGE_PERCENT` | no | `1` | re-alert only if discount rose by this |
| `CURL_IMPERSONATE` | no | `chrome131_android` | curl_cffi profile — must be Android-family to match the Android UA |
| `MAX_CYCLES` | no | `0` | `0` = forever, `1` = single test cycle |
| `PORT` | no | — | Railway injects; health server answers `OK` |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` shows per-request 403/429 detail |
| `CROMA_ENABLED` | no | `true` | run the Croma scraper alongside Myntra |
| `CROMA_IMPERSONATE` | no | `chrome120` | curl_cffi profile for Chroma (desktop Chrome) |
| `CROMA_CHANNEL_CODE` | no | `400049` | Chroma store/channel code in the category API |
| `CROMA_MAX_PAGES` | no | `10` | pages per Chroma category URL |
| `CROMA_CONCURRENCY` | no | `6` | parallel Chroma category requests |

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
* **Everything is env-tunable** — a site that starts 429-ing can be slowed down
  without a redeploy.

Adding a source (Blinkit, Zepto, Instamart) is a new adapter plus env vars; see
[`docs/FUTURE-SOURCES.md`](docs/FUTURE-SOURCES.md).

## Tests

```powershell
python scripts/smoke_test.py        # tiers, seen store, blacklist, parsing
python scripts/integration_test.py  # fake source through the real pipeline
python scripts/button_test.py      # simulated Telegram button round trip
```

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
cd C:\Users\bhati\Downloads\dns-utilities\myntra-scraper
copy .env.example .env   # fill BOT_TOKENS, ADMIN_CHAT_ID, CHANNEL_ALL
pip install -r requirements.txt
python main.py
# single test cycle:
$env:MAX_CYCLES=1; python main.py
```

## Deploy on Railway — steps

1. **Push to GitHub** (fresh repo, e.g. `dns-utilities/myntra-scraper`):
   ```powershell
   cd C:\Users\bhati\Downloads\dns-utilities\myntra-scraper
   git init; git add -A; git commit -m "myntra-scraper: clean rebuild with control bot"
   git remote add origin https://github.com/YOUR-ORG/myntra-scraper.git
   git branch -M main; git push -u origin main
   ```
   `.env`, `data/`, `*.db*` are gitignored — secrets never committed.
2. **Railway → New Project → Deploy from GitHub** → pick the repo.
3. **Settings → Deploy**: Start `python main.py`, Restart `ON_FAILURE` ×10, Healthcheck OFF (worker; built-in server answers `$PORT` anyway).
4. **Volumes → New Volume**, mount `/data`. **Variables** → `DATA_DIR=/data`.
5. **Variables** — set `BOT_TOKENS`, `ADMIN_CHAT_ID`, `CHANNEL_ALL`
   (+ optionals above). Get tokens from your old `bot_tokens_for_railway.txt`.
6. **Redeploy** → logs should show `control bot polling as @...`, `Loaded N urls`,
   `CYCLE #1`. Send `/status` to the control bot → it replies.
7. Seed links: repo `config/links.txt` auto-copies to `/data/links.txt` on first
   boot; afterwards manage with `/add_link` live (persists on volume).

## Cost note

24/7 worker ≈ $5–15/month usage pricing. Trial $5 credit covers testing.
