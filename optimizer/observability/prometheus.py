"""Event listener that exports Prometheus metrics (scraped by the lab Prometheus, shown in Grafana).

Numeric encodings used by the dashboard value mappings:

* ``optimizer_traffic_provider_index``: 1-based position of the provider in
  ``config.providers`` (lab: 1 = isp-a, 2 = isp-b).
* ``optimizer_engine_state``: see ``ENGINE_STATES`` below.
"""

from __future__ import annotations

import math
from functools import singledispatchmethod
from typing import Sequence

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

from optimizer import __version__
from optimizer.app.events import BgpStateChanged, ModeChanged, PrefixEvaluated, RouteChanged, Started
from optimizer.domain.models import Code

ENGINE_STATES: dict[Code, int] = {
    Code.STEADY: 0,
    Code.PENDING: 1,
    Code.HOLD_DOWN: 2,
    Code.RATE_LIMIT: 3,
    Code.INSUFFICIENT_DATA: 4,
    Code.ALL_BAD: 5,
    Code.BETTER_PATH: 6,  # a change is being executed this round
    Code.RETURN_TO_BASE: 6,
}


def _value(v: float | None) -> float:
    return math.nan if v is None else float(v)


class PrometheusExporter:
    def __init__(
        self,
        providers: Sequence[str],
        prefixes: Sequence[str],
        base_provider: str,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self.registry = registry or CollectorRegistry()
        self._provider_index = {name: i + 1 for i, name in enumerate(providers)}
        reg = self.registry

        pp = ["prefix", "provider"]
        self.rtt = Gauge("optimizer_rtt_ms", "Mean probe RTT over the sliding window", pp, registry=reg)
        self.loss = Gauge("optimizer_loss_percent", "Probe loss over the sliding window", pp, registry=reg)
        self.jitter = Gauge("optimizer_jitter_ms", "Probe jitter over the sliding window", pp, registry=reg)
        self.score = Gauge("optimizer_score", "Path score (lower is better)", pp, registry=reg)
        self.probes = Gauge("optimizer_probes_in_window", "Probes sent within the window", pp, registry=reg)
        self.in_use = Gauge(
            "optimizer_provider_in_use", "1 for the provider carrying the prefix (as decided)", pp, registry=reg
        )
        self.traffic_provider = Gauge(
            "optimizer_traffic_provider_index",
            "1-based index (config order) of the provider carrying the prefix",
            ["prefix"],
            registry=reg,
        )
        self.override = Gauge(
            "optimizer_override_active", "1 while an injected route overrides plain BGP", ["prefix"], registry=reg
        )
        self.engine_state = Gauge(
            "optimizer_engine_state",
            "Decision engine state: 0 steady, 1 pending, 2 hold-down, 3 rate-limit, "
            "4 insufficient-data, 5 all-bad, 6 changing",
            ["prefix"],
            registry=reg,
        )
        self.changes = Counter(
            "optimizer_route_changes", "Route changes (announce/withdraw)", ["prefix", "action", "mode"], registry=reg
        )
        self.bgp_up = Gauge("optimizer_bgp_session_up", "1 while the iBGP session to edge is up", registry=reg)
        self.dry_run = Gauge("optimizer_dry_run", "1 in dry-run mode, 0 in live mode", registry=reg)
        self.info = Gauge("optimizer_info", "Static optimizer information", ["version", "base_provider"], registry=reg)

        self.info.labels(__version__, base_provider).set(1)
        # create every counter child up front so increase() sees the first change
        for prefix in prefixes:
            for action in ("announce", "withdraw"):
                for mode in ("live", "dry-run"):
                    self.changes.labels(prefix, action, mode)

    def serve(self, listen: str, port: int) -> None:
        start_http_server(port, addr=listen, registry=self.registry)

    @singledispatchmethod
    def handle(self, event: object) -> None:
        """Events this listener does not care about are ignored."""

    @handle.register
    def _(self, event: Started) -> None:
        self.dry_run.set(1 if event.dry_run else 0)
        self.bgp_up.set(0)

    @handle.register
    def _(self, event: PrefixEvaluated) -> None:
        current = event.state.current(event.base_provider)
        prefix = event.prefix
        for name, m in event.metrics.items():
            self.rtt.labels(prefix, name).set(_value(m.rtt_ms))
            self.loss.labels(prefix, name).set(m.loss_pct)
            self.jitter.labels(prefix, name).set(_value(m.jitter_ms))
            self.score.labels(prefix, name).set(_value(event.decision.scores.get(name)))
            self.probes.labels(prefix, name).set(m.sent)
            self.in_use.labels(prefix, name).set(1 if name == current else 0)
        self.traffic_provider.labels(prefix).set(self._provider_index.get(current, 0))
        self.override.labels(prefix).set(1 if event.state.active else 0)
        self.engine_state.labels(prefix).set(ENGINE_STATES.get(event.decision.code, 0))

    @handle.register
    def _(self, event: RouteChanged) -> None:
        mode = "dry-run" if event.dry_run else "live"
        self.changes.labels(event.decision.prefix, str(event.decision.action), mode).inc()

    @handle.register
    def _(self, event: ModeChanged) -> None:
        self.dry_run.set(1 if event.new_dry_run else 0)

    @handle.register
    def _(self, event: BgpStateChanged) -> None:
        self.bgp_up.set(1 if event.state == "up" else 0)
