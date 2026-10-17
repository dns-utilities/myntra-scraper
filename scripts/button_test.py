"""Button-conversation test: exercises the Telegram callback round trip.

Simulates what Telegram actually delivers for a pressed button — a
``callback_query`` with the deal message attached — plus the follow-up text
messages, and asserts the resulting blacklist state. No network.

Run: python scripts/button_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dns_scraper.deal_buttons import DealButtonHandler, parse_deal_message
from dns_scraper.stores import BlacklistStore

FAILS: list[str] = []
ADMIN = 42
DEAL_CHAT = -1001


def check(label: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILS.append("%s: got %r want %r" % (label, got, want))
    print("%s %-46s %r" % ("PASS" if ok else "FAIL", label, got))


class FakeStore:
    """Stands in for a SeenStore: knows one product."""

    def __init__(self, brand, title, price):
        self._row = {"brand": brand, "name": title, "price": price,
                     "product_id": "TITAN1234567", "discount": 80,
                     "location": "387001", "store_codes": []}

    def get_product(self, product_id, location=""):
        return dict(self._row)


class Recorder:
    """Captures what the handler told Telegram to do."""

    def __init__(self):
        self.sent: list[tuple[str, dict | None]] = []
        self.toasts: list[str] = []

    def send(self, text, chat_id=None, keyboard=None):
        self.sent.append((text, keyboard))

    def answer(self, cqid, text):
        self.toasts.append(text)


def press(action: str, source="myntra", pid="TITAN1234567", price=1299,
          from_id=ADMIN, cqid="cb1", text=None) -> dict:
    """Build the callback_query Telegram would send for a button press."""
    return {
        "id": cqid,
        "from": {"id": from_id},
        "data": "blk|%s|%s|%s|%s" % (action, source, pid, price),
        "message": {"chat": {"id": DEAL_CHAT}, "text": text or DEAL_TEXT},
    }


DEAL_TEXT = ("🚨 80% Off - Titan Raga Watch For Women\n\n"
             "💰 Price: ₹1299 (MRP: ₹6495)\n\n"
             "https://www.myntra.com/TITAN1234567\n\nApp: Myntra")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        bl = BlacklistStore(Path(tmp) / "blacklist.db")
        rec = Recorder()
        handler = DealButtonHandler(bl, {"myntra": FakeStore("Titan", "Raga Watch For Women", 1299)},
                                    ADMIN, {DEAL_CHAT}, send=rec.send, answer=rec.answer)

        print("\n-- unauthorized caller rejected --")
        handler.handle_callback(press("p", from_id=999))
        check("toast is refusal", rec.toasts[-1], "Not authorized")
        check("nothing blocked", len(bl.product_blocks()), 0)

        print("\n-- Blacklist Product (one tap) --")
        handler.handle_callback(press("p", cqid="cb2"))
        check("toast confirms", rec.toasts[-1], "Blocked Titan at ₹1299")
        check("exact price blocked",
              bl.is_product_blocked("TITAN1234567", 1299, "myntra"), True)
        check("other price free",
              bl.is_product_blocked("TITAN1234567", 1199, "myntra"), False)
        check("admin got a message", len(rec.sent) >= 1, True)
        check("undo button offered",
              any(k and any("Undo" in b["text"] for b in k["inline_keyboard"][0])
                  for _, k in rec.sent), True)

        print("\n-- Undo --")
        undo = {"id": "cb3", "from": {"id": ADMIN},
                "data": "blk|u|myntra|TITAN1234567|1299",
                "message": {"chat": {"id": DEAL_CHAT}, "text": DEAL_TEXT}}
        handler.handle_callback(undo)
        check("undo toast", rec.toasts[-1], "Unblocked")
        check("block lifted",
              bl.is_product_blocked("TITAN1234567", 1299, "myntra"), False)

        print("\n-- Blacklist Brand -> prompt -> bad input -> good input -> confirm --")
        handler.handle_callback(press("b", cqid="cb4"))
        check("toast prompts", rec.toasts[-1], "Enter a discount % for Titan")
        check("prompt pending", handler.pending_prompt(ADMIN), True)
        reply = handler.handle_text("sixty", ADMIN)
        check("rejects non-number", "number 0–100" in (reply or ""), True)
        check("still pending", handler.pending_prompt(ADMIN), True)
        reply = handler.handle_text("150", ADMIN)
        check("rejects out of range", "0–100" in (reply or ""), True)
        reply = handler.handle_text("60", ADMIN)
        check("percent consumed, no text reply", reply, None)
        check("confirm keyboard sent",
              any(k and any("Confirm 60%" in b["text"] for b in k["inline_keyboard"][0])
                  for _, k in rec.sent), True)
        check("not yet applied", "titan" not in bl.all(), True)

        confirm = {"id": "cb5", "from": {"id": ADMIN}, "data": "blk|c|60",
                   "message": {"chat": {"id": DEAL_CHAT}, "text": DEAL_TEXT}}
        handler.handle_callback(confirm)
        check("brand rule added", bl.all().get("titan"), 60)
        check("prompt cleared", handler.pending_prompt(ADMIN), False)
        check("brand blocked at 55", bl.is_blocked("Titan", "Raga Watch", 55)[0], True)
        check("brand allowed at 60", bl.is_blocked("Titan", "Raga Watch", 60)[0], False)

        print("\n-- Cancel path --")
        handler.handle_callback(press("b", cqid="cb6"))
        reply = handler.handle_text("/cancel", ADMIN)
        check("cancel reply", reply, "Cancelled.")
        check("nothing added", len(bl.all()), 1)

        print("\n-- Cancel button on the confirm keyboard --")
        handler.handle_callback(press("b", cqid="cb7"))
        handler.handle_text("70", ADMIN)
        cancel = {"id": "cb8", "from": {"id": ADMIN}, "data": "blk|x|0",
                  "message": {"chat": {"id": DEAL_CHAT}, "text": DEAL_TEXT}}
        handler.handle_callback(cancel)
        check("cancel toast", rec.toasts[-1], "Cancelled")
        check("rule not added", "titan" in bl.all() and bl.all()["titan"] == 70, False)
        check("prompt cleared", handler.pending_prompt(ADMIN), False)

        print("\n-- brand fallback when SeenStore has no row --")
        bl2 = BlacklistStore(Path(tmp) / "b2.db")
        rec2 = Recorder()
        handler2 = DealButtonHandler(bl2, {}, ADMIN, {DEAL_CHAT},
                                     send=rec2.send, answer=rec2.answer)
        # message headline carries the brand, so it still resolves
        handler2.handle_callback(press("b", cqid="cb9"))
        check("brand from message headline", rec2.toasts[-1],
              "Enter a discount % for Titan")
        # now a message with no brand and no stored row -> first title word
        bare = ("🚨 80% Off - Wobble Mystery Box\n\n"
                "💰 Price: ₹99 (MRP: ₹4990)\n\n"
                "https://www.myntra.com/WOBBLE123456\n\nApp: Myntra")
        handler2.handle_callback(press("b", pid="WOBBLE123456", price=99,
                                       cqid="cb10", text=bare))
        check("falls back to title lead", rec2.toasts[-1],
              "Enter a discount % for Wobble")

        print("\n-- stale/old button (no price) --")
        handler2.handle_callback({"id": "cb11", "from": {"id": ADMIN},
                                  "data": "blk|p|myntra|WOBBLE123456",
                                  "message": {"chat": {"id": DEAL_CHAT}, "text": bare}})
        check("malformed handled", rec2.toasts[-1], "Malformed button (old message?)")

        print("\n-- anonymous channel admin allowed inside deal chat --")
        bl3 = BlacklistStore(Path(tmp) / "b3.db")
        rec3 = Recorder()
        handler3 = DealButtonHandler(bl3, {"myntra": FakeStore("Titan", "Raga Watch For Women", 1299)},
                                     ADMIN, {DEAL_CHAT}, send=rec3.send, answer=rec3.answer)
        handler3.handle_callback(press("p", from_id=1087968824, cqid="cb12"))
        check("channel admin allowed",
              bl3.is_product_blocked("TITAN1234567", 1299, "myntra"), True)

    print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES",
                                    len(FAILS), "" if len(FAILS) == 1 else "s"))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
