"""Decision engine: proposal from the path selector, filtered by change guards.

``DecisionEngine.decide`` is a pure method: same inputs, same outputs, no
I/O, no clock, no hidden state. The per-prefix state goes in and comes back
out as an immutable ``PrefixState``.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from optimizer.domain.guards import ChangeGuard, ConfirmationGuard, GuardContext, HoldDownGuard, RateLimitGuard
from optimizer.domain.models import Action, Code, Decision, Policy, PrefixState, ProviderMetrics
from optimizer.domain.scoring import ImprovementCriterion, ProviderClassifier, Scorer, WeightedScorer
from optimizer.domain.selection import PathSelector


class DecisionEngine:
    def __init__(
        self,
        base_provider: str,
        selector: PathSelector,
        scorer: Scorer,
        guards: Sequence[ChangeGuard],
    ) -> None:
        self._base = base_provider
        self._selector = selector
        self._scorer = scorer
        self._guards = tuple(guards)

    @classmethod
    def from_policy(cls, policy: Policy, extra_guards: Sequence[ChangeGuard] = ()) -> DecisionEngine:
        """Default wiring. ``extra_guards`` run after the built-in ones."""
        scorer = WeightedScorer(policy.loss_weight_ms, policy.jitter_weight)
        classifier = ProviderClassifier(policy.min_samples, policy.bad_loss_pct, policy.bad_rtt_ms)
        criterion = ImprovementCriterion(scorer, policy.rtt_threshold_ms, policy.loss_threshold_pct)
        selector = PathSelector(policy.base_provider, scorer, classifier, criterion, policy.return_ratio)
        guards: list[ChangeGuard] = [
            ConfirmationGuard(policy.confirm_rounds),
            HoldDownGuard(policy.hold_down_s),
            RateLimitGuard(policy.max_changes_per_minute),
            *extra_guards,
        ]
        return cls(policy.base_provider, selector, scorer, guards)

    @property
    def base_provider(self) -> str:
        return self._base

    def decide(
        self,
        state: PrefixState,
        metrics: Mapping[str, ProviderMetrics],
        now: float,
        recent_changes: Sequence[float] = (),
    ) -> tuple[Decision, PrefixState]:
        """One decision round for one prefix. Returns the decision and the new state."""
        current = state.current(self._base)
        scores = {n: self._scorer.score(m) for n, m in metrics.items()}
        proposal = self._selector.select(current, metrics)

        def keep(code: Code, reason: str, new_state: PrefixState) -> tuple[Decision, PrefixState]:
            decision = Decision(state.prefix, Action.NONE, code, current, current, proposal.wanted, reason, scores)
            return decision, new_state

        if proposal.wanted is None or proposal.wanted == current:
            return keep(proposal.code, proposal.reason, state.without_pending())

        tracking = state.wanting(proposal.wanted)
        ctx = GuardContext(tracking, now, recent_changes)
        for guard in self._guards:
            block = guard.check(ctx)
            if block is not None:
                return keep(block.code, f"{proposal.reason} {block.note}", tracking)

        action = Action.WITHDRAW if proposal.wanted == self._base else Action.ANNOUNCE
        new_state = state.switched_to(None if action is Action.WITHDRAW else proposal.wanted, now)
        decision = Decision(
            state.prefix, action, proposal.code, current, proposal.wanted, proposal.wanted, proposal.reason, scores
        )
        return decision, new_state
