"""Sliding window of samples and the metrics computed over it."""

from __future__ import annotations

from collections import deque
from statistics import fmean
from typing import Iterable

from optimizer.domain.models import ProviderMetrics, Sample


class SampleWindow:
    """Sliding window over the last N probing rounds."""

    def __init__(self, rounds: int) -> None:
        self._rounds: deque[list[Sample]] = deque(maxlen=rounds)

    def add_round(self, samples: Iterable[Sample]) -> None:
        self._rounds.append(list(samples))

    def samples(self) -> list[Sample]:
        return [s for r in self._rounds for s in r]

    def clear(self) -> None:
        self._rounds.clear()

    def __len__(self) -> int:
        return len(self._rounds)


class MetricsCalculator:
    """RTT, loss and jitter of a set of samples.

    Jitter is the mean absolute difference of consecutive RTTs (RFC 3550
    style, unsmoothed). It is computed per probe method, because mixing ICMP
    and TCP RTTs would add artificial jitter, and then averaged weighted by
    the number of samples.
    """

    def calculate(self, provider: str, samples: list[Sample]) -> ProviderMetrics:
        sent = len(samples)
        received = [s.rtt_ms for s in samples if s.rtt_ms is not None]
        loss = 100.0 * (sent - len(received)) / sent if sent else 0.0

        weighted, weight = 0.0, 0
        for method in sorted({s.method for s in samples}):
            rtts = [s.rtt_ms for s in samples if s.method == method and s.rtt_ms is not None]
            j = self._jitter(rtts)
            if j is not None:
                weighted += j * len(rtts)
                weight += len(rtts)

        return ProviderMetrics(
            provider=provider,
            sent=sent,
            received=len(received),
            rtt_ms=fmean(received) if received else None,
            jitter_ms=weighted / weight if weight else None,
            loss_pct=loss,
        )

    @staticmethod
    def _jitter(rtts: list[float]) -> float | None:
        if len(rtts) < 2:
            return None
        return fmean(abs(b - a) for a, b in zip(rtts, rtts[1:]))
