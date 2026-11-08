"""Abstractions (ports) the application depends on.

Concrete adapters live in ``probing``, ``bgp``, ``storage`` and
``observability``; they are wired together only in ``app.bootstrap``
(the composition root). Each protocol is deliberately small, so every
consumer depends on exactly what it uses.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from optimizer.domain.models import ProviderMetrics

# --- inputs of the optimizer service ------------------------------------------


class MetricsSource(Protocol):
    """Current window metrics of every provider for a prefix."""

    def metrics(self, prefix: str) -> Mapping[str, ProviderMetrics]: ...


class RoundRunner(Protocol):
    """Runs one probing round and returns its raw results."""

    async def run_round(self) -> Sequence[Any]: ...


class ModeRequestSource(Protocol):
    """Pending runtime mode request ("dry-run" | "live"), consumed when read."""

    def take_mode_request(self) -> str | None: ...


# --- outputs of the optimizer service -----------------------------------------


class RouteInjector(Protocol):
    """Puts routes into (or takes them out of) the network. Returns the BGP command it used."""

    @property
    def dry_run(self) -> bool: ...

    def announce(self, prefix: str, next_hop: str) -> str: ...

    def withdraw(self, prefix: str) -> str: ...


class AckSink(Protocol):
    """Receives ExaBGP acknowledgements for previously sent commands."""

    def on_ack(self, ok: bool) -> None: ...


class EventListener(Protocol):
    """Reacts to application events (persistence, logs, metrics, ...)."""

    def handle(self, event: object) -> None: ...


class EventPublisher(Protocol):
    def publish(self, event: object) -> None: ...


# --- read side, used by the CLI -----------------------------------------------


class StatusQuery(Protocol):
    def current_mode(self) -> str | None: ...

    def bgp_state(self) -> dict | None: ...

    def heartbeat(self) -> float | None: ...

    def provider_status(self) -> list[dict]: ...

    def overrides(self) -> list[dict]: ...

    def prefix_states(self) -> list[dict]: ...


class HistoryQuery(Protocol):
    def decisions(self, limit: int = 50, prefix: str | None = None) -> list[dict]: ...

    def events(self, limit: int = 50) -> list[dict]: ...


class ModeControl(Protocol):
    """CLI side of runtime mode switching."""

    def current_mode(self) -> str | None: ...

    def request_mode(self, mode: str) -> None: ...

    def pending_mode_request(self) -> str | None: ...
