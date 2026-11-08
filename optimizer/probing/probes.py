"""Probe methods. Each one measures a list of RTTs (None = lost) from a source to a target.

Each probe is sourced from the provider's probe address (10.99.x.1); edge
pins that source to one ISP with policy routing, so the measured path is the
provider's path regardless of the current BGP best path. Adding a method
(HTTP, DNS, ...) means adding a class with the same interface.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from typing import Protocol

log = logging.getLogger("prober")


class Probe(Protocol):
    method: str

    async def measure(self, source: str, target: str) -> list[float | None]:
        """RTT in ms of every attempt; None for a lost one."""
        ...


class PingOutputParser:
    """Per-sequence RTTs from iputils ping output.

    Duplicate replies (DUP!) and replies with an unexpected sequence number
    are ignored.
    """

    _REPLY_RE = re.compile(r"icmp_seq=(\d+)\b.*?\btime[=<]([\d.]+)\s*ms")

    def parse(self, text: str, count: int) -> list[float | None]:
        rtts: list[float | None] = [None] * count
        for line in text.splitlines():
            if "DUP!" in line:
                continue
            m = self._REPLY_RE.search(line)
            if not m:
                continue
            seq = int(m.group(1))
            if 1 <= seq <= count and rtts[seq - 1] is None:
                rtts[seq - 1] = float(m.group(2))
        return rtts


class IcmpProbe:
    """ICMP echo via iputils ``ping -I <source>``."""

    method = "icmp"

    def __init__(
        self,
        count: int = 5,
        interval_s: float = 0.2,
        timeout_s: float = 1.0,
        parser: PingOutputParser | None = None,
        binary: str = "ping",
    ) -> None:
        self._count = count
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._parser = parser or PingOutputParser()
        self._binary = binary

    def _command(self, source: str, target: str) -> list[str]:
        return [
            self._binary, "-n",
            "-c", str(self._count),
            "-i", str(self._interval_s),
            "-W", str(self._timeout_s),
            "-I", source,
            target,
        ]

    async def measure(self, source: str, target: str) -> list[float | None]:
        budget = self._count * self._interval_s + self._timeout_s + 2.0
        proc = await asyncio.create_subprocess_exec(
            *self._command(source, target), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=budget)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("ping timed out", extra={"source": source, "target": target})
            return [None] * self._count
        # rc 1 = some/all packets lost (normal); rc 2 = error (bad source, ...)
        if proc.returncode not in (0, 1):
            log.error(
                "ping failed",
                extra={"source": source, "target": target, "rc": proc.returncode,
                       "stderr": err.decode(errors="replace").strip()},
            )
        return self._parser.parse(out.decode(errors="replace"), self._count)


class TcpProbe:
    """TCP handshake RTT; a RST (connection refused) also proves the path works."""

    method = "tcp"

    def __init__(self, port: int = 80, timeout_s: float = 1.0) -> None:
        self._port = port
        self._timeout_s = timeout_s

    async def measure(self, source: str, target: str) -> list[float | None]:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.bind((source, 0))
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(loop.sock_connect(sock, (target, self._port)), timeout=self._timeout_s)
            except ConnectionRefusedError:
                pass
            return [(time.monotonic() - t0) * 1000.0]
        except (TimeoutError, OSError):
            return [None]
        finally:
            sock.close()
