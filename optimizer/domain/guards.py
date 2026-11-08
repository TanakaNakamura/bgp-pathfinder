"""Change guards: may a proposed route change be executed right now?

The engine runs guards in order; the first one that blocks wins. New rules
(maintenance windows, per-provider budgets, ...) are added as new guard
classes without touching the engine (open/closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from optimizer.domain.models import Code, PrefixState


@dataclass(frozen=True)
class GuardContext:
    state: PrefixState  # already counting the current round for the wanted target
    now: float
    recent_changes: Sequence[float]  # timestamps of recent changes, all prefixes


@dataclass(frozen=True)
class Block:
    code: Code
    note: str  # appended to the proposal's reason


class ChangeGuard(Protocol):
    def check(self, ctx: GuardContext) -> Block | None:
        """Return a Block to stop the change, None to let it through."""
        ...


class ConfirmationGuard:
    """Hysteresis: the same target must be wanted N rounds in a row."""

    def __init__(self, rounds: int) -> None:
        self._rounds = rounds

    def check(self, ctx: GuardContext) -> Block | None:
        n = ctx.state.pending_rounds
        if n < self._rounds:
            return Block(Code.PENDING, f"(round {n}/{self._rounds})")
        return None


class HoldDownGuard:
    """No change sooner than ``hold_down_s`` after the previous one (for this prefix)."""

    def __init__(self, hold_down_s: float) -> None:
        self._hold_down_s = hold_down_s

    def check(self, ctx: GuardContext) -> Block | None:
        changed_at = ctx.state.changed_at
        if changed_at is not None and ctx.now - changed_at < self._hold_down_s:
            left = self._hold_down_s - (ctx.now - changed_at)
            return Block(Code.HOLD_DOWN, f"(hold-down, {left:.0f}s left)")
        return None


class RateLimitGuard:
    """Global budget of route changes per window, across all prefixes."""

    def __init__(self, max_changes: int, window_s: float = 60.0) -> None:
        self._max_changes = max_changes
        self._window_s = window_s

    def check(self, ctx: GuardContext) -> Block | None:
        recent = [t for t in ctx.recent_changes if 0 <= ctx.now - t < self._window_s]
        if len(recent) >= self._max_changes:
            return Block(Code.RATE_LIMIT, f"(rate limit {self._max_changes}/min reached)")
        return None
