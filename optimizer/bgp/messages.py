"""Messages ExaBGP writes to the helper's stdin, parsed into typed objects.

    done / error                                  -> Ack
    {"type": "state", "neighbor": {...}}          -> NeighborState
    {"type": "notification", "notification": "shutdown"} or "shutdown" -> Shutdown
    anything that is not JSON                     -> Unparsed
    other JSON (updates, keepalives, ...)         -> None (ignored)
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Ack:
    ok: bool


@dataclass(frozen=True)
class NeighborState:
    peer: str | None
    state: str | None
    reason: str | None = None


@dataclass(frozen=True)
class Shutdown:
    pass


@dataclass(frozen=True)
class Unparsed:
    line: str


ExaBgpMessage = Ack | NeighborState | Shutdown | Unparsed


class ExaBgpMessageParser:
    def parse(self, line: str) -> ExaBgpMessage | None:
        line = line.strip()
        if not line:
            return None
        if line in ("done", "error"):
            return Ack(ok=line == "done")
        if line == "shutdown":
            return Shutdown()
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return Unparsed(line)
        if not isinstance(msg, dict):
            return Unparsed(line)

        kind = msg.get("type")
        if kind == "state":
            nb = msg.get("neighbor", {})
            return NeighborState(nb.get("address", {}).get("peer"), nb.get("state"), nb.get("reason"))
        if kind == "notification" and msg.get("notification") == "shutdown":
            return Shutdown()
        return None
