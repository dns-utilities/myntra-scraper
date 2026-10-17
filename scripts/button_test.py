"""Button-conversation test: exercises the Telegram callback round trip.

Simulates what Telegram actually delivers for a pressed button — a
``callback_query`` with the deal message attached — plus the follow-up text
messages, and asserts the resulting blacklist state. No network.

The buttons edit the deal message in place, so the recorder captures
``edit(chat_id, message_id, keyboard)`` calls and checks the keyboard the user
would see after each tap.

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
MSG = 500


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
        self.edits: list[tuple[int, int, dict]] = []

    def send(self, text, chat_id=None, keyboard=None, token=None):
        self.sent.append((text, keyboard))

    def answer(self, cqid, text, token=None):
        self.toasts.append(text)

    def edit(self, chat_id, message_id, keyboard, token=None):
        self.edits.append((chat_id, message_id, keyboard))
        return True

    def last_keyboard(self):
        return self.edits[-1][2] if self.edits else None


def labels(keyboard) -> list[str]:
    if not keyboard:
        return []
    return [b["text"] for row in keyboard["inline_keyboard"] for b in row]


def data(keyboard) -> list[str]:
    if not keyboard:
        return []
    return [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]


def press(action: str, source="myntra", pid="TITAN1234567", price=1299,
          from_id=ADMIN, cqid="cb1", text=None, message_id=MSG, raw=None) -> dict:
    """Build the callback_query Telegram would send for a button press."""
    payload = raw if raw is not None else "blk|%s|%s|%s|%s" % (
        action, source, pid, price)
    return {
        "id": cqid,
        "from": {"id": from_id},
        "data": payload,
        "message": {"chat": {"id": DEAL_CHAT}, "message_id": message_id,
                    "text": text or DEAL_TEXT},
    }


DEAL_TEXT = ("🚨 80% Off - Titan Raga Watch For Women\n\n"
             "💰 Price: ₹1299 (MRP: ₹6495)\n\n"
             "https://www.myntra.com/TITAN1234567\n\nApp: Myntra")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        bl = BlacklistStore(Path(tmp) / "blacklist.db")
        rec = Recorder()
        handler = DealButtonHandler(bl, {"myntra": FakeStore("Titan", "Raga Watch For Women", 1299)},
                                    ADMIN, {DEAL_CHAT}, send=rec.send,
                                    answer=rec.answer, edit=rec.edit)

        print("\n-- unauthorized caller rejected --")
        handler.handle_callback(press("p", from_id=999))
        check("toast is refusal", rec.toasts[-1], "Not authorized")
        check("nothing blocked", len(bl.product_blocks()), 0)

        print("\n-- Blacklist Product (one tap, edit in place) --")
        handler.handle_callback(press("p", cqid="cb2"))
        check("toast confirms", rec.toasts[-1], "Blocked Titan at ₹1299")
        check("exact price blocked",
              bl.is_product_blocked("TITAN1234567", 1299, "myntra"), True)
        check("other price free",
              bl.is_product_blocked("TITAN1234567", 1199, "myntra"), False)
        check("keyboard became Undo",
              any("Undo" in t for t in labels(rec.last_keyboard())), True)
        check("no DM spawned", len(rec.sent), 0)

        print("\n-- Undo (restores the deal keyboard) --")
        undo = press("u", cqid="cb3", raw="blk|u|myntra|TITAN1234567|1299")
        handler.handle_callback(undo)
        check("undo toast", rec.toasts[-1], "Unblocked")
        check("block lifted",
              bl.is_product_blocked("TITAN1234567", 1299, "myntra"), False)
        check("keyboard restored to deal buttons",
              labels(rec.last_keyboard()),
              ["🚫 Blacklist Product", "🏷️ Blacklist Brand"])

        print("\n-- Blacklist Brand -> confirm (full blacklist) --")
        handler.handle_callback(press("b", cqid="cb4"))
        check("toast asks to confirm", rec.toasts[-1],
              'Block "Titan"? Confirm, or ✏️ Custom to edit it.')
        check("confirm keyboard shown", labels(rec.last_keyboard()),
              ['✅ Block "Titan"', "✏️ Custom", "❌ Cancel"])
        check("confirm carries no payload", data(rec.last_keyboard())[0], "blk|c")
        check("not yet applied", "titan" not in bl.all(), True)
        handler.handle_callback(press("c", cqid="cb5", raw="blk|c"))
        check("confirm toast", rec.toasts[-1], "Titan blacklisted for every deal")
        check("brand rule = full", bl.all().get("titan"), 100)
        check("blocked at 99", bl.is_blocked("Titan", "Raga Watch", 99)[0], True)
        check("allowed at 100", bl.is_blocked("Titan", "Raga Watch", 100)[0], False)
        check("keyboard shows the rule",
              ["↩️ Undo" == t for t in labels(rec.last_keyboard())], [False, True])

        print("\n-- Undo the brand rule --")
        handler.handle_callback(press("r", cqid="cb6", raw="blk|r"))
        check("undo toast", rec.toasts[-1], "Removed Titan")
        check("rule gone", "titan" in bl.all(), False)

        print("\n-- Cancel (no change) --")
        handler.handle_callback(press("b", cqid="cb7", message_id=501))
        handler.handle_callback(press("x", cqid="cb8", message_id=501, raw="blk|x"))
        check("cancel toast", rec.toasts[-1], "Cancelled")
        check("nothing added", len(bl.all()), 0)
        check("keyboard restored",
              labels(rec.last_keyboard()),
              ["🚫 Blacklist Product", "🏷️ Blacklist Brand"])

        print("\n-- Custom brand, with a percentage --")
        handler.handle_callback(press("b", cqid="cb9", message_id=502))
        handler.handle_callback(press("e", cqid="cb10", message_id=502, raw="blk|e"))
        check("custom toast", rec.toasts[-1], "Type the brand in the bot chat")
        check("DM prompt sent", any("Send the brand" in t for t, _ in rec.sent), True)
        check("typing pending", handler.pending_prompt(ADMIN), True)
        reply = handler.handle_text("arry,150", ADMIN)
        check("rejects out-of-range pct", "optionally" in (reply or ""), True)
        check("still pending", handler.pending_prompt(ADMIN), True)
        reply = handler.handle_text("arry,99", ADMIN)
        check("custom applied", reply, "🏷️ *arry* blacklisted for deals below 99%.")
        check("typing cleared", handler.pending_prompt(ADMIN), False)
        check("rule at 99", bl.all().get("arry"), 99)
        check("blocks below 99", bl.is_blocked("arry", "Atta 5kg", 98)[0], True)
        check("posts at 99", bl.is_blocked("arry", "Atta 5kg", 99)[0], False)

        print("\n-- Custom brand, no percentage = full blacklist --")
        handler.handle_callback(press("b", cqid="cb11", message_id=503))
        handler.handle_callback(press("e", cqid="cb12", message_id=503, raw="blk|e"))
        reply = handler.handle_text("wobble", ADMIN)
        check("full blacklist", reply, "🏷️ *wobble* blacklisted for every deal.")
        check("rule at 100", bl.all().get("wobble"), 100)
        check("blocks everything", bl.is_blocked("wobble", "Mystery Box", 99)[0], True)

        print("\n-- cancel an open custom prompt by text --")
        handler.handle_callback(press("b", cqid="cb13", message_id=504))
        handler.handle_callback(press("e", cqid="cb14", message_id=504, raw="blk|e"))
        check("cancel reply", handler.handle_text("/cancel", ADMIN), "Cancelled.")
        check("typing cleared", handler.pending_prompt(ADMIN), False)

        print("\n-- brand fallback when SeenStore has no row --")
        bl2 = BlacklistStore(Path(tmp) / "b2.db")
        rec2 = Recorder()
        handler2 = DealButtonHandler(bl2, {}, ADMIN, {DEAL_CHAT},
                                     send=rec2.send, answer=rec2.answer, edit=rec2.edit)
        # message headline carries the brand, so it still resolves
        handler2.handle_callback(press("b", cqid="cb15", message_id=600))
        check("brand from message headline", rec2.toasts[-1],
              'Block "Titan"? Confirm, or ✏️ Custom to edit it.')
        # now a message with no brand and no stored row -> first title word
        bare = ("🚨 80% Off - Wobble Mystery Box\n\n"
                "💰 Price: ₹99 (MRP: ₹4990)\n\n"
                "https://www.myntra.com/WOBBLE123456\n\nApp: Myntra")
        handler2.handle_callback(press("b", pid="WOBBLE123456", price=99,
                                       cqid="cb16", message_id=601, text=bare))
        check("falls back to title lead", rec2.toasts[-1],
              'Block "Wobble"? Confirm, or ✏️ Custom to edit it.')

        print("\n-- stale/old button (no price) --")
        handler2.handle_callback({"id": "cb17", "from": {"id": ADMIN},
                                  "data": "blk|p|myntra|WOBBLE123456",
                                  "message": {"chat": {"id": DEAL_CHAT},
                                              "message_id": 602, "text": bare}})
        check("malformed handled", rec2.toasts[-1], "Malformed button (old message?)")

        print("\n-- anonymous channel admin allowed inside deal chat --")
        bl3 = BlacklistStore(Path(tmp) / "b3.db")
        rec3 = Recorder()
        handler3 = DealButtonHandler(bl3, {"myntra": FakeStore("Titan", "Raga Watch For Women", 1299)},
                                     ADMIN, {DEAL_CHAT}, send=rec3.send,
                                     answer=rec3.answer, edit=rec3.edit)
        handler3.handle_callback(press("p", from_id=1087968824, cqid="cb18"))
        check("channel admin allowed",
              bl3.is_product_blocked("TITAN1234567", 1299, "myntra"), True)

    print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES",
                                    len(FAILS), "" if len(FAILS) == 1 else "s"))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
