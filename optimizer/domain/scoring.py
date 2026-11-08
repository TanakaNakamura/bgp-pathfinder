"""How good is a path, is it usable, and is one path significantly better than another."""

from __future__ import annotations

from typing import Protocol

from optimizer.domain.models import ProviderMetrics


class Scorer(Protocol):
    """Lower is better. ``None`` means unusable (nothing received)."""

    def score(self, m: ProviderMetrics) -> float | None: ...


class WeightedScorer:
    """score = rtt + jitter_weight * jitter + loss_weight_ms * loss% (loss is penalised heavily)."""

    def __init__(self, loss_weight_ms: float = 25.0, jitter_weight: float = 1.0) -> None:
        self._loss_weight_ms = loss_weight_ms
        self._jitter_weight = jitter_weight

    def score(self, m: ProviderMetrics) -> float | None:
        if m.rtt_ms is None:
            return None
        return m.rtt_ms + self._jitter_weight * (m.jitter_ms or 0.0) + self._loss_weight_ms * m.loss_pct


class ProviderClassifier:
    """Is there enough data about a provider, and is it too bad to be used at all?"""

    def __init__(self, min_samples: int, bad_loss_pct: float, bad_rtt_ms: float) -> None:
        self._min_samples = min_samples
        self._bad_loss_pct = bad_loss_pct
        self._bad_rtt_ms = bad_rtt_ms

    def has_data(self, m: ProviderMetrics | None) -> bool:
        return m is not None and m.sent >= self._min_samples

    def is_bad(self, m: ProviderMetrics) -> bool:
        return m.rtt_ms is None or m.loss_pct >= self._bad_loss_pct or m.rtt_ms >= self._bad_rtt_ms

    def is_usable(self, m: ProviderMetrics | None) -> bool:
        return self.has_data(m) and not self.is_bad(m)


class ImprovementCriterion:
    """Is a candidate better than a reference by at least ``factor`` x threshold?

    Better means: a lower score AND a gain of >= rtt_threshold_ms in RTT
    or >= loss_threshold_pct in loss.
    """

    def __init__(self, scorer: Scorer, rtt_threshold_ms: float, loss_threshold_pct: float) -> None:
        self._scorer = scorer
        self._rtt_threshold_ms = rtt_threshold_ms
        self._loss_threshold_pct = loss_threshold_pct

    def is_better(self, cand: ProviderMetrics, ref: ProviderMetrics, factor: float = 1.0) -> bool:
        sc, sr = self._scorer.score(cand), self._scorer.score(ref)
        if sc is None:
            return False
        if sr is None:
            return True
        if sc >= sr:
            return False
        rtt_gain = ref.rtt_ms - cand.rtt_ms  # both set when scores are set
        loss_gain = ref.loss_pct - cand.loss_pct
        return rtt_gain >= factor * self._rtt_threshold_ms or loss_gain >= factor * self._loss_threshold_pct
