"""Deal-message buttons: inline keyboard, message parser, and the two-step
brand-blacklist conversation.

Every posted deal carries two buttons:

* **Blacklist Product** — one tap, blocks that exact ``(source, product_id,
  price)``. Priced, not blanket: the same product at a *lower* price still
  alerts, and so does a later price drop. This is the escape hatch for "I never
  want to see this 20% fridge again" without silencing the brand.
* **Blacklist Brand** — asks for a percentage, then confirms. Brand is resolved
  from the stored brand field, falling back to the deal message headline and
  finally the first word of the title, so a wrong guess is visible before it is
  committed.

Callback data is tiny on purpose (Telegram caps it at 64 bytes):
``blk|p|<source>|<product_id>|<price>`` and ``blk|b|<same>``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

log = logging.getLogger("myntra_scraper.deal_buttons")

CALLBACK_PREFIX = "blk"
# Telegram id used when a channel admin acts as the channel itself.
ANONYMOUS_ADMIN_ID = 1087968824
PROMPT_TTL_SECONDS = 600


# -- keyboard ---------------------------------------------------------------
def deal_keyboard(product_id: str, price: int, source: str = "") -> dict:
    """Inline keyboard attached to every deal message."""
    tail = "%s|%s|%s" % (source, product_id, price)
    return {"inline_keyboard": [[
        {"text": "🚫 Blacklist Product", "callback_data": "%s|p|%s" % (CALLBACK_PREFIX, tail)},
        {"text": "🏷️ Blacklist Brand", "callback_data": "%s|b|%s" % (CALLBACK_PREFIX, tail)},
    ]]}


def confirm_keyboard(pct: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Confirm %d%%" % pct, "callback_data": "%s|c|%d" % (CALLBACK_PREFIX, pct)},
        {"text": "❌ Cancel", "callback_data": "%s|x|0" % CALLBACK_PREFIX},
    ]]}


def undo_keyboard(source: str, product_id: str, price: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "↩️ Undo block", "callback_data": "%s|u|%s|%s|%s" % (
            CALLBACK_PREFIX, source, product_id, price)},
    ]]}


# -- message parsing --------------------------------------------------------
_DISCOUNT_RE = re.compile(r"(\d{1,3})%\s*Off\s*-\s*(.*)")
_PRICE_RE = re.compile(r"₹\s*([\d,]+)\s*\(MRP:\s*₹\s*([\d,]+)\)")
_URL_RE = re.compile(r"https?://\S+")
_APP_RE = re.compile(r"^App:\s*(.+)$", re.M)
_PID_RES = (re.compile(r"[?&]pid=([A-Za-z0-9]{4,25})", re.I),
            re.compile(r"myntra\.com/(?:buy/)?([A-Za-z0-9]{5,25})", re.I),
            re.compile(r"croma\.com/\S*?/(\d{5,12})", re.I))


def _int(raw: str) -> int:
    try:
        return int(re.sub(r"[^\d]", "", raw or ""))
    except ValueError:
        return 0


def extract_product_id(text: str) -> str:
    """Pull a product id out of a deal message (legacy `CLEAR#` helper too)."""
    if not text:
        return ""
    for pattern in _PID_RES:
        match = pattern.search(text)
        if match:
            return match.group(1).upper()
    return ""


def parse_deal_message(text: str) -> dict:
    """Recover the structured fields from a posted deal message."""
    out = {"brand": "", "title": "", "discount": 0, "price": 0, "mrp": 0,
           "url": "", "app": "", "product_id": "", "raw": text or ""}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not out["discount"]:
            m = _DISCOUNT_RE.search(line)
            if m:
                out["discount"] = int(m.group(1))
                rest = m.group(2).strip()
                # "<brand> <title>" — split on the first space when a brand is
                # present, else treat the whole thing as the title.
                if " " in rest:
                    out["brand"], out["title"] = rest.split(" ", 1)
                else:
                    out["title"] = rest
                continue
        if not out["price"]:
            m = _PRICE_RE.search(line)
            if m:
                out["price"] = _int(m.group(1))
                out["mrp"] = _int(m.group(2))
                continue
        if not out["url"]:
            m = _URL_RE.search(line)
            if m:
                out["url"] = m.group(0)
                continue
        m = _APP_RE.match(line)
        if m and not out["app"]:
            out["app"] = m.group(1).strip()
    if not out["product_id"]:
        out["product_id"] = extract_product_id(text)
    return out


class DealButtonHandler:
    """Owns the button conversation state and the blacklist side effects."""

    def __init__(self, blacklist, seen_stores: dict[str, object],
                 admin_chat_id: int, deal_chats: set[int],
                 send: Callable[[str, int | None, dict | None], None],
                 answer: Callable[[str, str], None]) -> None:
        self._blacklist = blacklist
        self._seen = seen_stores
        self._admin = admin_chat_id
        self._deal_chats = deal_chats
        self._send = send
        self._answer = answer
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()

    # -- auth --------------------------------------------------------------
    def _authorized(self, from_id: int, chat_id: int) -> bool:
        if from_id == self._admin:
            return True
        # Acting as the channel itself: only trusted inside a deal channel.
        return from_id == ANONYMOUS_ADMIN_ID and chat_id in self._deal_chats

    # -- entry points ------------------------------------------------------
    def handle_callback(self, cq: dict) -> None:
        try:
            data = cq.get("data") or ""
            if not data.startswith(CALLBACK_PREFIX + "|"):
                self._answer(cq.get("id", ""), "Unknown action")
                return
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id", 0)
            frm = cq.get("from") or {}
            from_id = frm.get("id", 0)
            if not self._authorized(from_id, chat_id):
                log.warning("Deal button from unauthorized user %s in chat %s",
                            from_id, chat_id)
                self._answer(cq.get("id", ""), "Not authorized")
                return
            parts = data.split("|")
            action = parts[1] if len(parts) > 1 else ""
            if action == "p":
                self._block_product(cq, parts)
            elif action == "b":
                self._start_brand_prompt(cq, parts)
            elif action == "c":
                self._confirm_brand(cq, parts)
            elif action == "u":
                self._undo_block(cq, parts)
            elif action == "x":
                self._cancel(cq)
            else:
                self._answer(cq.get("id", ""), "Unknown action")
        except Exception as exc:  # never let a button kill the poll loop
            log.error("Deal button handler error: %s", exc, exc_info=True)
            self._answer(cq.get("id", ""), "Button failed: %s" % exc)

    def handle_text(self, text: str, chat_id: int) -> str | None:
        """Consume a reply belonging to an open button conversation."""
        with self._lock:
            state = self._pending.get(chat_id)
        if not state:
            return None
        if time.time() - state.get("ts", 0) > PROMPT_TTL_SECONDS:
            with self._lock:
                self._pending.pop(chat_id, None)
            return "Prompt expired — press the button again."
        low = text.strip().lower()
        if low in ("/cancel", "cancel", "/abort", "abort", "no"):
            with self._lock:
                self._pending.pop(chat_id, None)
            return "Cancelled."
        stage = state.get("stage")
        if stage == "brand_pct":
            match = re.fullmatch(r"\s*(\d{1,3})\s*%?\s*", text)
            if not match:
                return "Send a number 0–100 (or /cancel)."
            pct = int(match.group(1))
            if pct > 100:
                return "0–100 only (or /cancel)."
            with self._lock:
                state["stage"] = "brand_confirm"
                state["pct"] = pct
                state["ts"] = time.time()
            self._send(
                "🏷️ Blacklist *%s* below *%d%%*?\n\n"
                "Deals for this brand will only post at %d%% or deeper."
                % (state["brand"], pct, pct),
                chat_id, confirm_keyboard(pct))
            return None
        return "Press ✅ Confirm or ❌ Cancel above (or /cancel)."

    def pending_prompt(self, chat_id: int) -> bool:
        with self._lock:
            return chat_id in self._pending

    # -- actions -----------------------------------------------------------
    def _meta(self, source: str, product_id: str, message_text: str) -> dict:
        """Best available brand/title for a product: stored row, else message."""
        store = self._seen.get(source)
        if store is not None:
            try:
                row = store.get_product(product_id)
            except Exception as exc:
                log.debug("seen lookup failed for %s: %s", product_id, exc)
                row = None
            if row:
                return {"brand": row.get("brand") or "", "title": row.get("name") or "",
                        "price": row.get("price"), "source": source,
                        "product_id": product_id}
        parsed = parse_deal_message(message_text)
        parsed.setdefault("source", source)
        parsed["product_id"] = parsed.get("product_id") or product_id
        return parsed

    def _resolve_brand(self, meta: dict) -> str:
        brand = (meta.get("brand") or "").strip()
        if brand and brand.lower() != "unknown":
            return brand
        title = (meta.get("title") or "").strip()
        if title:
            return title.split(" ", 1)[0]
        return "Unknown"

    def _block_product(self, cq: dict, parts: list[str]) -> None:
        _, _, source, product_id, price_s = (parts + ["", "", "", "", ""])[:5]
        try:
            price = int(price_s)
        except ValueError:
            self._answer(cq.get("id", ""), "Malformed button (old message?)")
            return
        meta = self._meta(source, product_id, (cq.get("message") or {}).get("text", ""))
        brand = self._resolve_brand(meta)
        self._blacklist.add_product(product_id, price, source=source,
                                    brand=brand, title=meta.get("title", ""))
        self._answer(cq.get("id", ""), "Blocked %s at ₹%d" % (brand or product_id, price))
        self._send("🚫 Blocked *%s* at ₹%d\n%s\n\n"
                   "Only this exact price is blocked — a lower price will still alert."
                   % (brand or product_id, price, meta.get("title", "") or product_id),
                   None, undo_keyboard(source, product_id, price))

    def _undo_block(self, cq: dict, parts: list[str]) -> None:
        _, _, source, product_id, price_s = (parts + ["", "", "", "", ""])[:5]
        try:
            price = int(price_s)
        except ValueError:
            self._answer(cq.get("id", ""), "Malformed button")
            return
        ok = self._blacklist.remove_product(product_id, price, source=source)
        self._answer(cq.get("id", ""),
                     "Unblocked" if ok else "Nothing to undo")
        if ok:
            self._send("↩️ Unblocked %s at ₹%d" % (product_id, price))

    def _start_brand_prompt(self, cq: dict, parts: list[str]) -> None:
        _, _, source, product_id, price_s = (parts + ["", "", "", "", ""])[:5]
        meta = self._meta(source, product_id, (cq.get("message") or {}).get("text", ""))
        brand = self._resolve_brand(meta)
        with self._lock:
            self._pending[self._admin] = {
                "stage": "brand_pct", "source": source, "product_id": product_id,
                "price": price_s, "brand": brand, "title": meta.get("title", ""),
                "ts": time.time(),
            }
        self._answer(cq.get("id", ""), "Enter a discount %% for %s" % brand)
        self._send("🏷️ Blacklist brand *%s*\n\n"
                   "Reply with the minimum discount you still want to see "
                   "(0–100), e.g. `60`.\n/cancel to abort." % brand)

    def _confirm_brand(self, cq: dict, parts: list[str]) -> None:
        with self._lock:
            state = self._pending.pop(self._admin, None)
        if not state:
            self._answer(cq.get("id", ""), "Nothing to confirm")
            return
        try:
            pct = int(parts[2])
        except (IndexError, ValueError):
            self._answer(cq.get("id", ""), "Malformed button")
            return
        brand = state.get("brand", "")
        if not brand or brand == "Unknown":
            self._answer(cq.get("id", ""), "No brand to block")
            return
        self._blacklist.add(brand, pct)
        self._answer(cq.get("id", ""), "Blacklisted %s below %d%%" % (brand, pct))
        self._send("🏷️ *%s* blacklisted below *%d%%*\n\n"
                   "Source: %s · %s\n"
                   "Remove with /remove_blacklist %s"
                   % (brand, pct, state.get("source", ""),
                      state.get("title", "") or state.get("product_id", ""),
                      brand.lower()))

    def _cancel(self, cq: dict) -> None:
        with self._lock:
            state = self._pending.pop(self._admin, None)
        self._answer(cq.get("id", ""), "Cancelled")
        if state:
            self._send("Cancelled — no change made.")
