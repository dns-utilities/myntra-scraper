"""Multi-bot Telegram deal sender with per-bot + global rate limiting."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque

import requests

from myntra_scraper.deal_buttons import deal_keyboard
from myntra_scraper.models import DiscountTier, Product

log = logging.getLogger("myntra_scraper.notifier")


class DealNotifier:
    def __init__(self, bot_tokens: list[str], channels: dict[str, int],
                 affiliate_tag: str | dict | None = "") -> None:
        self._tokens = bot_tokens
        self._channels = channels
        self._affiliate = ""
        self.set_affiliate_tag(affiliate_tag)
        self._queue: deque = deque()
        self._queue_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._last_bot: dict[str, float] = {t: 0.0 for t in bot_tokens}
        self._last_global = 0.0
        self._running = False
        self._workers: list[threading.Thread] = []
        # (bot, chat) pairings that cannot work, so round-robin stops feeding
        # them. Self-healing: a bot promoted to admin later stays excluded
        # until restart, which is exactly what /verify_bots surfaces.
        self._bot_chat_blocked: set[tuple[str, int]] = set()
        self._bot_chat_fails: dict[tuple[str, int], int] = {}
        self.sent = 0
        self.failed = 0
        self.by_channel = {name: 0 for name in channels}
        log.info("Notifier: %d bots | %d channels", len(bot_tokens), len(channels))
        self.start()

    def set_affiliate_tag(self, tag: str | dict | None) -> None:
        # Plain string query ("" = OFF). Dict accepted only for back-compat
        # with old callers; converted to a query string.
        if isinstance(tag, dict):
            from urllib.parse import urlencode as _urlencode
            tag = _urlencode(tag) if tag else ""
        self._affiliate = (tag or "").strip().lstrip("?&")

    @property
    def affiliate_tag(self) -> str:
        return self._affiliate

    def route(self, discount: int, source: str = "") -> list[int]:
        """Pick exactly ONE channel family per deal.

        * `< 70%`   -> that source's base channel (CHANNEL_MYNTRA /
          CHANNEL_CROMA), falling back to CHANNEL_ALL
        * `70-79%`  -> CHANNEL_70PLUS only
        * `80-89%`  -> CHANNEL_80PLUS only
        * `90%+`    -> CHANNEL_90PLUS only

        Tier deals never also land in the base channel — a 85% deal appearing
        in both D80 and Myntra-base is what makes a base feed feel spammy.
        Result is de-duplicated because unset tiers fall back to other ids.
        """
        if discount >= DiscountTier.JACKPOT:
            names = ("90plus",)
        elif discount >= DiscountTier.DEEP:
            names = ("80plus",)
        elif discount >= DiscountTier.HOT:
            names = ("70plus",)
        else:
            base = source if source in self._channels and self._channels.get(source) else "all"
            names = (base,)
        out: list[int] = []
        for name in names:
            chat = self._channels.get(name)
            if chat and chat not in out:
                out.append(chat)
        return out

    def queue(self, product: Product) -> None:
        text = product.format_message(self._affiliate)
        keyboard = deal_keyboard(product.product_id, product.price, product.source)
        targets = self.route(product.discount, product.source)
        with self._queue_lock:
            for chat_id in targets:
                self._queue.append({"text": text, "chat_id": chat_id,
                                    "discount": product.discount,
                                    "source": product.source,
                                    "keyboard": keyboard})
    @property
    def pending(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def _send(self, token: str, chat_id: int, text: str,
              keyboard: dict | None = None) -> str:
        now = time.time()
        if now - self._last_bot[token] < 1 / 25 or now - self._last_global < 1 / 67:
            return "rate_limit:0"
        try:
            payload = {"chat_id": chat_id, "text": text}
            if keyboard:
                payload["reply_markup"] = json.dumps(keyboard)
            resp = requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % token,
                data=payload, timeout=8)
            now = time.time()
            self._last_bot[token] = now
            self._last_global = now
            if resp.status_code == 200:
                return "ok"
            if resp.status_code == 429:
                wait = resp.json().get("parameters", {}).get("retry_after", 5)
                return "rate_limit:%d" % wait
            # 403 "bot is not a member of the channel chat" / 400 "chat not
            # found" are permanent for this bot — retrying cannot fix them.
            if resp.status_code in (400, 403):
                body = (resp.text or "")[:120]
                log.error("Send BLOCKED HTTP %d to %s: %s", resp.status_code,
                          chat_id, body)
                return "no_access:%d" % resp.status_code
            log.error("Send failed HTTP %d to %s: %s", resp.status_code,
                      chat_id, (resp.text or "")[:200])
            return "error"
        except Exception as exc:
            log.error("Send error: %s", exc)
            return "error"

    def _worker(self, token: str) -> None:
        while self._running:
            with self._queue_lock:
                msg = self._queue.popleft() if self._queue else None
            if not msg:
                time.sleep(0.05)
                continue
            if msg.get("_retry_after", 0) > time.time():
                with self._queue_lock:
                    self._queue.append(msg)
                time.sleep(0.1)
                continue
            chat_id = msg["chat_id"]
            # A bot that cannot post to a chat (not an admin, kicked, wrong
            # id) would otherwise keep failing on that chat forever and the
            # deal would be lost. Skip it here so another worker gets it.
            if (token, chat_id) in self._bot_chat_blocked:
                if not self._any_worker_for(chat_id, except_token=token):
                    # Nobody can serve this chat — dropping beats spinning.
                    log.error("Notifier: no bot can post to chat %s — message "
                              "dropped (is any bot an admin there?)", chat_id)
                    with self._stats_lock:
                        self.failed += 1
                else:
                    with self._queue_lock:
                        self._queue.append(msg)
                time.sleep(0.05)
                continue
            result = self._send(token, chat_id, msg["text"], msg.get("keyboard"))
            if result == "ok":
                with self._stats_lock:
                    self.sent += 1
                    self._bot_chat_fails.pop((token, chat_id), None)
                    for name, cid in self._channels.items():
                        if cid == chat_id:
                            self.by_channel[name] += 1
            elif result.startswith("rate_limit:"):
                wait = float(result.split(":")[1])
                delay = wait if wait > 0 else 0.05
                msg["_retry_after"] = time.time() + delay
                with self._queue_lock:
                    self._queue.append(msg)
                # A Telegram 429 is a real instruction and is honoured. Our own
                # local pacing only needs the short gap to elapse, so it must
                # not pay the same 1s penalty — that penalty is what makes a
                # multi-bot pool look lopsided under load.
                time.sleep(delay)
            elif result.startswith("no_access:"):
                # Permanent for this pairing — never retry with this bot.
                self._mark_blocked(token, chat_id, result)
                with self._stats_lock:
                    self.failed += 1
            else:
                with self._stats_lock:
                    self.failed += 1
                fails = self._bot_chat_fails.get((token, chat_id), 0) + 1
                self._bot_chat_fails[(token, chat_id)] = fails
                if fails >= 3:
                    self._mark_blocked(token, chat_id,
                                       "%d consecutive failures" % fails)

    def _any_worker_for(self, chat_id: int, except_token: str) -> bool:
        """True when some other bot could still serve this chat."""
        with self._stats_lock:
            for tok in self._tokens:
                if tok != except_token and (tok, chat_id) not in self._bot_chat_blocked:
                    return True
        return False

    def _mark_blocked(self, token: str, chat_id: int, reason: str) -> None:
        """Retire a (bot, chat) pairing so other workers pick up its messages."""
        if (token, chat_id) in self._bot_chat_blocked:
            return
        self._bot_chat_blocked.add((token, chat_id))
        self._bot_chat_fails.pop((token, chat_id), None)
        log.warning("Notifier: bot ...%s cannot post to chat %s (%s) — excluding "
                    "that pairing; another bot will handle it",
                    token[-6:], chat_id, reason)

    def start(self) -> None:
        if self._running or not self._tokens:
            return
        self._running = True
        for i, token in enumerate(self._tokens):
            thread = threading.Thread(target=self._worker, args=(token,),
                                      daemon=True, name="sender-%d" % i)
            thread.start()
            self._workers.append(thread)
        log.info("Notifier started (%d workers)", len(self._workers))

    def stop(self) -> None:
        self._running = False
        for thread in self._workers:
            thread.join(timeout=5)
        log.info("Notifier stopped")

    def stats(self) -> dict:
        with self._stats_lock:
            return {"sent": self.sent, "failed": self.failed,
                    "pending": self.pending, "by_channel": dict(self.by_channel),
                    "blocked_pairings": len(self._bot_chat_blocked)}

    def blocked_pairings(self) -> list[tuple[str, int]]:
        with self._stats_lock:
            return sorted(self._bot_chat_blocked)

    def reset_blocked(self) -> int:
        """Clear retired (bot, chat) pairings — use after promoting bots."""
        with self._stats_lock:
            count = len(self._bot_chat_blocked)
            self._bot_chat_blocked.clear()
            self._bot_chat_fails.clear()
        return count
