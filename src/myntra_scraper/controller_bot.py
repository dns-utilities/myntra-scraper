"""Telegram control bot — admin-only commands for the scraper.

Commands: /start /stop /status /add_link /remove_link /list_links
          /add_blacklist /remove_blacklist /list_blacklist
          /clear_product /clear_all /help
Legacy channel commands (ADD# REMOVE# LIST# STATUS# CLEAR# SPAMADD# ...) also
handled here for the deal channels.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Callable

import requests

if TYPE_CHECKING:
    from myntra_scraper.app import ScraperApp

log = logging.getLogger("myntra_scraper.controller")

HELP_TEXT = (
    "Myntra scraper controls:\n"
    "/start — start scraping\n"
    "/stop — stop scraping\n"
    "/status — live stats\n"
    "/diag — live Myntra probe (token + 1 search)\n"
    "/add_link <myntra url> — track a search\n"
    "/remove_link <url or number> — untrack\n"
    "/list_links — tracked searches\n"
    "/add_blacklist <brand> <min_%> — skip brand below % (e.g. /add_blacklist Nike 60)\n"
    "/remove_blacklist <brand>\n"
    "/list_blacklist\n"
    "/clear_product <product_id> — re-alert as new\n"
    "/clear_all — wipe seen DB\n"
    "/help — this message"
)


class ControllerBot:
    def __init__(self, bot_token: str, admin_chat_id: int, app: "ScraperApp") -> None:
        self._token = bot_token
        self._admin = admin_chat_id
        self._app = app
        self._running = False
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._seen: set[int] = set()

    # -- low-level API -----------------------------------------------------
    def _api(self, method: str, **params) -> dict:
        try:
            resp = requests.get(
                "https://api.telegram.org/bot%s/%s" % (self._token, method),
                params=params, timeout=35)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            log.debug("Controller API error: %s", exc)
        return {}


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
            # Live Myntra connectivity probe: token fetch + 1 search page.
            # Tells you in Telegram whether the 403 is fixed, no redeploy needed.
            try:
                import asyncio as _asyncio
                from curl_cffi.requests import AsyncSession as _AsyncSession
                from myntra_scraper.search_api import build_api_request as _build, parse_search_url as _parse
                tok = app.tokens.get_token(force_refresh=True)
                if not tok:
                    return "diag: token fetch FAILED (check network / impersonation)"
                urls = app.links.all()
                if not urls:
                    return "diag: token OK (%s…), but no links tracked" % tok[:8]
                info = _parse(urls[0])
                api_url, params = _build(info, 0)

                async def _probe():
                    cookies = app.tokens.cookies_snapshot()
                    async with _AsyncSession(impersonate=app.tokens.impersonate,
                                             cookies=cookies or None) as sess:
                        r = await sess.get(api_url, params=params,
                                           headers=app.scraper._headers(tok, urls[0]),
                                           timeout=20)
                        return r.status_code, (r.text or "")[:160]

                code, body = _asyncio.run(_probe())
                verdict = "OK — 403 fixed" if code == 200 else (
                    "still blocked (%d). Wait 10 min, lower SCRAPE_CONCURRENCY, check Railway IP" % code)
                return "diag: token OK | search HTTP %d | %s\n%s" % (code, verdict, body)
            except Exception as exc:
                return "diag error: %s" % exc
        if low.startswith("/help"):
            return HELP_TEXT
        if low.startswith("/list_links") or cmd.strip() == "LIST#":
            urls = app.links.all()
            if not urls:
                return "No links. Use /add_link <myntra url>"
            lines = ["Tracked links (%d):" % len(urls)]
            lines += ["%d. %s" % (i, u[:80]) for i, u in enumerate(urls[:20], 1)]
            if len(urls) > 20:
                lines.append("...and %d more" % (len(urls) - 20))
            return "\n".join(lines)
        if low.startswith("/add_link") or cmd.startswith("ADD#"):
            url = cmd.split(None, 1)[1].strip() if " " in cmd else cmd[4:].strip()
            if not url.startswith("http") or "myntra.com" not in url:
                return "Only Myntra URLs allowed."
            ok = app.links.add(url)
            return ("Link added. Total: %d" % len(app.links.all())
                    if ok else "Already tracked.")
        if low.startswith("/remove_link") or cmd.startswith("REMOVE#"):
            key = cmd.split(None, 1)[1].strip() if " " in cmd else cmd[7:].strip()
            return "Removed." if app.links.remove(key) else "Not found. See /list_links."
        if low.startswith("/list_blacklist"):
            rules = app.blacklist.all()
            if not rules:
                return "Blacklist empty."
            return "Blacklist (%d):\n" % len(rules) + "\n".join(
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
        if low.startswith("/clear_product") or cmd.startswith("CLEAR#"):
            pid = (cmd.split(None, 1)[1] if " " in cmd else cmd[6:]).strip()
            pid = pid or self._pid_from(reply_text)
            if not pid:
                return "Usage: /clear_product <product_id>"
            return "Forgot %s." % pid if app.seen.forget(pid) else "%s not in DB." % pid
        if low.startswith("/clear_all") or cmd.strip() == "CLEARALL#":
            return "Wiped %d products." % app.seen.clear()
        return None

    @staticmethod
    def _pid_from(text: str) -> str:
        if not text:
            return ""
        for pattern in (r"[?&]pid=([A-Z0-9]{10,25})", r"myntra\.com/(?:buy/)?(\d{6,10})"):
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1).upper()
        return ""

    def _loop(self) -> None:
        log.info("Controller polling started (admin=%d, watching %d deal chats)",
                 self._admin, len(set(self._app.settings.channels.values())))
        errors = 0
        deal_chats = set(self._app.settings.channels.values())
        while self._running:
            try:
                if errors >= 5:
                    time.sleep(30)
                    errors = 0
                    continue
                data = self._api("getUpdates", offset=self._offset + 1,
                                 timeout=30, allowed_updates=["message", "channel_post"])
                # getUpdates conflict (409) = same bot token polling elsewhere.
                # Surface it loudly instead of spinning silently.
                if data.get("error_code") == 409 or "conflict" in str(data.get("description", "")).lower():
                    log.error("Controller CONFLICT: %s — another instance (local run? old deploy?) "
                              "is polling this bot token. Stop it or commands/replies race.",
                              data.get("description", "terminated by other getUpdates"))
                    time.sleep(30)
                    continue
                errors = 0
                results = data.get("result", [])
                for update in results:
                    uid = update.get("update_id", 0)
                    if uid in self._seen:
                        continue
                    self._seen.add(uid)
                    self._offset = max(self._offset, uid)
                    if len(self._seen) > 500:
                        self._seen = {uid}
                    msg = update.get("message") or update.get("channel_post") or {}
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id", 0)
                    if not text:
                        continue
                    is_admin = chat_id == self._admin
                    legacy = ("ADD#", "REMOVE#", "LIST#", "CLEAR", "STATUS#", "HELP#", "#")
                    is_legacy = chat_id in deal_chats and text.startswith(legacy)
                    if not (is_admin or is_legacy):
                        log.warning("Controller ignoring msg from chat %s (not admin=%d): %s",
                                    chat_id, self._admin, text[:60])
                        continue
                    reply_to = msg.get("reply_to_message") or {}
                    response = self._handle(text, chat_id, reply_to.get("text", ""))
                    if response:
                        self.reply(response, None if is_admin else chat_id)
                        log.info("CMD from %s: %s", chat_id, text[:60])
                time.sleep(1)
            except Exception as exc:
                errors += 1
                log.error("Controller loop error: %s", exc)
                time.sleep(3)

    def start(self) -> None:
        if self._running:
            return
        self.verify()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="controller")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

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

    def reply(self, text: str, chat_id: int | None = None) -> None:
        self._post("sendMessage", chat_id=chat_id or self._admin,
                   text=text, disable_web_page_preview=True)

    def verify(self) -> str:
        me = self._api("getMe")
        username = (me.get("result") or {}).get("username", "?")
        log.info("Control bot polling as @%s (admin=%d)", username, self._admin)
        return username
