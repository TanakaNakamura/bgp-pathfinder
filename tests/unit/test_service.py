"""Application layer: optimizer service, mode controller, event bus and listeners.

The service is wired through the real composition root (``build_service``)
with a fake metrics source, a temp SQLite store and an in-memory ExaBGP pipe.
"""

import json
import logging

from optimizer.app.events import EventBus, RoundCompleted, RouteChanged, Started
from optimizer.app.mode import ModeController
from optimizer.bgp import Ack, DryRunInjector, ExaBgpCommands, NeighborState, Shutdown, Unparsed
from optimizer.observability.log_listener import JsonLogListener
from optimizer.storage import SqliteRecorder, SqliteStore
from tests.unit.helpers import A_DEGRADED, GOOD, PREFIX, RecordingListener, run


def test_live_mode_announces_and_withdraws(setup):
    svc, out, store, prober = setup(dry_run=False)
    t = run(svc, 100.0, 3)
    assert out.getvalue() == ""

    prober.current = A_DEGRADED
    t = run(svc, t, 3)
    assert out.getvalue().splitlines() == [
        "announce route 198.51.100.0/24 next-hop 10.2.0.2 local-preference 300 community [no-export no-advertise]"
    ]
    assert store.overrides()[0]["provider"] == "isp-b"

    prober.current = GOOD
    run(svc, t, 40)
    assert out.getvalue().splitlines()[-1] == "withdraw route 198.51.100.0/24"
    assert store.overrides() == []

    hist = store.decisions()
    assert [(r["action"], r["from_provider"], r["to_provider"]) for r in hist] == [
        ("announce", "isp-a", "isp-b"),
        ("withdraw", "isp-b", "isp-a"),
    ]
    assert hist[1]["ts"] - hist[0]["ts"] >= 60
    assert hist[0]["metrics"]["isp-a"]["rtt_ms"] == 301
    assert not hist[0]["dry_run"]
    assert store.heartbeat() is not None and store.current_mode() == "live"


def test_dry_run_only_records(setup):
    svc, out, store, prober = setup(dry_run=True)
    prober.current = A_DEGRADED
    run(svc, 100.0, 5)
    assert out.getvalue() == ""  # nothing reaches ExaBGP
    (row,) = store.decisions()
    assert row["dry_run"] and row["action"] == "announce" and row["to_provider"] == "isp-b"
    assert row["command"].startswith("announce route 198.51.100.0/24 next-hop 10.2.0.2")
    assert store.overrides()[0]["dry_run"] == 1


def test_switch_live_to_dry_run_withdraws(setup):
    svc, out, store, prober = setup(dry_run=False)
    prober.current = A_DEGRADED
    t = run(svc, 100.0, 3)
    assert svc.states[PREFIX].active == "isp-b"

    store.request_mode("dry-run")
    svc.poll_mode(t)
    assert out.getvalue().splitlines()[-1] == "withdraw route 198.51.100.0/24"
    assert svc.dry_run and store.current_mode() == "dry-run"
    assert store.pending_mode_request() is None
    assert svc.states[PREFIX].active is None and store.overrides() == []


def test_switch_dry_run_to_live_starts_fresh(setup):
    svc, out, store, prober = setup(dry_run=True)
    prober.current = A_DEGRADED
    t = run(svc, 100.0, 5)
    store.request_mode("live")
    svc.poll_mode(t)
    assert not svc.dry_run and out.getvalue() == ""
    run(svc, t, 3)  # needs confirm_rounds again, then a real announce
    assert out.getvalue().startswith("announce route 198.51.100.0/24 next-hop 10.2.0.2")


def test_exabgp_messages(setup):
    svc, out, store, _ = setup(dry_run=False)
    assert svc.on_exabgp_message(NeighborState("10.9.0.1", "up"), 5.0) is False
    assert store.bgp_state()["state"] == "up"
    assert svc.on_exabgp_message(Ack(True), 6.0) is False
    assert svc.on_exabgp_message(Unparsed("garbage"), 7.0) is False
    assert svc.on_exabgp_message(Shutdown(), 8.0) is True


def test_service_publishes_events_in_order(setup):
    rec = RecordingListener()
    svc, _, _, prober = setup(dry_run=False, extra_listeners=[rec])
    prober.current = A_DEGRADED
    run(svc, 100.0, 3)
    kinds = [type(e).__name__ for e in rec.events]
    assert kinds[0] == "Started"
    # 3 rounds: (PrefixEvaluated, RoundCompleted) x 2, then RouteChanged before PrefixEvaluated
    assert kinds[1:] == [
        "PrefixEvaluated", "RoundCompleted",
        "PrefixEvaluated", "RoundCompleted",
        "RouteChanged", "PrefixEvaluated", "RoundCompleted",
    ]
    change = next(e for e in rec.events if isinstance(e, RouteChanged))
    assert change.next_hop == "10.2.0.2" and not change.dry_run
    assert isinstance(rec.events[0], Started) and rec.events[0].providers == ("isp-a", "isp-b")


def test_log_listener_keeps_the_event_names_e2e_relies_on(setup, caplog):
    with caplog.at_level(logging.INFO, logger="optimizer"):
        svc, _, _, prober = setup(dry_run=True)
        prober.current = A_DEGRADED
        run(svc, 100.0, 3)
        prober.current = GOOD
        run(svc, 106.0, 10)
    events = [r.getMessage() for r in caplog.records]
    assert "optimizer started" in events
    would = next(r for r in caplog.records if r.getMessage() == "dry-run: would announce")
    assert would.to == "isp-b" and would.dry_run is True and would.command.startswith("announce route")
    holds = [r for r in caplog.records if r.getMessage() == "decision: no change"]
    assert any(r.code == "hold-down" and r.wanted == "isp-a" for r in holds)


def test_event_bus_delivers_to_every_listener_in_order():
    a, b = RecordingListener(), RecordingListener()
    bus = EventBus([a])
    bus.subscribe(b)
    ev = RoundCompleted(1.0, 1)
    bus.publish(ev)
    assert a.events == [ev] and b.events == [ev]


def test_listeners_ignore_events_they_do_not_handle(tmp_path):
    store = SqliteStore(str(tmp_path / "x.db"))
    SqliteRecorder(store).handle(object())
    JsonLogListener().handle("not an event")


class FakeRequests:
    def __init__(self, *requests):
        self.requests = list(requests)

    def take_mode_request(self):
        return self.requests.pop(0) if self.requests else None


def test_mode_controller():
    live, dry = object(), DryRunInjector(ExaBgpCommands())
    mc = ModeController(FakeRequests(None, "bogus", "live", "dry-run", "dry-run"), live=live, dry=dry, dry_run=False)
    assert mc.injector is live
    assert mc.poll() is None  # nothing requested
    assert mc.poll() is None  # unknown request ignored
    assert mc.poll() is None  # already live
    change = mc.poll()
    assert (change.old_dry_run, change.new_dry_run) == (False, True)
    assert mc.injector is dry and mc.live_injector is live
    assert mc.poll() is None  # already dry-run


def test_mode_changed_event_is_persisted(setup):
    svc, _, store, _ = setup(dry_run=False)
    store.request_mode("dry-run")
    svc.poll_mode(10.0)
    events = store.events()
    assert events[-1]["kind"] == "mode" and events[-1]["detail"] == {"old": "live", "new": "dry-run"}
    json.dumps(events)  # must stay serialisable for `optimizer history --json`
