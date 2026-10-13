# Future sources — Blinkit / Zepto / Swiggy Instamart

Deliberately **not built yet**. This file is the plan so the next person (or
the next session) does not have to re-derive it, and so the architecture does
not have to be reopened to add them.

## Research summary (Oct 2026)

| | Blinkit | Zepto | Swiggy Instamart |
|---|---|---|---|
| Public catalog API | **No** | **No** | **No** |
| DIY path | private mobile API, lat/lon location, anti-bot | JS-rendered web/app → needs a real browser (Playwright) | private mobile API, city/store id |
| Location model | per dark store; same SKU priced differently per pincode | per store, pincode-keyed | per store, city + pincode |
| Catalog size | ~30k SKUs | comparable | comparable |
| Typical discount depth | mostly 10–35% | mostly 10–35% | mostly 10–35% |
| Maintained paid APIs exist | yes | yes | yes |

Consequences that change the design, not just the code:

1. **Location is a first-class key.** `SeenStore` already keys on
   `(source, location, product_id)` — this was added *for* these sources. A
   Blinkit product observed at two pincodes is two rows.
2. **The 70/80/90 tier ladder does not fit.** Q-commerce discounts are shallow.
   A grocery deal channel needs its own thresholds — e.g. "≥ 40% off" — not the
   Myntra ladder. `SourceSpec` needs a `min_discount_new` / `min_discount_existing`
   per source (already plumbed through) plus per-source channel overrides in the
   notifier if they should not share the D70/D80/D90 feeds.
3. **The deal signal is different.** For grocery it is a price *drop* on a
   tracked item, not a discount percentage. `SeenStore.should_notify` would need
   a "price fell by ≥ N%" rule alongside the up-only-discount rule.
4. **Refresh cadence differs.** Prices move multiple times a day. Myntra at
   every-tick is fine; these want 1–4 h, which `SOURCE_<NAME>_INTERVAL`
   already supports.
5. **Anti-bot is heavier.** Zepto's rendering means a headless browser, which
   is 10–100× the CPU/RAM of a JSON call and needs a browser pool rather than a
   session-per-request. That is a different runtime shape from the current
   async-HTTP design, and the honest options are below.

## Options, in the order worth trying

### Option A — maintained third-party API (recommended first)

Providers observed in Oct 2026 that wrap these catalogs: Upscrape
(`zepto.catalog`, `zepto.search`, `blinkit.search`, `blinkit.product`, …),
QuickCommerce API (Swiggy Instamart), Parse.bot, Syphoon, Pricium, and Apify
actors (`zapticx/blinkit-products-prices-scraper`,
`smacient/swiggy-instamart-data-extractor`).

* Cost is pay-per-call, roughly $1.25–2 per 1k requests.
* No proxy management, no selector maintenance, no ToS exposure of your own.
* Downside: an external dependency and a recurring bill.

This fits the existing architecture perfectly, because it is just another
HTTP-JSON source — the same shape as Myntra and Croma.

### Option B — DIY against private APIs

Cheap per-request, but you own: fingerprint coherence (stable device id, TLS
profile matching the claimed client, cookie jar continuity), location header
resolution, and a breakage alarm for the day the endpoint changes. The Myntra
403 history in the README is exactly this failure mode.

### Option C — DIY with a headless browser (Zepto)

Needed if and only if the data is not in the raw HTML. Uses Playwright, a
browser pool with context recycling, and per-store location setup. Heaviest to
run and most likely to drift.

## What is already done for you

* `SourceSpec` + `SourceAdapter` — the seam is in place.
* `SeenStore` keyed by `(source, location, product_id)`.
* Per-source `interval`, `rps`, `max_pages`, `concurrency`,
  `min_discount_new`, `min_discount_existing`, `enabled` — all env-driven.
* `SourceHealth` circuit breaker per source — a fragile new site parks itself
  instead of hurting Myntra/Croma.
* Per-source `/status` and `/diag`.
* `Product.location` and the `App: <name>` line already render it.

## Steps to add one (Option A shape)

1. **Config**: add the seed links file (`config/<name>_links.txt`) and the
   `SOURCE_<NAME>_*` env block.
2. **Adapter**: create `src/myntra_scraper/sources/<name>.py` subclassing
   `SourceAdapter`, implementing `scrape_all()`. Use
   `self.rate_limiter.acquire()` before each request. Set
   `default_location` to the pincode/store id.
3. **Normalise**: map the provider's JSON to `Product`, setting `source`,
   `location`, `brand`, `title`, `price`, `mrp`, `discount`, `url`. Keep
   provider quirks inside `from_<name>_item()` like `from_croma_item()` does.
4. **Register**: add a `SourceSpec` to `build_sources()` and the class to
   `_ADAPTERS`.
5. **Thresholds**: decide whether it shares the D70/D80/D90 channels or gets
   its own `CHANNEL_<NAME>` plus a `MIN_DISCOUNT_*` pair. Do not reuse the
   Myntra ladder by default.
6. **Deal rule**: if the meaningful signal is a price drop rather than a
   discount, extend `should_notify` with a price-drop branch.
7. **Test**: `scripts/smoke_test.py` + `scripts/integration_test.py` cover the
   shared pipeline; add a parser test for the new `from_<name>_item`.
8. **Docs**: one row in the README source table, one line in `/help`.

## Open questions to settle before starting

* One pincode or several? Each additional location multiplies request volume
  and post volume linearly.
* What discount threshold actually constitutes a "deal" for grocery here?
* Should grocery deals go to the existing channels or a new one?
* Price-drop tracking: which items are worth a saved baseline?
