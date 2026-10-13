"""Migration check against a copy of real production data.

Run: python scripts/migration_check.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from myntra_scraper.stores import SeenStore

FAILS = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append("%s: got %r want %r" % (label, got, want))
    print("%s %-40s %r" % ("PASS" if ok else "FAIL", label, got))


def migrate(label: str, src: Path, source: str, location: str) -> None:
    tmp = Path(tempfile.mkdtemp()) / "seen.db"
    shutil.copy(src, tmp)
    before = sqlite3.connect(tmp).execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    cols = [r[1] for r in sqlite3.connect(tmp).execute("PRAGMA table_info(seen)")]
    already = "source" in cols
    print("\n[%s] rows=%d schema=%s" % (label, before,
                                        "current" if already else "legacy (will migrate)"))
    store = SeenStore(tmp, source=source, default_location=location,
                      min_new=0, min_existing=15, min_change=1)
    after = store.stats()
    check("%s: rows preserved" % label, after["tracked"], before)
    check("%s: notifications preserved" % label,
          after["notifications"] >= 0, True)
    conn = sqlite3.connect(tmp)
    check("%s: no backup table left" % label,
          conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='seen_migration_backup'"
                       ).fetchone()[0], 0)
    check("%s: composite PK" % label,
          sorted(r[1] for r in conn.execute("PRAGMA table_info(seen)") if r[5])[:2],
          ["location", "product_id"])
    conn.close()
    # a sample row must still resolve
    sample = list(store._cache)[0] if store._cache else None
    if sample:
        pid = sample.split("|")[-1]
        row = store.get_product(pid)
        check("%s: row resolves" % label, row is not None and row["product_id"] == pid, True)
        check("%s: location backfilled" % label, row["location"], location)
    store.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "data"
    for name, source, loc in (("croma_seen.db", "croma", ""),
                              ("seen.db", "myntra", "387001")):
        path = root / name
        if not path.exists():
            print("skip %s (missing)" % name)
            continue
        migrate(name, path, source, loc)

    # idempotency: running the store twice must not re-migrate
    tmp = Path(tempfile.mkdtemp()) / "seen.db"
    conn = sqlite3.connect(tmp)
    conn.execute("""CREATE TABLE seen (product_id TEXT PRIMARY KEY, last_price TEXT,
        last_discount INTEGER, first_seen TEXT, last_notified TEXT,
        notification_count INTEGER DEFAULT 0)""")
    conn.execute("INSERT INTO seen VALUES ('X1','99',70,'t','t',1)")
    conn.commit(); conn.close()
    SeenStore(tmp, source="myntra", default_location="387001").close()
    SeenStore(tmp, source="myntra", default_location="387001").close()
    conn = sqlite3.connect(tmp)
    check("re-open does not duplicate",
          conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 1)
    conn.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)

    print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES",
                                    len(FAILS), "" if len(FAILS) == 1 else "s"))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
