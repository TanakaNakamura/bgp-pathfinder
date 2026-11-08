"""SQLite state and history.

Tables
  measurements     raw per-round probe results (per method), pruned after retention
  decisions        every route change (announce / withdraw), dry-run ones included
  events           daemon lifecycle, mode changes, BGP session changes
  provider_status  latest window metrics per (prefix, provider)  -> `optimizer status`
  prefix_state     latest decision-engine state per prefix          -> `optimizer status`
  overrides        routes currently injected (or simulated in dry-run)
  kv               small key/value settings (mode, mode_request, bgp state, heartbeat)

The daemon is the only writer (through ``SqliteRecorder``); the CLI reads
concurrently (WAL mode). ``SqliteStore`` implements the read-side ports
``StatusQuery``, ``HistoryQuery``, ``ModeControl`` and ``ModeRequestSource``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable, Mapping

from optimizer.domain.models import Decision, PrefixState, ProviderMetrics

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    prefix TEXT NOT NULL,
    provider TEXT NOT NULL,
    method TEXT NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    rtt_ms REAL,
    rtt_min_ms REAL,
    rtt_max_ms REAL,
    loss_pct REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS measurements_ts ON measurements(ts);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    prefix TEXT NOT NULL,
    action TEXT NOT NULL,
    from_provider TEXT NOT NULL,
    to_provider TEXT NOT NULL,
    next_hop TEXT,
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    metrics TEXT NOT NULL,
    command TEXT,
    dry_run INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_status (
    prefix TEXT NOT NULL,
    provider TEXT NOT NULL,
    ts REAL NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    rtt_ms REAL,
    jitter_ms REAL,
    loss_pct REAL NOT NULL,
    score REAL,
    PRIMARY KEY (prefix, provider)
);

CREATE TABLE IF NOT EXISTS prefix_state (
    prefix TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    active TEXT,
    changed_at REAL,
    pending TEXT,
    pending_rounds INTEGER NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overrides (
    prefix TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    next_hop TEXT NOT NULL,
    since REAL NOT NULL,
    dry_run INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts REAL NOT NULL
);
"""


class SqliteStore:
    def __init__(self, path: str, readonly: bool = False) -> None:
        self.path = path
        if readonly:
            if not os.path.exists(path):
                raise FileNotFoundError(f"database {path} does not exist (is the daemon running?)")
            self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._db = sqlite3.connect(path, timeout=5)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        self._db.row_factory = sqlite3.Row

    def close(self) -> None:
        self._db.close()

    # --- writes (used by SqliteRecorder) --------------------------------------

    def reset_runtime(self) -> None:
        """On daemon start nothing is announced yet: drop stale runtime state."""
        with self._db:
            self._db.execute("DELETE FROM overrides")
            self._db.execute("DELETE FROM prefix_state")
            self._db.execute("DELETE FROM provider_status")
            self._db.execute("DELETE FROM kv WHERE key IN ('mode_request', 'bgp_state')")

    def add_measurements(self, rows: Iterable[tuple]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT INTO measurements (ts, prefix, provider, method, sent, received,"
                " rtt_ms, rtt_min_ms, rtt_max_ms, loss_pct) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def prune(self, older_than: float) -> int:
        with self._db:
            return self._db.execute("DELETE FROM measurements WHERE ts < ?", (older_than,)).rowcount

    def set_provider_status(
        self, prefix: str, ts: float, metrics: Mapping[str, ProviderMetrics], scores: Mapping[str, float | None]
    ) -> None:
        with self._db:
            for name, m in metrics.items():
                self._db.execute(
                    "INSERT OR REPLACE INTO provider_status VALUES (?,?,?,?,?,?,?,?,?)",
                    (prefix, name, ts, m.sent, m.received, m.rtt_ms, m.jitter_ms, m.loss_pct, scores.get(name)),
                )

    def set_prefix_state(self, ts: float, state: PrefixState, code: str, reason: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO prefix_state VALUES (?,?,?,?,?,?,?,?)",
                (state.prefix, ts, state.active, state.changed_at, state.pending, state.pending_rounds, code, reason),
            )

    def add_decision(
        self,
        ts: float,
        decision: Decision,
        next_hop: str | None,
        metrics: Mapping[str, ProviderMetrics],
        command: str | None,
        dry_run: bool,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO decisions (ts, prefix, action, from_provider, to_provider, next_hop,"
                " code, reason, metrics, command, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    decision.prefix,
                    str(decision.action),
                    decision.current,
                    decision.target,
                    next_hop,
                    str(decision.code),
                    decision.reason,
                    json.dumps({k: v.to_dict() for k, v in metrics.items()}),
                    command,
                    int(dry_run),
                ),
            )

    def set_override(self, prefix: str, provider: str, next_hop: str, since: float, dry_run: bool) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO overrides VALUES (?,?,?,?,?)",
                (prefix, provider, next_hop, since, int(dry_run)),
            )

    def clear_override(self, prefix: str | None = None) -> None:
        with self._db:
            if prefix is None:
                self._db.execute("DELETE FROM overrides")
            else:
                self._db.execute("DELETE FROM overrides WHERE prefix = ?", (prefix,))

    def add_event(self, ts: float, kind: str, **detail: Any) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO events (ts, kind, detail) VALUES (?,?,?)",
                (ts, kind, json.dumps(detail, default=str)),
            )

    def set_kv(self, key: str, value: Any) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                (key, json.dumps(value, default=str), time.time()),
            )

    def delete_kv(self, key: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    # --- ModeRequestSource / ModeControl -----------------------------------------

    def take_mode_request(self) -> str | None:
        req = self.get_kv("mode_request")
        if req is not None:
            self.delete_kv("mode_request")
        return req

    def request_mode(self, mode: str) -> None:
        self.set_kv("mode_request", mode)

    def pending_mode_request(self) -> str | None:
        return self.get_kv("mode_request")

    # --- StatusQuery ----------------------------------------------------------------

    def current_mode(self) -> str | None:
        return self.get_kv("mode")

    def bgp_state(self) -> dict | None:
        return self.get_kv("bgp_state")

    def heartbeat(self) -> float | None:
        return self.get_kv("heartbeat")

    def provider_status(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM provider_status ORDER BY prefix, provider")]

    def prefix_states(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM prefix_state ORDER BY prefix")]

    def overrides(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM overrides ORDER BY prefix")]

    # --- HistoryQuery ---------------------------------------------------------------

    def decisions(self, limit: int = 50, prefix: str | None = None) -> list[dict]:
        sql = "SELECT * FROM decisions"
        args: tuple = ()
        if prefix:
            sql += " WHERE prefix = ?"
            args = (prefix,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = [dict(r) for r in self._db.execute(sql, args + (limit,))]
        for r in rows:
            r["metrics"] = json.loads(r["metrics"])
            r["dry_run"] = bool(r["dry_run"])
        return list(reversed(rows))

    def events(self, limit: int = 50) -> list[dict]:
        rows = [dict(r) for r in self._db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return list(reversed(rows))
