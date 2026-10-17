# Adding a source

Myntra, Croma, Blinkit and BigBasket are built. This file is how the next one
gets added without reopening the architecture — and the research notes for the
ones still deliberately unbuilt (Zepto, Swiggy Instamart).

## Two shapes of source

| | national price | hyper-local price |
|---|---|---|
| examples | Myntra, Croma | Blinkit, BigBasket |
| location | one delivery pincode (or none) | a list of delivery points |
| seen-key | `(source, location, product_id)` | same, but `location` varies per row |
| channel | `CHANNEL_BASE_<NAME>` | `CHANNEL_BASE_<NAME>` |
| module | `<name>_api.py` + `<name>_scraper.py` | same + a locations file |

The only structural difference is that a hyper-local source yields several
`Product` rows with different `location` values from one run, and carries a
`locations_file` on its `SourceSpec`. `SeenStore` was keyed on location for
exactly this reason: the same SKU at two dark stores is two observations.

## Steps

1. **Config** — `config/<name>_links.txt` seed, plus a `<name>_links_file` /
   `<name>_seen_db` property on `Settings`. For a hyper-local source also add
   `<name>_locations.txt` (see `config/blinkit_locations.txt` for the documented
   format) and seed it in `ensure_data_dir()`. Nothing else: `CHANNEL_BASE_<NAME>`
   is discovered generically by `ChannelPlan.from_env`, and every spec field is
   settable with `SOURCE_<NAME>_<FIELD>` — adding a source needs no channel or
   config plumbing.
2. **Request building** — a `<name>_api.py` with the URL, headers, a `term()`
   that turns a links line into a query, and an `iter_<things>()` that flattens
   the payload's envelope. Keep it free of `Product` knowledge.
3. **Normalise** — a `Product.from_<name>_item()` classmethod. Put every
   provider quirk there: `from_croma_item` handles Croma's placeholder discount,
   `from_blinkit_item` drops sold-out cards, `from_bigbasket_item` checks
   `avail_status == 001`. Set `source`, `location`, `location_label`, `url`.
4. **Scraper** — `<name>_scraper.py`, mirroring the others: accept a
   `rate_limiter`, use `session` from `curl_cffi`, back off on 429/403 with a
   *shared* cooldown so all in-flight requests pause together, and return
   `Product`s. Never decide what a deal is — that is `models.py` and the app.
5. **Adapter** — subclass `SourceAdapter` in `sources.py`, add a `SourceSpec`
   to `build_sources()`, register in `_ADAPTERS`. Override `reload_points()` if
   location-scoped so `/add_location` works.
6. **Thresholds** — decide explicitly whether it shares D70/D80/D90 (the
   default) and what `min_discount_new` means for it. Q-commerce discounts are
   shallow (mostly 10–35%), so a grocery base feed at 0% is only sane with the
   empty-DB baseline on.
7. **Test** — `scripts/qcommerce_test.py` is the template for a parser +
   routing + plumbing suite. Cover the drops (sponsored, out-of-stock, zero
   price) explicitly; those are the rows that otherwise leak into a channel.
8. **Docs** — one row in the README table, one line in `/help`.

## Still unbuilt

| | Zepto | Swiggy Instamart |
|---|---|---|
| Public catalog API | **No** | **No** |
| DIY path | pincode-keyed, heavier anti-bot | private mobile API, city + store id |
| Location model | per store, pincode-keyed | per store, city + pincode |
| Typical discount | mostly 10–35% | mostly 10–35% |

Zepto's data is JS-rendered, so it needs a real browser (Playwright) rather
than a JSON call — a different runtime shape from every source here, and the
reason it is not in this pass. Options, in the order worth trying:

* **Option A — a maintained third-party API.** Just another HTTP-JSON source,
  same shape as Croma, at a pay-per-call cost (~$1.25–2 per 1k requests).
* **Option B — DIY against the private API.** Cheap per request, but you own
  fingerprint coherence, cookie continuity, location resolution and a
  breakage alarm. The Myntra 403 history in the README is this failure mode.
* **Option C — DIY with a headless browser.** Heaviest to run, most likely to
  drift; a browser pool rather than a session per request.

## Invariants to keep

* **Exclusive routing.** A tier deal posts to exactly one channel. Do not add
  a source that also fans out into its base feed for tier discounts.
* **Never post past a Telegram ceiling.** The send gate in `notifier.py` is the
  only thing standing between a burst and a ban. Do not bypass it.
* **A cleared DB posts nothing on the first cycle.** `BASELINE_ON_EMPTY` — keep
  it honoured for any new source.
* **Politeness is per host.** A new source gets its own `rps`, not a share of a
  global one.
