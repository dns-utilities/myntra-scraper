"""Multi-bot Telegram deal sender with per-bot + global rate limiting."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import requests

from myntra_scraper.models import DiscountTier, Product

log = logging.getLogger("myntra_scraper.notifier")


class DealNotifier:
    def __init__(self, bot_tokens: list[str], channels: dict[str, int],
                 affiliate_params: dict) -> None:
        self._tokens = bot_tokens
        self._channels = channels
        self._affiliate = affiliate_params
        self._queue: deque = deque()
        self._queue_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._last_bot: dict[str, float] = {t: 0.0 for t in bot_tokens}
        self._last_global = 0.0
        self._running = False
        self._workers: list[threading.Thread] = []
        self.sent = 0
        self.failed = 0
        self.by_channel = {name: 0 for name in channels}
        log.info("Notifier: %d bots | %d channels", len(bot_tokens), len(channels))
        self.start()

    def route(self, discount: int) -> list[int]:
        if discount >= DiscountTier.JACKPOT:
            names = ("all", "90plus")
        elif discount >= DiscountTier.HOT:
            names = ("all", "70plus")
        else:
            names = ("all",)
        return [self._channels[n] for n in names if n in self._channels]

    def queue(self, product: Product) -> None:
        text = product.format_message(self._affiliate)
        with self._queue_lock:
            for chat_id in self.route(product.discount):
                self._queue.append({"text": text, "chat_id": chat_id,
                                    "discount": product.discount})
    @property
    def pending(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def _send(self, token: str, chat_id: int, text: str) -> str:
        now = time.time()
        if now - self._last_bot[token] < 1 / 25 or now - self._last_global < 1 / 67:
            return "rate_limit:0"
        try:
            resp = requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % token,
                data={"chat_id": chat_id, "text": text}, timeout=8)
            now = time.time()
            self._last_bot[token] = now
            self._last_global = now
            if resp.status_code == 200:
                return "ok"
            if resp.status_code == 429:
                wait = resp.json().get("parameters", {}).get("retry_after", 5)
                return "rate_limit:%d" % wait
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
            result = self._send(token, msg["chat_id"], msg["text"])
            if result == "ok":
                with self._stats_lock:
                    self.sent += 1
                    for name, cid in self._channels.items():
                        if cid == msg["chat_id"]:
                            self.by_channel[name] += 1
            elif result.startswith("rate_limit:"):
                wait = int(result.split(":")[1])
                msg["_retry_after"] = time.time() + max(wait, 1)
                with self._queue_lock:
                    self._queue.append(msg)
                time.sleep(min(max(wait, 1), 10))
            else:
                with self._stats_lock:
                    self.failed += 1

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
                    "pending": self.pending, "by_channel": dict(self.by_channel)}
