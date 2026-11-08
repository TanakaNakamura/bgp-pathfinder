"""Event listener that persists what the optimizer does into SQLite."""

from __future__ import annotations

from functools import singledispatchmethod
from typing import Sequence

from optimizer.app.events import (
    BgpStateChanged,
    ModeChanged,
    PrefixEvaluated,
    RoundCompleted,
    RoundMeasured,
    RouteChanged,
    Started,
    Stopped,
    mode_name,
)
from optimizer.domain.models import Action
from optimizer.storage.sqlite import SqliteStore

RETENTION_S = 24 * 3600
PRUNE_EVERY_ROUNDS = 150


def measurement_rows(results: Sequence[object]) -> list[tuple]:
    """One row per (prefix, provider, method) of a probing round."""
    rows = []
    for r in results:
        for method in sorted({s.method for s in r.samples}):
            ss = [s for s in r.samples if s.method == method]
            rtts = [s.rtt_ms for s in ss if s.rtt_ms is not None]
            rows.append(
                (
                    ss[0].ts,
                    r.prefix,
                    r.provider,
                    method,
                    len(ss),
                    len(rtts),
                    sum(rtts) / len(rtts) if rtts else None,
                    min(rtts) if rtts else None,
                    max(rtts) if rtts else None,
                    100.0 * (len(ss) - len(rtts)) / len(ss),
                )
            )
    return rows


class SqliteRecorder:
    def __init__(
        self,
        store: SqliteStore,
        retention_s: float = RETENTION_S,
        prune_every_rounds: int = PRUNE_EVERY_ROUNDS,
    ) -> None:
        self._store = store
        self._retention_s = retention_s
        self._prune_every = prune_every_rounds

    @singledispatchmethod
    def handle(self, event: object) -> None:
        """Events this listener does not care about are ignored."""

    @handle.register
    def _(self, event: Started) -> None:
        self._store.reset_runtime()
        self._store.set_kv("mode", mode_name(event.dry_run))
        self._store.add_event(event.ts, "start", version=event.version, mode=mode_name(event.dry_run), pid=event.pid)

    @handle.register
    def _(self, event: Stopped) -> None:
        self._store.add_event(event.ts, "stop", reason=event.reason)

    @handle.register
    def _(self, event: RoundMeasured) -> None:
        self._store.add_measurements(measurement_rows(event.results))

    @handle.register
    def _(self, event: PrefixEvaluated) -> None:
        d = event.decision
        self._store.set_provider_status(event.prefix, event.ts, event.metrics, d.scores)
        self._store.set_prefix_state(event.ts, event.state, str(d.code), d.reason)

    @handle.register
    def _(self, event: RouteChanged) -> None:
        d = event.decision
        self._store.add_decision(event.ts, d, event.next_hop, event.metrics, event.command, event.dry_run)
        if d.action is Action.ANNOUNCE:
            self._store.set_override(d.prefix, d.target, event.next_hop, event.ts, event.dry_run)
        else:
            self._store.clear_override(d.prefix)

    @handle.register
    def _(self, event: ModeChanged) -> None:
        self._store.clear_override()
        self._store.set_kv("mode", mode_name(event.new_dry_run))
        self._store.add_event(event.ts, "mode", old=mode_name(event.old_dry_run), new=mode_name(event.new_dry_run))

    @handle.register
    def _(self, event: BgpStateChanged) -> None:
        info = {"peer": event.peer, "state": event.state, "reason": event.reason, "since": event.ts}
        self._store.set_kv("bgp_state", info)
        self._store.add_event(event.ts, "bgp", **info)

    @handle.register
    def _(self, event: RoundCompleted) -> None:
        self._store.set_kv("heartbeat", event.ts)
        if event.round_no % self._prune_every == 0:
            self._store.prune(event.ts - self._retention_s)
