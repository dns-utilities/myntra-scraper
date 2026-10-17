"""Multi-bot Telegram deal sender with per-bot, per-chat and global rate limits.

Why three buckets
-----------------
A deal feed is bursty by nature: one scrape cycle can surface hundreds of new
products at once, and a redeploy re-runs the pipeline from scratch. Telegram's
real ceilings are roughly:

* ~30 messages/second in total,
* ~20 messages/minute to the *same* chat.

The previous pacing counted only "per bot" and "global", which misses the one
that actually bites. Ten bots can each sit comfortably under their own limit
while collectively dumping a thousand messages into one channel in a few
seconds -- exactly the flood this module now makes impossible. So the gate
models all three ceilings and the per-chat bucket is the binding one.

Anything the gate cannot admit *right now* is re-queued with a delay instead of
sent, so a burst drains at Telegram's pace rather than ours. Three further
guards keep an unattended run honest:

* a hard **queue ceiling**, so a pathological cycle cannot grow memory without
  bound (excess is dropped and counted, never silently lost),
* an on-disk **outbox**, so a graceful shutdown hands over its unsent messages
  to the next boot instead of dropping deals that were already marked "seen",
* **429 penalties**, so a real ``retry_after`` closes the offending chat's
  bucket for exactly as long as Telegram asked.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

import requests

from dns_scraper.channels import ChannelPlan
from dns_scraper.deal_buttons import deal_keyboard
from dns_scraper.models import Product

log = logging.getLogger("dns_scraper.notifier")


class SendLimits:
    """Tunable ceilings for the outbound gate. Every value comes from env."""

    def __init__(self, global_per_sec: float = 20.0, per_bot_per_sec: float = 15.0,
                 per_chat_per_min: float = 18.0, max_queue: int = 5000,
                 burst: int = 2) -> None:
        self.global_per_sec = max(0.1, float(global_per_sec))
        self.per_bot_per_sec = max(0.1, float(per_bot_per_sec))
        self.per_chat_per_min = max(0.1, float(per_chat_per_min))
        self.max_queue = max(1, int(max_queue))
        self.burst = max(1, int(burst))

    @property
    def per_chat_per_sec(self) -> float:
        return self.per_chat_per_min / 60.0

    def describe(self) -> str:
        return ("global<=%.0f/s · bot<=%.0f/s · chat<=%.0f/min · queue<=%d"
                % (self.global_per_sec, self.per_bot_per_sec,
                   self.per_chat_per_min, self.max_queue))


class _Bucket:
    """One token bucket, plus a penalty that can hold it shut on a real 429."""

    def __init__(self, rate_per_sec: float, burst: int = 1) -> None:
        self._rate = max(float(rate_per_sec), 1e-3)
        self._capacity = float(max(1, burst))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._penalty_until = 0.0

    def _refill(self, now: float) -> None:
        self._tokens = min(self._capacity,
                           self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    def wait(self, now: float | None = None) -> float:
        """Seconds until a slot is available (0.0 when one is ready now)."""
        now = time.monotonic() if now is None else now
        self._refill(now)
        need = (1.0 - self._tokens) / self._rate if self._tokens < 1.0 else 0.0
        return max(need, self._penalty_until - now)

    def take(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._refill(now)
        self._tokens = max(0.0, self._tokens - 1.0)

    def penalise(self, seconds: float) -> None:
        """Hold this bucket shut for ``seconds`` (Telegram said retry_after)."""
        now = time.monotonic()
        self._penalty_until = max(self._penalty_until, now + max(0.0, seconds))
        self._tokens = 0.0
        self._updated = now

    def snapshot(self) -> float:
        return round(self.wait(), 2)


class SendGate:
    """Admission control across the global, per-bot and per-chat buckets.

    ``acquire`` is deliberately all-or-nothing: it consumes a token from every
    bucket only when every bucket has one, so a rejected message never eats
    capacity it will not use.
    """

    def __init__(self, limits: SendLimits) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._global = _Bucket(limits.global_per_sec, limits.burst)
        self._bots: dict[str, _Bucket] = {}
        self._chats: dict[int, _Bucket] = {}

    def _bot(self, token: str) -> _Bucket:
        bucket = self._bots.get(token)
        if bucket is None:
            bucket = self._bots[token] = _Bucket(self.limits.per_bot_per_sec,
                                                 self.limits.burst)
        return bucket

    def _chat(self, chat_id: int) -> _Bucket:
        bucket = self._chats.get(chat_id)
        if bucket is None:
            bucket = self._chats[chat_id] = _Bucket(self.limits.per_chat_per_sec,
                                                    self.limits.burst)
        return bucket

    def acquire(self, token: str, chat_id: int) -> float:
        """0.0 when admitted; otherwise seconds to wait (nothing consumed)."""
        with self._lock:
            now = time.monotonic()
            bot, chat = self._bot(token), self._chat(chat_id)
            longest = max(self._global.wait(now), bot.wait(now), chat.wait(now))
            if longest > 0:
                return longest
            self._global.take(now)
            bot.take(now)
            chat.take(now)
            return 0.0

    def penalise(self, chat_id: int, seconds: float) -> None:
        with self._lock:
            self._chat(chat_id).penalise(seconds)

    def snapshot(self) -> dict:
        with self._lock:
            return {"global_wait": self._global.snapshot(),
                    "chats": {cid: b.snapshot() for cid, b in self._chats.items()}}


class DealNotifier:
    def __init__(self, bot_tokens: list[str], plan: ChannelPlan,
                 affiliate_tag: str | dict | None = "",
                 limits: SendLimits | None = None) -> None:
        self._tokens = [t for t in (bot_tokens or []) if t]
        self.plan = plan
        self._affiliate = ""
        self.set_affiliate_tag(affiliate_tag)
        self.limits = limits or SendLimits()
        self.gate = SendGate(self.limits)

        self._queue: deque = deque()
        self._queue_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._running = False
        self._workers: list[threading.Thread] = []
        # (bot, chat) pairings that cannot work, so round-robin stops feeding
        # them. Self-healing: a bot promoted to admin later stays excluded
        # until restart, which is exactly what /verify_bots surfaces.
        self._bot_chat_blocked: set[tuple[str, int]] = set()
        self._bot_chat_fails: dict[tuple[str, int], int] = {}
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._warned_no_channel: set[str] = set()
        # Keyed by chat id; ChannelPlan.labels() turns those into names.
        self.by_channel: dict[int, int] = {c: 0 for c in plan.destinations()}
        log.info("Notifier: %d bots | %d destinations | caps %s",
                 len(self._tokens), len(self.by_channel), self.limits.describe())
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
        """The destinations for one deal. See ``channels.ChannelPlan.route``."""
        return self.plan.route(discount, source)

    def queue(self, product: Product) -> bool:
        """Enqueue one product for every channel it routes to.

        Returns False when the deal was dropped (no target channel, or the
        queue ceiling was hit) so the caller can count it.
        """
        targets = self.route(product.discount, product.source)
        if not targets:
            # Once per source: a missing base channel would otherwise emit one
            # error line per product, which is its own kind of flood.
            if product.source not in self._warned_no_channel:
                self._warned_no_channel.add(product.source)
                log.error("Notifier: no base channel for source %r (set "
                          "CHANNEL_BASE_%s or CHANNEL_BASE_DEFAULT) — its "
                          "sub-tier deals are being dropped",
                          product.source, product.source.upper())
            with self._stats_lock:
                self.dropped += 1
            return False
        text = product.format_message(self._affiliate)
        keyboard = deal_keyboard(product.product_id, product.price, product.source)
        with self._queue_lock:
            if len(self._queue) + len(targets) > self.limits.max_queue:
                over = len(targets)
                if self.dropped < 10 or self.dropped % 100 < over:
                    log.error("Notifier queue at ceiling %d — dropping %d message(s) "
                              "for %s", self.limits.max_queue, over, product.product_id)
                with self._stats_lock:
                    self.dropped += over
                return False
            for chat_id in targets:
                self._queue.append({"text": text, "chat_id": chat_id,
                                    "discount": product.discount,
                                    "source": product.source,
                                    "keyboard": keyboard})
        return True

    @property
    def pending(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def pending_by_chat(self) -> dict[int, int]:
        with self._queue_lock:
            counts: dict[int, int] = {}
            for msg in self._queue:
                cid = msg["chat_id"]
                counts[cid] = counts.get(cid, 0) + 1
            return counts

    # -- sending -----------------------------------------------------------
    def _send(self, token: str, chat_id: int, text: str,
              keyboard: dict | None = None) -> str:
        wait = self.gate.acquire(token, chat_id)
        if wait > 0:
            # Never send past a Telegram ceiling; hand the slot back to the
            # worker, which re-queues with a delay.
            return "rate_limit:%.2f" % wait
        try:
            payload = {"chat_id": chat_id, "text": text}
            if keyboard:
                payload["reply_markup"] = json.dumps(keyboard)
            resp = requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % token,
                data=payload, timeout=8)
            if resp.status_code == 200:
                return "ok"
            if resp.status_code == 429:
                wait = 5
                try:
                    wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    pass
                # Close this chat's bucket for exactly as long as asked.
                self.gate.penalise(chat_id, wait)
                log.warning("Telegram 429 on chat %s — pausing it %ds", chat_id, wait)
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

    def _requeue(self, msg: dict) -> None:
        with self._queue_lock:
            self._queue.append(msg)

    def _worker(self, token: str) -> None:
        while self._running:
            with self._queue_lock:
                msg = self._queue.popleft() if self._queue else None
            if not msg:
                time.sleep(0.05)
                continue
            if msg.get("_retry_after", 0) > time.time():
                self._requeue(msg)
                time.sleep(0.25)
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
                    self._requeue(msg)
                time.sleep(0.05)
                continue
            result = self._send(token, chat_id, msg["text"], msg.get("keyboard"))
            if result == "ok":
                with self._stats_lock:
                    self.sent += 1
                    self._bot_chat_fails.pop((token, chat_id), None)
                    self.by_channel[chat_id] = self.by_channel.get(chat_id, 0) + 1
            elif result.startswith("rate_limit:"):
                wait = float(result.split(":")[1] or 0)
                msg["_retry_after"] = time.time() + wait
                self._requeue(msg)
                # Rotate rather than sleep the whole wait: another chat that is
                # not throttled can still make progress in the meantime.
                time.sleep(min(max(wait, 0.02), 0.5))
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

    # -- outbox (graceful exit) --------------------------------------------
    def persist(self, path: Path) -> int:
        """Hand unsent messages to disk so the next boot finishes the job."""
        with self._queue_lock:
            items = list(self._queue)
        if not items:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(json.dumps(i) for i in items),
                           encoding="utf-8")
            tmp.replace(path)
        except (OSError, TypeError) as exc:
            log.error("Outbox write failed: %s", exc)
            return 0
        log.info("Outbox: persisted %d unsent message(s) to %s", len(items), path)
        return len(items)

    def restore(self, path: Path) -> int:
        """Re-admit messages persisted by a previous, unfinished run."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        restored = 0
        with self._queue_lock:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("chat_id"):
                    self._queue.append(msg)
                    restored += 1
        if restored:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            log.info("Outbox: restored %d unsent message(s) from the previous run",
                     restored)
        return restored

    # -- lifecycle ---------------------------------------------------------
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
            out = {"sent": self.sent, "failed": self.failed,
                   "dropped": self.dropped, "by_channel": dict(self.by_channel),
                   "blocked_pairings": len(self._bot_chat_blocked)}
        # Read the queue-derived numbers outside the stats lock: taking
        # _queue_lock while holding _stats_lock here would invert the order the
        # workers use and could deadlock.
        out["pending"] = self.pending
        out["pending_by_chat"] = self.pending_by_chat()
        out["gate"] = self.gate.snapshot()
        return out

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
