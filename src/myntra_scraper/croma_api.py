"""URL parsing + request building + headers for the Croma category API.

Mirrors search_api.py but for Croma's PLP (product listing page) endpoints.
Croma uses a category id in the URL path (/.../c/<id>) and an internal
searchservices API. No auth token needed — just a rotating desktop UA +
browser headers with curl_cffi impersonation.
"""

from __future__ import annotations

import random
from urllib.parse import parse_qs, quote, urlparse

API_BASE = "https://api.croma.com/searchservices/v1/category"
DEFAULT_CHANNEL_CODE = "400049"
ROWS_PER_PAGE = 24

# Rotating desktop UAs — keep the impersonation profile family (chrome120) in
# sync so the TLS fingerprint matches the UA. All Chrome/Windows here.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-IN,en;q=0.9,hi;q=0.8",
]


def parse_category_url(url: str) -> dict:
    """Croma browse URL → API params.

    Input:  https://www.croma.com/televisions-accessories/led-tvs/c/392?q=%3Adiscount-desc
    Output: {"category_id": "392", "category_name": "televisions-accessories",
             "referer": url, "extra_filters": ":discount-desc"}
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]

    category_name = path_parts[0] if path_parts else "products"
    category_id = path_parts[-1] if path_parts else ""

    # Only accept a numeric trailing id; otherwise treat as no-id (caller skips).
    if not category_id.isdigit():
        category_id = ""

    qs = parse_qs(parsed.query)
    existing_q = qs.get("q", [""])[0]

    return {
        "category_id": category_id,
        "category_name": category_name,
        "referer": url,
        "extra_filters": existing_q,  # preserve user filters (e.g. :discount-desc)
    }


def build_api_url(info: dict, page: int = 0, channel_code: str = DEFAULT_CHANNEL_CODE) -> str:
    """Build the Croma category API URL. Default sort is discount-desc."""
    base_query = ":discount-desc"
    extra = info.get("extra_filters", "")
    if extra and extra != ":discount-desc":
        base_query += extra

    encoded_query = quote(base_query, safe="")
    return (
        f"{API_BASE}/{info['category_id']}"
        f"?currentPage={page}"
        f"&query={encoded_query}"
        f"&fields=FULL"
        f"&channel=WEB"
        f"&channelCode={channel_code}"
    )


def api_headers(referer: str) -> dict:
    """Rotating browser headers for the category API call."""
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Host": "api.croma.com",
        "Origin": "https://www.croma.com",
        "Referer": referer,
        "sec-ch-ua": '"Google Chrome";v="120", "Not.A/Brand";v="8", "Chromium";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": random.choice(USER_AGENTS),
    }
