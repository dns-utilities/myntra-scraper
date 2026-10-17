"""Central configuration. Every secret/tunable comes from env — nothing hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dns_scraper.channels import ChannelPlan

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
SEED_LINKS_FILE = PROJECT_ROOT / "config" / "links.txt"
SEED_CROMA_LINKS_FILE = PROJECT_ROOT / "config" / "croma_links.txt"
SEED_BLINKIT_LINKS_FILE = PROJECT_ROOT / "config" / "blinkit_links.txt"
SEED_BIGBASKET_LINKS_FILE = PROJECT_ROOT / "config" / "bigbasket_links.txt"
SEED_BLINKIT_LOCATIONS_FILE = PROJECT_ROOT / "config" / "blinkit_locations.txt"
SEED_BIGBASKET_LOCATIONS_FILE = PROJECT_ROOT / "config" / "bigbasket_locations.txt"
SEED_AUTO_CLEAR_BRANDS_FILE = PROJECT_ROOT / "config" / "auto_clear_brands.json"


def _parse_tokens(raw: str) -> list[str]:
    parts = raw.replace("\n", ",").replace(";", ",").split(",")
    return [p.strip() for p in parts if ":" in p.strip()]


TRUTHY = ("1", "true", "yes", "on")


def _as_int(raw: object, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


def _as_float(raw: object, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError):
        return default


def _as_bool(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in TRUTHY


def _env_int(name: str, default: int) -> int:
    return _as_int(os.environ.get(name), default)


def _env_float(name: str, default: float) -> float:
    return _as_float(os.environ.get(name), default)


def _env_bool(name: str, default: bool) -> bool:
    return _as_bool(os.environ.get(name), default)


# Per-source tunables. Env looks like SOURCE_MYNTRA_INTERVAL=600,
# SOURCE_CROMA_RPS=4, SOURCE_BLINKIT_ENABLED=false. Anything unset falls back
# to the source spec's own default, so adding a source never needs code edits
# here — just env vars.
# The fields a ``SOURCE_<NAME>_<FIELD>`` env var may set. Every entry must name
# a real ``SourceSpec`` attribute (see sources.py); that pairing is what keeps
# the env surface and the spec from drifting apart.
_SOURCE_KEYS = ("ENABLED", "INTERVAL", "RPS", "IMPERSONATE",
                "MIN_DISCOUNT_NEW", "MIN_DISCOUNT_EXISTING", "MAX_PAGES",
                "CONCURRENCY", "PAGE_LIMIT", "COOLDOWN", "COOKIE_TTL",
                "WARM_COOKIES", "CHANNEL_CODE")
_SOURCE_FLOAT_KEYS = ("RPS", "INTERVAL")
_SOURCE_TEXT_KEYS = ("IMPERSONATE", "CHANNEL_CODE")
_SOURCE_BOOL_KEYS = ("ENABLED", "WARM_COOKIES")


def _parse_source_overrides(env: dict) -> dict:
    """Collect ``SOURCE_<NAME>_<FIELD>`` env vars into ``{name: {field: value}}``.

    Matching runs longest-suffix-first because some field names contain an
    underscore themselves (``MIN_DISCOUNT_NEW``), which a naive split on the
    last ``_`` would mis-read as field ``NEW``.
    """
    out: dict[str, dict] = {}
    fields = sorted(_SOURCE_KEYS, key=len, reverse=True)
    for key, raw in env.items():
        if not key.startswith("SOURCE_"):
            continue
        rest = key[len("SOURCE_"):]
        for field in fields:
            suffix = "_" + field
            if not rest.endswith(suffix) or len(rest) <= len(suffix):
                continue
            name = rest[: -len(suffix)].strip().lower()
            if not name:
                continue
            text = str(raw).strip()
            if field in _SOURCE_TEXT_KEYS:
                value: object = text
            elif field in _SOURCE_BOOL_KEYS:
                value = text.lower() in TRUTHY
            elif field in _SOURCE_FLOAT_KEYS:
                try:
                    value = float(text)
                except ValueError:
                    break
            else:
                try:
                    value = int(text)
                except ValueError:
                    break
            out.setdefault(name, {})[field.lower()] = value
            break
    return out


# Superseded by the names above. Kept only so that forgetting to rename one's
# environment fails loudly with the mapping instead of silently posting nothing.
LEGACY_CHANNEL_VARS = (
    "CHANNEL_ALL", "CHANNEL_MYNTRA", "CHANNEL_CROMA", "CHANNEL_BLINKIT",
    "CHANNEL_BIGBASKET", "CHANNEL_70PLUS", "CHANNEL_80PLUS", "CHANNEL_90PLUS",
)


@dataclass
class Settings:
    """All runtime configuration, loaded once at startup."""

    bot_tokens: list[str] = field(default_factory=list)
    control_bot_token: str = ""
    admin_chat_id: int = 0
    # Telegram destinations, split by role (see channels.py). A chat id of 0
    # means "not configured".
    channel_bases: dict = field(default_factory=dict)   # source name -> chat id
    channel_tiers: dict = field(default_factory=dict)   # tier floor  -> chat id
    data_dir: Path = DEFAULT_DATA_DIR
    pincode: str = "387001"
    cycle_delay: int = 20
    scrape_concurrency: int = 20
    max_pages_per_url: int = 5
    min_discount_new: int = 0
    min_discount_existing: int = 15
    min_change_percent: int = 1
    max_cycles: int = 0
    port: int = 0
    log_level: str = "INFO"
    # Everything site-specific — rate budget, impersonation profile, page
    # limits, cooldowns, cookie handling, the Croma channel code — lives on a
    # source's own ``SourceSpec`` (see sources.py) and is overridden with
    # ``SOURCE_<NAME>_<FIELD>``. Settings keeps only the deployment-wide
    # defaults a source inherits.
    # ---- Delivery-point resolution (Blinkit / BigBasket locations) ----
    geocode_enabled: bool = True
    geocode_url: str = "https://nominatim.openstreetmap.org/search"
    # ---- Telegram send pacing (the anti-flood gate) ----
    # Telegram tolerates ~30 msg/s overall and ~20 msg/min to one chat; the
    # per-chat ceiling is the one that actually bites, so it is the tightest.
    send_global_per_sec: float = 20.0
    send_per_bot_per_sec: float = 15.0
    send_per_chat_per_min: float = 18.0
    send_max_queue: int = 5000
    # A first run against an empty seen-DB records everything but posts
    # nothing, so clearing the database (or a first deploy) cannot flood.
    baseline_on_empty: bool = True
    # ---- Auto-clear (re-post eligible deals on a cooldown) ----
    auto_clear_enabled: bool = True
    auto_clear_hours: int = 24
    auto_clear_min_discount: int = 50
    auto_clear_max_per_hour: int = 25
    auto_clear_jitter_hours: int = 6
    # ---- Retention / housekeeping ----
    seen_retention_days: int = 45
    prune_interval_hours: int = 24
    # Per-source overrides, keyed by source name: {"myntra": {"interval": 600, "rps": 5}}
    source_overrides: dict = field(default_factory=dict)
    # Deal-link tag. "" = OFF -> plain `https://www.myntra.com/<ID>`.
    # Set AFFILIATE_TAG (or /set_affiliate) later to e.g.
    # `utm_source=ugc_affiliate&utm_medium=social_share_pdp&utm_campaign=XYZ`.
    affiliate_tag: str = ""

    # Legacy dict-style params (renamed — was AFFILIATE_PARAMS). Only read
    # when affiliate_tag is empty AND AFFILIATE_TAG unset; will be removed.
    legacy_affiliate_params: dict = field(default_factory=dict)
    DEFAULT_SPAM_WORDS: tuple = (
        "gift card", "membership", "subscription", "coupon", "bundle",
        "pack", "combo", "set", "collection", "set of", "bulk",
        "wholesale", "pre-order", "back-order", "invalid",
    )

    @classmethod
    def load(cls) -> "Settings":
        env = os.environ
        tokens = _parse_tokens(env.get("BOT_TOKENS", ""))
        plan = ChannelPlan.from_env(env)
        settings = cls(
            bot_tokens=tokens,
            control_bot_token=tokens[0] if tokens else "",
            admin_chat_id=_env_int("ADMIN_CHAT_ID", 0),
            channel_bases=plan.bases,
            channel_tiers=plan.tiers,
            data_dir=Path(env.get("DATA_DIR", str(DEFAULT_DATA_DIR))),
            pincode=env.get("PINCODE", "387001").strip(),
            cycle_delay=_env_int("CYCLE_DELAY", 20),
            scrape_concurrency=_env_int("SCRAPE_CONCURRENCY", 20),
            max_pages_per_url=_env_int("MAX_PAGES_PER_URL", 5),
            min_discount_new=_env_int("MIN_DISCOUNT_NEW", 0),
            min_discount_existing=_env_int("MIN_DISCOUNT_EXISTING", 15),
            min_change_percent=_env_int("MIN_CHANGE_PERCENT", 1),
            max_cycles=_env_int("MAX_CYCLES", 0),
            port=_env_int("PORT", 0),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            affiliate_tag=env.get("AFFILIATE_TAG", "").strip().lstrip("?&"),
            geocode_enabled=_env_bool("GEOCODE_ENABLED", True),
            geocode_url=env.get(
                "GEOCODE_URL", "https://nominatim.openstreetmap.org/search").strip(),
            send_global_per_sec=_env_float("SEND_GLOBAL_PER_SEC", 20.0),
            send_per_bot_per_sec=_env_float("SEND_PER_BOT_PER_SEC", 15.0),
            send_per_chat_per_min=_env_float("SEND_PER_CHAT_PER_MIN", 18.0),
            send_max_queue=_env_int("SEND_MAX_QUEUE", 5000),
            baseline_on_empty=_env_bool("BASELINE_ON_EMPTY", True),
            auto_clear_enabled=_env_bool("AUTO_CLEAR_ENABLED", True),
            auto_clear_hours=_env_int("AUTO_CLEAR_HOURS", 24),
            auto_clear_min_discount=_env_int("AUTO_CLEAR_MIN_DISCOUNT", 50),
            auto_clear_max_per_hour=_env_int("AUTO_CLEAR_MAX_PER_HOUR", 25),
            auto_clear_jitter_hours=_env_int("AUTO_CLEAR_JITTER_HOURS", 6),
            seen_retention_days=_env_int("SEEN_RETENTION_DAYS", 45),
            prune_interval_hours=_env_int("PRUNE_INTERVAL_HOURS", 24),
            source_overrides=_parse_source_overrides(env),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = []
        if not self.bot_tokens:
            missing.append("BOT_TOKENS")
        if not self.admin_chat_id:
            missing.append("ADMIN_CHAT_ID")
        # Destinations are per-source bases plus shared tiers now, so there is
        # no single mandatory channel variable — but at least one destination
        # must exist, or the bot would scrape forever and post nowhere.
        if not self.channel_plan.destinations():
            missing.append("CHANNEL_BASE_DEFAULT (or any CHANNEL_BASE_* / CHANNEL_TIER_*)")
        if missing:
            raise ValueError("Missing required env vars: %s. Copy .env.example "
                             "to .env.%s" % (", ".join(missing), self._legacy_hint()))

    @staticmethod
    def _legacy_hint() -> str:
        """Explain the channel-variable rename when old names are still set."""
        stale = sorted(k for k in LEGACY_CHANNEL_VARS if os.environ.get(k))
        if not stale:
            return ""
        return ("\nChannel variables were renamed: CHANNEL_ALL -> "
                "CHANNEL_BASE_DEFAULT, CHANNEL_<SOURCE> -> CHANNEL_BASE_<SOURCE>, "
                "CHANNEL_70PLUS/80PLUS/90PLUS -> CHANNEL_TIER_70/80/90. "
                "Still set (now ignored): %s" % ", ".join(stale))

    @property
    def links_file(self) -> Path:
        return self.data_dir / "links.txt"

    @property
    def croma_links_file(self) -> Path:
        return self.data_dir / "croma_links.txt"

    @property
    def croma_stores_file(self) -> Path:
        return self.data_dir / "croma_stores.json"

    @property
    def blinkit_links_file(self) -> Path:
        return self.data_dir / "blinkit_links.txt"

    @property
    def bigbasket_links_file(self) -> Path:
        return self.data_dir / "bigbasket_links.txt"

    @property
    def blinkit_locations_file(self) -> Path:
        return self.data_dir / "blinkit_locations.txt"

    @property
    def bigbasket_locations_file(self) -> Path:
        return self.data_dir / "bigbasket_locations.txt"

    @property
    def pincode_cache_file(self) -> Path:
        return self.data_dir / "pincode_cache.json"

    @property
    def outbox_file(self) -> Path:
        return self.data_dir / "outbox.jsonl"

    @property
    def seen_db(self) -> Path:
        return self.data_dir / "seen.db"

    @property
    def croma_seen_db(self) -> Path:
        return self.data_dir / "croma_seen.db"

    @property
    def blinkit_seen_db(self) -> Path:
        return self.data_dir / "blinkit_seen.db"

    @property
    def bigbasket_seen_db(self) -> Path:
        return self.data_dir / "bigbasket_seen.db"

    @property
    def blacklist_db(self) -> Path:
        return self.data_dir / "blacklist.db"

    @property
    def auto_clear_brands_file(self) -> Path:
        return self.data_dir / "auto_clear_brands.json"

    @property
    def spam_words_file(self) -> Path:
        return self.data_dir / "spam_words.txt"

    @property
    def affiliate_tag_file(self) -> Path:
        return self.data_dir / "affiliate_tag.txt"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "scraper.log"

    @property
    def channel_plan(self) -> ChannelPlan:
        """The routing table: which destination every (source, band) posts to."""
        return ChannelPlan(self.channel_bases, self.channel_tiers)

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Seed = repo default, copied to the volume exactly once so that live
        # edits (via the control bot) survive redeploys instead of being
        # overwritten by whatever ships in the image.
        for target, seed in ((self.links_file, SEED_LINKS_FILE),
                             (self.croma_links_file, SEED_CROMA_LINKS_FILE),
                             (self.blinkit_links_file, SEED_BLINKIT_LINKS_FILE),
                             (self.bigbasket_links_file, SEED_BIGBASKET_LINKS_FILE),
                             (self.blinkit_locations_file, SEED_BLINKIT_LOCATIONS_FILE),
                             (self.bigbasket_locations_file, SEED_BIGBASKET_LOCATIONS_FILE),
                             (self.auto_clear_brands_file, SEED_AUTO_CLEAR_BRANDS_FILE)):
            if not target.exists() and seed.exists():
                target.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
