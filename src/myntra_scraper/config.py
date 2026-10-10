"""Central configuration. Every secret/tunable comes from env — nothing hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
SEED_LINKS_FILE = PROJECT_ROOT / "config" / "links.txt"
SEED_CROMA_LINKS_FILE = PROJECT_ROOT / "config" / "croma_links.txt"


def _parse_tokens(raw: str) -> list[str]:
    parts = raw.replace("\n", ",").replace(";", ",").split(",")
    return [p.strip() for p in parts if ":" in p.strip()]


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (ValueError, AttributeError):
        return default


@dataclass
class Settings:
    """All runtime configuration, loaded once at startup."""

    bot_tokens: list[str] = field(default_factory=list)
    control_bot_token: str = ""
    admin_chat_id: int = 0
    channel_all: int = 0
    channel_70plus: int = 0
    channel_90plus: int = 0
    data_dir: Path = DEFAULT_DATA_DIR
    pincode: str = "387001"
    cycle_delay: int = 20
    scrape_concurrency: int = 20
    max_pages_per_url: int = 5
    min_discount_new: int = 0
    min_discount_existing: int = 15
    min_change_percent: int = 1
    curl_impersonate: str = "chrome131_android"
    max_cycles: int = 0
    port: int = 0
    log_level: str = "INFO"
    # ---- Croma scraper (runs alongside Myntra in the same deployment) ----
    croma_enabled: bool = True
    croma_impersonate: str = "chrome120"
    croma_channel_code: str = "400049"
    croma_max_pages: int = 10
    croma_concurrency: int = 6
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
        tokens = _parse_tokens(os.environ.get("BOT_TOKENS", ""))
        admin = _env_int("ADMIN_CHAT_ID", 0)
        channel_all = _env_int("CHANNEL_ALL", 0)
        channel_70 = _env_int("CHANNEL_70PLUS", channel_all)
        channel_90 = _env_int("CHANNEL_90PLUS", channel_all)
        data_dir = Path(os.environ.get("DATA_DIR", str(DEFAULT_DATA_DIR)))
        port = _env_int("PORT", 0)
        settings = cls(
            bot_tokens=tokens,
            control_bot_token=tokens[0] if tokens else "",
            admin_chat_id=admin,
            channel_all=channel_all,
            channel_70plus=channel_70,
            channel_90plus=channel_90,
            data_dir=data_dir,
            pincode=os.environ.get("PINCODE", "387001").strip(),
            cycle_delay=_env_int("CYCLE_DELAY", 20),
            scrape_concurrency=_env_int("SCRAPE_CONCURRENCY", 20),
            max_pages_per_url=_env_int("MAX_PAGES_PER_URL", 5),
            min_discount_new=_env_int("MIN_DISCOUNT_NEW", 0),
            min_discount_existing=_env_int("MIN_DISCOUNT_EXISTING", 15),
            min_change_percent=_env_int("MIN_CHANGE_PERCENT", 1),
            curl_impersonate=os.environ.get("CURL_IMPERSONATE", "chrome131_android").strip(),
            max_cycles=_env_int("MAX_CYCLES", 0),
            port=port,
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            affiliate_tag=os.environ.get("AFFILIATE_TAG", "").strip().lstrip("?&"),
            croma_enabled=os.environ.get("CROMA_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on"),
            croma_impersonate=os.environ.get("CROMA_IMPERSONATE", "chrome120").strip(),
            croma_channel_code=os.environ.get("CROMA_CHANNEL_CODE", "400049").strip(),
            croma_max_pages=_env_int("CROMA_MAX_PAGES", 10),
            croma_concurrency=_env_int("CROMA_CONCURRENCY", 6),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = []
        if not self.bot_tokens:
            missing.append("BOT_TOKENS")
        if not self.admin_chat_id:
            missing.append("ADMIN_CHAT_ID")
        if not self.channel_all:
            missing.append("CHANNEL_ALL")
        if missing:
            raise ValueError(
                "Missing required env vars: %s. Copy .env.example to .env."
                % ", ".join(missing)
            )

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
    def seen_db(self) -> Path:
        return self.data_dir / "seen.db"

    @property
    def croma_seen_db(self) -> Path:
        return self.data_dir / "croma_seen.db"

    @property
    def blacklist_db(self) -> Path:
        return self.data_dir / "blacklist.db"

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
    def channels(self) -> dict[str, int]:
        return {
            "all": self.channel_all,
            "70plus": self.channel_70plus,
            "90plus": self.channel_90plus,
        }

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.links_file.exists() and SEED_LINKS_FILE.exists():
            self.links_file.write_text(
                SEED_LINKS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
            )
        if not self.croma_links_file.exists() and SEED_CROMA_LINKS_FILE.exists():
            self.croma_links_file.write_text(
                SEED_CROMA_LINKS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
            )
