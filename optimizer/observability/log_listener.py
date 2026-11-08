"""Event listener that writes the optimizer's story as structured JSON log lines.

Event names and fields are a stable interface: the e2e test and operators
grep for them (e.g. "dry-run: would announce", "decision: no change").
"""

from __future__ import annotations

import logging
from functools import singledispatchmethod

from optimizer.app.events import (
    BgpStateChanged,
    ModeChanged,
    PrefixEvaluated,
    RouteChanged,
    Started,
    Stopped,
    mode_name,
)
from optimizer.domain.models import Code, Decision

METRICS_LOG_EVERY_ROUNDS = 15


class JsonLogListener:
    def __init__(
        self,
        logger: logging.Logger | None = None,
        metrics_every_rounds: int = METRICS_LOG_EVERY_ROUNDS,
    ) -> None:
        self._log = logger or logging.getLogger("optimizer")
        self._metrics_every = metrics_every_rounds
        self._last_hold: dict[str, tuple] = {}

    @singledispatchmethod
    def handle(self, event: object) -> None:
        """Events this listener does not care about are ignored."""

    @handle.register
    def _(self, event: Started) -> None:
        self._log.info(
            "optimizer started",
            extra={
                "version": event.version,
                "mode": mode_name(event.dry_run),
                "prefixes": list(event.prefixes),
                "providers": list(event.providers),
                "base_provider": event.base_provider,
            },
        )

    @handle.register
    def _(self, event: Stopped) -> None:
        self._log.info("optimizer stopping", extra={"reason": event.reason})

    @handle.register
    def _(self, event: RouteChanged) -> None:
        d = event.decision
        self._log.warning(
            ("dry-run: would " if event.dry_run else "route ") + str(d.action),
            extra={
                "prefix": d.prefix,
                "action": str(d.action),
                "from": d.current,
                "to": d.target,
                "next_hop": event.next_hop,
                "code": str(d.code),
                "reason": d.reason,
                "dry_run": event.dry_run,
                "command": event.command,
                "metrics": {k: v.to_dict() for k, v in event.metrics.items()},
            },
        )
        self._last_hold.pop(d.prefix, None)

    @handle.register
    def _(self, event: PrefixEvaluated) -> None:
        if not event.decision.changed:
            self._log_hold(event.decision)
        if event.round_no % self._metrics_every == 0:
            self._log.info(
                "metrics",
                extra={
                    "prefix": event.prefix,
                    "metrics": {k: v.to_dict() for k, v in event.metrics.items()},
                    "active": event.state.active,
                },
            )

    @handle.register
    def _(self, event: ModeChanged) -> None:
        self._log.warning(
            "mode changed", extra={"old": mode_name(event.old_dry_run), "new": mode_name(event.new_dry_run)}
        )

    @handle.register
    def _(self, event: BgpStateChanged) -> None:
        info = {"peer": event.peer, "state": event.state, "reason": event.reason, "since": event.ts}
        level = logging.INFO if event.state in ("up", "connected") else logging.WARNING
        self._log.log(level, "bgp neighbor state", extra=info)

    def _log_hold(self, d: Decision) -> None:
        """Log non-actions only on transitions, to keep the log readable."""
        key = (d.code, d.wanted)
        if d.code is Code.STEADY and self._last_hold.get(d.prefix, key)[0] is Code.STEADY:
            self._last_hold[d.prefix] = key
            return
        if self._last_hold.get(d.prefix) == key and d.code is not Code.PENDING:
            return
        self._last_hold[d.prefix] = key
        self._log.info(
            "decision: no change",
            extra={"prefix": d.prefix, "code": str(d.code), "current": d.current, "wanted": d.wanted,
                   "reason": d.reason},
        )
