"""Check every bot against every channel, without needing the bot running.

Telegram does not allow a bot to add another bot to a group, so onboarding
bots is manual. This makes the manual part verifiable: it asks each bot about
each channel and prints a matrix, so you know exactly which pairings need an
admin promotion instead of guessing from missing messages.

Reads the same env vars as the app:
    BOT_TOKENS, CHANNEL_ALL, CHANNEL_MYNTRA, CHANNEL_CROMA,
    CHANNEL_70PLUS, CHANNEL_80PLUS, CHANNEL_90PLUS

Usage (from the project root, with your real tokens in the environment):
    python scripts/verify_bots.py

Nothing here touches your data directory or the running deployment — it is
read-only getMe/getChatMember calls.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

CHANNEL_KEYS = ("all", "myntra", "croma", "70plus", "80plus", "90plus")


def load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def api(token: str, method: str, **params):
    try:
        r = requests.get("https://api.telegram.org/bot%s/%s" % (token, method),
                         params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        return {"ok": False, "description": "HTTP %d" % r.status_code}
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


def main() -> int:
    load_dotenv()
    raw = os.environ.get("BOT_TOKENS", "")
    tokens = [p.strip() for p in raw.replace("\n", ",").replace(";", ",").split(",")
              if ":" in p.strip()]
    if not tokens:
        print("No BOT_TOKENS found. Put them in .env or export them.")
        return 1

    chats: list[tuple[str, int]] = []
    for key in CHANNEL_KEYS:
        val = os.environ.get("CHANNEL_%s" % key.upper(), "").strip()
        if not val:
            continue
        try:
            cid = int(val)
        except ValueError:
            print("CHANNEL_%s is not a number: %r" % (key.upper(), val))
            return 1
        if cid and cid not in [c for _, c in chats]:
            chats.append((key, cid))
    if not chats:
        print("No CHANNEL_* vars found — nothing to verify.")
        return 1

    print("Checking %d bot(s) against %d channel(s)\n" % (len(tokens), len(chats)))
    header = "%-8s %-14s" % ("bot", "name") + "".join("%-12s" % k for k, _ in chats)
    print(header)
    print("-" * len(header))
    bad = 0
    for idx, tok in enumerate(tokens, 1):
        me = api(tok, "getMe")
        if not me.get("ok"):
            print("%-8s %-14s TOKEN INVALID (%s)" % ("bot %d" % idx, "...%s" % tok[-6:],
                                                     me.get("description", "?")[:30]))
            bad += len(chats)
            continue
        bot_id = (me.get("result") or {}).get("id")
        name = (me.get("result") or {}).get("username", "")[:12]
        row = "%-8s %-14s" % ("bot %d" % idx, "@%s" % name)
        for _, cid in chats:
            member = api(tok, "getChatMember", chat_id=cid, user_id=bot_id)
            status = (member.get("result") or {}).get("status") if member.get("ok") else None
            ok = status in ("administrator", "creator")
            if not ok:
                bad += 1
            row += "%-12s" % ("OK" if ok else (status or "FAIL")[:11])
        print(row)

    print()
    if bad:
        print("%d pairing(s) FAILED." % bad)
        print("For each failure: open that channel -> Administrators -> Add Admin")
        print("-> search the bot's @username -> grant 'Post Messages'.")
        print("Bots cannot add other bots, so this step has to be done by you.")
        print("Re-run this script (or /verify_bots in the control bot) to confirm.")
        return 1
    print("All pairings OK — every bot can post to every channel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
