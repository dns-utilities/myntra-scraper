"""Request building for BigBasket's product-listing API.

BigBasket's web app talks to ``/listing-svc/v1/products`` with a small set of
context headers and no signature. Prices and stock are location-bound: the
``x-entry-context-id`` / ``x-sa-id`` / ``x-city-id`` headers decide which
service area answers, so the delivery point is part of a product's identity
(see ``locations.py``). ``/listing-svc/v2`` answers 403 -- v1 is the live one.

A ``links.txt`` line is any of:

* a category slug      ``fruits-vegetables``   (or ``pc:fruits-vegetables``)
* a search term        ``search:atta``
* a BigBasket pc URL   ``https://www.bigbasket.com/pc/<id>/<slug>/``
"""

from __future__ import annotations

import random
import uuid
from urllib.parse import urlparse

BASE = "https://www.bigbasket.com"
LIST_PATH = "/listing-svc/v1/products"

# Desktop UAs: the BB-WEB endpoint prefers them over the mobile pool. One UA is
# chosen per scraper instance because BB ties its anti-bot cookie to it --
# rotating mid-session is what turns a working run into a wall of 403s.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
]
DEFAULT_UA = USER_AGENTS[0]


def pick_user_agent() -> str:
    return random.choice(USER_AGENTS) if USER_AGENTS else DEFAULT_UA


def is_usable(line: str) -> bool:
    """False for a blank line or a ``#`` comment in the links file.

    ``LinkStore`` keeps comments on purpose (the files are meant to be hand
    edited), and Myntra/Croma never notice because their parsers only accept
    URLs. A slug-driven source does: ``# --- Audio ---`` would be requested as a
    category called "# --- Audio ---".
    """
    text = (line or "").strip()
    return bool(text) and not text.startswith("#")


def parse_link(line: str) -> tuple[str, str]:
    """A links.txt line -> ``(type_code, slug)``; type is ``pc`` or ``ps``."""
    raw = line.strip()
    low = raw.lower()
    if not is_usable(raw):
        return "", ""
    if low.startswith("search:"):
        return "ps", raw.split(":", 1)[1].strip()
    if low.startswith("pc:"):
        return "pc", raw.split(":", 1)[1].strip()
    if "/pc/" in low or "/pd/" in low:
        parts = [p for p in urlparse(raw).path.split("/") if p]
        return "pc", parts[-1] if parts else raw
    return "pc", raw


def term(line: str) -> str:
    kind, slug = parse_link(line)
    return ("search:" if kind == "ps" else "") + slug


def search_params(kind: str, slug: str, page: int) -> dict:
    return {"type": kind, "slug": slug, "page": page}


def headers(store: dict, user_agent: str) -> dict:
    """Context headers for one delivery point."""
    out = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": BASE,
        "referer": BASE + "/",
        "user-agent": user_agent,
        "x-channel": "BB-WEB",
        "x-entry-context": store.get("ec", "bbnow"),
        "x-entry-context-id": str(store.get("ec_id", "10")),
        "x-caller": "UIKIRK",
        # A fresh tracker id per request is what the site itself does.
        "x-tracker": str(uuid.uuid4()),
    }
    sa = str(store.get("sa_id", "0") or "0")
    city = str(store.get("city_id", "1") or "1")
    if sa and sa != "0":
        out["x-sa-id"] = sa
    if city:
        out["x-city-id"] = city
    return out


def iter_products(data: dict):
    """Yield the raw product dicts out of one listing response.

    Listings arrive either flat (``data.products``) or grouped into tabs
    (``data.tabs[].product_info.products``) depending on the slug type.
    """
    tabs = data.get("tabs") or []
    products: list = []
    if tabs:
        for tab in tabs:
            if isinstance(tab, dict):
                products += (tab.get("product_info") or {}).get("products", []) or []
    else:
        products = data.get("products", []) or []
    for product in products:
        if isinstance(product, dict):
            yield product


def home_headers(store: dict, user_agent: str) -> dict:
    return {
        "user-agent": user_agent,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "x-entry-context": store.get("ec", "bbnow"),
        "x-entry-context-id": str(store.get("ec_id", "10")),
    }
