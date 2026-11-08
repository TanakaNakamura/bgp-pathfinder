"""Structured JSON logging (formatter + setup).

stdout is the ExaBGP command channel, so logs always go to stderr (ExaBGP
passes the helper's stderr through to the container log) and optionally to a
JSON-lines file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                doc[key] = value
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str, separators=(",", ":"))


def setup(level: str = "INFO", file: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    formatter = JsonFormatter()

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)

    if file:
        os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
        fh = logging.FileHandler(file)
        fh.setFormatter(formatter)
        root.addHandler(fh)
