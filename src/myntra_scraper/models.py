"""Domain models: Product + discount routing tiers + deal link builder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from urllib.parse import urlencode


class DiscountTier(IntEnum):
    STANDARD = 0
    HOT = 70
    JACKPOT = 90

    @classmethod
    def for_discount(cls, discount: int) -> "DiscountTier":
        if discount >= cls.JACKPOT:
            return cls.JACKPOT
        if discount >= cls.HOT:
            return cls.HOT
        return cls.STANDARD


# Badge ladder for deal headlines: 95+ 💎✨ / 90+ 🔥 / 80+ 🚨 / else plain.
def deal_badge(discount: int) -> str:
    if discount >= 95:
        return "💎✨"
    if discount >= 90:
        return "🔥"
    if discount >= 80:
        return "🚨"
    return ""


def clean_product_url(url_or_id: str) -> str:
    """Strip EVERYTHING after the product id.

    Handles slugs (`/action-men-cool-1003-slides/ABCDEF123456?pid=...`),
    `/buy/<id>`, bare ids, and any query (?pid= / &... / utm_ / affiliate).
    Always returns `https://www.myntra.com/<PRODUCT_ID>`.
    """
    raw = (url_or_id or "").strip()
    # Strip query/fragment first so trailing junk can't pollute the match.
    no_q = re.split(r"[?#&]", raw, maxsplit=1)[0].rstrip("/")
    tail = no_q.rsplit("/", 1)[-1]
    if re.fullmatch(r"[A-Z0-9]{10,25}|\d{6,10}", tail, re.I):
        return "https://www.myntra.com/%s" % tail.upper()
    match = re.search(r"myntra\.com/(?:buy/)?([A-Z0-9]{10,25}|\d{6,10})",
                      no_q, re.I)
    if match:
        return "https://www.myntra.com/%s" % match.group(1).upper()
    if re.fullmatch(r"[A-Z0-9]{10,25}|\d{6,10}", raw, re.I):
        return "https://www.myntra.com/%s" % raw.upper()
    return no_q


def build_deal_link(product_id: str, tag: str = "") -> str:
    """Clean link, optionally tagged.

    tag == ""  -> plain `https://www.myntra.com/<ID>` (default, OFF).
    tag != ""  -> `https://www.myntra.com/<ID>?<tag>` where tag is the raw
                  query the owner supplies later (e.g. `utm_source=...`).
    """
    base = "https://www.myntra.com/%s" % (product_id or "").strip().upper()
    tag = (tag or "").strip().lstrip("?&")
    return "%s?%s" % (base, tag) if tag else base


@dataclass
class Product:
    product_id: str
    brand: str
    title: str
    price: int
    mrp: int
    discount: int
    url: str = ""
    image: str = ""
    rating: float = 0.0

    @property
    def tier(self) -> DiscountTier:
        return DiscountTier.for_discount(self.discount)

    @staticmethod
    def calculate_discount(price: int, mrp: int) -> int:
        if mrp <= 0 or price <= 0 or price > mrp:
            return 0
        return round(((mrp - price) / mrp) * 100)

    @classmethod
    def from_api_item(cls, item: dict) -> "Product | None":
        try:
            price = int(item.get("price", 0))
            mrp = int(item.get("mrp", 0))
            discount = cls.calculate_discount(price, mrp)
            label = item.get("discountDisplayLabel", "")
            if label:
                match = re.search(r"(\d+)%\s*OFF", label, re.I)
                if match:
                    discount = int(match.group(1))
            landing = item.get("landingPageUrl", "")
            product_id = str(item.get("productId", ""))
            url = "https://www.myntra.com/%s" % (landing or product_id)
            if not product_id or price <= 0:
                return None
            return cls(
                product_id=product_id,
                brand=str(item.get("brand", "Unknown")),
                title=str(item.get("productName", "")),
                price=price,
                mrp=mrp,
                discount=discount,
                url=url,
                image=str(item.get("searchImage", "")),
                rating=float(item.get("rating", 0) or 0),
            )
        except (ValueError, TypeError, AttributeError):
            return None

    def affiliate_url(self, params: dict | str | None = None) -> str:
        # New path: plain string tag ("" = OFF). Dict only for back-compat.
        if isinstance(params, str):
            return build_deal_link(self.product_id, params)
        if params:
            return "%s?%s" % (clean_product_url(self.product_id),
                              urlencode(params))
        return build_deal_link(self.product_id, "")

    def format_message(self, affiliate_params: dict | str | None = None) -> str:
        tag = affiliate_params if isinstance(affiliate_params, str) else ""
        badge = deal_badge(self.discount)
        head = "%s %d%% Off - %s %s" % (badge, self.discount,
                                       self.brand, self.title)
        return "\n".join([
            head.strip(),
            "",
            "💰 Price: ₹%d (MRP: ₹%d)" % (self.price, self.mrp),
            "",
            build_deal_link(self.product_id, tag),
        ])
