"""Shared test doubles and helpers for the application-layer tests."""

from pathlib import Path

from optimizer.domain.models import ProviderMetrics

LAB_CONFIG = Path(__file__).resolve().parents[2] / "lab" / "configs" / "optimizer" / "config.yaml"
PREFIX = "198.51.100.0/24"


def m(provider, rtt=1.0, loss=0.0):
    return ProviderMetrics(provider, 30, round(30 * (1 - loss / 100)), rtt, 0.2, loss)


GOOD = {"isp-a": m("isp-a"), "isp-b": m("isp-b")}
A_DEGRADED = {"isp-a": m("isp-a", 301, 5), "isp-b": m("isp-b")}


class FakeProber:
    """Implements ports.MetricsSource."""

    def __init__(self):
        self.current = GOOD

    def metrics(self, prefix):
        return self.current


class RecordingListener:
    """Implements ports.EventListener; remembers every event."""

    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


def run(service, start, rounds, step=2.0):
    """Process ``rounds`` rounds starting at ``start``; returns the next timestamp."""
    t = start
    for _ in range(rounds):
        service.process_round(t)
        t += step
    return t
