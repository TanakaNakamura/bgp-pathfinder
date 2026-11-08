"""Application events and a synchronous event bus.

The optimizer service only *publishes* what happened. Persistence (SQLite),
logs (JSON) and metrics (Prometheus) are listeners, so a new consumer is a new
listener class, with no change to the service (open/closed).

Listener errors are not swallowed: a failing listener crashes the daemon,
which by design fails closed (see docs/DECISIONS.md D17).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from optimizer.domain.models import Decision, PrefixState, ProviderMetrics
from optimizer.ports import EventListener


def mode_name(dry_run: bool) -> str:
    return "dry-run" if dry_run else "live"


@dataclass(frozen=True)
class Started:
    ts: float
    version: str
    dry_run: bool
    pid: int
    prefixes: tuple[str, ...]
    providers: tuple[str, ...]
    base_provider: str


@dataclass(frozen=True)
class Stopped:
    ts: float
    reason: str


@dataclass(frozen=True)
class RoundMeasured:
    """Raw probe results of one round (``probing.RoundResult`` items)."""

    ts: float
    results: Sequence[object]


@dataclass(frozen=True)
class PrefixEvaluated:
    """The engine looked at one prefix this round (whether or not it changed anything)."""

    ts: float
    round_no: int
    prefix: str
    metrics: Mapping[str, ProviderMetrics]
    decision: Decision
    state: PrefixState
    base_provider: str


@dataclass(frozen=True)
class RouteChanged:
    ts: float
    decision: Decision
    next_hop: str | None
    metrics: Mapping[str, ProviderMetrics]
    command: str
    dry_run: bool


@dataclass(frozen=True)
class ModeChanged:
    ts: float
    old_dry_run: bool
    new_dry_run: bool


@dataclass(frozen=True)
class BgpStateChanged:
    ts: float
    peer: str | None
    state: str | None
    reason: str | None


@dataclass(frozen=True)
class RoundCompleted:
    ts: float
    round_no: int


@dataclass
class EventBus:
    """Implements ``ports.EventPublisher``: delivers every event to every listener, in order."""

    listeners: list[EventListener] = field(default_factory=list)

    def subscribe(self, listener: EventListener) -> None:
        self.listeners.append(listener)

    def publish(self, event: object) -> None:
        for listener in self.listeners:
            listener.handle(event)
