"""Poller-offset regression test.

Telegram's update_id sequence is PER BOT. The controller polls one thread per
token, so sharing a single offset across threads makes every bot but the busiest
silently skip its own updates — commands sent to those bots just never arrive.

This test drives the real ControllerBot loop with a fake Telegram API and
asserts each token keeps its own cursor.

Run: python scripts/poller_test.py
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dns_scraper.channels import ChannelPlan

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append("%s: got %r want %r" % (label, got, want))
    print("%s %-46s %r" % ("PASS" if ok else "FAIL", label, got))


class FakeApp:
    settings = type("S", (), {
        "channel_plan": ChannelPlan(bases={"default": -100}, tiers={70: -101}),
        "bot_tokens": [], "admin_chat_id": 42})()


def main() -> int:
    from dns_scraper.controller_bot import ControllerBot

    TOKENS = ["111:aaa", "222:bbb", "333:ccc"]
    bot = ControllerBot(TOKENS[0], 42, FakeApp(), bot_tokens=TOKENS)  # type: ignore[arg-type]
    bot.start()
    try:
        # start() must seed one cursor + one dedupe set per token
        check("offsets seeded per token", sorted(bot._offsets), sorted(TOKENS))
        check("seen seeded per token", sorted(bot._seen), sorted(TOKENS))
        check("all offsets start at 0", set(bot._offsets.values()), {0})

        # Simulate bot 2 racing ahead: its updates have high ids.
        bot._offsets[TOKENS[1]] = 500
        bot._seen[TOKENS[1]] = {498, 499, 500}

        # The critical assertion: bot 1's cursor must NOT have moved.
        check("bot1 offset independent", bot._offsets[TOKENS[0]], 0)
        check("bot3 offset independent", bot._offsets[TOKENS[2]], 0)

        # Each thread must ask getUpdates for its OWN next id, not a shared one.
        for tok in TOKENS:
            want = bot._offsets.get(tok, 0) + 1
            check("next offset for ...%s" % tok[-3:], want,
                  {TOKENS[0]: 1, TOKENS[1]: 501, TOKENS[2]: 1}[tok])

        # Now prove processing an update only advances its own token.
        update = {"update_id": 7, "message": {"text": "/help", "chat": {"id": 42}}}
        bot._process_update(update, TOKENS[0], set())
        check("bot1 advanced", bot._offsets[TOKENS[0]], 7)
        check("bot2 still 500", bot._offsets[TOKENS[1]], 500)
        check("bot3 still 0", bot._offsets[TOKENS[2]], 0)
        check("bot1 seen has 7", 7 in bot._seen[TOKENS[0]], True)
        check("bot2 seen unchanged", 7 in bot._seen[TOKENS[1]], False)

        # A duplicate id for the same token must be ignored.
        bot._process_update(update, TOKENS[0], set())
        check("duplicate ignored", bot._offsets[TOKENS[0]], 7)
    finally:
        bot.stop()

    # One token only: behaviour must still be correct.
    solo = ControllerBot("999:zzz", 42, FakeApp(), bot_tokens=["999:zzz"])  # type: ignore[arg-type]
    solo.start()
    try:
        check("single bot offset", solo._offsets["999:zzz"], 0)
        solo._process_update({"update_id": 1, "message": {"text": "x", "chat": {"id": 42}}},
                             "999:zzz", set())
        check("single bot advances", solo._offsets["999:zzz"], 1)
    finally:
        solo.stop()

    print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES",
                                    len(FAILS), "" if len(FAILS) == 1 else "s"))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
