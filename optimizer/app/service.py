"""Optimizer service: one probing round in, route decisions and events out.

Synchronous and free of direct I/O: it only talks to abstractions (metrics
source, decision engine, mode controller / injectors, event publisher), so it
is fully unit-testable with fakes.
"""

from __future__ import annotations

import logging
from typing import Mapping, Sequence

from optimizer import __version__
from optimizer.app.events import (
    BgpStateChanged,
    ModeChanged,
    PrefixEvaluated,
    RoundCompleted,
    RoundMeasured,
    RouteChanged,
    Started,
    Stopped,
)
from optimizer.app.mode import ModeController
from optimizer.bgp.messages import Ack, ExaBgpMessage, NeighborState, Shutdown, Unparsed
from optimizer.domain.engine import DecisionEngine
from optimizer.domain.models import Action, Decision, PrefixState, ProviderMetrics
from optimizer.ports import AckSink, EventPublisher, MetricsSource

log = logging.getLogger("optimizer")

RATE_WINDOW_S = 60.0


class OptimizerService:
    def __init__(
        self,
        *,
        prefixes: Sequence[str],
        next_hops: Mapping[str, str],
        engine: DecisionEngine,
        metrics: MetricsSource,
        mode: ModeController,
        events: EventPublisher,
        acks: AckSink,
    ) -> None:
        self._prefixes = tuple(prefixes)
        self._next_hops = dict(next_hops)
        self._engine = engine
        self._metrics = metrics
        self._mode = mode
        self._events = events
        self._acks = acks
        self._states = self._fresh_states()
        self._changes: list[float] = []
        self._round = 0

    # --- read-only views ------------------------------------------------------

    @property
    def states(self) -> Mapping[str, PrefixState]:
        return dict(self._states)

    @property
    def dry_run(self) -> bool:
        return self._mode.dry_run

    # --- lifecycle --------------------------------------------------------------

    def start(self, now: float, pid: int) -> None:
        self._events.publish(
            Started(
                ts=now,
                version=__version__,
                dry_run=self._mode.dry_run,
                pid=pid,
                prefixes=self._prefixes,
                providers=tuple(self._next_hops),
                base_provider=self._engine.base_provider,
            )
        )

    def stop(self, now: float, reason: str) -> None:
        self._events.publish(Stopped(now, reason))

    # --- runtime mode switch (optimizer mode dry-run|live) ------------------------

    def poll_mode(self, now: float) -> None:
        change = self._mode.poll()
        if change is None:
            return
        if change.new_dry_run:
            # leaving live mode: take every real route back first
            for prefix, state in self._states.items():
                if state.active is not None:
                    self._mode.live_injector.withdraw(prefix)
        # start from a clean slate in the new mode: plain BGP, no pending changes
        self._states = self._fresh_states()
        self._changes.clear()
        self._events.publish(ModeChanged(now, change.old_dry_run, change.new_dry_run))

    # --- one probing round --------------------------------------------------------

    def process_round(self, now: float, results: Sequence[object] = ()) -> list[Decision]:
        self._round += 1
        if results:
            self._events.publish(RoundMeasured(now, results))

        decisions = []
        for prefix in self._prefixes:
            metrics = self._metrics.metrics(prefix)
            decision, new_state = self._engine.decide(self._states[prefix], metrics, now, self._changes)
            if decision.changed:
                self._apply(now, decision, metrics)
            self._states[prefix] = new_state
            self._events.publish(
                PrefixEvaluated(now, self._round, prefix, metrics, decision, new_state, self._engine.base_provider)
            )
            decisions.append(decision)

        self._events.publish(RoundCompleted(now, self._round))
        return decisions

    def _apply(self, now: float, d: Decision, metrics: Mapping[str, ProviderMetrics]) -> None:
        self._changes = [t for t in self._changes if now - t < RATE_WINDOW_S] + [now]
        injector = self._mode.injector
        if d.action is Action.ANNOUNCE:
            next_hop = self._next_hops[d.target]
            command = injector.announce(d.prefix, next_hop)
        else:
            next_hop = None
            command = injector.withdraw(d.prefix)
        self._events.publish(RouteChanged(now, d, next_hop, metrics, command, injector.dry_run))

    # --- messages from ExaBGP -------------------------------------------------------

    def on_exabgp_message(self, msg: ExaBgpMessage, now: float) -> bool:
        """Handle one message from ExaBGP. Returns True when ExaBGP is shutting down."""
        match msg:
            case Ack(ok=ok):
                self._acks.on_ack(ok)
            case NeighborState(peer=peer, state=state, reason=reason):
                self._events.publish(BgpStateChanged(now, peer, state, reason))
            case Shutdown():
                return True
            case Unparsed(line=line):
                log.warning("unparsed message from exabgp", extra={"line": line[:200]})
        return False

    def _fresh_states(self) -> dict[str, PrefixState]:
        return {p: PrefixState(p) for p in self._prefixes}
