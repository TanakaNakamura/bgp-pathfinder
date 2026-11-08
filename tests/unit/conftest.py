import io
from dataclasses import replace

import pytest

from optimizer.app.bootstrap import build_service
from optimizer.config import load
from optimizer.observability.log_listener import JsonLogListener
from optimizer.storage import SqliteRecorder, SqliteStore
from tests.unit.helpers import LAB_CONFIG, FakeProber


@pytest.fixture
def setup(tmp_path):
    """Factory: a started OptimizerService wired by the real composition root.

    Returns (service, exabgp_pipe, store, fake_prober).
    """

    def make(dry_run: bool, extra_listeners=()):
        cfg = replace(load(LAB_CONFIG), dry_run=dry_run, db_path=str(tmp_path / "opt.db"))
        out = io.StringIO()
        store = SqliteStore(cfg.db_path)
        prober = FakeProber()
        listeners = [SqliteRecorder(store), JsonLogListener(), *extra_listeners]
        service = build_service(cfg, commands_out=out, metrics=prober, mode_requests=store, listeners=listeners)
        service.start(0.0, pid=1)
        return service, out, store, prober

    return make
