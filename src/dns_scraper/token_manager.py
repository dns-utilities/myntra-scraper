"""Myntra auth token rotation (/auth/v1/token). Thread-safe, cached 25 min."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime

from curl_cffi import requests as cf_requests

log = logging.getLogger("dns_scraper.token_manager")

AUTH_URL = "https://api.myntra.com/auth/v1/token"
TOKEN_TTL_MINUTES = 25

# Single stable device identity. Rotating device_id / install_id / client_id
# per token fetch is the #1 cause of continuous search-403: Myntra binds the
# `at` token to the device fingerprint + cookies, then the search call (with
# a DIFFERENT device fingerprint and NO cookies) fails the WAF check.
DEVICE = {"device_id": "caab1b9cd9ae0063", "install_id": "1eb49953-845b-36cf-a6b3-ee5988d7836b",
     "client_id": "myntra-02d7dec5-8a00-4c74-9cf7-9d62dbea5e61", "model": "LEX821", "os": "13", "dpi": "560"}

# Back-compat alias (old code imported DEVICES).
DEVICES = (DEVICE,)

USER_AGENT = ("Mozilla/5.0 (Linux; Android 13; LEX821 Build/TQ3C.250905.001.C2; wv) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.46 Mobile Safari/537.36; "
     "MyntraRetailAndroid/3.33.3 (Phone, 560dpi); MyntraAndroid/3.33.3 (Phone, 560dpi); api;")

ABTEST_POOL = (
    "1035=2; 1042=1046; 1048=2; 1049=2; 1050=27; 1052=2; 1054=26; 1055=27; 1060=47; 1061=1062; 1064=27; 1065=27; 1069=1071; 1075=27; 1077=27; 2156=2158; 2261=2269; 2376=27; 3002=587; 3247=844; 3418=27; 3427=27; 3428=1674; 3460=26; 3492=27; 3502=26; 3517=26; 3518=822; 3530=27; 3532=27; 482=27; cart.bucketisation=disabled-group",
    "1035=1; 1042=1043; 1048=1; 1049=1; 1050=26; 1052=1; 1054=25; 1055=26; 1060=46; 1061=1060; 1064=26; 1065=26; 1069=1070; 1075=26; 1077=26; 2156=2157; 2261=2268; 2376=26; 3002=586; 3247=843; 3418=26; 3427=26; 3428=1673; 3460=25; 3492=26; 3502=25; 3517=25; 3518=821; 3530=26; 3532=26; 482=26; cart.bucketisation=enabled-group",
)


class TokenManager:
    def __init__(self, impersonate: str = "chrome131_android") -> None:
        self._token: str | None = None
        self._token_time: datetime | None = None
        self._lock = threading.RLock()
        self._refreshing = False
        self._impersonate = impersonate
        self._device = DEVICE
        self._abtest = ABTEST_POOL[0]
        # Stable session id: Myntra binds token <-> X-MYNTRA-APP sessionID.
        # Rotating it per request invalidates the token on next call.
        self._sid = "%s-%s" % (uuid.uuid4(), self._device["device_id"])
        self._session = cf_requests.Session(impersonate=impersonate)
        log.info("TokenManager ready (stable device %s… | %s)",
                 self._device["device_id"][:8], impersonate)

    @property
    def device(self) -> dict:
        return self._device

    @property
    def impersonate(self) -> str:
        return self._impersonate

    @property
    def abtest(self) -> str:
        return self._abtest

    @property
    def session_id(self) -> str:
        return self._sid

    @property
    def user_agent(self) -> str:
        return USER_AGENT

    def cookies_snapshot(self) -> dict:
        try:
            jar = getattr(self._session, "cookies", None)
            if jar is None:
                return {}
            if hasattr(jar, "get_dict"):
                return dict(jar.get_dict())
            return {c.name: c.value for c in jar}
        except Exception:
            return {}

    def _auth_headers(self) -> dict:
        device = self._device
        return {
            "Accept": "application/json; charset=utf-8",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US,en;q=0.5",
            "clientid": device["client_id"],
            "Connection": "Keep-Alive",
            "IGNORE-REFRESH": "true",
            "User-Agent": USER_AGENT,
            "x-device-state": "connectionClass=UNKNOWN; connectionType=WIFI; bandwidth=-1.0; deviceYear=2015; deviceYearType=2;",
            "X-MYNTRA-APP": "appFamily=MyntraRetailAndroid; appVersion=3.33.3; appBuild=80110249; deviceCategory=Phone; osVersion=%s; sdkVersion=%s; deviceID=%s; installationID=%s; sessionID=%s;advertisingID=0;advertisingIDKeySpace=GAID;bundleVersion=1.0.0;" % (device["os"], device["os"], device["device_id"], device["install_id"], self._sid),
            "X-SLOT-MODE": "disabled",
        }

    def _fetch(self) -> str | None:
        try:
            resp = self._session.get(AUTH_URL, headers=self._auth_headers(), timeout=15)
            if resp.status_code == 200:
                token = resp.headers.get("at")
                if token:
                    log.info("Token refreshed (cookies=%d)", len(self.cookies_snapshot()))
                    return token
                log.warning("Token fetch 200 but no 'at' header")
            else:
                log.warning("Token fetch HTTP %d: %s", resp.status_code, (resp.text or "")[:200])
        except Exception as exc:
            log.error("Token fetch error: %s", exc)
        return None

    def get_token(self, force_refresh: bool = False) -> str | None:
        with self._lock:
            if not force_refresh and self._token and self._token_time:
                age = (datetime.now() - self._token_time).total_seconds() / 60
                if age < TOKEN_TTL_MINUTES:
                    return self._token
            if self._refreshing:
                return self._token
            self._refreshing = True
        try:
            token = self._fetch()
        finally:
            with self._lock:
                self._refreshing = False
        with self._lock:
            if token:
                self._token, self._token_time = token, datetime.now()
                return token
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token, self._token_time = None, None
            log.warning("Token invalidated (401/403) — will refresh with SAME device+cookies")
