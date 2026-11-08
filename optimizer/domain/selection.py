"""Which provider should carry a prefix, ignoring hysteresis, hold-down and limits."""

from __future__ import annotations

from typing import Mapping

from optimizer.domain.models import Code, Proposal, ProviderMetrics
from optimizer.domain.scoring import ImprovementCriterion, ProviderClassifier, Scorer


def _describe(a: str, ma: ProviderMetrics, b: str, mb: ProviderMetrics) -> str:
    def f(v):
        return "-" if v is None else f"{v:.1f}"

    return f"{a}: rtt {f(ma.rtt_ms)} ms loss {ma.loss_pct:.1f} %; {b}: rtt {f(mb.rtt_ms)} ms loss {mb.loss_pct:.1f} %"


class PathSelector:
    """Selection rules, in order:

    1. not enough samples for the current provider -> no opinion;
    2. no provider is usable (all bad or without data) -> no opinion;
    3. while overriding: back to base as soon as the override is no longer
       better than base by ``return_ratio`` x threshold (a narrower band than
       the switch threshold, so we do not oscillate around it);
    4. otherwise the best usable provider, if it is significantly better.
    """

    def __init__(
        self,
        base_provider: str,
        scorer: Scorer,
        classifier: ProviderClassifier,
        criterion: ImprovementCriterion,
        return_ratio: float,
    ) -> None:
        self._base = base_provider
        self._scorer = scorer
        self._classifier = classifier
        self._criterion = criterion
        self._return_ratio = return_ratio

    def select(self, current: str, metrics: Mapping[str, ProviderMetrics]) -> Proposal:
        cur = metrics.get(current)
        if not self._classifier.has_data(cur):
            return Proposal(None, Code.INSUFFICIENT_DATA, f"not enough samples for {current}")

        usable = {n: m for n, m in metrics.items() if self._classifier.is_usable(m)}
        if not usable:
            return Proposal(None, Code.ALL_BAD, "every provider is bad or has no data")

        base = self._base
        if current != base and base in usable:
            if not self._criterion.is_better(cur, metrics[base], factor=self._return_ratio):
                return Proposal(
                    base, Code.RETURN_TO_BASE, "base path is good again: " + _describe(base, metrics[base], current, cur)
                )

        alternatives = [n for n in usable if n != current]
        if alternatives:
            best = min(alternatives, key=lambda n: self._scorer.score(usable[n]))
            if self._criterion.is_better(usable[best], cur):
                code = Code.RETURN_TO_BASE if best == base else Code.BETTER_PATH
                return Proposal(best, code, f"{best} better than {current}: " + _describe(best, usable[best], current, cur))

        return Proposal(current, Code.STEADY, f"{current} is fine")
