"""Deal-message buttons: inline keyboard, message parser, and the interactive
brand-blacklist conversation.

Every posted deal carries two buttons:

* **Blacklist Product** — one tap, blocks that exact ``(source, product_id,
  price)``. Priced, not blanket: the same product at a *lower* price still
  alerts, and so does a later price drop. This is the escape hatch for "I never
  want to see this 20% fridge again" without silencing the brand.
* **Blacklist Brand** — one tap opens an *in-place* confirmation on the same
  message: ``✅ Block "brand"`` / ``✏️ Custom`` / ``❌ Cancel``.

The old flow asked you to reply with a percentage in the bot DM and then press a
Confirm button that never changed anything. The buttons here edit the message
they live on (``editMessageReplyMarkup``), so the keyboard visibly changes and
the whole confirmation happens where the deal was posted. The only time the bot
DM is used is the optional *Custom* path, where a wrong brand name has to be
typed — and there is no way to type a message in a channel.

Pressing **Confirm** blocks the brand outright (every deal, all depths).
Pressing **Custom** asks for a brand in the DM and accepts either ``arry``
(block everything) or ``arry,99`` (only post when the discount is 99% or
deeper), so a wrong auto-detected name becomes a precise rule instead of a
blanket ban.

Callback data is tiny on purpose (Telegram caps it at 64 bytes). The confirm /
custom / cancel / undo-brand steps carry no payload at all — the brand and the
product live server-side in ``_pending`` keyed by ``(chat_id, message_id)``,
which the callback's own ``message`` field always provides.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

log = logging.getLogger("dns_scraper.deal_buttons")

CALLBACK_PREFIX = "blk"
# Telegram id used when a channel admin acts as the channel itself.
ANONYMOUS_ADMIN_ID = 1087968824
PROMPT_TTL_SECONDS = 600

# Action codes inside the ``blk|<action>|...`` callback payload.
ACT_PRODUCT = "p"     # block this product at this price
ACT_BRAND = "b"       # open the brand confirmation
ACT_CONFIRM = "c"     # confirm the brand block (full blacklist)
ACT_CUSTOM = "e"      # ask for a hand-typed brand name in the DM
ACT_CANCEL = "x"      # abort the conversation
ACT_UNDO_PRODUCT = "u"
ACT_UNDO_BRAND = "r"
ACT_NOOP = "n"        # "blacklisted" pill — nothing to do when tapped


# -- keyboards --------------------------------------------------------------
def deal_keyboard(product_id: str, price: int, source: str = "") -> dict:
    """Inline keyboard attached to every deal message."""
    tail = "%s|%s|%s" % (source, product_id, price)
    return {"inline_keyboard": [[
        {"text": "🚫 Blacklist Product",
         "callback_data": "%s|%s|%s" % (CALLBACK_PREFIX, ACT_PRODUCT, tail)},
        {"text": "🏷️ Blacklist Brand",
         "callback_data": "%s|%s|%s" % (CALLBACK_PREFIX, ACT_BRAND, tail)},
    ]]}


def _clip(text: str, limit: int = 58) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def brand_confirm_keyboard(brand: str) -> dict:
    """``Block "brand"?`` — confirm outright, type a custom one, or cancel."""
    return {"inline_keyboard": [[
        {"text": _clip('✅ Block "%s"' % brand),
         "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_CONFIRM)},
        {"text": "✏️ Custom",
         "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_CUSTOM)},
        {"text": "❌ Cancel",
         "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_CANCEL)},
    ]]}


def brand_done_keyboard(brand: str) -> dict:
    """Replaces the confirmation once the rule is committed."""
    return {"inline_keyboard": [[
        {"text": _clip('🏷️ "%s" blacklisted' % brand),
         "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_NOOP)},
        {"text": "↩️ Undo",
         "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_UNDO_BRAND)},
    ]]}


def undo_product_keyboard(source: str, product_id: str, price: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "↩️ Undo block",
         "callback_data": "%s|%s|%s|%s|%s" % (
             CALLBACK_PREFIX, ACT_UNDO_PRODUCT, source, product_id, price)},
    ]]}


def awaiting_text_keyboard() -> dict:
    """Shown on the deal message while a custom brand is being typed in the DM."""
    return {"inline_keyboard": [[
        {"text": "⌨️ Waiting for brand in the bot chat…",
         "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_NOOP)},
        {"text": "❌ Cancel",
         "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_CANCEL)},
    ]]}


def _brand_spec(text: str) -> tuple[str, int] | None:
    """Parse a hand-typed brand rule: ``arry`` or ``arry,99``.

    No comma means a full blacklist (min discount 100 → nothing ever posts).
    A comma is required before the percentage so a name that merely ends in a
    number ("Redmi Note 12") is not silently read as a 12% rule.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    match = re.fullmatch(r"(.+?)\s*,\s*(\d{1,3})\s*%?", raw)
    if match:
        brand, pct = match.group(1).strip(), int(match.group(2))
        if not brand or not 0 <= pct <= 100:
            return None
        return (brand, pct) if re.search(r"[^\W_]", brand) else None
    if re.fullmatch(r"(.+?),\s*", raw):     # "arry," — dangling comma
        return None
    if not re.search(r"[^\W_]", raw):        # punctuation/emoji only — not a brand
        return None
    return raw, 100


def _describe(min_discount: int) -> str:
    return ("every deal" if min_discount >= 100
            else "deals below %d%%" % min_discount)


# -- message parsing --------------------------------------------------------
_DISCOUNT_RE = re.compile(r"(\d{1,3})%\s*Off\s*-\s*(.*)")
_PRICE_RE = re.compile(r"₹\s*([\d,]+)\s*\(MRP:\s*₹\s*([\d,]+)\)")
_URL_RE = re.compile(r"https?://\S+")
_APP_RE = re.compile(r"^App:\s*(.+)$", re.M)
_PID_RES = (re.compile(r"[?&]pid=([A-Za-z0-9]{4,25})", re.I),
            re.compile(r"myntra\.com/(?:buy/)?([A-Za-z0-9]{5,25})", re.I),
            re.compile(r"croma\.com/\S*?/(\d{5,12})", re.I),
            re.compile(r"blinkit\.com/\S*?/prid/(\d{4,20})", re.I),
            re.compile(r"bigbasket\.com/pd/(\d{4,20})", re.I))


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
    """Owns the button conversation state and the blacklist side effects.

    ``send``/``answer``/``edit`` are injected so the conversation can be driven
    without Telegram in tests. Each takes an optional ``token``: a callback is
    delivered to the *one* bot token whose message carried the button, and that
    is the only token allowed to answer the callback or edit the message.
    """

    def __init__(self, blacklist, seen_stores: dict[str, object],
                 admin_chat_id: int, deal_chats: set[int],
                 send: Callable[..., object],
                 answer: Callable[..., object],
                 edit: Callable[..., bool] | None = None) -> None:
        self._blacklist = blacklist
        self._seen = seen_stores
        self._admin = admin_chat_id
        self._deal_chats = deal_chats
        self._send = send
        self._answer = answer
        self._edit = edit
        # Button conversations, keyed by (chat_id, message_id) of the deal post.
        self._pending: dict[tuple[int, int], dict] = {}
        # Text-entry conversations, keyed by the chat the user must type in (DM).
        self._typing: dict[int, dict] = {}
        self._lock = threading.Lock()

    # -- auth --------------------------------------------------------------
    def _authorized(self, from_id: int, chat_id: int) -> bool:
        if from_id == self._admin:
            return True
        # Acting as the channel itself: only trusted inside a deal channel.
        return from_id == ANONYMOUS_ADMIN_ID and chat_id in self._deal_chats

    @staticmethod
    def _loc(cq: dict) -> tuple[int, int]:
        msg = cq.get("message") or {}
        return (msg.get("chat") or {}).get("id", 0), msg.get("message_id", 0)

    # -- entry points ------------------------------------------------------
    def handle_callback(self, cq: dict, token: str | None = None) -> None:
        self._prune()
        try:
            data = cq.get("data") or ""
            if not data.startswith(CALLBACK_PREFIX + "|"):
                self._answer(cq.get("id", ""), "Unknown action", token)
                return
            msg = cq.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id", 0)
            from_id = (cq.get("from") or {}).get("id", 0)
            if not self._authorized(from_id, chat_id):
                log.warning("Deal button from unauthorized user %s in chat %s",
                            from_id, chat_id)
                self._answer(cq.get("id", ""), "Not authorized", token)
                return
            parts = data.split("|")
            action = parts[1] if len(parts) > 1 else ""
            if action == ACT_PRODUCT:
                self._block_product(cq, parts, token)
            elif action == ACT_BRAND:
                self._start_brand_prompt(cq, parts, token)
            elif action == ACT_CONFIRM:
                self._confirm_brand(cq, token)
            elif action == ACT_CUSTOM:
                self._custom_brand(cq, token)
            elif action == ACT_UNDO_PRODUCT:
                self._undo_product(cq, parts, token)
            elif action == ACT_UNDO_BRAND:
                self._undo_brand(cq, token)
            elif action == ACT_CANCEL:
                self._cancel(cq, token)
            elif action == ACT_NOOP:
                self._answer(cq.get("id", ""), "", token)
            else:
                self._answer(cq.get("id", ""), "Unknown action", token)
        except Exception as exc:  # never let a button kill the poll loop
            log.error("Deal button handler error: %s", exc, exc_info=True)
            self._answer(cq.get("id", ""), "Button failed: %s" % exc, token)

    def handle_text(self, text: str, chat_id: int,
                    token: str | None = None) -> str | None:
        """Consume a DM reply belonging to an open custom-brand conversation."""
        with self._lock:
            state = self._typing.get(chat_id)
        if not state:
            return None
        if time.time() - state.get("ts", 0) > PROMPT_TTL_SECONDS:
            with self._lock:
                self._typing.pop(chat_id, None)
                self._pending.pop((state.get("deal_chat"), state.get("deal_mid")), None)
            return "Prompt expired — press the button again."
        low = text.strip().lower()
        if low in ("/cancel", "cancel", "/abort", "abort", "no"):
            with self._lock:
                self._typing.pop(chat_id, None)
                self._pending.pop((state.get("deal_chat"), state.get("deal_mid")), None)
            return "Cancelled."
        parsed = _brand_spec(text)
        if parsed is None:
            return ("Send the brand name, optionally `name,99` to only post at "
                    "99%% or deeper. `/cancel` to abort.")
        brand, pct = parsed
        with self._lock:
            self._typing.pop(chat_id, None)
            deal = self._pending.get((state.get("deal_chat"), state.get("deal_mid")))
            if deal is not None:
                deal["stage"] = "done"
                deal["brand"] = brand
                deal["pct"] = pct
        self._blacklist.add(brand, pct)
        self._finish(state.get("deal_chat", 0), state.get("deal_mid", 0), brand, token)
        return '🏷️ *%s* blacklisted for %s.' % (brand, _describe(pct))

    def pending_prompt(self, chat_id: int) -> bool:
        with self._lock:
            return chat_id in self._typing

    def _prune(self) -> None:
        """Drop button conversations nobody finished (or undid) in time."""
        cutoff = time.time() - PROMPT_TTL_SECONDS
        with self._lock:
            for key in [k for k, v in self._pending.items()
                        if v.get("ts", 0) < cutoff]:
                self._pending.pop(key, None)
            for key in [k for k, v in self._typing.items()
                        if v.get("ts", 0) < cutoff]:
                self._typing.pop(key, None)

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

    def _edit_markup(self, chat_id: int, message_id: int, keyboard: dict,
                     token: str | None) -> bool:
        if not self._edit or not chat_id or not message_id:
            return False
        try:
            return bool(self._edit(chat_id, message_id, keyboard, token))
        except Exception as exc:
            log.debug("editMessageReplyMarkup failed: %s", exc)
            return False

    def _block_product(self, cq: dict, parts: list[str], token: str | None) -> None:
        _, _, source, product_id, price_s = (parts + ["", "", "", "", ""])[:5]
        try:
            price = int(price_s)
        except ValueError:
            self._answer(cq.get("id", ""), "Malformed button (old message?)", token)
            return
        meta = self._meta(source, product_id, (cq.get("message") or {}).get("text", ""))
        brand = self._resolve_brand(meta)
        self._blacklist.add_product(product_id, price, source=source,
                                    brand=brand, title=meta.get("title", ""))
        self._answer(cq.get("id", ""), "Blocked %s at ₹%d" % (brand or product_id, price),
                     token)
        chat_id, mid = self._loc(cq)
        keyboard = undo_product_keyboard(source, product_id, price)
        if not self._edit_markup(chat_id, mid, keyboard, token):
            self._send("🚫 Blocked *%s* at ₹%d\n%s\n\n"
                       "Only this exact price is blocked — a lower price will still alert."
                       % (brand or product_id, price, meta.get("title", "") or product_id),
                       None, keyboard)

    def _undo_product(self, cq: dict, parts: list[str], token: str | None) -> None:
        _, _, source, product_id, price_s = (parts + ["", "", "", "", ""])[:5]
        try:
            price = int(price_s)
        except ValueError:
            self._answer(cq.get("id", ""), "Malformed button", token)
            return
        ok = self._blacklist.remove_product(product_id, price, source=source)
        self._answer(cq.get("id", ""), "Unblocked" if ok else "Nothing to undo", token)
        chat_id, mid = self._loc(cq)
        self._edit_markup(chat_id, mid, deal_keyboard(product_id, price, source), token)

    def _start_brand_prompt(self, cq: dict, parts: list[str], token: str | None) -> None:
        _, _, source, product_id, price_s = (parts + ["", "", "", "", ""])[:5]
        meta = self._meta(source, product_id, (cq.get("message") or {}).get("text", ""))
        brand = self._resolve_brand(meta)
        if not brand or brand.lower() == "unknown":
            self._answer(cq.get("id", ""),
                         "No brand detected — use /add_blacklist <brand> <pct>", token)
            return
        chat_id, mid = self._loc(cq)
        with self._lock:
            self._pending[(chat_id, mid)] = {
                "stage": "confirm", "brand": brand, "pct": 100,
                "source": source, "product_id": product_id, "price": price_s,
                "title": meta.get("title", ""), "ts": time.time(),
            }
        self._answer(cq.get("id", ""),
                     'Block "%s"? Confirm, or ✏️ Custom to edit it.' % brand, token)
        keyboard = brand_confirm_keyboard(brand)
        if not self._edit_markup(chat_id, mid, keyboard, token):
            # Could not rewrite the deal message (too old, or the bot lacks
            # edit rights). Fall back to the manual commands rather than
            # buttons on a DM message, whose ids would not match the state.
            self._send('🏷️ Could not update that message. Block *%s* with\n'
                       '`/add_blacklist %s 100` (all deals) or\n'
                       '`/add_blacklist %s 99` (only below 99%%).'
                       % (brand, brand.lower(), brand.lower()))

    def _confirm_brand(self, cq: dict, token: str | None) -> None:
        chat_id, mid = self._loc(cq)
        with self._lock:
            state = self._pending.get((chat_id, mid))
        if not state or state.get("stage") != "confirm":
            self._answer(cq.get("id", ""), "Nothing to confirm", token)
            return
        brand = state.get("brand", "")
        pct = int(state.get("pct", 100))
        with self._lock:
            state["stage"] = "done"
            state["pct"] = pct
        self._blacklist.add(brand, pct)
        self._answer(cq.get("id", ""),
                     "%s blacklisted for %s" % (brand, _describe(pct)), token)
        self._finish(chat_id, mid, brand, token)

    def _custom_brand(self, cq: dict, token: str | None) -> None:
        chat_id, mid = self._loc(cq)
        with self._lock:
            state = self._pending.get((chat_id, mid))
        if not state or state.get("stage") != "confirm":
            self._answer(cq.get("id", ""), "Nothing to customise", token)
            return
        with self._lock:
            state["stage"] = "typing"
            self._typing[self._admin] = {
                "stage": "custom", "brand": state.get("brand", ""),
                "source": state.get("source", ""), "product_id": state.get("product_id", ""),
                "price": state.get("price", ""), "title": state.get("title", ""),
                "deal_chat": chat_id, "deal_mid": mid, "ts": time.time(),
            }
        self._answer(cq.get("id", ""), "Type the brand in the bot chat", token)
        self._edit_markup(chat_id, mid, awaiting_text_keyboard(), token)
        # The DM goes out from the control bot (the one that reads admin
        # commands), not the token that happened to post the deal.
        self._send(
            '✏️ Send the brand to block.\n'
            '`arry` — every deal. `arry,99` — only post at 99%% or deeper.\n'
            'Detected: *%s*. /cancel to abort.' % (state.get("brand") or "—"),
            self._admin, None, None)

    def _undo_brand(self, cq: dict, token: str | None) -> None:
        chat_id, mid = self._loc(cq)
        with self._lock:
            state = self._pending.get((chat_id, mid))
        brand = (state or {}).get("brand", "")
        if not brand:
            self._answer(cq.get("id", ""), "Nothing to undo", token)
            return
        ok = self._blacklist.remove(brand)
        self._answer(cq.get("id", ""),
                     "Removed %s" % brand if ok else "Already removed", token)
        with self._lock:
            self._pending.pop((chat_id, mid), None)
        if state and state.get("product_id"):
            self._edit_markup(chat_id, mid, deal_keyboard(
                state.get("product_id", ""), _int(str(state.get("price", "0"))),
                state.get("source", "")), token)
        else:
            self._edit_markup(chat_id, mid, {"inline_keyboard": [[
                {"text": "↩️ Undone", "callback_data": "%s|%s" % (CALLBACK_PREFIX, ACT_NOOP)}
            ]]}, token)

    def _cancel(self, cq: dict, token: str | None) -> None:
        chat_id, mid = self._loc(cq)
        with self._lock:
            state = self._pending.pop((chat_id, mid), None)
            self._typing.pop(self._admin, None)
        self._answer(cq.get("id", ""), "Cancelled", token)
        if state and state.get("product_id"):
            self._edit_markup(chat_id, mid, deal_keyboard(
                state.get("product_id", ""), _int(str(state.get("price", "0"))),
                state.get("source", "")), token)

    def _finish(self, chat_id: int, mid: int, brand: str,
                token: str | None) -> None:
        """Swap the deal message's buttons to the committed rule."""
        if not chat_id or not mid:
            return
        self._edit_markup(chat_id, mid, brand_done_keyboard(brand), token)
