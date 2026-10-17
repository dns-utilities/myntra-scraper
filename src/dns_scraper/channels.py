"""Where a deal is posted: one base feed per source, plus shared tier feeds.

A deal belongs to exactly one family:

* **base** — one feed per source, carrying that source's sub-tier deals.
  ``DEFAULT_BASE`` is the fallback for a source with no base of its own, which
  is what keeps a single-channel deployment a one-variable setup.
* **tier** — shared by every source, keyed by the band's floor percentage.
  Bands are mutually exclusive half-open intervals ``[floor, next_floor)``: 79%
  is tier 70, 80% is tier 80. The keys are floors rather than "70plus" because
  "plus" would be false — the band stops at 79, it is not "70 and above".

Modelled as a value object instead of a flat ``dict[str, int]`` so the routing
rule is unit-testable on its own and no caller has to know how a tier is
spelled internally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dns_scraper.models import DiscountTier

# Key in ``bases`` used by a source that has no base feed of its own.
DEFAULT_BASE = "default"

# Env surface: CHANNEL_BASE_<NAME> per source, CHANNEL_TIER_<floor> per band.
CHANNEL_BASE_PREFIX = "CHANNEL_BASE_"
CHANNEL_TIER_PREFIX = "CHANNEL_TIER_"
BASE_VAR = CHANNEL_BASE_PREFIX + DEFAULT_BASE.upper()

# Tier floors, derived from the domain model so the two can never drift apart.
TIER_FLOORS = tuple(int(t) for t in DiscountTier if int(t) > 0)


def _chat_id(raw: object, default: int = 0) -> int:
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


def band_label(floor: int) -> str:
    """Human label for a tier band, e.g. ``70-79%`` and ``90%+``."""
    nxt = next((f for f in TIER_FLOORS if f > floor), None)
    return "%d%%+" % floor if nxt is None else "%d-%d%%" % (floor, nxt - 1)


@dataclass(frozen=True)
class ChannelPlan:
    """The configured destinations. A chat id of 0 means "not configured"."""

    bases: dict[str, int] = field(default_factory=dict)
    tiers: dict[int, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env) -> "ChannelPlan":
        """Build a plan from ``CHANNEL_BASE_*`` / ``CHANNEL_TIER_*`` variables.

        Bases are discovered generically rather than from a fixed source list,
        so adding a source needs no edit here. Every unset base and tier
        inherits ``CHANNEL_BASE_DEFAULT``.
        """
        default = _chat_id(env.get(BASE_VAR), 0)
        bases: dict[str, int] = {}
        for key, raw in env.items():
            if not key.startswith(CHANNEL_BASE_PREFIX):
                continue
            name = key[len(CHANNEL_BASE_PREFIX):].strip().lower()
            if name and name != DEFAULT_BASE:
                bases[name] = _chat_id(raw, default)
        tiers = {floor: _chat_id(env.get("%s%d" % (CHANNEL_TIER_PREFIX, floor)),
                                 default) for floor in TIER_FLOORS}
        return cls({DEFAULT_BASE: default,
                    **{name: bases[name] for name in sorted(bases)}}, tiers)

    def base_for(self, source: str) -> int:
        """The base feed for one source, falling back to the default base."""
        return int(self.bases.get(source) or self.bases.get(DEFAULT_BASE) or 0)

    def route(self, discount: int, source: str = "") -> list[int]:
        """The chat ids one deal belongs to.

        A list rather than an int because the contract is "everywhere this deal
        belongs"; callers must not assume there is exactly one destination.
        """
        tier = DiscountTier.for_discount(discount)
        if tier is DiscountTier.BASE:
            chat = self.base_for(source)
        else:
            chat = int(self.tiers.get(int(tier)) or 0)
        return [chat] if chat else []

    def destinations(self) -> list[int]:
        """Every distinct chat id: the bases, then the tiers in band order."""
        out: list[int] = []
        for chat in list(self.bases.values()) + [self.tiers[f]
                                                 for f in sorted(self.tiers)]:
            if chat and chat not in out:
                out.append(chat)
        return out

    def labels(self) -> dict[int, str]:
        """chat id -> short label, for /status, /verify_bots and stats.

        Bases that merely inherited the default id share its label, so the
        result reads as the actual set of destinations rather than one entry
        per environment variable.
        """
        out: dict[int, str] = {}
        default = self.bases.get(DEFAULT_BASE) or 0
        if default:
            out[default] = "base:%s" % DEFAULT_BASE
        for name in sorted(self.bases):
            chat = self.bases[name]
            if chat and chat != default:
                out.setdefault(chat, "base:%s" % name)
        for floor in sorted(self.tiers):
            chat = self.tiers[floor]
            if chat:
                out.setdefault(chat, "tier:%d" % floor)
        return out

    def describe(self) -> str:
        """One line describing where each band posts, for the /status header."""
        default = self.bases.get(DEFAULT_BASE) or 0

        def show(chat: int) -> str:
            if not chat:
                return "unset"
            return DEFAULT_BASE if chat == default else str(chat)

        named = [n for n in sorted(self.bases) if n != DEFAULT_BASE and self.bases[n]]
        parts = ["<70%->base:" + (",".join(named) if named else DEFAULT_BASE)]
        parts += ["%s->%s" % (band_label(f), show(self.tiers.get(f, 0)))
                  for f in TIER_FLOORS]
        return ("routes: " + " | ".join(parts)
                + "  [base per source, tiers shared and exclusive]")
