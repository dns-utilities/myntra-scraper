"""Request building for Blinkit's internal product-search endpoint.

Blinkit publishes no API. The website's own listing call is
``POST https://blinkit.com/v1/layout/search`` and it is location-bound: the
``lat`` / ``lon`` request headers decide which dark store answers, and therefore
which prices and stock come back. Prices are per store, so the delivery point is
part of a product's identity -- see ``locations.py``.

Three headers do the real work: ``app_client: consumer_web``,
``platform: desktop_web``, and the ``lat``/``lon`` pair. Drop any of them and
the endpoint returns an empty snippet list rather than an error, which is easy
to misread as "no deals today".

Products arrive inside ``response.snippets[]`` as entries whose ``widget_type``
is ``product_card_snippet_type_2``; everything else in the list is a banner,
filter chip or sponsor slot.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

SEARCH_URL = "https://blinkit.com/v1/layout/search"
HOME_URL = "https://blinkit.com/"
PRODUCT_CARD = "product_card_snippet_type_2"
PAGE_LIMIT = 15
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Cards that advertise themselves as paid placement. The raw layout payload
# names this differently across builds, so every spelling we have seen is
# checked; an absent field simply means "not flagged".
SPONSORED_KEYS = ("is_sponsored", "sponsored", "is_ad", "ad", "is_promoted")
AD_WIDGET_HINTS = ("ad", "banner", "promo")


def is_usable(line: str) -> bool:
    """False for a blank line or a ``#`` comment in the links file.

    ``LinkStore`` deliberately keeps comments so the files stay readable and
    hand-editable, and Myntra/Croma ignore them implicitly because their parsers
    only accept URLs. A keyword-driven source has no such luck: searching for
    ``# --- Atta, Rice, Oil & Dals ---`` is a real request that returns nothing.
    """
    text = (line or "").strip()
    return bool(text) and not text.startswith("#")


def term(line: str) -> str:
    """A links.txt line -> search query (bare keyword or a Blinkit search URL)."""
    if not is_usable(line):
        return ""
    match = re.search(r"[?&]q=([^&\s]+)", line)
    if match:
        return unquote(match.group(1))
    return line.strip()


def search_params(query: str, offset: int = 0, limit: int = PAGE_LIMIT) -> dict:
    return {"q": query, "search_type": "7", "offset": offset, "limit": limit}


def headers(point_payload: dict) -> dict:
    """Headers for one delivery point (``{"lat": ..., "lon": ...}``)."""
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://blinkit.com",
        "referer": "https://blinkit.com/",
        "user-agent": USER_AGENT,
        # The trio that makes Blinkit answer with real product results.
        "app_client": "consumer_web",
        "platform": "desktop_web",
        "lat": str(point_payload.get("lat", "")),
        "lon": str(point_payload.get("lon", "")),
    }


def home_headers(point_payload: dict) -> dict:
    return {
        "user-agent": USER_AGENT,
        "accept": "text/html,application/xhtml+xml",
        "lat": str(point_payload.get("lat", "")),
        "lon": str(point_payload.get("lon", "")),
    }


def iter_cards(data: dict):
    """Yield the raw product-card dicts out of one search response."""
    try:
        snippets = data["response"]["snippets"]
    except (KeyError, TypeError):
        return
    if not isinstance(snippets, list):
        return
    for snippet in snippets:
        if not isinstance(snippet, dict):
            continue
        if snippet.get("widget_type") != PRODUCT_CARD:
            continue
        card = snippet.get("data")
        if isinstance(card, dict):
            yield card


def is_sponsored(card: dict) -> bool:
    """True when the card advertises itself as paid placement."""
    for key in SPONSORED_KEYS:
        if card.get(key):
            return True
    # Some builds tag the wrapper instead of the card.
    widget = str(card.get("widget_type", "") or "").lower()
    if widget and widget != PRODUCT_CARD and any(h in widget for h in AD_WIDGET_HINTS):
        return True
    return False
