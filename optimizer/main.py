"""Optimizer daemon entry point, started by ExaBGP as a helper process.

    python -m optimizer.main --config /etc/optimizer/config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from optimizer import config as config_mod
from optimizer.app.bootstrap import build_application
from optimizer.observability import logjson

log = logging.getLogger("optimizer")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="optimizer-daemon")
    ap.add_argument("-c", "--config", default=os.environ.get("OPTIMIZER_CONFIG", "/etc/optimizer/config.yaml"))
    args = ap.parse_args(argv)

    logjson.setup("INFO")
    try:
        cfg = config_mod.load(args.config)
    except (OSError, config_mod.ConfigError) as exc:
        log.critical("invalid configuration", extra={"error": str(exc), "path": args.config})
        return 2
    logjson.setup(cfg.log_level, cfg.log_file)

    commands_out = sys.stdout
    sys.stdout = sys.stderr  # stray prints must never reach the ExaBGP command pipe

    app = build_application(cfg, commands_out)
    try:
        return asyncio.run(app.daemon.run())
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
