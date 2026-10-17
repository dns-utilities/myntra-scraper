# TO-DO / hand-over

_Updated 2026-10-17. Repo: `dns-utilities/dns-scraper`._

Everything is pushed to `main`. This file is the short version of where the
project stands, what one thing blocks the grocery sources, and what is left to
build. Read this before running it.

## Status

| Source | State | Notes |
|---|---|---|
| Myntra | **works** | national pricing, fine on any egress including cloud |
| Croma | **works** | national pricing, fine on any egress including cloud |
| Blinkit | **built, needs a clean egress** | code is correct; a cloud IP is refused (see below) |
| BigBasket | **built, needs a clean egress + a real browser** | see the open task below |

**We are no longer targeting Railway.** Run it wherever you like — the code is
host-agnostic; only the two grocery sources care about the egress.

## The one blocking item: grocery egress

Blinkit and BigBasket both defend themselves **per IP**, and a
cloud/datacenter address is refused outright no matter what we send. Point the
two grocery sources at a residential/ISP (ideally Indian) proxy:

```dotenv
SOURCE_BLINKIT_PROXY=http://user:pass@host:port
SOURCE_BIGBASKET_PROXY=http://user:pass@host:port
```

* **Blinkit then works as-is.** The actual bug there was a browser fingerprint,
  and that is already fixed (below).
* **BigBasket still needs a real browser session** on top of the clean IP — see
  the first open task.
* Myntra/Croma need no proxy and are happy on a cloud IP. Leave them direct
  unless the proxy is cheap, since a proxy is billed per GB and BigBasket's
  listings are large (a full sweep is tens of MB per cycle).

Without a proxy the two grocery sources **park themselves** via the per-source
circuit breaker — they log one error and back off rather than hammering the
wall, and the other two sources are unaffected.

## Open tasks

- [ ] **BigBasket: browser-backed fetcher.** `/listing-svc/*` sits behind
      Akamai Bot Manager (`_abck`/`bm_s`/`bm_sz`). Measured, on a home IP:
      * a real **headed** Chrome launched with `--remote-debugging-port` and
        attached over CDP → `HTTP 200` + real products, for **both** `/v1` and
        `/v2`;
      * **headless** Chrome → `Access Denied` / 403 (detected);
      * replaying that browser's cookies through `curl_cffi` → 403 (Akamai
        binds the session to the browser's TLS/HTTP2 fingerprint).
      So the fetcher must issue the listing calls **from inside the page**
      (`page.evaluate("fetch(...)")`, batching with `Promise.all`), not by
      re-using cookies. On a headless box that means Chrome + **Xvfb**.
      Note `/ui-svc/*` is *not* protected (200 via plain `curl_cffi`), only
      `/listing-svc/*`; the search HTML has no product data (`SSRData: null`).
- [ ] **BigBasket delivery points.** `pin:` lines are **not** auto-resolved.
      Needs an explicit `ec_id | sa_id | city_id | name` line in
      `data/bigbasket_locations.txt`; the file header explains how to read the
      three ids from DevTools. (Blinkit `pin:` lines *are* geocoded and cached.)
- [ ] **Proxy scope decision.** `SOURCE_<NAME>_PROXY` is per source today. If
      one egress should cover everything, add a global fallback.
- [ ] **Confirm the grocery links files** in `config/` match what you want to
      track; the volume is dominated by link count × pages.

## Fixed today (already on `main`)

1. **Blinkit produced nothing because of one wrong fingerprint.** Blinkit
   answers some TLS profiles with real product cards and others with a flat
   **403 on the search call itself** — not a rate limit, and a retry does not
   help. Swept 23 profiles against the live endpoint:

   | profile | homepage | search |
   |---|---|---|
   | `chrome124` (our old value, and the reference scraper's) | 403 | **403** |
   | `chrome131`, `chrome119`, `chrome123`, `firefox135` | 200 | **403** |
   | `chrome136`, `chrome133a`, `safari18_0`, `firefox133`, `chrome120` | 200 | **200 + cards** |

   Default is now `chrome136`; a refused fingerprint makes the run retry through
   the known-good list (`blinkit_api.IMPERSONATIONS`) instead of failing the
   source. The hard-coded Chrome/124 User-Agent is gone — it sat on top of
   whichever fingerprint was in use, so the two could disagree and advertise the
   scrape on their own. (`43300d1`)
2. **Per-source egress proxy** — `SOURCE_<NAME>_PROXY`. (`5b79b19`)
3. **`/clear_all` no longer re-announces the catalogue.** The empty-DB baseline
   was suppressed by an in-memory per-boot flag, so a mid-run clear made the next
   cycle treat every product as new (~19k messages, rate-limited, mostly dropped
   at the queue ceiling). The trigger is now the **store's emptiness at run
   time**, so a first deploy, a redeploy, a wiped volume and a mid-run
   `/clear_all` behave identically. (`f4e34ca`)
4. **Project renamed** `myntra-scraper` → `dns-scraper`, channels reworked to
   `CHANNEL_BASE_<SOURCE>` + shared `CHANNEL_TIER_70/80/90` with per-source
   tier fallback. (`acfaa8c`, `9d9a18a`)

## Run it

```powershell
pip install -r requirements.txt
copy .env.example .env      # fill BOT_TOKENS, ADMIN_CHAT_ID, CHANNEL_BASE_*/CHANNEL_TIER_*
python main.py
$env:MAX_CYCLES=1; python main.py     # single test cycle
```

Python version is pinned in `.python-version`. `curl_cffi` does the TLS
impersonation; no browser is required unless you build the BigBasket fetcher.

The control bot (token #1) takes `/status`, `/diag`, `/clear_all`, `/add_link`,
`/add_location`, `/list_locations`, `/verify_bots`. `/status` shows the routing
line and per-source counts; `/diag` shows per-source `403=`/`aborted=`.

## Tests

```powershell
python scripts/smoke_test.py
python scripts/integration_test.py
python scripts/button_test.py
python scripts/poller_test.py
python scripts/migration_check.py
python scripts/qcommerce_test.py    # Blinkit/BigBasket parsers, routing, send gate
```

All six pass on `main`. `qcommerce_test.py` is the one to extend when you touch
either grocery source.

## Configuration reference

`.env.example` (annotated, the source of truth), `README.md` (env tables and the
"Egress matters more than headers here" note under *Hyper-local sources*), and
`docs/SCRAPING-GUIDELINES.md` §10.
