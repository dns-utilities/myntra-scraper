"""Delivery locations for hyper-local sources (Blinkit, BigBasket).

Why this exists
---------------
Blinkit and BigBasket do not price nationally. The same SKU is a different
price and a different stock state at every dark store, and which store you get
is decided by where the request *claims* to be. So a source is really "a
source per delivery point", and the seen-key carries the location: a price at
one store must never mask the price at another.

Entry forms
-----------
Each line is one delivery point. Coordinates/context are exact; pincodes are
convenient, so both are accepted::

    Blinkit     raw : 17.4027233 | 78.3773127 | HYD-Manikonda
                pin : pin:132103 | Panipat
    BigBasket   raw : 10 | 0 | 1 | Hyderabad-bbnow
                pin : pin:134114 | Ambala

A ``pin:`` entry is resolved once and cached on the volume, so the resolution
cost is paid a single time ever. An unresolvable pincode is skipped loudly
rather than guessed -- a wrong dark store silently answers with another area's
prices, which is worse than answering with nothing.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger("dns_scraper.locations")

_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_PIN = re.compile(r"^pin:?\s*(\d{6})$", re.I)

# Public geocoder used to turn a pincode into a coordinate for Blinkit. Cached
# on disk, so this is one request per pincode for the lifetime of the volume.
DEFAULT_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_USER_AGENT = "dns-scraper/2.0 (personal deal monitor)"


@dataclass
class DeliveryPoint:
    """One place a source is priced at."""

    key: str                       # scopes the seen-key; must be stable
    label: str                     # human name shown in the deal footer
    payload: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return "%s (%s)" % (self.label, self.key)


class PincodeCache:
    """pincode -> resolved delivery point payload, per source, on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Pincode cache unwritable: %s", exc)

    def get(self, kind: str, pincode: str) -> dict | None:
        entry = (self._data.get(kind) or {}).get(str(pincode))
        return dict(entry) if isinstance(entry, dict) else None

    def put(self, kind: str, pincode: str, payload: dict) -> None:
        self._data.setdefault(kind, {})[str(pincode)] = payload
        self.save()

    def dump(self) -> dict:
        return json.loads(json.dumps(self._data))


def _split(line: str) -> list[str]:
    """Split on '|' (preferred) or ',' -- both files are hand-edited."""
    parts = [p.strip() for p in (line.split("|") if "|" in line else line.split(","))]
    return [p for p in parts if p != ""]


def parse_lines(lines: list[str], kind: str) -> tuple[list[DeliveryPoint], list[str]]:
    """Turn location-file lines into delivery points.

    Returns ``(points, unresolved_pins)``. ``points`` only contains entries
    whose key could be computed now; ``unresolved_pins`` are ``pin:`` entries
    still waiting on a lookup.
    """
    points: list[DeliveryPoint] = []
    pending: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if not parts:
            continue
        pin = _PIN.match(parts[0])
        if pin:
            code = pin.group(1)
            label = parts[1] if len(parts) > 1 else "pin %s" % code
            points.append(DeliveryPoint(key="", label=label, payload={"_pin": code}))
            pending.append(code)
            continue
        if kind == "blinkit":
            if len(parts) < 2 or not (_NUM.match(parts[0]) and _NUM.match(parts[1])):
                log.warning("Blinkit location skipped (want 'lat | lon | name'): %s", line)
                continue
            lat, lon = parts[0], parts[1]
            label = parts[2] if len(parts) > 2 else "%s,%s" % (lat, lon)
            points.append(DeliveryPoint(
                key="%s,%s" % (lat, lon), label=label,
                payload={"lat": lat, "lon": lon}))
            continue
        if kind == "bigbasket":
            if not parts[0].isdigit():
                log.warning("BigBasket location skipped (want 'ec | sa | city | name'): %s", line)
                continue
            ec = parts[0]
            sa = parts[1] if len(parts) > 1 and parts[1] else "0"
            city = parts[2] if len(parts) > 2 and parts[2] else "1"
            label = parts[3] if len(parts) > 3 else "ec%s" % ec
            points.append(DeliveryPoint(
                key="%s:%s" % (ec, sa), label=label,
                payload={"ec_id": ec, "sa_id": sa, "city_id": city,
                         "ec": "bbnow"}))
            continue
        log.warning("Unknown location kind %r for line: %s", kind, line)
    return points, pending


def geocode_pincode(pincode: str, url: str = DEFAULT_GEOCODE_URL,
                    timeout: int = 15) -> dict | None:
    """Resolve an Indian pincode to a representative coordinate.

    Uses a public geocoder (Nominatim by default). One call per pincode,
    cached by the caller, so the service's once-per-second policy is never
    stressed. Returns ``{"lat", "lon", "label"}`` or None.
    """
    try:
        resp = requests.get(url, params={
            "postalcode": pincode, "country": "India", "format": "json", "limit": 1,
        }, headers={"User-Agent": GEOCODE_USER_AGENT}, timeout=timeout)
        if resp.status_code != 200:
            log.warning("Geocode %s: HTTP %d", pincode, resp.status_code)
            return None
        hits = resp.json()
    except Exception as exc:
        log.warning("Geocode %s failed: %s", pincode, exc)
        return None
    if not isinstance(hits, list) or not hits:
        return None
    hit = hits[0]
    try:
        lat, lon = str(hit["lat"]), str(hit["lon"])
    except (KeyError, TypeError):
        return None
    label = str(hit.get("display_name", "")).split(",")[0] or ("pin %s" % pincode)
    return {"lat": lat, "lon": lon, "label": label}


class LocationSet:
    """The delivery points a source scrapes, with pincode resolution applied."""

    def __init__(self, kind: str, points: list[DeliveryPoint]) -> None:
        self.kind = kind
        self._points = points

    def __len__(self) -> int:
        return len(self._points)

    def __iter__(self):
        return iter(self._points)

    def all(self) -> list[DeliveryPoint]:
        return list(self._points)

    def keys(self) -> list[str]:
        return [p.key for p in self._points]

    def describe(self) -> str:
        if not self._points:
            return "none"
        return ", ".join("%s=%s" % (p.label, p.key) for p in self._points)


def build_location_set(kind: str, lines: list[str], cache: PincodeCache,
                       geocode: bool = True, geocode_url: str = DEFAULT_GEOCODE_URL,
                       geocode_sleep: float = 1.1) -> LocationSet:
    """Resolve a location file into ready-to-use delivery points.

    Raw entries pass straight through. ``pin:`` entries resolve from the cache
    first; on a miss Blinkit coordinates are geocoded once and cached. Pincodes
    that cannot be resolved (or that belong to a source with no automatic
    lookup, like BigBasket) are dropped with an actionable warning rather than
    guessed -- see ``config/*_locations.txt`` for the manual form.
    """
    points, pending = parse_lines(lines, kind)
    resolved: list[DeliveryPoint] = []
    for point in points:
        pin = point.payload.get("_pin")
        if not pin:
            resolved.append(point)
            continue
        cached = cache.get(kind, pin)
        if cached:
            # The label always comes from the location file, never the cache.
            # Otherwise the deal footer would read "Panipat" on the first boot
            # and whatever the geocoder returned thereafter -- the same deal
            # described two ways across restarts.
            resolved.append(DeliveryPoint(key=cached["key"], label=point.label,
                                          payload={k: v for k, v in cached.items()
                                                   if k not in ("key", "label")}))
            continue
        if kind == "blinkit" and geocode:
            hit = geocode_pincode(pin, geocode_url)
            if hit:
                key = "%s,%s" % (hit["lat"], hit["lon"])
                payload = {"lat": hit["lat"], "lon": hit["lon"]}
                cache.put(kind, pin, {"key": key, "label": hit.get("label") or point.label,
                                      **payload})
                resolved.append(DeliveryPoint(key=key, label=point.label, payload=payload))
                time.sleep(max(0.0, geocode_sleep))
                continue
        log.warning(
            "%s: pincode %s could not be resolved automatically. Add an explicit "
            "line to config/%s_locations.txt instead (see the file header).",
            kind, pin, kind)
    return LocationSet(kind, resolved)
