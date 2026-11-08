"""Probing: ping output parsing, window, metrics, prober orchestration."""

import asyncio

import pytest

from optimizer.domain.models import Sample
from optimizer.probing import MetricsCalculator, PingOutputParser, ProbePath, Prober, SampleWindow

PING_OK = """\
PING 198.51.100.10 (198.51.100.10) from 10.99.1.1 : 56(84) bytes of data.
64 bytes from 198.51.100.10: icmp_seq=1 ttl=61 time=0.216 ms
64 bytes from 198.51.100.10: icmp_seq=2 ttl=61 time=0.240 ms
64 bytes from 198.51.100.10: icmp_seq=3 ttl=61 time=0.408 ms

--- 198.51.100.10 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 411ms
rtt min/avg/max/mdev = 0.216/0.288/0.408/0.085 ms
"""

PING_LOSSY = """\
PING 198.51.100.10 (198.51.100.10) from 10.99.1.1 : 56(84) bytes of data.
64 bytes from 198.51.100.10: icmp_seq=1 ttl=61 time=300.5 ms
64 bytes from 198.51.100.10: icmp_seq=3 ttl=61 time=301.0 ms
64 bytes from 198.51.100.10: icmp_seq=3 ttl=61 time=305.0 ms (DUP!)
64 bytes from 198.51.100.10: icmp_seq=5 ttl=61 time=302 ms

--- 198.51.100.10 ping statistics ---
5 packets transmitted, 3 received, +1 duplicates, 40% packet loss, time 1000ms
"""

parser = PingOutputParser()
calc = MetricsCalculator()


def test_parse_ping_all_received():
    assert parser.parse(PING_OK, 3) == [0.216, 0.240, 0.408]


def test_parse_ping_lost_and_duplicates():
    assert parser.parse(PING_LOSSY, 5) == [300.5, None, 301.0, None, 302.0]


def test_parse_ping_nothing_received():
    assert parser.parse("PING x\n\n--- stats ---\n3 packets transmitted, 0 received", 3) == [None] * 3


def test_parse_ping_ignores_out_of_range_seq():
    assert parser.parse("64 bytes from x: icmp_seq=9 ttl=61 time=1.0 ms\n", 3) == [None, None, None]


def s(rtt, method="icmp"):
    return Sample(0.0, method, rtt)


def test_metrics_loss_rtt_jitter():
    met = calc.calculate("isp-a", [s(10), s(12), s(None), s(14)])
    assert met.sent == 4 and met.received == 3
    assert met.loss_pct == pytest.approx(25.0)
    assert met.rtt_ms == pytest.approx(12.0)
    assert met.jitter_ms == pytest.approx(2.0)  # |12-10|, |14-12|


def test_metrics_jitter_is_per_method():
    # ICMP steady at 1 ms, TCP steady at 3 ms: interleaving them must not create jitter
    met = calc.calculate("isp-a", [s(1.0), s(3.0, "tcp"), s(1.0), s(3.0, "tcp"), s(1.0)])
    assert met.jitter_ms == pytest.approx(0.0)
    assert met.rtt_ms == pytest.approx(1.8)


def test_metrics_empty_and_all_lost():
    empty = calc.calculate("isp-a", [])
    assert empty.sent == 0 and empty.rtt_ms is None and empty.loss_pct == 0.0
    lost = calc.calculate("isp-a", [s(None)] * 4)
    assert lost.rtt_ms is None and lost.loss_pct == 100.0 and lost.jitter_ms is None


def test_window_keeps_last_rounds():
    w = SampleWindow(rounds=2)
    w.add_round([s(1)])
    w.add_round([s(2), s(3)])
    w.add_round([s(4)])
    assert [x.rtt_ms for x in w.samples()] == [2, 3, 4]
    assert len(w) == 2
    w.clear()
    assert w.samples() == []


class FakeProbe:
    """Any object with .method and async .measure() is a Probe (no inheritance needed)."""

    def __init__(self, method, rtts_by_source):
        self.method = method
        self._rtts = rtts_by_source
        self.calls = []

    async def measure(self, source, target):
        self.calls.append((source, target))
        return list(self._rtts[source])


def test_prober_runs_every_probe_over_every_path_and_windows_results():
    icmp = FakeProbe("icmp", {"10.99.1.1": [1.0, 3.0], "10.99.2.1": [300.0, None]})
    tcp = FakeProbe("tcp", {"10.99.1.1": [2.0], "10.99.2.1": [None]})
    paths = [
        ProbePath("198.51.100.0/24", "isp-a", "10.99.1.1", ("198.51.100.10",)),
        ProbePath("198.51.100.0/24", "isp-b", "10.99.2.1", ("198.51.100.10",)),
    ]
    prober = Prober(paths, [icmp, tcp], window_rounds=2, clock=lambda: 42.0)

    results = asyncio.run(prober.run_round())
    assert [(r.provider, [x.method for x in r.samples]) for r in results] == [
        ("isp-a", ["icmp", "icmp", "tcp"]),
        ("isp-b", ["icmp", "icmp", "tcp"]),
    ]
    assert all(x.ts == 42.0 for r in results for x in r.samples)
    assert icmp.calls == [("10.99.1.1", "198.51.100.10"), ("10.99.2.1", "198.51.100.10")]

    metrics = prober.metrics("198.51.100.0/24")
    assert list(metrics) == ["isp-a", "isp-b"]
    assert metrics["isp-a"].rtt_ms == pytest.approx(2.0) and metrics["isp-a"].loss_pct == 0
    assert metrics["isp-b"].loss_pct == pytest.approx(200 / 3)
    assert prober.metrics("192.0.2.0/24") == {}
