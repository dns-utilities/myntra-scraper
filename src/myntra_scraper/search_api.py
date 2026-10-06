"""URL parsing + request building for the Myntra search API."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, quote, urlparse

API_BASE = "https://api.myntra.com/v1/search"
ROWS_PER_PAGE = 48


def parse_search_url(url: str) -> dict:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    parts = parsed.path.strip("/").split("/")
    query = parts[-1] if "api.myntra.com" in parsed.netloc else (parts[0] if parts else "")
    return {
        "query": query,
        "filters": params.get("f", [None])[0],
        "range_filters": params.get("rf", [None])[0],
        "sort": params.get("sort", [None])[0],
        "raw_params": params,
    }


def build_api_request(url_info: dict, page: int) -> tuple[str, dict]:
    api_url = "%s/%s" % (API_BASE, quote(url_info["query"], safe=""))
    params: dict = {
        "rows": ROWS_PER_PAGE, "o": page * ROWS_PER_PAGE,
        "userQuery": "false", "returnDocs": "true",
        "searchRequestId": str(uuid.uuid4()),
    }
    if url_info.get("filters"):
        params["f"] = url_info["filters"]
    if url_info.get("range_filters"):
        params["rf"] = url_info["range_filters"]
    if url_info.get("sort"):
        params["sort"] = url_info["sort"]
    for key, val in url_info.get("raw_params", {}).items():
        if key not in params and key not in ("f", "rf", "sort"):
            params[key] = val[0] if isinstance(val, list) else val
    return api_url, params
