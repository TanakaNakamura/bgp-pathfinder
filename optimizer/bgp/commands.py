"""ExaBGP 4.x API command syntax.

Verified against exabgp==4.2.25 (each command is acknowledged with "done",
an unknown one with "error"):

    announce route 198.51.100.0/24 next-hop 10.2.0.2 local-preference 300 community [no-export no-advertise]
    withdraw route 198.51.100.0/24

Re-announcing a prefix with another next-hop replaces the previous route
(implicit withdraw), so switching between two non-base providers is a single
announce.
"""

from __future__ import annotations

from typing import Sequence


class ExaBgpCommands:
    def __init__(
        self,
        local_preference: int = 300,
        communities: Sequence[str] = ("no-export", "no-advertise"),
        neighbor: str | None = None,
    ) -> None:
        self._local_preference = local_preference
        self._communities = tuple(communities)
        self._neighbor = neighbor

    def announce(self, prefix: str, next_hop: str) -> str:
        cmd = f"announce route {prefix} next-hop {next_hop} local-preference {self._local_preference}"
        if self._communities:
            cmd += f" community [{' '.join(self._communities)}]"
        return self._scoped(cmd)

    def withdraw(self, prefix: str) -> str:
        return self._scoped(f"withdraw route {prefix}")

    def _scoped(self, cmd: str) -> str:
        return f"neighbor {self._neighbor} {cmd}" if self._neighbor else cmd
