"""Configuration loading and validation (config.yaml).

``ConfigParser`` validates one section per method; ``load()`` / ``parse()``
are thin entry points. Unknown keys are errors everywhere.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from optimizer.domain.models import Policy


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    probe_source: str
    next_hop: str


@dataclass(frozen=True)
class MonitoredPrefix:
    prefix: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class ProbeConfig:
    interval_s: float = 2.0
    window_rounds: int = 10
    icmp_count: int = 5
    icmp_interval_s: float = 0.2
    icmp_timeout_s: float = 1.0
    tcp_enabled: bool = True
    tcp_port: int = 80
    tcp_timeout_s: float = 1.0


@dataclass(frozen=True)
class BgpConfig:
    local_preference: int = 300
    communities: tuple[str, ...] = ("no-export", "no-advertise")
    neighbor: str | None = None  # prefix commands with "neighbor X" when set


@dataclass(frozen=True)
class MetricsConfig:
    """Prometheus endpoint. Absent from config.yaml = disabled."""

    listen: str = "0.0.0.0"
    port: int = 9101


@dataclass(frozen=True)
class Config:
    providers: tuple[Provider, ...]
    prefixes: tuple[MonitoredPrefix, ...]
    policy: Policy
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    bgp: BgpConfig = field(default_factory=BgpConfig)
    metrics: MetricsConfig | None = None
    dry_run: bool = True
    db_path: str = "/var/lib/optimizer/optimizer.db"
    log_level: str = "INFO"
    log_file: str | None = None

    def provider(self, name: str) -> Provider:
        for p in self.providers:
            if p.name == name:
                return p
        raise KeyError(name)


class ConfigParser:
    """Turns the raw YAML mapping into a validated ``Config``."""

    def parse(self, raw: Any) -> Config:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a mapping")
        providers = self._providers(raw.get("providers"))
        names = [p.name for p in providers]
        base = raw.get("base_provider")
        if base not in names:
            raise ConfigError(f"base_provider {base!r} is not one of {names}")
        log = raw.get("log") or {}
        return Config(
            providers=providers,
            prefixes=self._prefixes(raw.get("prefixes")),
            policy=self._policy(raw.get("decision"), base),
            probe=self._probe(raw.get("probe")),
            bgp=self._bgp(raw.get("bgp")),
            metrics=self._metrics(raw.get("metrics")),
            dry_run=bool(raw.get("dry_run", True)),
            db_path=str(raw.get("db_path", Config.db_path)),
            log_level=str(log.get("level", "INFO")),
            log_file=log.get("file"),
        )

    # --- sections -------------------------------------------------------------------

    def _providers(self, raw: Any) -> tuple[Provider, ...]:
        providers = []
        for i, p in enumerate(raw or []):
            where = f"providers[{i}]"
            prov = self._pick(Provider, p, where)
            providers.append(
                Provider(
                    name=str(prov.name),
                    probe_source=self._ip(prov.probe_source, f"{where}.probe_source"),
                    next_hop=self._ip(prov.next_hop, f"{where}.next_hop"),
                )
            )
        if len(providers) < 2:
            raise ConfigError("at least two providers are required")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ConfigError("provider names must be unique")
        return tuple(providers)

    def _prefixes(self, raw: Any) -> tuple[MonitoredPrefix, ...]:
        prefixes = []
        for i, p in enumerate(raw or []):
            where = f"prefixes[{i}]"
            try:
                net = ipaddress.IPv4Network(str(p["prefix"]), strict=True)
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigError(f"{where}.prefix: {exc}") from exc
            targets = tuple(self._ip(t, f"{where}.targets") for t in p.get("targets") or [])
            if not targets:
                raise ConfigError(f"{where}: at least one target IP is required")
            for t in targets:
                if ipaddress.IPv4Address(t) not in net:
                    raise ConfigError(f"{where}: target {t} is outside {net}")
            prefixes.append(MonitoredPrefix(prefix=str(net), targets=targets))
        if not prefixes:
            raise ConfigError("at least one prefix is required")
        return tuple(prefixes)

    def _probe(self, raw: Any) -> ProbeConfig:
        data = dict(raw or {})
        icmp = data.pop("icmp", None) or {}
        tcp = data.pop("tcp", None) or {}
        probe = self._pick(
            ProbeConfig,
            data,
            "probe",
            **{f"icmp_{k}": v for k, v in icmp.items()},
            **{f"tcp_{k}": v for k, v in tcp.items()},
        )
        if probe.window_rounds < 1 or probe.icmp_count < 1 or probe.interval_s <= 0:
            raise ConfigError("probe: window_rounds, icmp.count and interval_s must be positive")
        return probe

    def _policy(self, raw: Any, base: str) -> Policy:
        policy = self._pick(Policy, raw, "decision", base_provider=base)
        if policy.confirm_rounds < 1:
            raise ConfigError("decision.confirm_rounds must be >= 1")
        if not 0 < policy.return_ratio <= 1:
            raise ConfigError("decision.return_ratio must be in (0, 1]")
        return policy

    def _bgp(self, raw: Any) -> BgpConfig:
        data = dict(raw or {})
        if "communities" in data:
            data["communities"] = tuple(data["communities"])
        return self._pick(BgpConfig, data, "bgp")

    def _metrics(self, raw: Any) -> MetricsConfig | None:
        if raw is None:
            return None
        metrics = self._pick(MetricsConfig, raw, "metrics")
        self._ip(metrics.listen, "metrics.listen")
        if not 0 < int(metrics.port) < 65536:
            raise ConfigError("metrics.port must be 1..65535")
        return metrics

    # --- helpers --------------------------------------------------------------------

    @staticmethod
    def _pick(cls, data: dict[str, Any] | None, where: str, **extra):
        """Build a dataclass from a dict, rejecting unknown keys."""
        data = {**(data or {}), **extra}
        allowed = {f.name for f in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ConfigError(f"{where}: {exc}") from exc

    @staticmethod
    def _ip(value: Any, where: str) -> str:
        try:
            return str(ipaddress.IPv4Address(str(value)))
        except ValueError as exc:
            raise ConfigError(f"{where}: invalid IPv4 address {value!r}") from exc


def parse(raw: dict[str, Any]) -> Config:
    return ConfigParser().parse(raw)


def load(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as fh:
        return parse(yaml.safe_load(fh))
