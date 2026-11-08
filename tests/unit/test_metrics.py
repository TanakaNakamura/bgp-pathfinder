"""Prometheus exporter (an event listener): what the Grafana dashboard reads."""

import math
from dataclasses import replace

from prometheus_client import CollectorRegistry

from optimizer.app.events import BgpStateChanged
from optimizer.observability import PrometheusExporter
from tests.unit.helpers import A_DEGRADED, GOOD, m, run

PREFIX = "198.51.100.0/24"


def make(setup, dry_run=False):
    exporter = PrometheusExporter(["isp-a", "isp-b"], [PREFIX], "isp-a", registry=CollectorRegistry())
    svc, _, store, prober = setup(dry_run=dry_run, extra_listeners=[exporter])
    return svc, exporter, prober, store


def val(exporter, name, **labels):
    return exporter.registry.get_sample_value(name, labels)


def test_metrics_follow_a_switch_and_return(setup):
    svc, ex, prober, _ = make(setup)
    t = run(svc, 100.0, 3)
    assert val(ex, "optimizer_rtt_ms", prefix=PREFIX, provider="isp-a") == 1.0
    assert val(ex, "optimizer_traffic_provider_index", prefix=PREFIX) == 1
    assert val(ex, "optimizer_override_active", prefix=PREFIX) == 0
    assert val(ex, "optimizer_engine_state", prefix=PREFIX) == 0  # steady
    assert val(ex, "optimizer_dry_run") == 0

    prober.current = A_DEGRADED
    svc.process_round(t)
    assert val(ex, "optimizer_engine_state", prefix=PREFIX) == 1  # pending
    t = run(svc, t + 2, 2)
    assert val(ex, "optimizer_rtt_ms", prefix=PREFIX, provider="isp-a") == 301
    assert val(ex, "optimizer_loss_percent", prefix=PREFIX, provider="isp-a") == 5
    assert val(ex, "optimizer_traffic_provider_index", prefix=PREFIX) == 2
    assert val(ex, "optimizer_override_active", prefix=PREFIX) == 1
    assert val(ex, "optimizer_provider_in_use", prefix=PREFIX, provider="isp-b") == 1
    assert val(ex, "optimizer_route_changes_total", prefix=PREFIX, action="announce", mode="live") == 1

    prober.current = GOOD
    run(svc, t, 3)
    assert val(ex, "optimizer_engine_state", prefix=PREFIX) == 2  # hold-down
    run(svc, t + 6, 40)
    assert val(ex, "optimizer_traffic_provider_index", prefix=PREFIX) == 1
    assert val(ex, "optimizer_route_changes_total", prefix=PREFIX, action="withdraw", mode="live") == 1


def test_counters_exist_before_first_change(setup):
    _, ex, _, _ = make(setup)
    for action in ("announce", "withdraw"):
        for mode in ("live", "dry-run"):
            assert val(ex, "optimizer_route_changes_total", prefix=PREFIX, action=action, mode=mode) == 0


def test_dry_run_and_bgp_state(setup):
    svc, ex, prober, store = make(setup, dry_run=True)
    assert val(ex, "optimizer_dry_run") == 1
    assert val(ex, "optimizer_bgp_session_up") == 0
    ex.handle(BgpStateChanged(1.0, "10.9.0.1", "up", None))
    assert val(ex, "optimizer_bgp_session_up") == 1

    prober.current = A_DEGRADED
    run(svc, 100.0, 3)
    assert val(ex, "optimizer_route_changes_total", prefix=PREFIX, action="announce", mode="dry-run") == 1
    assert val(ex, "optimizer_route_changes_total", prefix=PREFIX, action="announce", mode="live") == 0

    store.request_mode("live")
    svc.poll_mode(200.0)
    assert val(ex, "optimizer_dry_run") == 0


def test_unknown_values_are_nan(setup):
    svc, ex, prober, _ = make(setup)
    prober.current = {"isp-a": replace(m("isp-a", loss=100), rtt_ms=None, jitter_ms=None), "isp-b": m("isp-b")}
    svc.process_round(1.0)
    assert math.isnan(val(ex, "optimizer_rtt_ms", prefix=PREFIX, provider="isp-a"))
    assert math.isnan(val(ex, "optimizer_score", prefix=PREFIX, provider="isp-a"))
