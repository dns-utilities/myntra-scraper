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
├── Procfile / railway.json / railway.toml / nixpacks.toml / .python-version
├── config/
│   ├── links.txt                # Myntra seed URLs (copied to DATA_DIR on first boot)
│   └── croma_links.txt          # Croma seed category URLs (same seeding)
└── src/myntra_scraper/
    ├── __init__.py
    ├── config.py                # Settings dataclass — ALL secrets/env in one place
    ├── logging_setup.py         # logging factory (no global side effects)
    ├── models.py                # Product dataclass + DiscountTier enum (myntra | croma)
    ├── token_manager.py         # Myntra /auth/v1/token (STABLE device + cookies)
    ├── search_api.py            # Myntra search URL parsing + request building
    ├── scraper.py               # Myntra API client (async bulk scrape)
    ├── croma_api.py             # Croma category URL parsing + request building + headers
    ├── croma_scraper.py         # Croma API client (async bulk scrape)
    ├── croma_stores.py          # CromaStoreMapper — SA-XXXX → store name/city (STOCK#)
    ├── stores.py                # LinkStore / SeenStore / BlacklistStore (SQLite)
    ├── notifier.py              # multi-bot Telegram sender w/ rate limiting
    ├── controller_bot.py        # control bot: /start /stop /status /diag /add_link ...
    └── app.py                   # ScraperApp — scrape-loop orchestration (both sources)
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
| `/status` | cycles, uptime, queue, tracked products, blacklist size, api 403/429 split |
| `/diag` | live Myntra probe: fresh token + 1 search page, reports HTTP code |
| `/add_link <url>` | add a Myntra OR Croma URL (auto-detected by domain, persists to volume) |
| `/remove_link <url-or-index>` | remove by URL or `LIST#` index |
| `/list_links` | show tracked URLs (Myntra + Croma sections) |
| `/croma_stores` | refresh the Croma store-name map (for `/stock`) |
| `/stock <croma_product_id>` | which Croma stores carry it (resolves SA-XXXX codes) |
| `/add_blacklist <brand> <min_discount>` | e.g. `/add_blacklist Nike 60` — skip Nike below 60% |
| `/remove_blacklist <brand>` | lift a brand rule |
| `/list_blacklist` | show brand rules |
| `/clear_product <product_id>` | forget a product (will re-alert as new) |
| `/clear_all` | wipe seen database |
| `/help` | command list |

Legacy `ADD#` / `REMOVE#` / `LIST#` / `STATUS#` / `CLEAR#` / `SPAM*#` still
work in deal channels via the channel listener.

## Blacklist semantics

`/add_blacklist <brand> <percent>` means: **skip this brand unless the deal
is at least `<percent>`% off**. Brand match is case-insensitive substring
on `brand + title`. Examples:

- `/add_blacklist nike 70` → Nike at 65% skipped, at 75% sent
- `/add_blacklist priority 90` → Priority spam filtered unless 90%+
- `/remove_blacklist nike` → rule deleted

Rules persist in `blacklist.db` (SQLite, on the volume).

## Environment variables (ALL secrets here — nothing hardcoded)

| Key | Required | Default | Notes |
|---|---|---|---|
| `BOT_TOKENS` | **yes** | — | comma/newline-separated bot tokens, token #1 = control bot |
| `ADMIN_CHAT_ID` | **yes** | — | your Telegram user/chat id — only this id may control |
| `CHANNEL_ALL` | **yes** | — | deal channel id (all deals) |
| `CHANNEL_70PLUS` | no | same as ALL | 70%+ deals |
| `CHANNEL_90PLUS` | no | same as ALL | 90%+ deals |
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
