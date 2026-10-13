"""Auto-clear brand registry — which brands are worth re-posting on a cycle.

The list is derived from the curated deal-channel brand registry in
``research/Brands Analysis`` (653 brands, alias-matched against real deal
titles). Only brands with *deal evidence* ship as auto-clear eligible: a brand
that has actually shown up in deal traffic is the signal that re-posting its
deep discounts is wanted, whereas "reputed but never seen" entries would just
re-post noise.

Matching is deliberately conservative — a brand alias only matches when it
equals the source brand field or *leads* the title ("Titan" matches
"Titan Raga Watch…", "Pineapple" does not match "Apple").
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("myntra_scraper.brand_registry")

# Title words considered when the brand field is empty/unknown. Registry
# brands are title-leads, so the first few words carry the brand.
_TITLE_LEAD_WORDS = 4


def normalize(text: str) -> str:
    """Lowercase, collapse every non-alphanumeric run to a single space."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class BrandRegistry:
    """Alias -> canonical brand map with longest-alias-first matching."""

    def __init__(self, entries: list[dict]) -> None:
        self._lock = threading.Lock()
        self._alias_to_name: dict[str, str] = {}
        self._mentions: dict[str, int] = {}
        self._category: dict[str, str] = {}
        self._aliases: list[str] = []      # longest first
        self._load_entries(entries)

    # -- construction -----------------------------------------------------
    def _load_entries(self, entries: list[dict]) -> None:
        self._alias_to_name.clear()
        self._mentions.clear()
        self._category.clear()
        for entry in entries:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            mentions = int(entry.get("mentions") or 0)
            category = entry.get("category") or ""
            aliases = [a for a in (entry.get("aliases") or []) if a]
            if not aliases:
                aliases = [name.lower()]
            # Longest alias wins, so "Faber-Castell" beats "Faber" and
            # "United Colors of Benetton" beats a bare "United".
            for alias in sorted((normalize(a) for a in aliases), key=len, reverse=True):
                if alias and alias not in self._alias_to_name:
                    self._alias_to_name[alias] = name
            prev = self._mentions.get(name, 0)
            self._mentions[name] = max(prev, mentions)
            if category:
                self._category[name] = category
        self._aliases = sorted(self._alias_to_name, key=len, reverse=True)

    @classmethod
    def load(cls, path: Path) -> "BrandRegistry":
        """Load from JSON. Missing/corrupt file -> empty registry (never fatal)."""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            entries = payload.get("brands", payload if isinstance(payload, list) else [])
        except FileNotFoundError:
            log.warning("Auto-clear brand list missing at %s — auto-clear disabled", path)
            return cls([])
        except (OSError, ValueError) as exc:
            log.error("Auto-clear brand list unreadable (%s) — auto-clear disabled", exc)
            return cls([])
        registry = cls(entries)
        log.info("Auto-clear brands: %d brands / %d aliases", len(registry._mentions),
                 len(registry._aliases))
        return registry

    # -- matching ---------------------------------------------------------
    def _candidates(self, brand: str, title: str) -> list[str]:
        cands: list[str] = []
        b = normalize(brand)
        if b and b not in ("unknown", "n a", "na"):
            cands.append(b)
        t = normalize(title)
        if t:
            words = t.split()
            cands.append(" ".join(words[:_TITLE_LEAD_WORDS]))
        return cands

    def resolve(self, brand: str, title: str) -> str | None:
        """Canonical registry brand for a product, or None if not in the list."""
        cands = self._candidates(brand, title)
        if not cands:
            return None
        with self._lock:
            for alias in self._aliases:
                for cand in cands:
                    if cand == alias or cand.startswith(alias + " "):
                        return self._alias_to_name[alias]
        return None

    def is_eligible(self, brand: str, title: str, discount: int,
                    min_discount: int) -> bool:
        """True when the product may re-post on the auto-clear cooldown."""
        if discount < min_discount:
            return False
        return self.resolve(brand, title) is not None

    # -- live editing -----------------------------------------------------
    def add(self, name: str, aliases: list[str] | None = None,
            mentions: int = 0, category: str = "") -> str:
        """Add/refresh a brand. Returns the canonical name stored."""
        name = (name or "").strip()
        if not name:
            raise ValueError("brand name required")
        keys = [normalize(a) for a in (aliases or []) if normalize(a)]
        if normalize(name) not in keys:
            keys.append(normalize(name))
        with self._lock:
            for key in keys:
                self._alias_to_name[key] = name
            self._mentions[name] = max(self._mentions.get(name, 0), mentions)
            if category:
                self._category[name] = category
            self._aliases = sorted(self._alias_to_name, key=len, reverse=True)
        return name

    def remove(self, name: str) -> bool:
        target = normalize(name)
        with self._lock:
            canonical = self._alias_to_name.get(target)
            if not canonical:
                return False
            for alias, mapped in list(self._alias_to_name.items()):
                if mapped == canonical:
                    del self._alias_to_name[alias]
            self._mentions.pop(canonical, None)
            self._category.pop(canonical, None)
            self._aliases = sorted(self._alias_to_name, key=len, reverse=True)
            return True

    # -- introspection ----------------------------------------------------
    def all(self) -> dict[str, int]:
        with self._lock:
            return dict(self._mentions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._mentions)

    def save(self, path: Path) -> None:
        with self._lock:
            entries = [{"name": n, "mentions": m,
                        "category": self._category.get(n, ""),
                        "aliases": sorted(a for a, v in self._alias_to_name.items()
                                          if v == n)}
                       for n, m in sorted(self._mentions.items(), key=lambda kv: -kv[1])]
        Path(path).write_text(json.dumps({
            "generated": "live-edited via /autoclear_add + /autoclear_remove",
            "rule": "brands with real deal evidence (mentions > 0)",
            "brand_count": len(entries),
            "brands": entries,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
