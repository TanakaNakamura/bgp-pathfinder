"""Asyncio runner: drives probing rounds and feeds ExaBGP messages to the service.

    stdin  <- ExaBGP: JSON events (neighbor state changes) and "done"/"error" acks
    stdout -> ExaBGP: announce / withdraw commands (written by ExaBgpInjector only)
    stderr -> structured JSON logs

Any unhandled exception terminates the process; the wrapper
(docker/optimizer/run-optimizer.sh) then terminates ExaBGP, the iBGP session
drops and edge falls back to plain BGP. Failing closed is intentional.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Callable, TextIO

from optimizer.app.service import OptimizerService
from optimizer.bgp.messages import ExaBgpMessageParser
from optimizer.ports import RoundRunner

log = logging.getLogger("optimizer")


class Daemon:
    def __init__(
        self,
        service: OptimizerService,
        prober: RoundRunner,
        parser: ExaBgpMessageParser,
        interval_s: float,
        stdin: TextIO | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._service = service
        self._prober = prober
        self._parser = parser
        self._interval_s = interval_s
        self._stdin = stdin or sys.stdin
        self._clock = clock

    async def run(self) -> int:
        self._service.start(self._clock(), pid=os.getpid())
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        reader = asyncio.create_task(self._read_exabgp(stop))

        reason = "stop requested"
        try:
            while not stop.is_set():
                started = time.monotonic()
                self._service.poll_mode(self._clock())
                results = await self._prober.run_round()
                self._service.process_round(self._clock(), results)
                await self._sleep(stop, self._interval_s - (time.monotonic() - started))
        except Exception as exc:
            reason = f"crash: {exc!r}"
            log.critical("optimizer crashed", exc_info=True)
            raise
        finally:
            reader.cancel()
            self._service.stop(self._clock(), reason)
        return 0

    @staticmethod
    async def _sleep(stop: asyncio.Event, delay: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, delay))
        except TimeoutError:
            pass

    async def _read_exabgp(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), self._stdin)
        while True:
            raw = await reader.readline()
            if not raw:
                log.warning("exabgp closed the pipe")
                break
            msg = self._parser.parse(raw.decode(errors="replace"))
            if msg is not None and self._service.on_exabgp_message(msg, self._clock()):
                log.info("exabgp is shutting down")
                break
        stop.set()
