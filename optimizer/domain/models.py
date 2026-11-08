"""Domain value objects. All immutable."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Mapping


@dataclass(frozen=True)
class Sample:
    """One probe packet / connection attempt. ``rtt_ms`` is None when lost."""

    ts: float
    method: str  # "icmp" | "tcp"
    rtt_ms: float | None


@dataclass(frozen=True)
class ProviderMetrics:
    """Aggregated metrics of one (prefix, provider) pair over the sliding window."""

    provider: str
    sent: int
    received: int
    rtt_ms: float | None
    jitter_ms: float | None
    loss_pct: float

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "sent": self.sent,
            "received": self.received,
            "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 3),
            "jitter_ms": None if self.jitter_ms is None else round(self.jitter_ms, 3),
            "loss_pct": round(self.loss_pct, 2),
        }


@dataclass(frozen=True)
class Policy:
    """Tunable thresholds of the decision engine (the ``decision`` config section)."""

    base_provider: str
    min_samples: int = 15
    rtt_threshold_ms: float = 20.0
    loss_threshold_pct: float = 2.0
    return_ratio: float = 0.5
    confirm_rounds: int = 3
    hold_down_s: float = 60.0
    max_changes_per_minute: int = 2
    bad_loss_pct: float = 30.0
    bad_rtt_ms: float = 1000.0
    loss_weight_ms: float = 25.0
    jitter_weight: float = 1.0


class Action(StrEnum):
    NONE = "none"
    ANNOUNCE = "announce"
    WITHDRAW = "withdraw"


class Code(StrEnum):
    STEADY = "steady"
    INSUFFICIENT_DATA = "insufficient-data"
    ALL_BAD = "all-bad"
    PENDING = "pending"
    HOLD_DOWN = "hold-down"
    RATE_LIMIT = "rate-limit"
    BETTER_PATH = "better-path"
    RETURN_TO_BASE = "return-to-base"


@dataclass(frozen=True)
class Proposal:
    """What the path selector would like, before hysteresis and timing guards.

    ``wanted`` is None when the selector has no opinion ("do not touch").
    """

    wanted: str | None
    code: Code
    reason: str


@dataclass(frozen=True)
class PrefixState:
    """Per-prefix state of the decision engine.

    * ``active``: provider we inject a route for; None = plain BGP (base path).
    * ``pending`` / ``pending_rounds``: hysteresis bookkeeping.
    * ``changed_at``: time of the last change (hold-down).
    """

    prefix: str
    active: str | None = None
    changed_at: float | None = None
    pending: str | None = None
    pending_rounds: int = 0

    def current(self, base_provider: str) -> str:
        return self.active or base_provider

    def without_pending(self) -> PrefixState:
        return replace(self, pending=None, pending_rounds=0)

    def wanting(self, target: str) -> PrefixState:
        """Count one more round of wanting ``target`` (restart the count if the target changed)."""
        rounds = self.pending_rounds + 1 if self.pending == target else 1
        return replace(self, pending=target, pending_rounds=rounds)

    def switched_to(self, active: str | None, now: float) -> PrefixState:
        return PrefixState(prefix=self.prefix, active=active, changed_at=now)


@dataclass(frozen=True)
class Decision:
    prefix: str
    action: Action
    code: Code
    current: str  # provider carrying traffic before the decision
    target: str  # provider carrying traffic after the decision (== current for NONE)
    wanted: str | None  # provider the engine would like to use (None = no opinion)
    reason: str
    scores: Mapping[str, float | None]

    @property
    def changed(self) -> bool:
        return self.action is not Action.NONE
