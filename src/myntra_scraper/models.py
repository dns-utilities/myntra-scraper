"""Domain models: Product + discount routing tiers."""

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

    def affiliate_url(self, params: dict) -> str:
        base = "https://www.myntra.com/%s" % self.product_id
        return "%s?%s" % (base, urlencode(params))

    def format_message(self, affiliate_params: dict) -> str:
        emoji = "🔥💎" if self.discount >= 90 else "🔥" if self.discount >= 70 else "✨"
        return "\n".join([
            "%s %d%% | Rs. %d | Rs. %d" % (emoji, self.discount, self.price, self.mrp),
            "%s %s" % (self.brand, self.title),
            self.affiliate_url(affiliate_params),
        ])
