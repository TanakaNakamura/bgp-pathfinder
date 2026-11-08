"""Command line interface: ``optimizer status|history|mode|probe``.

Each subcommand is a ``Command`` object (command pattern). Commands depend on
narrow read-side ports (``StatusQuery``, ``HistoryQuery``, ``ModeControl``)
and receive the store opener and output streams from the ``Cli``, so they can
be tested without a database or a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable, ClassVar, Sequence, TextIO

from optimizer import config as config_mod
from optimizer.config import Config
from optimizer.storage.sqlite import SqliteStore

DEFAULT_CONFIG = os.environ.get("OPTIMIZER_CONFIG", "/etc/optimizer/config.yaml")

StoreOpener = Callable[[str, bool], SqliteStore]  # (db_path, readonly) -> store


def _fmt(value, spec: str = ".1f", none: str = "-") -> str:
    return none if value is None else format(value, spec)


def _ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ago(ts: float | None) -> str:
    return "-" if ts is None else f"{time.time() - ts:.0f}s ago"


class Command(ABC):
    name: ClassVar[str]
    help: ClassVar[str]

    def register(self, sub: argparse._SubParsersAction) -> None:
        parser = sub.add_parser(self.name, help=self.help)
        self.configure(parser)
        parser.set_defaults(handler=self)

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add command-specific arguments."""

    @abstractmethod
    def run(self, cfg: Config, args: argparse.Namespace, out: TextIO, err: TextIO) -> int: ...


class StoreCommand(Command, ABC):
    """A command that reads the optimizer's SQLite database."""

    def __init__(self, opener: StoreOpener = SqliteStore) -> None:
        self._opener = opener

    def _open_readonly(self, cfg: Config, err: TextIO) -> SqliteStore | None:
        try:
            return self._opener(cfg.db_path, True)
        except FileNotFoundError as exc:
            print(f"optimizer: {exc}", file=err)
            return None


class StatusCommand(StoreCommand):
    name = "status"
    help = "current metrics per provider and active overrides"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--json", action="store_true")

    def run(self, cfg: Config, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
        store = self._open_readonly(cfg, err)
        if store is None:
            return 1
        try:
            heartbeat = store.heartbeat()
            doc = {
                "mode": store.current_mode(),
                "bgp": store.bgp_state(),
                "heartbeat": heartbeat,
                "heartbeat_age_s": None if heartbeat is None else round(time.time() - heartbeat, 1),
                "base_provider": cfg.policy.base_provider,
                "providers": store.provider_status(),
                "overrides": store.overrides(),
                "engine": store.prefix_states(),
            }
        finally:
            store.close()
        if args.json:
            print(json.dumps(doc, indent=2), file=out)
        else:
            self._render(doc, cfg.policy.base_provider, out)
        return 0

    @staticmethod
    def _render(doc: dict, base: str, out: TextIO) -> None:
        bgp = doc["bgp"] or {}
        print(
            f"mode: {doc['mode'] or '-'} | bgp {bgp.get('peer', '-')}: {bgp.get('state', 'unknown')}"
            f" ({_ago(bgp.get('since'))}) | heartbeat {_ago(doc['heartbeat'])}",
            file=out,
        )
        active = {o["prefix"]: o["provider"] for o in doc["overrides"]}
        print(file=out)
        print(
            f"{'PREFIX':<18} {'PROVIDER':<8} {'RTT ms':>8} {'JITTER':>7} {'LOSS %':>7} {'SENT':>5} {'SCORE':>8}  ROLE",
            file=out,
        )
        for r in doc["providers"]:
            role = []
            if r["provider"] == base:
                role.append("base")
            if active.get(r["prefix"]) == r["provider"]:
                role.append("ACTIVE override")
            elif r["prefix"] not in active and r["provider"] == base:
                role.append("in use (plain BGP)")
            print(
                f"{r['prefix']:<18} {r['provider']:<8} {_fmt(r['rtt_ms']):>8} {_fmt(r['jitter_ms']):>7}"
                f" {r['loss_pct']:>7.1f} {r['sent']:>5} {_fmt(r['score']):>8}  {', '.join(role)}",
                file=out,
            )
        print(file=out)
        print("overrides:" + ("" if doc["overrides"] else " none (plain BGP)"), file=out)
        for o in doc["overrides"]:
            flag = "dry-run, NOT announced" if o["dry_run"] else "announced"
            print(
                f"  {o['prefix']} -> {o['provider']} via {o['next_hop']} since {_ts(o['since'])}"
                f" ({_ago(o['since'])}), {flag}",
                file=out,
            )
        print("engine:", file=out)
        for e in doc["engine"]:
            print(f"  {e['prefix']}: {e['code']} - {e['reason']}", file=out)


class HistoryCommand(StoreCommand):
    name = "history"
    help = "route decisions (announce / withdraw)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-n", "--limit", type=int, default=50)
        parser.add_argument("-p", "--prefix")
        parser.add_argument("-e", "--events", action="store_true", help="also show daemon/BGP/mode events")
        parser.add_argument("--json", action="store_true")

    def run(self, cfg: Config, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
        store = self._open_readonly(cfg, err)
        if store is None:
            return 1
        try:
            rows = store.decisions(limit=args.limit, prefix=args.prefix)
            events = store.events(limit=args.limit) if args.events else []
        finally:
            store.close()
        if args.json:
            print(json.dumps({"decisions": rows, "events": events}, indent=2), file=out)
            return 0

        print(f"{'TIME (UTC)':<19}  {'PREFIX':<16} {'ACTION':<8} {'FROM -> TO':<16} {'MODE':<7} REASON", file=out)
        for r in rows:
            mode = "dry-run" if r["dry_run"] else "live"
            print(
                f"{_ts(r['ts']):<19}  {r['prefix']:<16} {r['action']:<8} "
                f"{r['from_provider'] + ' -> ' + r['to_provider']:<16} {mode:<7} {r['reason']}",
                file=out,
            )
        if not rows:
            print("(no route changes yet)", file=out)
        if events:
            print(file=out)
            print(f"{'TIME (UTC)':<19}  {'EVENT':<6} DETAIL", file=out)
            for e in events:
                print(f"{_ts(e['ts']):<19}  {e['kind']:<6} {json.dumps(e['detail'])}", file=out)
        return 0


class ModeCommand(StoreCommand):
    name = "mode"
    help = "show or switch the runtime mode (until daemon restart)"

    def __init__(
        self,
        opener: StoreOpener = SqliteStore,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(opener)
        self._sleep = sleep
        self._clock = clock

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("mode", nargs="?", choices=["dry-run", "live"])
        parser.add_argument("--wait", type=float, default=15.0, help="seconds to wait for the daemon")

    def run(self, cfg: Config, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
        if args.mode is None:
            store = self._open_readonly(cfg, err)
            if store is None:
                return 1
            try:
                print(store.current_mode() or "unknown", file=out)
            finally:
                store.close()
            return 0

        store = self._opener(cfg.db_path, False)
        try:
            store.request_mode(args.mode)
            deadline = self._clock() + args.wait
            while self._clock() < deadline:
                if store.pending_mode_request() is None and store.current_mode() == args.mode:
                    print(f"mode: {args.mode}", file=out)
                    return 0
                self._sleep(0.5)
        finally:
            store.close()
        print(f"optimizer: daemon did not confirm mode {args.mode} within {args.wait}s", file=err)
        return 1


class ProbeCommand(Command):
    name = "probe"
    help = "run probing rounds standalone and print metrics"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-n", "--rounds", type=int, default=5)
        parser.add_argument("--json", action="store_true")

    def run(self, cfg: Config, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
        from optimizer.app.bootstrap import build_prober

        prober = build_prober(cfg)

        async def rounds() -> None:
            for _ in range(args.rounds):
                await prober.run_round()
                await asyncio.sleep(cfg.probe.interval_s)

        asyncio.run(rounds())
        rows = [
            {"prefix": p.prefix, **m.to_dict()} for p in cfg.prefixes for m in prober.metrics(p.prefix).values()
        ]
        if args.json:
            print(json.dumps(rows, indent=2), file=out)
            return 0
        print(f"{'PREFIX':<18} {'PROVIDER':<8} {'SENT':>5} {'RCVD':>5} {'RTT ms':>8} {'JITTER':>8} {'LOSS %':>7}", file=out)
        for r in rows:
            print(
                f"{r['prefix']:<18} {r['provider']:<8} {r['sent']:>5} {r['received']:>5} "
                f"{_fmt(r['rtt_ms']):>8} {_fmt(r['jitter_ms']):>8} {r['loss_pct']:>7.1f}",
                file=out,
            )
        return 0


class Cli:
    def __init__(
        self,
        commands: Sequence[Command],
        out: TextIO | None = None,
        err: TextIO | None = None,
        config_loader: Callable[[str], Config] = config_mod.load,
    ) -> None:
        self._commands = tuple(commands)
        self._out = out or sys.stdout
        self._err = err or sys.stderr
        self._load = config_loader

    def build_parser(self) -> argparse.ArgumentParser:
        ap = argparse.ArgumentParser(prog="optimizer", description="BGP optimizer prototype")
        ap.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to config.yaml")
        sub = ap.add_subparsers(dest="command", required=True)
        for command in self._commands:
            command.register(sub)
        return ap

    def run(self, argv: Sequence[str] | None = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            cfg = self._load(args.config)
        except (OSError, config_mod.ConfigError) as exc:
            print(f"optimizer: config error: {exc}", file=self._err)
            return 2
        return args.handler.run(cfg, args, self._out, self._err)


def default_commands() -> list[Command]:
    return [StatusCommand(), HistoryCommand(), ModeCommand(), ProbeCommand()]


def main(argv: list[str] | None = None) -> int:
    return Cli(default_commands()).run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
