"""Composition root: the only place that knows every concrete class.

Everything else depends on abstractions (``optimizer.ports``) and receives its
collaborators through the constructor (dependency inversion).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence, TextIO

from optimizer.app.daemon import Daemon
from optimizer.app.events import EventBus
from optimizer.app.mode import ModeController
from optimizer.app.service import OptimizerService
from optimizer.bgp.commands import ExaBgpCommands
from optimizer.bgp.injectors import DryRunInjector, ExaBgpInjector
from optimizer.bgp.messages import ExaBgpMessageParser
from optimizer.config import Config
from optimizer.domain.engine import DecisionEngine
from optimizer.observability.log_listener import JsonLogListener
from optimizer.observability.prometheus import PrometheusExporter
from optimizer.ports import EventListener, MetricsSource, ModeRequestSource
from optimizer.probing.probes import IcmpProbe, Probe, TcpProbe
from optimizer.probing.prober import ProbePath, Prober
from optimizer.storage.recorder import SqliteRecorder
from optimizer.storage.sqlite import SqliteStore

log = logging.getLogger("optimizer")


def build_prober(cfg: Config) -> Prober:
    p = cfg.probe
    probes: list[Probe] = [IcmpProbe(p.icmp_count, p.icmp_interval_s, p.icmp_timeout_s)]
    if p.tcp_enabled:
        probes.append(TcpProbe(p.tcp_port, p.tcp_timeout_s))
    paths = [
        ProbePath(prefix.prefix, provider.name, provider.probe_source, prefix.targets)
        for prefix in cfg.prefixes
        for provider in cfg.providers
    ]
    return Prober(paths, probes, p.window_rounds)


def build_commands(cfg: Config) -> ExaBgpCommands:
    return ExaBgpCommands(cfg.bgp.local_preference, cfg.bgp.communities, cfg.bgp.neighbor)


def build_service(
    cfg: Config,
    *,
    commands_out: TextIO,
    metrics: MetricsSource,
    mode_requests: ModeRequestSource,
    listeners: Sequence[EventListener],
) -> OptimizerService:
    commands = build_commands(cfg)
    live = ExaBgpInjector(commands_out, commands)
    mode = ModeController(mode_requests, live=live, dry=DryRunInjector(commands), dry_run=cfg.dry_run)
    return OptimizerService(
        prefixes=[p.prefix for p in cfg.prefixes],
        next_hops={p.name: p.next_hop for p in cfg.providers},
        engine=DecisionEngine.from_policy(cfg.policy),
        metrics=metrics,
        mode=mode,
        events=EventBus(list(listeners)),
        acks=live,
    )


def build_exporter(cfg: Config) -> PrometheusExporter:
    return PrometheusExporter(
        [p.name for p in cfg.providers], [p.prefix for p in cfg.prefixes], cfg.policy.base_provider
    )


@dataclass
class Application:
    daemon: Daemon
    store: SqliteStore

    def close(self) -> None:
        self.store.close()


def build_application(cfg: Config, commands_out: TextIO) -> Application:
    store = SqliteStore(cfg.db_path)
    prober = build_prober(cfg)
    listeners: list[EventListener] = [SqliteRecorder(store), JsonLogListener()]
    if cfg.metrics:
        exporter = build_exporter(cfg)
        exporter.serve(cfg.metrics.listen, cfg.metrics.port)
        log.info("metrics endpoint listening", extra={"listen": cfg.metrics.listen, "port": cfg.metrics.port})
        listeners.append(exporter)
    service = build_service(
        cfg, commands_out=commands_out, metrics=prober, mode_requests=store, listeners=listeners
    )
    daemon = Daemon(service, prober, ExaBgpMessageParser(), cfg.probe.interval_s)
    return Application(daemon, store)
