"""Runtime mode (dry-run / live): which injector is active, and switching between them."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from optimizer.ports import ModeRequestSource, RouteInjector

log = logging.getLogger("optimizer")

MODES = ("dry-run", "live")


@dataclass(frozen=True)
class ModeChange:
    old_dry_run: bool
    new_dry_run: bool


class ModeController:
    def __init__(
        self,
        requests: ModeRequestSource,
        live: RouteInjector,
        dry: RouteInjector,
        dry_run: bool,
    ) -> None:
        self._requests = requests
        self._live = live
        self._dry = dry
        self._dry_run = dry_run

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def injector(self) -> RouteInjector:
        """The injector of the current mode."""
        return self._dry if self._dry_run else self._live

    @property
    def live_injector(self) -> RouteInjector:
        """Always the real one: needed to take routes back when leaving live mode."""
        return self._live

    def poll(self) -> ModeChange | None:
        """Consume a pending request; return the change if the mode actually changed."""
        req = self._requests.take_mode_request()
        if req is None:
            return None
        if req not in MODES:
            log.warning("ignoring unknown mode request", extra={"request": req})
            return None
        want_dry = req == "dry-run"
        if want_dry == self._dry_run:
            return None
        change = ModeChange(self._dry_run, want_dry)
        self._dry_run = want_dry
        return change
