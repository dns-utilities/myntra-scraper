"""Domain models: Product + discount routing tiers + deal link builder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from urllib.parse import urlencode


class DiscountTier(IntEnum):
    """Mutually exclusive discount bands. Each value *is* the band's floor.

    A deal belongs to exactly one band, the half-open interval
    ``[floor, next_floor)``: 79% is ``TIER_70`` and 80% is ``TIER_80``. Members
    are named for their floor rather than "70plus", which would be a lie — the
    band stops at 79. ``BASE`` is "below every tier" and routes to the source's
    own base feed.
    """

    BASE = 0
    TIER_70 = 70
    TIER_80 = 80
    TIER_90 = 90

    @classmethod
    def for_discount(cls, discount: int) -> "DiscountTier":
        """The band ``discount`` falls in. Adding a tier needs no edit here."""
        band = cls.BASE
        for tier in cls:          # definition order = floors ascending
            if discount >= tier:
                band = tier
        return band


# Badge ladder for deal headlines: 95+ 💎✨ / 90+ 🔥 / 80+ 🚨 / else plain.
def deal_badge(discount: int) -> str:
    if discount >= 95:
        return "💎✨"
    if discount >= 90:
        return "🔥"
    if discount >= 80:
        return "🚨"
    return ""


# Human label per source. Deal messages carry this as a trailing "App:" line
# instead of emoji branding at the top of the message.
SOURCE_LABELS = {"myntra": "Myntra", "croma": "Croma",
                 "blinkit": "Blinkit", "bigbasket": "BigBasket"}

# Sources whose Product.url is already the canonical product link and for which
# no affiliate tag scheme applies: the link is used verbatim. Everything else
# goes through build_deal_link().
LINK_AS_IS = frozenset({"croma", "blinkit", "bigbasket"})

# Shared text scrapers for the q-commerce payloads, whose numbers arrive as
# display strings ("₹210", "20%\nOFF") rather than integers.
_MONEY_RE = re.compile(r"\d[\d,]*\.?\d*")
_PCT_RE = re.compile(r"(\d+)\s*%")


def parse_money(text) -> int:
    """'₹210' / 'Rs. 210' / '210.5' -> 210. 0 when unparseable."""
    if text is None:
        return 0
    match = _MONEY_RE.search(str(text))
    if not match:
        return 0
    try:
        return int(round(float(match.group(0).replace(",", ""))))
    except ValueError:
        return 0


def parse_percent(text) -> int:
    """'20%\\nOFF' / '20% OFF' -> 20. 0 when absent."""
    if not text:
        return 0
    match = _PCT_RE.search(str(text))
    return int(match.group(1)) if match else 0


def node_text(node) -> str:
    """Blinkit fields are sometimes ``{'text': ...}``, sometimes a raw string."""
    if isinstance(node, dict):
        return str(node.get("text", "") or "")
    return str(node or "")


def strip_brand_prefix(name: str, brand: str) -> str:
    """Drop a leading brand from a product name.

    Q-commerce payloads return the brand *inside* the product name ("Amul
    Butter", brand "Amul"), and the deal headline already prints the brand, so
    leaving it produces "Amul Amul Butter". Only a leading, whole-word match is
    removed, and only when something meaningful remains.
    """
    text = (name or "").strip()
    brand = (brand or "").strip()
    if not text or not brand or brand.lower() == "unknown":
        return text
    if text.lower().startswith(brand.lower()):
        rest = text[len(brand):].lstrip(" -–—:,.")
        if rest:
            return rest
    return text


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
    source: str = "myntra"          # "myntra" | "croma" — drives link building + labels
    store_codes: tuple = ()          # Croma only: SA-XXXX codes for STOCK#
    # Delivery scope the price was observed at. Empty for sources with a single
    # national price; a pincode/store id for location-scoped sources (q-commerce
    # prices per dark store, so the same SKU is a different "product" per place).
    location: str = ""
    # Display form of `location` for the footer. A raw lat/lon or ec:sa key is
    # precise but unreadable, so a label is carried alongside; falls back to
    # `location` when empty.
    location_label: str = ""

    @property
    def tier(self) -> DiscountTier:
        return DiscountTier.for_discount(self.discount)

    @property
    def app_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source.replace("_", " ").title())

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
        head = "%s %d%% Off - %s %s" % (
            badge, self.discount, self.brand, self.title)
        # Sources with a real product URL link straight there (no tag scheme).
        link = self.url if self.source in LINK_AS_IS and self.url else build_deal_link(self.product_id, tag)
        # Source identity lives at the bottom, never in the headline.
        place = self.location_label or self.location
        app = self.app_label + (" (%s)" % place if place else "")
        return "\n".join([
            head.strip(),
            "",
            "💰 Price: ₹%d (MRP: ₹%d)" % (self.price, self.mrp),
            "",
            link,
            "",
            "App: %s" % app,
        ])

    @classmethod
    def from_croma_item(cls, item: dict) -> "Product | None":
        """Parse one Croma category-API product into a Product (or None).

        Two Croma-specific quirks handled here:
        - `discountValue` is often "0%" (a placeholder for store-priced deals),
          so discount is computed from price vs mrp (what Croma displays), with
          discountValue only as a fallback.
        - empty `stockFlag` does NOT mean out-of-stock (online-delivery items
          have none); we only skip on an explicit outOfStock flag.
        """
        try:
            code = str(item.get("code", ""))
            name = str(item.get("name", ""))
            price_obj = item.get("price", {}) or {}
            mrp_obj = item.get("mrp", {}) or {}
            price = int(price_obj.get("value", 0) or 0)
            mrp = int(mrp_obj.get("value", 0) or 0)
            if not price:
                pstr = str(price_obj.get("formattedValue", "0")).replace("₹", "").replace(",", "").strip()
                price = int(float(pstr)) if pstr else 0
            if not mrp:
                mstr = str(mrp_obj.get("formattedValue", "0")).replace("₹", "").replace(",", "").strip()
                mrp = int(float(mstr)) if mstr else price

            discount = cls.calculate_discount(price, mrp)
            if discount == 0:
                raw = item.get("discountValue", 0)
                if raw and str(raw).replace("%", "").strip().isdigit():
                    discount = int(str(raw).replace("%", "").strip())

            url = item.get("url", "") or ""
            if url and not url.startswith("http"):
                url = "https://www.croma.com%s" % url

            if not code or not name or price <= 0:
                return None

            # Explicit out-of-stock → skip. Empty stockFlag is fine (online buy).
            stock_obj = item.get("stock", {}) or {}
            stock_status = stock_obj.get("stockLevelStatus", "") if isinstance(stock_obj, dict) else ""
            if stock_status and stock_status == "outOfStock":
                return None

            store_codes = item.get("stockFlag", []) or []
            if not isinstance(store_codes, list):
                store_codes = []

            return cls(
                product_id=code,
                brand=str(item.get("brandName", item.get("manufacturer", "")) or ""),
                title=name,
                price=price,
                mrp=mrp,
                discount=discount,
                url=url,
                image=str(item.get("plpImage", "") or ""),
                source="croma",
                store_codes=tuple(store_codes),
            )
        except (ValueError, TypeError, AttributeError):
            return None

    @classmethod
    def from_blinkit_item(cls, card: dict, location: str = "",
                          location_label: str = "") -> "Product | None":
        """Parse one Blinkit product card.

        ``card`` is a raw ``product_card_snippet_type_2`` payload, already
        flattened out of the snippet envelope by ``blinkit_api.iter_cards``.
        Two drops happen here: sold-out cards (a price error nobody can buy is
        not a deal) and anything without a usable price.
        """
        if not isinstance(card, dict):
            return None
        try:
            pid = card.get("product_id") or (card.get("identity") or {}).get("id")
            name = node_text(card.get("name"))
            if not pid or not name:
                return None
            if card.get("is_sold_out"):
                return None
            price = parse_money(node_text(card.get("normal_price")))
            if price <= 0:
                return None
            mrp = parse_money(node_text(card.get("mrp"))) or price
            offer = card.get("offer_tag")
            discount = parse_percent(
                node_text(offer.get("title")) if isinstance(offer, dict) else offer)
            if not discount:
                discount = cls.calculate_discount(price, mrp)
            variant = node_text(card.get("variant"))
            brand = node_text(card.get("brand_name")) or "Unknown"
            title = ("%s %s" % (name, variant)).strip() if variant else name
            title = strip_brand_prefix(title, brand)
            # The image node is ``{"url": ...}`` (not ``{"text": ...}``), so it
            # needs its own unwrap rather than node_text().
            image = card.get("image")
            if isinstance(image, dict):
                image = image.get("url") or image.get("text") or ""
            image = str(image or card.get("image_url") or "")
            return cls(
                product_id=str(pid),
                brand=brand,
                title=title[:200],
                price=price,
                mrp=mrp,
                discount=discount,
                url="https://blinkit.com/prn/x/prid/%s" % pid,
                image=image,
                source="blinkit",
                location=location,
                location_label=location_label,
            )
        except (ValueError, TypeError, AttributeError):
            return None

    @classmethod
    def from_bigbasket_item(cls, item: dict, location: str = "",
                            location_label: str = "") -> "Product | None":
        """Parse one BigBasket listing row.

        BigBasket's stock signal is stricter than "carries a price": a row is
        buyable only when ``avail_status == 001``, the selling price is > 0,
        and the call to action is an add-to-cart. Out-of-stock rows routinely
        keep stale prices, so every one of those is checked before trusting it.
        """
        if not isinstance(item, dict):
            return None
        try:
            pid = str(item.get("id") or "")
            name = str(item.get("desc") or "")
            if not pid or not name:
                return None
            pricing = item.get("pricing") or {}
            discount_block = pricing.get("discount") or {}
            primary = discount_block.get("prim_price") or {}
            price = parse_money(primary.get("sp") or primary.get("base_price"))
            if price <= 0:
                return None
            mrp = parse_money(discount_block.get("mrp")) or price
            availability = item.get("availability") or {}
            status = str(availability.get("avail_status", "")).strip()
            button = str(availability.get("button", "")).strip().lower()
            if (status != "001"
                    or availability.get("display_sp") is False
                    or availability.get("not_for_sale")
                    or availability.get("is_out_of_stock")
                    or button in ("notify me", "notify", "sold out", "out of stock")):
                return None
            discount = cls.calculate_discount(price, mrp)
            brand = item.get("brand")
            brand = brand.get("name") if isinstance(brand, dict) else (brand or "")
            pack = str(item.get("w") or item.get("pack_desc") or "")
            url = str(item.get("absolute_url") or item.get("url") or "").strip()
            if url.startswith("/"):
                url = "https://www.bigbasket.com%s" % url
            if not url.startswith("http"):
                # The link is the point of the post, so never ship a deal
                # without one: /pd/<id>/ always resolves.
                url = "https://www.bigbasket.com/pd/%s/" % pid
            title = ("%s %s" % (name, pack)).strip() if pack else name
            title = strip_brand_prefix(title, brand or "Unknown")
            return cls(
                product_id=pid,
                brand=brand or "Unknown",
                title=title[:200],
                price=price,
                mrp=mrp,
                discount=discount,
                url=url,
                image=str(item.get("image") or ""),
                source="bigbasket",
                location=location,
                location_label=location_label,
            )
        except (ValueError, TypeError, AttributeError):
            return None
