"""CromaStoreMapper — resolve Croma store codes (SA-XXXX) to store name/city.

Powers the STOCK# command. Fetches the full store list from Croma once,
caches it as JSON on the volume, then reads from cache (no per-call API hit).
Thread-safe fetch (called from the controller thread); resolution is read-only.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from curl_cffi import requests as cf_requests

log = logging.getLogger("myntra_scraper.croma_stores")

STORE_API_URL = ("https://api.croma.com/lookup/mobile-app/v1/storelocation"
                 "?pageSize=500&sort=asc&fields=FULL")

STORE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Host": "api.croma.com",
    "Origin": "https://www.croma.com",
    "Referer": "https://www.croma.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"),
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# Fallback: SA- letter → region (only if the live store map can't resolve).
REGION_GUESS = {
    "A": "Mumbai / Maharashtra", "B": "Bengaluru / South India",
    "C": "Chennai / Tamil Nadu", "D": "Delhi / NCR", "E": "East India / Kolkata",
    "G": "Gujarat", "H": "Hyderabad / Telangana", "J": "Jaipur / Rajasthan",
    "K": "Kerala", "P": "Pune", "N": "Noida / UP",
}


class CromaStoreMapper:
    def __init__(self, json_path: Path) -> None:
        self._path = Path(json_path)
        self._map: dict[str, dict] = {}   # "E004" and "SA-E004" → {name, city, ...}
        self._loaded = False
        self._lock = threading.Lock()
        self._load_from_file()

    def _load_from_file(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._map = data.get("stores", {})
                self._loaded = bool(self._map)
                if self._loaded:
                    log.info("Croma store map loaded: %d entries (cache)", len(self._map))
        except Exception as exc:
            log.warning("Croma store cache read error: %s", exc)

    def is_loaded(self) -> bool:
        return self._loaded

    def total_stores(self) -> int:
        return len(self._map)

    def get_store_info(self, store_code: str) -> dict | None:
        return self._map.get(store_code)

    def fetch_and_save(self) -> int:
        """Fetch all stores from Croma and cache. Returns count (0 on failure)."""
        log.info("Fetching Croma store map...")
        try:
            resp = cf_requests.get(STORE_API_URL, headers=STORE_HEADERS,
                                   impersonate="chrome120", timeout=30)
            if resp.status_code != 200:
                log.warning("Croma store API HTTP %d", resp.status_code)
                return 0
            data = resp.json()
            stores = (data.get("stores") or data.get("pointOfServices")
                      or data.get("results") or (data if isinstance(data, list) else []))

            new_map: dict[str, dict] = {}
            for store in stores:
                store_id = store.get("name", "")  # API returns id in "name": "A010"
                if not store_id:
                    continue
                address_obj = store.get("address", {}) or {}
                city = address_obj.get("town", "")
                area = store.get("displayName", "") or store_id
                state_obj = address_obj.get("state", {})
                state = state_obj.get("name", "") if isinstance(state_obj, dict) else str(state_obj)
                store_data = {
                    "name": area,
                    "city": city,
                    "state": state,
                    "address": address_obj.get("formattedAddress", ""),
                    "pincode": address_obj.get("postalCode", ""),
                }
                # Save under BOTH keys: product stockFlag uses "SA-E004", API uses "E004".
                new_map[store_id] = store_data
                new_map[f"SA-{store_id}"] = store_data

            if new_map:
                with self._lock:
                    self._map = new_map
                    self._loaded = True
                    try:
                        self._path.parent.mkdir(parents=True, exist_ok=True)
                        self._path.write_text(json.dumps({
                            "stores": new_map,
                            "total_stores": len(new_map),
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                    except OSError as exc:
                        log.warning("Croma store cache write failed: %s", exc)
                log.info("Croma store map saved: %d entries -> %s", len(new_map), self._path)
                return len(new_map)
        except Exception as exc:
            log.error("Croma store fetch error: %s", exc)
        return 0

    def resolve_codes(self, codes: list[str]) -> str:
        """List of SA codes -> readable multi-line string (name/city/state/pin)."""
        if not codes:
            return "No store has this (online-delivery only)"
        lines: list[str] = []
        cities: set[str] = set()
        for code in codes:
            info = self.get_store_info(code)
            if not info and code.startswith("SA-"):
                info = self.get_store_info(code[3:])
            if info:
                name = info.get("name", code)
                city = info.get("city", "")
                state = info.get("state", "")
                pincode = info.get("pincode", "")
                loc = f"{city}, {state}" if state else city
                pin_str = f" | {pincode}" if pincode else ""
                lines.append(f"  - {name}" + (f" ({loc}){pin_str}" if loc else pin_str))
                if city:
                    cities.add(city)
            else:
                letter = code[3] if len(code) > 3 else "?"
                region = REGION_GUESS.get(letter, "Unknown region")
                lines.append(f"  - {code} (~{region})")
        result = "\n".join(lines)
        if cities:
            result += f"\n\nCities: {', '.join(sorted(cities))}"
        return result

