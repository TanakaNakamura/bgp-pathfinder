"""Runs every probe method over every (prefix, provider) path and keeps the windows."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from optimizer.domain.models import ProviderMetrics, Sample
from optimizer.probing.probes import Probe
from optimizer.probing.window import MetricsCalculator, SampleWindow


@dataclass(frozen=True)
class ProbePath:
    """Probe ``targets`` inside ``prefix`` through ``provider`` (sourced from ``source``)."""

    prefix: str
    provider: str
    source: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class RoundResult:
    prefix: str
    provider: str
    samples: tuple[Sample, ...]


class Prober:
    """Implements ``RoundRunner`` and ``MetricsSource``."""

    def __init__(
        self,
        paths: Sequence[ProbePath],
        probes: Sequence[Probe],
        window_rounds: int,
        calculator: MetricsCalculator | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._paths = tuple(paths)
        self._probes = tuple(probes)
        self._calculator = calculator or MetricsCalculator()
        self._clock = clock
        self._windows = {(p.prefix, p.provider): SampleWindow(window_rounds) for p in self._paths}
        self._providers: dict[str, list[str]] = {}
        for p in self._paths:
            self._providers.setdefault(p.prefix, []).append(p.provider)

    async def run_round(self) -> list[RoundResult]:
        results = await asyncio.gather(*(self._probe_path(p) for p in self._paths))
        for path, result in zip(self._paths, results):
            self._windows[(path.prefix, path.provider)].add_round(result.samples)
        return list(results)

    def metrics(self, prefix: str) -> dict[str, ProviderMetrics]:
        return {
            provider: self._calculator.calculate(provider, self._windows[(prefix, provider)].samples())
            for provider in self._providers.get(prefix, [])
        }

    async def _probe_path(self, path: ProbePath) -> RoundResult:
        now = self._clock()
        jobs = [(probe.method, probe.measure(path.source, target)) for target in path.targets for probe in self._probes]
        measured = await asyncio.gather(*(job for _, job in jobs))
        samples = tuple(
            Sample(now, method, rtt) for (method, _), rtts in zip(jobs, measured) for rtt in rtts
        )
        return RoundResult(path.prefix, path.provider, samples)
