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
| Concurrency cap | `SourceSpec.concurrency` | 20 myntra / 6 croma |
| Request budget (token bucket) | `SourceSpec.rps` → `AsyncRateLimiter` | 5 req/s per source |
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
| `401 / 403` | do **not** hammer. Myntra: force one token refresh, retry the same page once, then stop. Croma: stop and log the body. |
| `429` | honour `Retry-After`, back off 5–10 s, drop the page for this cycle |
| `5xx` / timeout / connection error | back off, count it, keep the source's circuit closed |
| `200` but 0 products across all links | treated as a failure, not success (see §3) |

Retry budgets are capped and always jittered, so a fleet of failing URLs does
not retry in lockstep.

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

`Product.from_api_item` / `from_croma_item` return `None` on malformed rows,
and `should_notify` rejects `price >= mrp` or `price <= 0` outright. Discount
is recomputed from price/MRP with the site's own `% OFF` label as a cross-check.
Bad rows are dropped at the boundary, never stored.

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
* Adding a source = a new adapter class + env vars. No edits to `app.py`,
  `notifier.py`, or `controller_bot.py`.
* Source metadata is declarative (`SourceSpec`), not scattered through
  conditionals.
* Fetch and parse are separate: raw site shape is normalised to `Product` at
  the adapter boundary, so downstream filters never branch on the source.
* Every per-site behaviour is env-overridable, so a site that starts 429-ing
  can be slowed down without a redeploy.

## 10. Config reference (new)

```bash
# --- tier channels (non-overlapping bands) ---
CHANNEL_80PLUS=            # 80-89% deals. Falls back to CHANNEL_ALL if unset.

# --- auto-clear ---
AUTO_CLEAR_ENABLED=true
AUTO_CLEAR_HOURS=24        # cooldown before an eligible deal may re-post
AUTO_CLEAR_MIN_DISCOUNT=50 # only deep discounts are candidates

# --- housekeeping ---
SEEN_RETENTION_DAYS=45
PRUNE_INTERVAL_HOURS=24

# --- per-source politeness (add a block per source) ---
SOURCE_MYNTRA_RPS=5
SOURCE_MYNTRA_INTERVAL=0   # 0 = every scheduler tick
SOURCE_MYNTRA_MAX_PAGES=5
SOURCE_MYNTRA_CONCURRENCY=20
SOURCE_MYNTRA_MIN_DISCOUNT_EXISTING=15
SOURCE_CROMA_RPS=5
SOURCE_CROMA_ENABLED=true
```

Anything left unset keeps the previous behaviour, so an existing deployment
upgrades without changing its request pattern.
