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

from dns_scraper.deal_buttons import DealButtonHandler, extract_product_id

if TYPE_CHECKING:
    from dns_scraper.app import ScraperApp

log = logging.getLogger("dns_scraper.controller")

HELP_TEXT = (
    "dns-scraper controls — Myntra + Croma + Blinkit + BigBasket:\n"
    "/start — start scraping\n"
    "/stop — stop scraping\n"
    "/status — live stats per source\n"
    "/diag [source] — live probe + per-source health\n"
    "/run — force one pass over every source now\n"
    "/affiliate — show current link tag (OFF = clean links)\n"
    "/set_affiliate <query> — set tag, e.g. /set_affiliate utm_source=x&utm_campaign=y\n"
    "/clear_affiliate — back to clean links\n"
    "/add_link <url> — track a Myntra/Croma/Blinkit/BigBasket URL (auto-detected)\n"
    "/add_link <source>:<term> — track a search term, e.g. /add_link blinkit:atta\n"
    "/remove_link <url or number> — untrack\n"
    "/list_links — tracked URLs/terms per source\n"
    "/add_location <source> <line> — add a delivery point (Blinkit/BigBasket)\n"
    "/list_locations — delivery points each source is priced at\n"
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
    "/verify_bots — test every bot against every channel (matrix)\n"
    "/reset_bots — clear retired bot/chat pairings after promoting bots\n"
    "/prune — force the seen-DB retention sweep\n"
    "/help — this message\n"
    "\n"
    "Deal messages carry two buttons: 🚫 Blacklist Product (exact price only) "
    "and 🏷️ Blacklist Brand. Blacklist Brand opens an in-place prompt — "
    "✅ Block \"brand\", ✏️ Custom (type a name, or name,99), or ❌ Cancel.\n"
    "\n"
    "Blinkit/BigBasket are priced per delivery point, so a deal's footer shows "
    "which point it came from. Posting is paced to Telegram's limits: at most "
    "~18 messages/min per channel and ~20/s overall."
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
        # Telegram's update_id sequence is PER BOT, not shared. Sharing one
        # offset across pollers makes every bot but the busiest one skip its
        # own updates silently — the classic "commands do nothing" bug. Keyed
        # by token, filled in start().
        self._offsets: dict[str, int] = {}
        self._seen: dict[str, set[int]] = {}

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

    def answer_callback(self, callback_query_id: str, text: str,
                        token: str | None = None) -> None:
        """Toast shown when a deal button is pressed.

        ``token`` must be the bot that *received* the callback — Telegram only
        lets the original bot answer, and the notifier round-robins deal posts
        across every token.
        """
        if not callback_query_id:
            return
        try:
            requests.post(
                "https://api.telegram.org/bot%s/answerCallbackQuery" % (token or self._token),
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
        if low.startswith("/verify_bots") or low.startswith("/verify"):
            return self._verify_bots()
        if low.startswith("/reset_bots"):
            count = app.notifier.reset_blocked()
            return ("Cleared %d retired bot/chat pairing(s). Re-verified on the "
                    "next send." % count)
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
            if not url:
                return "Usage: /add_link <url>  or  /add_link <source>:<term>"
            # "<source>:<term>" tracks a bare search term. Q-commerce sources
            # are keyword-driven, so a term is the natural unit there.
            if "://" not in url and ":" in url:
                name, _, term = url.partition(":")
                name, term = name.strip().lower(), term.strip()
                source = app.source(name)
                if not source or not term:
                    return "Unknown source '%s'. Try: %s" % (
                        name, ", ".join(app.source_names()) or "none")
                store = source.links()
                ok = store.add(term)
                return ("%s term added. Total: %d"
                        % (source.spec.label, len(store.all()))
                        if ok else "Already tracked.")
            if not url.startswith("http"):
                return "Only http(s) URLs (or <source>:<term>) allowed."
            # Auto-route by domain.
            for host, name in (("croma.com", "croma"), ("blinkit.com", "blinkit"),
                               ("bigbasket.com", "bigbasket")):
                if host in url:
                    source = app.source(name)
                    if not source:
                        return "%s is not enabled in this deployment." % name
                    store = source.links()
                    ok = store.add(url)
                    return ("%s link added. Total: %d"
                            % (source.spec.label, len(store.all()))
                            if ok else "Already tracked.")
            if "myntra.com" not in url:
                return "Only Myntra, Croma, Blinkit or BigBasket URLs allowed."
            ok = app.links.add(url)
            return ("Link added. Total: %d" % len(app.links.all())
                    if ok else "Already tracked.")
        if low.startswith("/remove_link") or cmd.startswith("REMOVE#"):
            key = cmd.split(None, 1)[1].strip() if " " in cmd else cmd[7:].strip()
            for source in app.sources:
                store = source.links()
                if store and store.remove(key):
                    return "Removed from %s." % source.spec.label
            return "Not found. See /list_links."
        if low.startswith("/list_locations"):
            return self._list_locations()
        if low.startswith("/add_location"):
            return self._add_location(cmd)
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

    def _list_locations(self) -> str:
        app = self._app
        lines = []
        for source in app.sources:
            points = getattr(source, "points", None)
            if points is None:
                continue
            spec = source.spec
            name = spec.locations_file.name if spec.locations_file else "?"
            lines.append("%s (%d) from %s:" % (source.spec.label, len(points), name))
            if not points.all():
                lines.append("  (none resolved — add one: /add_location %s <line>)"
                             % source.name)
                continue
            for point in points.all():
                lines.append("  %s -> %s" % (point.label, point.key))
        return "\n".join(lines) or "No location-scoped sources enabled."

    def _add_location(self, cmd: str) -> str:
        app = self._app
        parts = cmd.split(None, 2)
        if len(parts) < 3:
            return ("Usage: /add_location <source> <line>\n"
                    "  /add_location blinkit 17.4027233|78.3773127|HYD-Manikonda\n"
                    "  /add_location blinkit pin:132103|Panipat\n"
                    "  /add_location bigbasket 10|0|1|Hyderabad-bbnow")
        name = parts[1].strip().lower()
        source = app.source(name)
        if source is None or getattr(source, "points", None) is None:
            available = ", ".join(s.name for s in app.sources
                                  if getattr(s, "points", None) is not None)
            return "No location-scoped source '%s'. Available: %s" % (
                name, available or "none")
        spec = source.spec
        if not spec.locations_file:
            return "Source %s has no location file." % name
        line = parts[2].strip()
        try:
            spec.locations_file.parent.mkdir(parents=True, exist_ok=True)
            with spec.locations_file.open("a", encoding="utf-8") as handle:
                handle.write("\n%s\n" % line)
        except OSError as exc:
            return "Write failed: %s" % exc
        count = source.reload_points()
        note = ""
        if not count:
            note = ("\nNothing resolved yet — the file's header explains the "
                    "exact format this source needs.")
        return ("Added `%s` to %s. %d point(s) now active for %s.%s"
                % (line, spec.locations_file.name, count, source.spec.label, note))

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
            lines.append("%s: req=%d ok=%d err=%d (403=%d 429=%d aborted=%d) runs=%d items=%d"
                         % (source.spec.label, st.get("requests", 0), st.get("success", 0),
                            st.get("errors", 0), st.get("forbidden", 0),
                            st.get("rate_limited", 0), st.get("aborted_runs", 0),
                            st.get("runs", 0), st.get("items", 0)))
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

    def _verify_bots(self) -> str:
        """Probe every (bot, channel) pairing.

        Telegram will not let a bot add another bot to a group, so the only
        way to confirm "is bot 4 an admin in the 90plus group?" is to ask each
        bot about it. This does exactly that and reports a matrix, so the
        manual part of onboarding bots becomes verifiable instead of hopeful.
        """
        app = self._app
        tokens = [t for t in app.settings.bot_tokens if t]
        if not tokens:
            return "No BOT_TOKENS configured."
        chats = [(label, cid)
                 for cid, label in sorted(app.settings.channel_plan.labels().items())]
        if not chats:
            return "No channels configured."
        lines = ["Bot / channel matrix (%d bots x %d channels):"
                 % (len(tokens), len(chats))]
        bad = 0
        for idx, tok in enumerate(tokens, 1):
            me = self._api("getMe", token=tok)
            bot_id = str((me.get("result") or {}).get("id", "")) if me.get("ok") else ""
            marks = []
            for name, cid in chats:
                if bot_id:
                    member = self._api("getChatMember", token=tok,
                                       chat_id=cid, user_id=int(bot_id))
                    status = (member.get("result") or {}).get("status") or ""
                    ok = status in ("administrator", "creator")
                else:
                    ok, status = False, "no-getMe"
                if not ok:
                    bad += 1
                marks.append("%s=%s" % (name, "OK" if ok else (status or "FAIL")))
            lines.append("bot %d (...%s): %s" % (idx, tok[-6:], "  ".join(marks)))
        lines.append("")
        if bad:
            lines.append("%d pairing(s) FAILED — add those bots as admins to "
                         "those channels, then /verify_bots again." % bad)
            lines.append("Retired pairings clear with /reset_bots.")
        else:
            lines.append("All pairings OK — every bot can post to every channel.")
        return "\n".join(lines)

    # -- long poll ---------------------------------------------------------
    def _loop(self, token: str, primary: bool) -> None:
        label = "control" if primary else token[-6:]
        log.info("Telegram poller [%s] started (admin=%d, watching %d deal chats)",
                 label, self._admin,
                 len(set(self._app.settings.channel_plan.destinations())))
        errors = 0
        deal_chats = set(self._app.settings.channel_plan.destinations())
        while self._running:
            try:
                if errors >= 5:
                    time.sleep(30)
                    errors = 0
                    continue
                data = self._api("getUpdates", token=token,
                                 offset=self._offsets.get(token, 0) + 1,
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
                    self._process_update(update, token, deal_chats)
                time.sleep(0.5)
            except Exception as exc:
                errors += 1
                log.error("Poller [%s] loop error: %s", label, exc)
                time.sleep(3)
        log.info("Poller [%s] stopped", label)

    def _process_update(self, update: dict, token: str, deal_chats: set[int]) -> None:
        """De-duplicate and advance THIS bot's cursor, then dispatch.

        Telegram's update_id sequence is per bot, so the cursor and the seen-set
        are keyed by token. Sharing them across poller threads is what makes
        commands to all but the busiest bot silently disappear: one bot's high
        update ids advance the shared cursor past every other bot's pending
        updates, and Telegram then never returns them.
        """
        uid = update.get("update_id", 0)
        seen = self._seen.setdefault(token, set())
        if uid in seen:
            return
        seen.add(uid)
        if len(seen) > 2000:
            self._seen[token] = {uid}
        self._offsets[token] = max(self._offsets.get(token, 0), uid)
        self._dispatch(update, token, deal_chats)

    def _dispatch(self, update: dict, token: str, deal_chats: set[int]) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            buttons: DealButtonHandler | None = getattr(self._app, "_deal_buttons", None)
            if buttons:
                buttons.handle_callback(cq, token)
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
            response = buttons.handle_text(text, chat_id, token)
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
        for tok in unique:
            self._offsets[tok] = 0
            self._seen[tok] = set()
        for idx, tok in enumerate(unique):
            thread = threading.Thread(target=self._loop, args=(tok, idx == 0),
                                      daemon=True, name="tg-poller-%d" % idx)
            thread.start()
            self._threads.append(thread)
        log.info("Controller started: %d poller thread(s) (one per bot token, "
                 "each with its own update offset)", len(self._threads))

    def stop(self) -> None:
        self._running = False
        for thread in self._threads:
            thread.join(timeout=5)

    def _post(self, method: str, token: str | None = None, **data) -> dict:
        try:
            resp = requests.post(
                "https://api.telegram.org/bot%s/%s" % (token or self._token, method),
                json=data, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            log.debug("%s -> HTTP %d %s", method, resp.status_code, resp.text[:200])
        except Exception as exc:
            log.debug("Controller API error: %s", exc)
        return {}

    def edit_markup(self, chat_id: int, message_id: int, keyboard: dict,
                    token: str | None = None) -> bool:
        """Rewrite a message's inline keyboard in place.

        Used for the interactive deal buttons: the same message swaps from
        "Blacklist Brand" to a Confirm/Custom/Cancel prompt and then to the
        committed rule, instead of the bot posting a new DM per step.
        """
        if not chat_id or not message_id:
            return False
        result = self._post("editMessageReplyMarkup", token=token,
                            chat_id=chat_id, message_id=message_id,
                            reply_markup=keyboard)
        return bool(result.get("ok"))

    def reply(self, text: str, chat_id: int | None = None,
              keyboard: dict | None = None, token: str | None = None) -> None:
        payload = {"chat_id": chat_id or self._admin, "text": text,
                   "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        self._post("sendMessage", token=token, **payload)

    def verify(self) -> str:
        me = self._api("getMe")
        username = (me.get("result") or {}).get("username", "?")
        log.info("Control bot polling as @%s (admin=%d)", username, self._admin)
        return username
