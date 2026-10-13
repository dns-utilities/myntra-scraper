"""Telegram control bot — admin commands, deal-channel legacy commands, and the
deal-message buttons.

Buttons matter for one non-obvious reason: a ``callback_query`` is delivered to
the bot that *sent* the message, and the notifier round-robins deal posts
across every token in ``BOT_TOKENS``. So each token gets its own long-poll
thread; token #1 additionally sends the replies. Drop that and half the buttons
would silently do nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

import requests

from myntra_scraper.deal_buttons import DealButtonHandler, extract_product_id

if TYPE_CHECKING:
    from myntra_scraper.app import ScraperApp

log = logging.getLogger("myntra_scraper.controller")

HELP_TEXT = (
    "Myntra + Croma deal scraper controls:\n"
    "/start — start scraping\n"
    "/stop — stop scraping\n"
    "/status — live stats per source\n"
    "/diag [source] — live probe + per-source health\n"
    "/run — force one pass over every source now\n"
    "/affiliate — show current link tag (OFF = clean links)\n"
    "/set_affiliate <query> — set tag, e.g. /set_affiliate utm_source=x&utm_campaign=y\n"
    "/clear_affiliate — back to clean links\n"
    "/add_link <url> — track a Myntra OR Croma URL (auto-detected)\n"
    "/remove_link <url or number> — untrack\n"
    "/list_links — tracked URLs (Myntra + Croma)\n"
    "/croma_stores — refresh Croma store-name map\n"
    "/stock <product_id> — which Croma stores carry it\n"
    "/add_blacklist <brand> <min_%> — skip brand below % (e.g. /add_blacklist Nike 60)\n"
    "/remove_blacklist <brand>\n"
    "/list_blacklist\n"
    "/blacklist_product <product_id> <price> — block one product at one price\n"
    "/unblock_product <product_id> <price> — lift that block\n"
    "/blacklist_products — list priced product blocks\n"
    "/clear_product <product_id> — re-alert as new\n"
    "/clear_all — wipe seen DB\n"
    "/autoclear — auto-clear config + eligible brand count\n"
    "/autoclear_add <brand> — let a brand re-post on the cooldown\n"
    "/autoclear_remove <brand> — stop re-posting it\n"
    "/prune — force the seen-DB retention sweep\n"
    "/help — this message\n"
    "\n"
    "Deal messages carry two buttons: 🚫 Blacklist Product (exact price only) "
    "and 🏷️ Blacklist Brand (asks for a %, then confirms)."
)


class ControllerBot:
    def __init__(self, bot_token: str, admin_chat_id: int, app: "ScraperApp",
                 bot_tokens: list[str] | None = None) -> None:
        self._token = bot_token
        self._tokens = list(bot_tokens or [bot_token])
        self._admin = admin_chat_id
        self._app = app
        self._running = False
        self._threads: list[threading.Thread] = []
        self._offset = 0
        self._seen: set[int] = set()

    # -- low-level API -----------------------------------------------------
    def _api(self, method: str, token: str | None = None, **params) -> dict:
        try:
            resp = requests.get(
                "https://api.telegram.org/bot%s/%s" % (token or self._token, method),
                params=params, timeout=35)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            log.debug("Controller API error: %s", exc)
        return {}

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        """Toast shown when a deal button is pressed."""
        if not callback_query_id:
            return
        try:
            requests.post(
                "https://api.telegram.org/bot%s/answerCallbackQuery" % self._token,
                data={"callback_query_id": callback_query_id, "text": text[:190]},
                timeout=10)
        except Exception as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    # -- command dispatch --------------------------------------------------
    def _handle(self, text: str, chat_id: int, reply_text: str = "") -> str | None:
        cmd = text.strip()
        low = cmd.lower()
        app = self._app
        if low.startswith("/start"):
            if app.scraping_enabled:
                return "Already running."
            app.scraping_enabled = True
            return "Scraping STARTED."
        if low.startswith("/stop"):
            app.scraping_enabled = False
            return "Scraping STOPPED. Queue keeps draining."
        if low.startswith("/status"):
            return app.status_text()
        if low.startswith("/diag"):
            return self._diag(cmd)
        if low.startswith("/run"):
            return "Run queued — one pass over every source."
        if low.startswith("/help"):
            return HELP_TEXT
        if low.startswith("/list_links") or cmd.strip() == "LIST#":
            return self._list_links()
        if low.startswith("/croma_stores"):
            count = app.croma_stores.fetch_and_save()
            return ("Croma store map refreshed: %d stores." % count if count
                    else "Store fetch failed — will retry next /stock.")
        if low.startswith("/stock"):
            pid = (cmd.split(None, 1)[1] if " " in cmd else "").strip()
            if not pid:
                return "Usage: /stock <croma_product_id>"
            row = app.croma_seen.get_product(pid) if app.croma_seen else None
            if not row or not row.get("store_codes"):
                return "No store codes cached for %s yet (try after next cycle)." % pid
            return "%s — %s\n%s" % (pid, row.get("name", ""),
                                    app.croma_stores.resolve_codes(row["store_codes"]))
        if low.startswith("/add_link") or cmd.startswith("ADD#"):
            url = cmd.split(None, 1)[1].strip() if " " in cmd else cmd[4:].strip()
            if not url.startswith("http"):
                return "Only http(s) URLs allowed."
            # Auto-route by domain: Croma -> croma store, else Myntra.
            if "croma.com" in url:
                store = app.croma_links
                ok = store.add(url) if store else False
                return ("Croma link added. Total: %d" % len(store.all())
                        if ok else "Already tracked.")
            if "myntra.com" not in url:
                return "Only Myntra or Croma URLs allowed."
            ok = app.links.add(url)
            return ("Link added. Total: %d" % len(app.links.all())
                    if ok else "Already tracked.")
        if low.startswith("/remove_link") or cmd.startswith("REMOVE#"):
            key = cmd.split(None, 1)[1].strip() if " " in cmd else cmd[7:].strip()
            if "croma.com" in key and app.croma_links:
                return "Removed." if app.croma_links.remove(key) else "Not found. See /list_links."
            if app.links and app.links.remove(key):
                return "Removed."
            if app.croma_links and app.croma_links.remove(key):
                return "Removed."
            return "Not found. See /list_links."
        if low.startswith("/list_blacklist"):
            rules = app.blacklist.all()
            if not rules:
                return "Blacklist empty."
            return "Brand blacklist (%d):\n" % len(rules) + "\n".join(
                "- %s: skip below %d%%" % (b, p) for b, p in sorted(rules.items()))
        if low.startswith("/add_blacklist"):
            try:
                _, brand, pct = cmd.split(None, 2)
                app.blacklist.add(brand, int(pct))
                return "Blacklisted '%s' below %d%%." % (brand.lower(), int(pct))
            except ValueError:
                return "Usage: /add_blacklist <brand> <0-100>"
        if low.startswith("/remove_blacklist"):
            parts = cmd.split(None, 1)
            if len(parts) < 2:
                return "Usage: /remove_blacklist <brand>"
            ok = app.blacklist.remove(parts[1])
            return "Removed '%s'." % parts[1].lower() if ok else "Rule not found."
        if low.startswith("/blacklist_products"):
            return self._list_product_blocks()
        if low.startswith("/blacklist_product"):
            return self._add_product_block(cmd)
        if low.startswith("/unblock_product"):
            return self._remove_product_block(cmd)
        if low.startswith("/clear_product") or cmd.startswith("CLEAR#"):
            pid = (cmd.split(None, 1)[1] if " " in cmd else cmd[6:]).strip()
            pid = pid or extract_product_id(reply_text)
            if not pid:
                return "Usage: /clear_product <product_id>"
            erased = False
            for source in app.sources:
                if source.seen().forget(pid):
                    erased = True
            return "Forgot %s." % pid if erased else "%s not in DB." % pid
        if low.startswith("/clear_all") or cmd.strip() == "CLEARALL#":
            total = sum(s.seen().clear() for s in app.sources)
            return "Wiped %d products." % total
        if low.startswith("/prune"):
            removed = sum(s.seen().prune(app.settings.seen_retention_days)
                          for s in app.sources)
            return "Pruned %d seen rows (retention %dd)." % (
                removed, app.settings.seen_retention_days)
        if low.startswith("/autoclear"):
            return self._autoclear(cmd)
        if low.startswith("/affiliate"):
            tag = app.affiliate_tag
            if not tag:
                return ("Affiliate: OFF — posting clean links\n"
                        "https://www.myntra.com/<ID>\n"
                        "Set one: /set_affiliate utm_source=x&utm_campaign=y")
            return "Affiliate: ON\n%s\nPreview: https://www.myntra.com/<ID>?%s" % (tag, tag[:120])
        if low.startswith("/set_affiliate"):
            tag = cmd.split(None, 1)[1].strip() if " " in cmd else ""
            if not tag:
                return ("Usage: /set_affiliate <query>\n"
                        "e.g. /set_affiliate utm_source=ugc_affiliate&utm_campaign=YOURTAG")
            tag = app.set_affiliate_tag(tag)
            return ("Affiliate ON.\nNew links: https://www.myntra.com/<ID>?%s\n"
                    "Persists on volume; survives redeploys." % tag[:160])
        if low.startswith("/clear_affiliate"):
            app.set_affiliate_tag("")
            return "Affiliate OFF — back to clean links."
        return None

    # -- subcommands -------------------------------------------------------
    def _list_links(self) -> str:
        app = self._app
        sources = [s for s in app.sources if s.links().all()]
        if not sources:
            return "No links. Use /add_link <url>"
        lines = []
        for source in sources:
            urls = source.links().all()
            lines.append("%s (%d):" % (source.spec.label, len(urls)))
            lines += ["  %d. %s" % (i, u[:76]) for i, u in enumerate(urls[:20], 1)]
            if len(urls) > 20:
                lines.append("  ...and %d more" % (len(urls) - 20))
        return "\n".join(lines)

    def _diag(self, cmd: str) -> str:
        app = self._app
        parts = cmd.split()
        wanted = parts[1].strip().lower() if len(parts) > 1 else ""
        targets = [s for s in app.sources if s.name == wanted] if wanted else app.sources
        if not targets:
            return "No source named '%s'. Sources: %s" % (
                wanted, ", ".join(s.name for s in app.sources) or "none")
        lines = []
        for source in targets:
            st = source.stats()
            lines.append("%s: req=%d ok=%d err=%d (403=%d 429=%d) runs=%d items=%d"
                         % (source.spec.label, st.get("requests", 0), st.get("success", 0),
                            st.get("errors", 0), st.get("forbidden", 0),
                            st.get("rate_limited", 0), st.get("runs", 0),
                            st.get("items", 0)))
            lines.append("  state: %s | last run %.1fs | last error: %s"
                         % ("PARKED %.0fs" % st.get("cooldown", 0) if st.get("open") else "ok",
                            st.get("last_duration", 0.0), st.get("last_error") or "none"))
            if hasattr(source, "diag"):
                lines.append("  %s" % source.diag())
        return "\n".join(lines)

    def _add_product_block(self, cmd: str) -> str:
        app = self._app
        parts = cmd.split()
        if len(parts) < 3:
            return "Usage: /blacklist_product <product_id> <price>"
        pid, price_s = parts[1], parts[2]
        try:
            price = int(float(price_s))
        except ValueError:
            return "Price must be a number."
        brand = ""
        for source in app.sources:
            row = source.seen().get_product(pid)
            if row:
                brand = row.get("brand") or ""
                break
        app.blacklist.add_product(pid, price, source="", brand=brand)
        return "Blocked %s at ₹%d only." % (pid, price)

    def _remove_product_block(self, cmd: str) -> str:
        app = self._app
        parts = cmd.split()
        if len(parts) < 3:
            return "Usage: /unblock_product <product_id> <price>"
        try:
            price = int(float(parts[2]))
        except ValueError:
            return "Price must be a number."
        ok = app.blacklist.remove_product(parts[1], price)
        return "Unblocked %s at ₹%d." % (parts[1], price) if ok else "No such block."

    def _list_product_blocks(self) -> str:
        blocks = self._app.blacklist.product_blocks()
        if not blocks:
            return "No priced product blocks."
        lines = ["Product blocks (%d):" % len(blocks)]
        for b in blocks[:30]:
            lines.append("- %s ₹%s · %s · %s" % (
                b["product_id"], b["price"], b.get("brand") or "?", b.get("title") or ""))
        if len(blocks) > 30:
            lines.append("...and %d more" % (len(blocks) - 30))
        return "\n".join(lines)

    def _autoclear(self, cmd: str) -> str:
        app = self._app
        parts = cmd.split()
        if len(parts) == 1:
            policy = app.auto_clear_policy
            budget = policy.snapshot()
            return ("Auto-clear: %s\nbrands=%d · min discount=%d%% · cooldown=%dh (+<=%dh jitter)\n"
                    "budget=%d/hour · used this hour=%d · deferred=%d\n\n"
                    "A deal from one of these brands re-posts once the cooldown "
                    "elapses, even at the same price. Re-posts are capped per "
                    "hour so a restart cannot flood the channel.\n\n"
                    "/autoclear_list — show brands\n"
                    "/autoclear_add <brand> — add one\n"
                    "/autoclear_remove <brand> — remove one"
                    % ("ON" if policy.enabled else "OFF",
                       len(app.brand_registry), policy.min_discount,
                       policy.hours, policy.jitter_hours,
                       policy.max_per_hour, budget["used_this_hour"],
                       budget["deferred"]))
        sub = parts[1].lower()
        if sub == "list":
            brands = app.brand_registry.all()
            if not brands:
                return "Auto-clear brand list is empty."
            shown = sorted(brands, key=lambda b: -brands[b])[:40]
            return "Auto-clear brands (%d, top %d):\n%s" % (
                len(brands), len(shown),
                "\n".join("- %s (%d deals seen)" % (b, brands[b]) for b in shown))
        if sub == "add":
            if len(parts) < 3:
                return "Usage: /autoclear_add <brand>"
            brand = " ".join(parts[2:]).strip()
            name = app.brand_registry.add(brand)
            app.brand_registry.save(app.settings.auto_clear_brands_file)
            return "Added '%s' to auto-clear. Total: %d brands." % (name, len(app.brand_registry))
        if sub == "remove":
            if len(parts) < 3:
                return "Usage: /autoclear_remove <brand>"
            brand = " ".join(parts[2:]).strip()
            ok = app.brand_registry.remove(brand)
            if ok:
                app.brand_registry.save(app.settings.auto_clear_brands_file)
                return "Removed '%s'. Remaining: %d brands." % (brand, len(app.brand_registry))
            return "'%s' is not in the auto-clear list." % brand
        return "Usage: /autoclear [list|add <brand>|remove <brand>]"

    # -- long poll ---------------------------------------------------------
    def _loop(self, token: str, primary: bool) -> None:
        label = "control" if primary else token[-6:]
        log.info("Telegram poller [%s] started (admin=%d, watching %d deal chats)",
                 label, self._admin, len(set(self._app.settings.channels.values())))
        errors = 0
        deal_chats = set(self._app.settings.channels.values())
        while self._running:
            try:
                if errors >= 5:
                    time.sleep(30)
                    errors = 0
                    continue
                data = self._api("getUpdates", token=token, offset=self._offset + 1,
                                 timeout=30,
                                 allowed_updates=["message", "channel_post", "callback_query"])
                # getUpdates conflict (409) = same bot token polling elsewhere.
                if data.get("error_code") == 409 or "conflict" in str(data.get("description", "")).lower():
                    log.error("Poller [%s] CONFLICT: %s — another instance (local run? "
                              "old deploy?) is polling this bot token. Stop it or "
                              "commands/replies race.", label,
                              data.get("description", "terminated by other getUpdates"))
                    time.sleep(30)
                    continue
                errors = 0
                for update in data.get("result", []):
                    uid = update.get("update_id", 0)
                    if uid in self._seen:
                        continue
                    self._seen.add(uid)
                    self._offset = max(self._offset, uid)
                    if len(self._seen) > 2000:
                        self._seen = {uid}
                    self._dispatch(update, token, deal_chats)
                time.sleep(0.5)
            except Exception as exc:
                errors += 1
                log.error("Poller [%s] loop error: %s", label, exc)
                time.sleep(3)
        log.info("Poller [%s] stopped", label)

    def _dispatch(self, update: dict, token: str, deal_chats: set[int]) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            buttons: DealButtonHandler | None = getattr(self._app, "_deal_buttons", None)
            if buttons:
                buttons.handle_callback(cq)
            return
        msg = update.get("message") or update.get("channel_post") or {}
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id", 0)
        if not text:
            return
        is_admin = chat_id == self._admin
        legacy = ("ADD#", "REMOVE#", "LIST#", "CLEAR", "STATUS#", "HELP#", "#")
        is_legacy = chat_id in deal_chats and text.startswith(legacy)
        if not (is_admin or is_legacy):
            # Expected, not exceptional: the bot sits in the deal channels, so
            # every posted deal arrives as an update. Only DEBUG-worthy.
            log.debug("Controller ignoring msg from chat %s: %s",
                      chat_id, text[:60])
            return
        # An open button conversation owns the next message, whatever it says.
        buttons: DealButtonHandler | None = getattr(self._app, "_deal_buttons", None)
        if buttons and is_admin and buttons.pending_prompt(chat_id):
            response = buttons.handle_text(text, chat_id)
            if response:
                self.reply(response, chat_id)
            return
        reply_to = msg.get("reply_to_message") or {}
        try:
            response = self._handle(text, chat_id, reply_to.get("text", ""))
        except Exception as exc:
            log.error("Command '%s' failed: %s", text[:40], exc, exc_info=True)
            response = "Command failed: %s" % exc
        if response:
            self.reply(response, None if is_admin else chat_id)
            log.info("CMD from %s: %s", chat_id, text[:60])

    def start(self) -> None:
        if self._running:
            return
        self.verify()
        self._running = True
        unique: list[str] = []
        for tok in self._tokens:
            if tok and tok not in unique:
                unique.append(tok)
        for idx, tok in enumerate(unique):
            thread = threading.Thread(target=self._loop, args=(tok, idx == 0),
                                      daemon=True, name="tg-poller-%d" % idx)
            thread.start()
            self._threads.append(thread)
        log.info("Controller started: %d poller thread(s)", len(self._threads))

    def stop(self) -> None:
        self._running = False
        for thread in self._threads:
            thread.join(timeout=5)

    def _post(self, method: str, **data) -> dict:
        try:
            resp = requests.post(
                "https://api.telegram.org/bot%s/%s" % (self._token, method),
                json=data, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            log.debug("Controller API error: %s", exc)
        return {}

    def reply(self, text: str, chat_id: int | None = None,
              keyboard: dict | None = None) -> None:
        payload = {"chat_id": chat_id or self._admin, "text": text,
                   "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        self._post("sendMessage", **payload)

    def verify(self) -> str:
        me = self._api("getMe")
        username = (me.get("result") or {}).get("username", "?")
        log.info("Control bot polling as @%s (admin=%d)", username, self._admin)
        return username
