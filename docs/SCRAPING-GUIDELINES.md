# Scraping guidelines — how this bot behaves, and why

This bot runs unattended for weeks against sites that will rate-limit or block
it. The rules below are what keep it alive. They are implementation notes for
the current design, plus the reasoning, so the next source added does not
 quietly undo them.

## 1. Politeness is per-host, never global

A burst aimed at *one* site is what gets an IP throttled. 100 requests spread
over 100 sites is invisible; 100 requests at one site is an incident.

So every source owns:

| Control | Where | Default |
|---|---|---|
| Concurrency cap | `SourceSpec.concurrency` | 20 myntra / 6 croma / 5 blinkit / 8 bigbasket |
| Request budget (token bucket) | `SourceSpec.rps` → `AsyncRateLimiter` | 5 req/s myntra+croma, 3 blinkit, 4 bigbasket |
| Minimum gap between runs | `SourceSpec.interval` | 0 = every tick (`CYCLE_DELAY`) |
| Per-request timeout | `SourceSpec.timeout` | 20 s |
| Jitter between requests | both scrapers | 0.2–0.9 s randomised |

Concurrency alone is **not** enough — 20 parallel workers each finishing in
200 ms still produce a burst. The token bucket is what turns that into a
steady 5/s. Both are configurable without a code change:

```bash
SOURCE_MYNTRA_RPS=3
SOURCE_MYNTRA_INTERVAL=1800     # poll Myntra every 30 min instead of every tick
SOURCE_CROMA_CONCURRENCY=4
```

**Adaptive rule to keep in mind:** if a host's latency rises, slow down rather
than retry. Rising latency is the earliest sign of strain and it arrives before
the 429s do.

## 2. Classify failures instead of retrying everything

| Signal | Behaviour here |
|---|---|
| `200` | parse, count yield |
| `401 / 403` | do **not** hammer. Myntra: force one token refresh, retry the same page once, then stop. Croma: stop and log the body. Blinkit/BigBasket: treat it as a block — cool down, and after 3 in a row **abandon the run** rather than finishing the sweep. |
| `429` | honour `Retry-After`, back off 5–10 s, drop the page for this cycle |
| `5xx` / timeout / connection error | back off, count it, keep the source's circuit closed |
| `200` but 0 products across all links | treated as a failure, not success (see §3) |

Retry budgets are capped and always jittered, so a fleet of failing URLs does
not retry in lockstep.

The q-commerce rule is worth spelling out, because it is a deliberate
departure. A 403 from one search may be a flake; a 403 from three in a row,
across different terms, from the same egress IP, is a WAF saying no. The sweep
is 89 terms × N delivery points — a hundred-plus requests — so finishing it
against a wall deepens the block and burns time. Bailing after three, logging
once, and letting `SourceHealth` park the source is the behaviour that keeps
the IP's reputation intact. See `scripts/qcommerce_test.py` for the parser
side and `probe_live.py`-style probes for the network side.

## 3a. Any loop that can re-emit needs a budget, not just a cooldown

Auto-clear re-posts deals on a cooldown. With eligibility set to "evidenced
brand at ≥50%", **hundreds** of live products qualify simultaneously — so the
moment the process restarts after any downtime, every one of them is past its
cooldown and they all fire in the same cycle. Observed live: 722 Croma deals
posted in one run.

A cooldown alone cannot prevent that, because "time since last post" is
uniformly old for dormant rows. The fix is two-fold:

| Guard | Why |
|---|---|
| `AUTO_CLEAR_MAX_PER_HOUR` | hard cap on re-posts per rolling hour — a backlog drains at a steady rate instead of flooding |
| `AUTO_CLEAR_JITTER_HOURS` | per-product offset derived from a **stable** hash (crc32, not `hash()`, which Python randomises per process), so daily re-posts spread across the window rather than clustering |

Same reasoning applies to any future source: prefer "at most N per window" over
"once per window" for anything that fans out.

## 3b. Outbound sends need a budget too

The same argument applies to *sending*. The worst incident this bot had was not
scraping: it was ~1000 messages hitting Telegram in a few seconds — on a
refresh and again on restart. Two separate bugs:

* **No pacing.** Queueing was unbounded and the workers drained it as fast as
  the network allowed. Telegram tolerates ~30 msg/s overall and **~20/min to one
  chat**; the per-chat ceiling is the one that actually bites.
* **Restarts re-sent.** A product was marked seen *before* it was sent, so
  messages lost at shutdown were gone forever — and a naive "don't mark until
  sent" retry storm replaced them with duplicates.

The fix is three buckets plus an outbox:

| Guard | Why |
|---|---|
| global bucket (`SEND_GLOBAL_PER_SEC`) | Telegram's overall tolerance |
| per-bot bucket (`SEND_PER_BOT_PER_SEC`) | one bot's own share |
| **per-chat** bucket (`SEND_PER_CHAT_PER_MIN`) | the binding constraint, set just under 20/min |
| `SEND_MAX_QUEUE` | memory ceiling; overflow is counted and dropped, not buffered forever |
| `data/outbox.jsonl` | unsent messages persist across a restart instead of vanishing |
| 429 `retry_after` penalty | that chat is closed for exactly as long as Telegram asked |

Acquire is **all-or-nothing**: if any bucket would block, nothing is consumed and
the worker rotates to another message rather than sleeping on a queue head — so
one busy channel never stalls the others. A self-imposed pacing miss costs
~50 ms of rotation, not the second a real 429 deserves.

`BASELINE_ON_EMPTY` closes the remaining hole: clearing the database makes every
qualifying product *new* at once, which is the same flood by a different route.
The first cycle over an empty seen-DB records state and posts nothing.

## 3. A silent zero is worse than a crash

The original Myntra 403 bug produced "Found 0 products" with no error. That is
the most dangerous failure mode: you trust the empty result.

Now, per source, `SourceHealth` tracks consecutive empty runs and opens the
circuit after 3. `/status` and `/diag` show `empty_runs`, `last_error`, and
`cooldown` so a broken source is visible from Telegram in one command.

## 4. Circuit breaker, per source

`OPEN_AFTER_FAILURES = 5` consecutive run failures (or 3 empty runs) parks a
source with exponential backoff, 300 s → 3600 s cap. While parked it is
skipped entirely; the cooldown is surfaced in `/status`. One broken site cannot
take the feed down, and it cannot keep spending the shared egress IP's
reputation.

When a source fails or empties, the admin gets **one** Telegram alert per
source per 30 minutes (`ALERT_COOLDOWN_SECONDS`) — enough to notice, not enough
to become spam.

## 5. Identity and state are scoped, not global

`SeenStore` keys on `(source, location, product_id)`.

`location` is empty for national-price sources. It matters the moment a
location-scoped source is added: quick-commerce prices and stock are set per
dark store, so the same SKU at a different pincode is a *different observation*,
not a duplicate. Getting this wrong silently drops every second location's
deals.

Adding the dimension was done as a migration that adopts existing rows as the
current source's default location — so no product re-alerts just because the
key grew. If the migration fails it logs, rolls back, and keeps the legacy
schema rather than losing dedupe state.

**Retention:** `SEEN_RETENTION_DAYS=45` prunes rows not notified inside the
window, once per `PRUNE_INTERVAL_HOURS`. A permanently-running bot otherwise
grows its seen table forever, and the volume is finite. Live deals churn
constantly so they are always younger than the cutoff; dead entries stop
costing disk and query time.

## 6. Validate before you trust

`Product.from_api_item` / `from_croma_item` / `from_blinkit_item` /
`from_bigbasket_item` return `None` on malformed rows, and `should_notify`
rejects `price >= mrp` or `price <= 0` outright. Discount is recomputed from
price/MRP with the site's own `% OFF` label as a cross-check. Bad rows are
dropped at the boundary, never stored.

The q-commerce parsers also drop the rows that are technically valid but are not
deals: sponsored Blinkit placements (paid, not discounted), sold-out cards, and
BigBasket rows whose `avail_status` is not `001`. Those are the rows that
otherwise leak straight into a channel.

## 7. Observability you can check from Telegram

`/status` gives per-source links/tracked/hot, per-source request split
(`403` / `429` separated, because they mean different things), runs, items,
empty runs, circuit state and last error.

`/diag [source]` does a live probe. For a daily-running bot, the ability to ask
"is this site actually still working?" without SSH or redeploying is the
difference between a 5-minute fix and a lost week.

## 8. Ethics and terms

* Private app APIs (which most of these are) are usually outside the site's
  ToS. We stay at low volume, one location, no credential replay, and no
  attempt to defeat access controls. If a site puts up a login wall, that is
  the signal to stop, not a puzzle to solve.
* No personal data is collected — the pipeline only ever sees product rows.
* `robots.txt` is not consulted because these are JSON endpoints rather than
  HTML pages, and none of the current sources publish a relevant policy. Any
  future HTML-based source **must** honour it (RFC 9309: most specific
  `User-agent` group only, longest match wins, `Allow` beats `Disallow` on a
  tie, 4xx = no restrictions, 5xx = assume disallowed, cache ≤ 24 h).
* Prefer an official or maintained data API over DIY scraping when one exists.
  The engineering time saved usually dwarfs the subscription.

## 9. Design rules that made this easy to extend

* One `SourceAdapter` per site; the pipeline in `app.py` is source-agnostic.
* Adding a numeric source = a new adapter class + env vars. `app.py` and
  `notifier.py` never change; `controller_bot.py` needs a command only if the
  source has its own input shape (as Blinkit/BigBasket did, via
  `/add_location`).
* A hyper-local source is the same shape plus an entry in `locations.py`'s
  format table — `SeenStore` already keys on location, so nothing downstream
  learns a new concept.
* Source metadata is declarative (`SourceSpec`), not scattered through
  conditionals.
* Fetch and parse are separate: raw site shape is normalised to `Product` at
  the adapter boundary, so downstream filters never branch on the source.
* Every per-site behaviour is env-overridable, so a site that starts 429-ing
  can be slowed down without a redeploy.

## 11. Adding more bots

`BOT_TOKENS` takes several tokens. Token #1 is the control bot; the rest are
extra senders, and the notifier round-robins deal posts across all of them so
one bot's rate limit is not the ceiling.

Two facts make this less painful than it looks:

**You probably need fewer bots than you think.** Telegram allows roughly 30
messages/second per bot. With 5 channels and a trickle of deals per cycle, one
bot is nowhere near the ceiling. Extra bots buy resilience (a banned or
rate-limited bot does not stop the feed), not throughput — so 2–3 is usually
the sweet spot, not 10.

**Adding them is manual and quick.** Telegram does not allow a bot to add
another bot to a group, so nobody can script this part. In Telegram Desktop,
per bot per channel: open the channel → Administrators → Add Admin → search the
bot's @username → grant *Post Messages*. About 10 seconds each, so 10 bots × 5
groups is roughly 10 minutes of clicking.

What you do *not* have to do is guess whether it worked:

```powershell
python scripts/verify_bots.py     # from your machine, reads .env
```

…or `/verify_bots` in the control bot. Both probe every (bot, channel) pairing
with `getChatMember` and print a matrix, so a missing admin shows up as a named
cell instead of a mysteriously absent message.

Two behaviours to know about:

* Each bot gets its own long-poll thread, because a button callback is
  delivered to whichever bot posted the message, and because Telegram's
  `update_id` sequence is **per bot** — a shared cursor would make every bot
  but the busiest silently drop its own commands.
* If a (bot, chat) pairing fails with 403/400, the notifier retires that
  pairing and another bot takes the message, so one un-promoted bot cannot
  black-hole a channel. `/status` reports `blocked-pairings`, and
  `/reset_bots` clears them after you fix the permissions.

Local pacing also does not punish a multi-bot pool: a self-imposed pacing miss
costs ~50 ms, not the full second a real Telegram 429 deserves.

## 10. Config reference (new)

```bash
# --- destinations: one base feed per source + shared, exclusive tiers ---
CHANNEL_BASE_DEFAULT=      # fallback base; everything below inherits it
CHANNEL_BASE_MYNTRA=
CHANNEL_BASE_CROMA=
CHANNEL_BASE_BLINKIT=      # per-source base, below 70%
CHANNEL_BASE_BIGBASKET=
CHANNEL_TIER_70=           # 70-79%, shared by every source
CHANNEL_TIER_80=           # 80-89%
CHANNEL_TIER_90=           # 90%+ (an unset band falls back to its source's base)

# --- send pacing (the anti-flood gate) ---
SEND_GLOBAL_PER_SEC=20
SEND_PER_BOT_PER_SEC=15
SEND_PER_CHAT_PER_MIN=18   # Telegram's own ceiling is ~20/min per chat
SEND_MAX_QUEUE=5000
BASELINE_ON_EMPTY=true     # first cycle over an empty DB records, never posts

# --- auto-clear ---
AUTO_CLEAR_ENABLED=true
AUTO_CLEAR_HOURS=24        # cooldown before an eligible deal may re-post
AUTO_CLEAR_MIN_DISCOUNT=50 # only deep discounts are candidates

# --- housekeeping ---
SEEN_RETENTION_DAYS=45
PRUNE_INTERVAL_HOURS=24

# --- per-source politeness: SOURCE_<NAME>_<FIELD>, one block per site ---
SOURCE_MYNTRA_RPS=5
SOURCE_MYNTRA_INTERVAL=0   # 0 = every scheduler tick
SOURCE_MYNTRA_MAX_PAGES=5
SOURCE_MYNTRA_CONCURRENCY=20
SOURCE_MYNTRA_MIN_DISCOUNT_EXISTING=15
SOURCE_CROMA_ENABLED=true
SOURCE_CROMA_RPS=5
SOURCE_CROMA_CHANNEL_CODE=400049

# --- q-commerce (hyper-local, priced per delivery point) ---
SOURCE_BLINKIT_ENABLED=true
SOURCE_BLINKIT_RPS=3
SOURCE_BLINKIT_MAX_PAGES=6
SOURCE_BLINKIT_PAGE_LIMIT=15
SOURCE_BLINKIT_COOLDOWN=20    # back off on 429/403, seconds
SOURCE_BLINKIT_WARM_COOKIES=true
SOURCE_BLINKIT_PROXY=         # residential/ISP egress; cloud IPs are refused
SOURCE_BLINKIT_QUIET_ALERTS=true   # no "⚠️ Source Blinkit …" admin DM
SOURCE_BIGBASKET_ENABLED=true
SOURCE_BIGBASKET_RPS=4
SOURCE_BIGBASKET_MAX_PAGES=5
SOURCE_BIGBASKET_COOLDOWN=25
SOURCE_BIGBASKET_COOKIE_TTL=600  # seconds before BigBasket cookies are re-warmed
SOURCE_BIGBASKET_PROXY=       # residential/ISP egress; cloud IPs are refused
SOURCE_BIGBASKET_QUIET_ALERTS=true # no "⚠️ Source BigBasket …" admin DM
GEOCODE_ENABLED=true
GEOCODE_URL=https://nominatim.openstreetmap.org/search
```

Anything left unset keeps the previous behaviour, so an existing deployment
upgrades without changing its request pattern.
