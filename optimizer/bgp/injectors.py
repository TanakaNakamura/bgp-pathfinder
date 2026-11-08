"""Route injectors: interchangeable implementations of ``ports.RouteInjector``.

Both return the command they used, so callers never need to know which mode
is active (Liskov): the live one writes it to the ExaBGP pipe, the dry-run one
only hands it back for logging and history.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TextIO

from optimizer.bgp.commands import ExaBgpCommands

log = logging.getLogger("injector")


class ExaBgpInjector:
    """Writes commands to the ExaBGP pipe (stdout) and matches the "done"/"error" acks."""

    def __init__(self, out: TextIO, commands: ExaBgpCommands) -> None:
        self._out = out
        self._commands = commands
        self._inflight: deque[str] = deque()
        self.errors = 0

    @property
    def dry_run(self) -> bool:
        return False

    def announce(self, prefix: str, next_hop: str) -> str:
        return self._send(self._commands.announce(prefix, next_hop))

    def withdraw(self, prefix: str) -> str:
        return self._send(self._commands.withdraw(prefix))

    def on_ack(self, ok: bool) -> None:
        cmd = self._inflight.popleft() if self._inflight else None
        if ok:
            log.debug("exabgp ack", extra={"ack": "done", "command": cmd})
        else:
            self.errors += 1
            log.error("exabgp rejected command", extra={"ack": "error", "command": cmd})

    def _send(self, cmd: str) -> str:
        self._out.write(cmd + "\n")
        self._out.flush()
        self._inflight.append(cmd)
        log.info("exabgp command sent", extra={"command": cmd})
        return cmd


class DryRunInjector:
    """Announces nothing; returns the command it *would* have sent."""

    def __init__(self, commands: ExaBgpCommands) -> None:
        self._commands = commands

    @property
    def dry_run(self) -> bool:
        return True

    def announce(self, prefix: str, next_hop: str) -> str:
        return self._commands.announce(prefix, next_hop)

    def withdraw(self, prefix: str) -> str:
        return self._commands.withdraw(prefix)
