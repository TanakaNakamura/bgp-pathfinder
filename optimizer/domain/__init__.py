"""Domain layer: pure decision logic.

No I/O, no clock (``now`` is always an argument), no framework imports.
Everything here is deterministic and unit-testable in isolation.
"""

from optimizer.domain.engine import DecisionEngine
from optimizer.domain.guards import (
    Block,
    ChangeGuard,
    ConfirmationGuard,
    GuardContext,
    HoldDownGuard,
    RateLimitGuard,
)
from optimizer.domain.models import (
    Action,
    Code,
    Decision,
    Policy,
    PrefixState,
    Proposal,
    ProviderMetrics,
    Sample,
)
from optimizer.domain.scoring import ImprovementCriterion, ProviderClassifier, Scorer, WeightedScorer
from optimizer.domain.selection import PathSelector

__all__ = [
    "Action",
    "Block",
    "ChangeGuard",
    "Code",
    "ConfirmationGuard",
    "Decision",
    "DecisionEngine",
    "GuardContext",
    "HoldDownGuard",
    "ImprovementCriterion",
    "PathSelector",
    "Policy",
    "PrefixState",
    "Proposal",
    "ProviderClassifier",
    "ProviderMetrics",
    "RateLimitGuard",
    "Sample",
    "Scorer",
    "WeightedScorer",
]
