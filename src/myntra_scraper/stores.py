"""Persistent stores: links (txt), seen products + blacklist (SQLite)."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("myntra_scraper.stores")


class LinkStore:
    """Tracked Myntra search URLs, persisted as one-per-line text file."""

    def __init__(self, filepath: Path) -> None:
        self.filepath = filepath
        self._lock = threading.Lock()
        self._urls = self._load()

    def _load(self) -> list[str]:
        try:
            return [l.strip() for l in self.filepath.read_text(encoding="utf-8").splitlines() if l.strip()]
        except FileNotFoundError:
            return []

    def _save(self) -> None:
        self.filepath.write_text("\n".join(self._urls) + ("\n" if self._urls else ""), encoding="utf-8")

    def add(self, url: str) -> bool:
        url = url.strip()
        with self._lock:
            if not url or url in self._urls:
                return False
            self._urls.append(url)
            self._save()
            return True

    def remove(self, url_or_index: str) -> bool:
        with self._lock:
            key = url_or_index.strip()
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < len(self._urls):
                    del self._urls[idx]
                    self._save()
                    return True
                return False
            if key in self._urls:
                self._urls.remove(key)
                self._save()
                return True
            return False

    def all(self) -> list[str]:
        with self._lock:
            return list(self._urls)

    def reload(self) -> list[str]:
        with self._lock:
            self._urls = self._load()
            return list(self._urls)

    def __len__(self) -> int:
        return len(self.all())

    @property
    def count(self) -> int:
        return len(self.all())


class SeenStore:
    """UP-ONLY duplicate filter: re-alert only when discount rises enough."""

    def __init__(self, db_path: Path, min_new: int, min_existing: int, min_change: int) -> None:
        self._path = db_path
        self._min_new = min_new
        self._min_existing = min_existing
        self._min_change = min_change
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, int]] = {}
        self._init_db()
        self._warm_cache()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS seen (
            product_id TEXT PRIMARY KEY, last_price TEXT, last_discount INTEGER,
            first_seen TEXT, last_notified TEXT, notification_count INTEGER DEFAULT 0)""")
        # Optional metadata columns (Croma uses these; Myntra leaves them blank).
        for col in ("store_codes", "product_name"):
            try:
                conn.execute(f"ALTER TABLE seen ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # already exists
        conn.commit()
        conn.close()
        self._conn = self._connect()

    def _warm_cache(self) -> None:
        with self._lock:
            rows = self._conn.execute("SELECT product_id, last_price, last_discount FROM seen").fetchall()
            self._cache = {r[0]: (r[1], r[2]) for r in rows}
        log.info("SeenStore: %d products cached", len(self._cache))

    def should_notify(self, product_id: str, price: int, mrp: int, discount: int) -> tuple[bool, str]:
        if price >= mrp or price <= 0:
            return False, "invalid pricing"
        with self._lock:
            if product_id not in self._cache:
                if discount < self._min_new:
                    return False, "new too low %d%% (need %d%%)" % (discount, self._min_new)
                return True, "NEW at %d%%" % discount
            old_price, old_discount = self._cache[product_id]
            if discount < self._min_existing:
                return False, "existing too low %d%%" % discount
            if str(price) == old_price and discount == old_discount:
                return False, "duplicate"
            change = discount - old_discount
            if change < 0:
                return False, "discount dropped %d%%->%d%%" % (old_discount, discount)
            if change < self._min_change:
                return False, "change too small +%d%%" % change
            return True, "UP %d%%->%d%% (+%d)" % (old_discount, discount, change)

    def save(self, product_id: str, price: int, discount: int,
             store_codes: list | tuple | None = None, product_name: str = "") -> None:
        now = datetime.now().isoformat()
        codes_str = ",".join(store_codes) if store_codes else ""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT notification_count FROM seen WHERE product_id=?", (product_id,)).fetchone()
                if not row:
                    self._conn.execute(
                        "INSERT INTO seen VALUES (?,?,?,?,?,?,?,?)",
                        (product_id, str(price), discount, now, now, 1, codes_str, product_name))
                else:
                    self._conn.execute(
                        "UPDATE seen SET last_price=?, last_discount=?, last_notified=?, "
                        "notification_count=?, store_codes=?, product_name=? WHERE product_id=?",
                        (str(price), discount, now, row[0] + 1, codes_str, product_name, product_id))
                self._conn.commit()
                self._cache[product_id] = (str(price), discount)
        except Exception as exc:
            log.error("SeenStore save error: %s", exc)

    def get_product(self, product_id: str) -> dict | None:
        """Fetch stored metadata for a product (Croma STOCK# uses this)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_price, last_discount, store_codes, product_name, "
                "last_notified FROM seen WHERE product_id=?", (product_id,)).fetchone()
        if not row:
            return None
        price, discount, codes_str, name, last_seen = row
        return {
            "product_id": product_id,
            "price": price,
            "discount": discount,
            "name": name,
            "last_seen": last_seen,
            "store_codes": [c for c in (codes_str or "").split(",") if c],
        }

    def forget(self, product_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM seen WHERE product_id=?", (product_id,))
            self._conn.commit()
            self._cache.pop(product_id, None)
            return cur.rowcount > 0

    def clear(self) -> int:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            self._conn.execute("DELETE FROM seen")
            self._conn.commit()
            self._cache.clear()
            return total

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            hot = self._conn.execute("SELECT COUNT(*) FROM seen WHERE last_discount >= 70").fetchone()[0]
            notifs = self._conn.execute("SELECT COALESCE(SUM(notification_count),0) FROM seen").fetchone()[0]
            return {"tracked": total, "hot_70plus": hot, "notifications": notifs}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


    def __len__(self) -> int:
        return len(self.all())

    @property
    def count(self) -> int:
        return len(self.all())


class BlacklistStore:
    """Brand rules: skip <brand> unless discount >= min_discount.

    Brand match is case-insensitive substring over "brand + title".
    Rules persist in SQLite on the volume.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._rules: dict[str, int] = {}
        conn = self._connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS blacklist (
            brand TEXT PRIMARY KEY, min_discount INTEGER NOT NULL)""")
        conn.commit()
        for brand, pct in conn.execute("SELECT brand, min_discount FROM blacklist"):
            self._rules[brand] = pct
        conn.close()
        log.info("BlacklistStore: %d rules", len(self._rules))

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._path), check_same_thread=False)

    def add(self, brand: str, min_discount: int) -> None:
        brand = brand.strip().lower()
        if not brand or not 0 <= min_discount <= 100:
            raise ValueError("usage: /add_blacklist <brand> <0-100>")
        with self._lock:
            conn = self._connect()
            conn.execute("INSERT OR REPLACE INTO blacklist VALUES (?,?)", (brand, min_discount))
            conn.commit()
            conn.close()
            self._rules[brand] = min_discount

    def remove(self, brand: str) -> bool:
        brand = brand.strip().lower()
        with self._lock:
            conn = self._connect()
            cur = conn.execute("DELETE FROM blacklist WHERE brand=?", (brand,))
            conn.commit()
            conn.close()
            existed = brand in self._rules or cur.rowcount > 0
            self._rules.pop(brand, None)
            return existed

    def is_blocked(self, brand: str, title: str, discount: int) -> tuple[bool, str]:
        haystack = ("%s %s" % (brand, title)).lower()
        with self._lock:
            for rule_brand, min_pct in self._rules.items():
                if rule_brand in haystack and discount < min_pct:
                    return True, "%s blocked below %d%% (got %d%%)" % (rule_brand, min_pct, discount)
        return False, ""

    def all(self) -> dict[str, int]:
        with self._lock:
            return dict(self._rules)
