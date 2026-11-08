"""Decision engine unit tests (pure, synthetic metrics, fake clock).

The scenarios are the behavioral spec of the engine; they did not change in
the OOP refactor, only the way the engine is called.
"""

import random

import pytest

from optimizer.domain import (
    Action,
    Block,
    Code,
    ConfirmationGuard,
    DecisionEngine,
    GuardContext,
    HoldDownGuard,
    ImprovementCriterion,
    Policy,
    PrefixState,
    ProviderClassifier,
    ProviderMetrics,
    RateLimitGuard,
    WeightedScorer,
)
from optimizer.domain.selection import PathSelector

PREFIX = "198.51.100.0/24"
A, B, C = "isp-a", "isp-b", "isp-c"


def m(provider, rtt=1.0, loss=0.0, jitter=0.2, sent=30):
    received = round(sent * (1 - loss / 100))
    return ProviderMetrics(provider, sent, received, rtt if received else None, jitter, loss)


def good(provider, rtt=1.0):
    return m(provider, rtt=rtt)


def degraded(provider):
    # what degrade.sh a 150 5 looks like in the lab
    return m(provider, rtt=301.0, loss=5.0, jitter=1.0)


@pytest.fixture
def policy():
    return Policy(
        base_provider=A,
        min_samples=15,
        rtt_threshold_ms=20,
        loss_threshold_pct=2,
        return_ratio=0.5,
        confirm_rounds=3,
        hold_down_s=60,
        max_changes_per_minute=2,
        bad_loss_pct=30,
        bad_rtt_ms=1000,
    )


@pytest.fixture
def scorer(policy):
    return WeightedScorer(policy.loss_weight_ms, policy.jitter_weight)


@pytest.fixture
def criterion(policy, scorer):
    return ImprovementCriterion(scorer, policy.rtt_threshold_ms, policy.loss_threshold_pct)


class Runner:
    """Feeds rounds into the engine like the service does, with a fake clock."""

    def __init__(self, policy, interval=2.0, start=1000.0, engine=None):
        self.engine = engine or DecisionEngine.from_policy(policy)
        self.policy = policy
        self.state = PrefixState(PREFIX)
        self.now = start
        self.interval = interval
        self.changes: list[float] = []
        self.decisions = []

    def step(self, metrics):
        d, self.state = self.engine.decide(self.state, metrics, self.now, self.changes)
        if d.changed:
            self.changes.append(self.now)
        self.decisions.append(d)
        self.now += self.interval
        return d

    def run(self, metrics, rounds):
        return [self.step(metrics) for _ in range(rounds)]

    def actions(self):
        return [(d.action, d.target) for d in self.decisions if d.changed]


# --- scoring ---------------------------------------------------------------


def test_score_penalises_loss_heavily(scorer):
    lossy_fast = m(A, rtt=10, loss=4)
    clean_slow = m(B, rtt=80, loss=0)
    assert scorer.score(clean_slow) < scorer.score(lossy_fast)


def test_score_is_none_without_replies(scorer):
    assert scorer.score(m(A, loss=100)) is None


def test_is_better_requires_threshold(criterion):
    assert not criterion.is_better(m(B, rtt=5), m(A, rtt=20))  # 15 ms < 20 ms
    assert criterion.is_better(m(B, rtt=5), m(A, rtt=30))  # 25 ms >= 20 ms
    assert criterion.is_better(m(B, rtt=5, loss=0), m(A, rtt=5, loss=3))  # 3 % >= 2 %


def test_classifier():
    c = ProviderClassifier(min_samples=15, bad_loss_pct=30, bad_rtt_ms=1000)
    assert not c.has_data(None) and not c.has_data(m(A, sent=10))
    assert c.is_usable(m(A))
    assert c.is_bad(m(A, loss=30)) and c.is_bad(m(A, rtt=1000)) and c.is_bad(m(A, loss=100))


# --- switching on degradation -------------------------------------------


def test_switch_on_degradation(policy):
    r = Runner(policy)
    r.run({A: good(A), B: good(B)}, 5)
    assert r.actions() == []

    d1, d2, d3 = r.run({A: degraded(A), B: good(B)}, 3)
    assert d1.code is Code.PENDING and d2.code is Code.PENDING
    assert d3.action is Action.ANNOUNCE
    assert d3.target == B and d3.current == A and d3.code is Code.BETTER_PATH
    assert r.state.active == B


def test_switch_on_loss_only(policy):
    r = Runner(policy)
    r.run({A: m(A, rtt=1, loss=5), B: m(B, rtt=1, loss=0)}, 3)
    assert r.actions() == [(Action.ANNOUNCE, B)]


# --- noise below threshold ----------------------------------------------


def test_no_switch_on_noise_below_threshold(policy):
    rng = random.Random(42)
    r = Runner(policy)
    for _ in range(300):
        a = m(A, rtt=5 + rng.uniform(0, 12), loss=rng.choice([0, 0, 0, 1.0]), jitter=rng.uniform(0, 3))
        b = m(B, rtt=2 + rng.uniform(0, 5), loss=0, jitter=rng.uniform(0, 1))
        r.step({A: a, B: b})
    assert r.actions() == []
    assert r.state.active is None


# --- hysteresis -----------------------------------------------------------


def test_hysteresis_needs_consecutive_rounds(policy):
    r = Runner(policy)
    bad, ok = {A: degraded(A), B: good(B)}, {A: good(A), B: good(B)}
    r.run(bad, 2)
    r.run(ok, 1)  # streak broken
    r.run(bad, 2)
    r.run(ok, 1)
    assert r.actions() == []
    r.run(bad, 3)
    assert r.actions() == [(Action.ANNOUNCE, B)]


def test_pending_counter_resets_when_target_changes():
    policy = Policy(base_provider=A, confirm_rounds=3)
    r = Runner(policy)
    r.run({A: degraded(A), B: good(B, 1), C: good(C, 5)}, 2)  # wants B
    r.run({A: degraded(A), B: good(B, 5), C: good(C, 1)}, 2)  # now wants C: count restarts
    assert r.actions() == []
    r.step({A: degraded(A), B: good(B, 5), C: good(C, 1)})
    assert r.actions() == [(Action.ANNOUNCE, C)]


# --- hold-down -------------------------------------------------------------


def test_hold_down_delays_return(policy):
    r = Runner(policy)
    r.run({A: degraded(A), B: good(B)}, 3)
    switched_at = r.changes[-1]
    assert r.state.active == B

    # A recovers immediately; we must stay on B until hold-down expires
    while r.now - switched_at < policy.hold_down_s:
        d = r.step({A: good(A), B: good(B)})
        assert not d.changed
    assert r.decisions[-1].code is Code.HOLD_DOWN

    d = r.step({A: good(A), B: good(B)})
    assert d.action is Action.WITHDRAW
    assert d.code is Code.RETURN_TO_BASE
    assert r.changes[-1] - switched_at >= policy.hold_down_s
    assert r.state.active is None


def test_hold_down_applies_after_return_too(policy):
    r = Runner(policy)
    r.run({A: degraded(A), B: good(B)}, 3)
    r.run({A: good(A), B: good(B)}, 40)
    assert [a for a, _ in r.actions()] == [Action.ANNOUNCE, Action.WITHDRAW]
    returned_at = r.changes[-1]

    r.run({A: degraded(A), B: good(B)}, 10)  # 20 s later: still in hold-down
    assert len(r.actions()) == 2
    assert r.decisions[-1].code is Code.HOLD_DOWN
    r.run({A: degraded(A), B: good(B)}, 30)
    assert r.actions()[-1] == (Action.ANNOUNCE, B)
    assert r.changes[-1] - returned_at >= policy.hold_down_s


# --- rate limit ------------------------------------------------------------


def test_rate_limit_blocks_changes(policy):
    engine = DecisionEngine.from_policy(policy)
    now = 5000.0
    state = PrefixState(PREFIX, pending=B, pending_rounds=policy.confirm_rounds)
    metrics = {A: degraded(A), B: good(B)}

    d, new = engine.decide(state, metrics, now, recent_changes=[now - 10, now - 30])
    assert d.action is Action.NONE and d.code is Code.RATE_LIMIT
    assert new.pending == B  # still wanted, will execute when budget frees up

    d, _ = engine.decide(new, metrics, now, recent_changes=[now - 70, now - 30])
    assert d.action is Action.ANNOUNCE


def test_rate_limit_is_global_across_prefixes():
    policy = Policy(base_provider=A, confirm_rounds=1, max_changes_per_minute=2, hold_down_s=0)
    engine = DecisionEngine.from_policy(policy)
    metrics = {A: degraded(A), B: good(B)}
    changes: list[float] = []
    results = []
    for i in range(3):
        d, _ = engine.decide(PrefixState(f"192.0.{i}.0/24"), metrics, 100.0, changes)
        if d.changed:
            changes.append(100.0)
        results.append(d.code)
    assert results == [Code.BETTER_PATH, Code.BETTER_PATH, Code.RATE_LIMIT]


# --- do nothing cases -------------------------------------------------------


def test_both_bad_do_nothing(policy):
    r = Runner(policy)
    r.run({A: m(A, rtt=50, loss=50), B: m(B, rtt=40, loss=45)}, 20)
    assert r.actions() == []
    assert r.decisions[-1].code is Code.ALL_BAD


def test_both_bad_while_overriding_keeps_override(policy):
    engine = DecisionEngine.from_policy(policy)
    state = PrefixState(PREFIX, active=B, changed_at=0.0)
    d, new = engine.decide(state, {A: m(A, loss=60), B: m(B, loss=70)}, 1000.0)
    assert d.action is Action.NONE and d.code is Code.ALL_BAD
    assert new.active == B


def test_insufficient_data_do_nothing(policy):
    r = Runner(policy)
    r.run({A: m(A, rtt=300, loss=5, sent=6), B: m(B, sent=6)}, 10)
    assert r.actions() == []
    assert r.decisions[-1].code is Code.INSUFFICIENT_DATA


def test_candidate_without_enough_data_is_ignored(policy):
    r = Runner(policy)
    r.run({A: degraded(A), B: m(B, sent=5)}, 10)
    assert r.actions() == []


def test_candidate_that_is_bad_is_ignored(policy):
    r = Runner(policy)
    # B has lower score than a dead-slow A, but B itself is "bad" (loss >= 30 %)
    r.run({A: m(A, rtt=900), B: m(B, rtt=1, loss=35)}, 10)
    assert r.actions() == []


def test_dead_current_provider_switches(policy):
    r = Runner(policy)
    r.run({A: m(A, loss=100), B: good(B)}, 3)
    assert r.actions() == [(Action.ANNOUNCE, B)]


# --- return to base --------------------------------------------------------


def test_return_to_base_when_base_recovers(policy):
    r = Runner(policy, start=1000.0)
    r.state = PrefixState(PREFIX, active=B, changed_at=0.0)
    d1, d2, d3 = r.run({A: good(A, 1.5), B: good(B, 1.0)}, 3)
    assert d1.code is d2.code is Code.PENDING
    assert d3.action is Action.WITHDRAW and d3.target == A
    assert r.state.active is None


def test_return_band_prevents_flapping(policy):
    """B stays while it is still better than A by >= return_ratio * threshold."""
    r = Runner(policy)
    r.state = PrefixState(PREFIX, active=B, changed_at=0.0)
    r.run({A: good(A, 16.0), B: good(B, 1.0)}, 50)  # 15 ms advantage, band is 10 ms
    assert r.actions() == []
    r.run({A: good(A, 9.0), B: good(B, 1.0)}, 3)  # 8 ms < 10 ms -> back to base
    assert r.actions() == [(Action.WITHDRAW, A)]


def test_switch_between_non_base_providers():
    policy = Policy(base_provider=A, confirm_rounds=2, hold_down_s=0)
    r = Runner(policy)
    r.state = PrefixState(PREFIX, active=B, changed_at=0.0)
    # base A still degraded, C much better than B
    r.run({A: degraded(A), B: good(B, 80), C: good(C, 2)}, 2)
    assert r.actions() == [(Action.ANNOUNCE, C)]


def test_override_dies_returns_to_base(policy):
    r = Runner(policy)
    r.state = PrefixState(PREFIX, active=B, changed_at=0.0)
    r.run({A: good(A), B: m(B, loss=100)}, 3)
    assert r.actions() == [(Action.WITHDRAW, A)]


# --- end-to-end style scenario ----------------------------------------------


def test_degrade_restore_scenario_has_exactly_one_return(policy):
    """degrade -> one switch; restore -> exactly one return, no flapping."""
    rng = random.Random(7)

    def noisy_good(p):
        return m(p, rtt=0.5 + rng.uniform(0, 1.5), jitter=rng.uniform(0, 1))

    def noisy_bad(p):
        return m(p, rtt=300 + rng.uniform(0, 5), loss=rng.choice([3, 5, 7, 10]), jitter=rng.uniform(0, 2))

    r = Runner(policy)
    r.run({A: noisy_good(A), B: noisy_good(B)}, 10)
    for _ in range(15):
        r.step({A: noisy_bad(A), B: noisy_good(B)})
    for _ in range(100):
        r.step({A: noisy_good(A), B: noisy_good(B)})

    assert r.actions() == [(Action.ANNOUNCE, B), (Action.WITHDRAW, A)]
    assert r.changes[1] - r.changes[0] >= policy.hold_down_s


# --- components in isolation --------------------------------------------------


def test_selector_reports_reason(policy, scorer, criterion):
    classifier = ProviderClassifier(policy.min_samples, policy.bad_loss_pct, policy.bad_rtt_ms)
    selector = PathSelector(A, scorer, classifier, criterion, policy.return_ratio)
    proposal = selector.select(A, {A: degraded(A), B: good(B)})
    assert proposal.wanted == B and proposal.code is Code.BETTER_PATH
    assert "isp-b better than isp-a" in proposal.reason


def test_guards_individually():
    ctx = GuardContext(PrefixState(PREFIX, pending=B, pending_rounds=2, changed_at=100.0), now=130.0,
                       recent_changes=[100.0, 125.0])
    assert ConfirmationGuard(3).check(ctx) == Block(Code.PENDING, "(round 2/3)")
    assert ConfirmationGuard(2).check(ctx) is None
    assert HoldDownGuard(60).check(ctx) == Block(Code.HOLD_DOWN, "(hold-down, 30s left)")
    assert HoldDownGuard(30).check(ctx) is None
    assert RateLimitGuard(2).check(ctx) == Block(Code.RATE_LIMIT, "(rate limit 2/min reached)")
    assert RateLimitGuard(3).check(ctx) is None


def test_engine_is_open_for_new_guards(policy):
    """Open/closed: a new rule is a new guard class; the engine is not modified."""

    class MaintenanceWindowGuard:
        def __init__(self, start, end):
            self.start, self.end = start, end

        def check(self, ctx):
            if self.start <= ctx.now < self.end:
                return Block(Code.HOLD_DOWN, "(maintenance window)")
            return None

    engine = DecisionEngine.from_policy(policy, extra_guards=[MaintenanceWindowGuard(1000.0, 1100.0)])
    r = Runner(policy, engine=engine)
    r.run({A: degraded(A), B: good(B)}, 40)  # 1000..1078: frozen
    assert r.actions() == []
    assert "(maintenance window)" in r.decisions[-1].reason
    r.run({A: degraded(A), B: good(B)}, 15)  # past 1100
    assert r.actions() == [(Action.ANNOUNCE, B)]


def test_decide_is_pure(policy):
    engine = DecisionEngine.from_policy(policy)
    state = PrefixState(PREFIX, pending=B, pending_rounds=1)
    metrics = {A: degraded(A), B: good(B)}
    first = engine.decide(state, metrics, 500.0, [400.0])
    second = engine.decide(state, metrics, 500.0, [400.0])
    assert first == second
    assert state == PrefixState(PREFIX, pending=B, pending_rounds=1)  # input untouched
