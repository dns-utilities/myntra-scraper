"""Build config/auto_clear_brands.json from the curated brand registry.

Only brands with real deal evidence (mentions > 0) are kept: they are the
ones worth re-posting on the auto-clear cycle. "Reputed but no deals seen
yet" entries stay out until a corpus shows evidence.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT.parent / "research" / "Brands Analysis" / "data" / "brands-registry.json"
OUT = ROOT / "config" / "auto_clear_brands.json"


def main() -> None:
    data = json.loads(SRC.read_text(encoding="utf-8"))
    brands, total = [], 0
    for cat, entries in data["categories"].items():
        for b in entries:
            total += 1
            mentions = int(b.get("mentions") or 0)
            if mentions <= 0:
                continue
            aliases = [a for a in (b.get("aliases") or []) if a]
            if not aliases:
                aliases = [b["name"].lower()]
            brands.append({
                "name": b["name"],
                "mentions": mentions,
                "category": cat,
                "aliases": aliases,
            })
    brands.sort(key=lambda x: -x["mentions"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": data.get("generated", ""),
        "source": "research/Brands Analysis/data/brands-registry.json",
        "rule": "brands with real deal evidence (mentions > 0); reputed-but-unseen excluded",
        "brand_count": len(brands),
        "registry_brand_count": total,
        "brands": brands,
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"registry={total} auto_clear={len(brands)} -> {OUT}")


if __name__ == "__main__":
    main()
