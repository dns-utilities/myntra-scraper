"""Persistent stores: links (txt), seen products + blacklist (SQLite).

Two identities matter here and they are deliberately kept separate:

* **SeenStore** — "have I already posted this deal?" keyed by
  ``(source, location, product_id)``. Location is empty for national-price
  sources; a pincode/store id for location-scoped ones, because the same SKU
  priced at a different dark store is a genuinely different observation.
* **BlacklistStore** — two rule kinds:
  - *brand rules*: skip `<brand>` unless discount >= N (pre-existing)
  - *product rules*: skip one ``(source, product_id, price)`` triple, added
    from the "Blacklist Product" button. Priced, not blanket, so a later price
    drop on the same product still alerts.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("myntra_scraper.stores")


class LinkStore:
    """Tracked URLs for one source, persisted as one-per-line text file."""

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


@dataclass(frozen=True)
class AutoClearPolicy:
    """When a duplicate deal may re-post because it is worth repeating."""

    enabled: bool = False
    hours: int = 24
    min_discount: int = 50


class SeenStore:
    """Up-only duplicate filter with an auto-clear escape hatch.

    Default behaviour is unchanged: a seen product re-alerts only when its
    discount rises by at least ``min_change``. ``auto_clear`` adds one extra
    path — an eligible deep-discount deal may re-post once the cooldown since
    its last notification has elapsed, even at an identical price. That is what
    makes a long-running Titan/Lacoste-style deal cycle instead of posting once
    and going silent.
    """

    def __init__(self, db_path: Path, source: str = "myntra",
                 default_location: str = "", min_new: int = 0,
                 min_existing: int = 15, min_change: int = 1) -> None:
        self._path = db_path
        self._source = source
        self._default_location = default_location or ""
        self._min_new = min_new
        self._min_existing = min_existing
        self._min_change = min_change
        self._lock = threading.Lock()
        # key -> (last_price, last_discount, last_notified_iso)
        self._cache: dict[str, tuple[str, int, str]] = {}
        self._conn = self._init_db()

    # -- schema -----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    _SCHEMA = """CREATE TABLE IF NOT EXISTS seen (
        source TEXT NOT NULL DEFAULT 'myntra',
        location TEXT NOT NULL DEFAULT '',
        product_id TEXT NOT NULL,
        last_price TEXT, last_discount INTEGER,
        first_seen TEXT, last_notified TEXT, notification_count INTEGER DEFAULT 0,
        store_codes TEXT DEFAULT '', product_name TEXT DEFAULT '',
        brand TEXT DEFAULT '', auto_clear INTEGER DEFAULT 0,
        PRIMARY KEY (source, location, product_id))"""

    def _init_db(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.execute(self._SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_notified ON seen(last_notified)")
        self._migrate_legacy(conn)
        conn.commit()
        conn.close()
        conn = self._connect()
        self._warm_cache(conn)
        return conn

    def _migrate_legacy(self, conn: sqlite3.Connection) -> None:
        """Rebuild the pre-location schema (PK was product_id alone).

        Existing rows are adopted as this source's default location so no
        product re-alerts just because the key grew a dimension. If anything
        goes wrong the original table is left untouched and the store keeps
        running on the legacy key layout.
        """
        cols = [r[1] for r in conn.execute("PRAGMA table_info(seen)")]
        if not cols or "source" in cols:
            return
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        log.info("SeenStore %s: migrating %d legacy rows to (source,location,product_id)",
                 self._path, total)
        try:
            rows = conn.execute("SELECT * FROM seen").fetchall()
            conn.execute("ALTER TABLE seen RENAME TO seen_migration_backup")
            conn.execute(self._SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_notified ON seen(last_notified)")
            now = datetime.now().isoformat()
            for row in rows:
                rec = dict(zip(cols, row))
                conn.execute(
                    "INSERT OR REPLACE INTO seen (source, location, product_id, last_price,"
                    " last_discount, first_seen, last_notified, notification_count,"
                    " store_codes, product_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (self._source, self._default_location, rec.get("product_id"),
                     rec.get("last_price"), rec.get("last_discount"),
                     rec.get("first_seen") or now, rec.get("last_notified") or now,
                     rec.get("notification_count") or 1,
                     rec.get("store_codes") or "", rec.get("product_name") or ""))
            migrated = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            if migrated != total:
                raise RuntimeError("row count mismatch after migration (%d != %d)"
                                   % (migrated, total))
            conn.execute("DROP TABLE seen_migration_backup")
            conn.commit()
            log.info("SeenStore %s: migration complete", self._path)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("SeenStore %s: migration failed (%s) — keeping legacy schema",
                      self._path, exc)
            conn.rollback()
            if conn.execute("SELECT name FROM sqlite_master WHERE name='seen_migration_backup'").fetchone():
                conn.execute("DROP TABLE IF EXISTS seen")
                conn.execute("ALTER TABLE seen_migration_backup RENAME TO seen")
                conn.commit()

    def _warm_cache(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT source, location, product_id, last_price, last_discount, last_notified"
            " FROM seen").fetchall()
        self._cache = {"%s|%s|%s" % (r[0], r[1], r[2]): (r[3], r[4], r[5] or "") for r in rows}
        log.info("SeenStore[%s]: %d products cached", self._source, len(self._cache))

    def _key(self, product_id: str, location: str) -> str:
        return "%s|%s|%s" % (self._source, location or self._default_location, product_id)

    # -- decision ---------------------------------------------------------
    def should_notify(self, product_id: str, price: int, mrp: int, discount: int,
                      location: str = "", auto_clear: AutoClearPolicy | None = None,
                      brand: str = "", title: str = "") -> tuple[bool, str]:
        if price >= mrp or price <= 0:
            return False, "invalid pricing"
        key = self._key(product_id, location)
        with self._lock:
            if key not in self._cache:
                if discount < self._min_new:
                    return False, "new too low %d%% (need %d%%)" % (discount, self._min_new)
                return True, "NEW at %d%%" % discount
            old_price, old_discount, last_notified = self._cache[key]
            if discount < self._min_existing:
                verdict = False, "existing too low %d%%" % discount
            elif str(price) == old_price and discount == old_discount:
                verdict = False, "duplicate"
            else:
                change = discount - old_discount
                if change < 0:
                    verdict = False, "discount dropped %d%%->%d%%" % (old_discount, discount)
                elif change < self._min_change:
                    verdict = False, "change too small +%d%%" % change
                else:
                    verdict = True, "UP %d%%->%d%% (+%d)" % (old_discount, discount, change)
            if verdict[0]:
                return verdict
            recycle = self._auto_clear_ok(product_id, key, discount, auto_clear,
                                         brand, title, old_price, old_discount, last_notified)
            return recycle or verdict

    def _auto_clear_ok(self, product_id: str, key: str, discount: int,
                       policy: AutoClearPolicy | None, brand: str, title: str,
                       old_price: str, old_discount: int, last_notified: str) -> tuple[bool, str] | None:
        """Second chance for eligible deals whose cooldown has elapsed."""
        if not policy or not policy.enabled or discount < policy.min_discount:
            return None
        if not last_notified:
            return None
        try:
            last = datetime.fromisoformat(last_notified)
        except (ValueError, TypeError):
            return None
        elapsed = datetime.now() - last
        if elapsed < timedelta(hours=policy.hours):
            return None
        same = "same %d%%" % discount if (str(old_price) and discount == old_discount) else "%d%% (was %d%%)" % (discount, old_discount)
        hours = elapsed.total_seconds() / 3600.0
        return True, "AUTO-CLEAR %s, %.0fh since last post" % (same, hours)

    # -- persistence ------------------------------------------------------
    def save(self, product_id: str, price: int, discount: int,
             location: str = "", store_codes: list | tuple | None = None,
             product_name: str = "", brand: str = "", auto_clear: bool = False) -> None:
        now = datetime.now().isoformat()
        codes_str = ",".join(store_codes) if store_codes else ""
        loc = location or self._default_location
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT notification_count FROM seen WHERE source=? AND location=? AND product_id=?",
                    (self._source, loc, product_id)).fetchone()
                if not row:
                    self._conn.execute(
                        "INSERT INTO seen (source, location, product_id, last_price, last_discount,"
                        " first_seen, last_notified, notification_count, store_codes, product_name,"
                        " brand, auto_clear) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self._source, loc, product_id, str(price), discount, now, now, 1,
                         codes_str, product_name, brand, 1 if auto_clear else 0))
                else:
                    self._conn.execute(
                        "UPDATE seen SET last_price=?, last_discount=?, last_notified=?,"
                        " notification_count=?, store_codes=?, product_name=?, brand=?,"
                        " auto_clear=? WHERE source=? AND location=? AND product_id=?",
                        (str(price), discount, now, row[0] + 1, codes_str, product_name,
                         brand, 1 if auto_clear else 0, self._source, loc, product_id))
                self._conn.commit()
                self._cache[self._key(product_id, loc)] = (str(price), discount, now)
        except Exception as exc:
            log.error("SeenStore save error: %s", exc)

    def get_product(self, product_id: str, location: str = "") -> dict | None:
        """Fetch stored metadata for a product (used by STOCK# and buttons)."""
        loc = location or self._default_location
        with self._lock:
            row = self._conn.execute(
                "SELECT last_price, last_discount, store_codes, product_name, brand,"
                " last_notified, location, auto_clear FROM seen"
                " WHERE source=? AND location=? AND product_id=?",
                (self._source, loc, product_id)).fetchone()
            if not row:
                row = self._conn.execute(
                    "SELECT last_price, last_discount, store_codes, product_name, brand,"
                    " last_notified, location, auto_clear FROM seen"
                    " WHERE product_id=? ORDER BY last_notified DESC LIMIT 1",
                    (product_id,)).fetchone()
                if not row:
                    return None
        price, discount, codes_str, name, brand, last_notified, loc2, autoc = row
        return {
            "product_id": product_id,
            "price": price,
            "discount": discount,
            "name": name,
            "brand": brand,
            "last_seen": last_notified,
            "location": loc2 or "",
            "auto_clear": bool(autoc),
            "store_codes": [c for c in (codes_str or "").split(",") if c],
        }

    def forget(self, product_id: str, location: str = "") -> bool:
        loc = location or self._default_location
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM seen WHERE source=? AND location=? AND product_id=?",
                (self._source, loc, product_id))
            deleted = cur.rowcount
            if not deleted:
                cur = self._conn.execute("DELETE FROM seen WHERE product_id=?", (product_id,))
                deleted = cur.rowcount
            self._conn.commit()
            self._cache.pop(self._key(product_id, loc), None)
            return deleted > 0

    def clear(self) -> int:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            self._conn.execute("DELETE FROM seen")
            self._conn.commit()
            self._cache.clear()
            return total

    # -- housekeeping -----------------------------------------------------
    def prune(self, max_age_days: int) -> int:
        """Drop rows not notified within the retention window.

        Bounds volume growth for a permanently-running deployment; the churn
        means genuinely-live deals are always younger than the cutoff, while
        dead entries stop costing disk and query time.
        """
        if max_age_days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM seen WHERE last_notified < ?", (cutoff,))
            self._conn.commit()
            removed = cur.rowcount
            if removed:
                survivors = self._conn.execute(
                    "SELECT source, location, product_id, last_price, last_discount,"
                    " last_notified FROM seen").fetchall()
                self._cache = {"%s|%s|%s" % (r[0], r[1], r[2]): (r[3], r[4], r[5] or "")
                               for r in survivors}
        if removed:
            log.info("SeenStore[%s]: pruned %d rows older than %dd",
                     self._source, removed, max_age_days)
        return removed

    def vacuum(self) -> None:
        with self._lock:
            try:
                self._conn.execute("VACUUM")
            except sqlite3.Error as exc:
                log.debug("VACUUM skipped: %s", exc)

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            hot = self._conn.execute(
                "SELECT COUNT(*) FROM seen WHERE last_discount >= 70").fetchone()[0]
            notifs = self._conn.execute(
                "SELECT COALESCE(SUM(notification_count),0) FROM seen").fetchone()[0]
            recycled = self._conn.execute(
                "SELECT COUNT(*) FROM seen WHERE auto_clear=1").fetchone()[0]
        return {"tracked": total, "hot_70plus": hot, "notifications": notifs,
                "auto_clear_eligible": recycled}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __len__(self) -> int:
        return len(self._cache)

    @property
    def count(self) -> int:
        return len(self._cache)


class BlacklistStore:
    """Two rule kinds, both SQLite-backed on the volume.

    * brand rules  — ``skip <brand> unless discount >= min_discount``
    * product rules — ``skip this (source, product_id, price)``, added from the
      deal message's "Blacklist Product" button. Deliberately priced: blocking
      a 20% fridge must not silence the same fridge at 80%.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._rules: dict[str, int] = {}
        self._products: set[tuple[str, str, int]] = set()
        conn = self._connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS blacklist (
            brand TEXT PRIMARY KEY, min_discount INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS product_blocks (
            source TEXT NOT NULL DEFAULT '', product_id TEXT NOT NULL,
            price INTEGER NOT NULL, brand TEXT DEFAULT '', title TEXT DEFAULT '',
            added TEXT DEFAULT '', PRIMARY KEY (source, product_id, price))""")
        conn.commit()
        for brand, pct in conn.execute("SELECT brand, min_discount FROM blacklist"):
            self._rules[brand] = pct
        for src, pid, price in conn.execute(
                "SELECT source, product_id, price FROM product_blocks"):
            self._products.add((src or "", pid, int(price)))
        conn.close()
        log.info("BlacklistStore: %d brand rules | %d product blocks",
                 len(self._rules), len(self._products))

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._path), check_same_thread=False)

    # -- brand rules ------------------------------------------------------
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

    # -- product rules ----------------------------------------------------
    def add_product(self, product_id: str, price: int, source: str = "",
                    brand: str = "", title: str = "") -> None:
        key = (source or "", product_id, int(price))
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO product_blocks VALUES (?,?,?,?,?,?)",
                (key[0], product_id, int(price), brand, title,
                 datetime.now().isoformat()))
            conn.commit()
            conn.close()
            self._products.add(key)

    def remove_product(self, product_id: str, price: int, source: str = "") -> bool:
        key = (source or "", product_id, int(price))
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM product_blocks WHERE source=? AND product_id=? AND price=?",
                (key[0], product_id, int(price)))
            conn.commit()
            conn.close()
            removed = key in self._products or cur.rowcount > 0
            self._products.discard(key)
            return removed

    def is_product_blocked(self, product_id: str, price: int, source: str = "") -> bool:
        return (source or "", product_id, int(price)) in self._products

    def product_blocks(self) -> list[dict]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT source, product_id, price, brand, title, added"
                " FROM product_blocks ORDER BY added DESC").fetchall()
            conn.close()
        return [{"source": r[0], "product_id": r[1], "price": r[2],
                 "brand": r[3], "title": r[4], "added": r[5]} for r in rows]
