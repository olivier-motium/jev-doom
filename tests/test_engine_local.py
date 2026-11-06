"""Run a keyless local game through the real engine and web start command."""

import json
import threading

import pytest

from jevdoom.adapters.decider import AsyncDecider
from jevdoom.adapters.files import FileStore
from jevdoom.adapters.snapshot import SnapshotView
from jevdoom.adapters.web import create_app, encode_frame
from jevdoom.run import Run
from test_web import request

pytestmark = pytest.mark.engine


class NoProvider:
    def ask(self, job):
        raise AssertionError("Local play contacted a provider")

    def close(self):
        pass


class WatchingView(SnapshotView):
    def __init__(self):
        super().__init__()
        self.ready = threading.Event()
        self.playing = threading.Event()

    def publish(self, snapshot, frame, tick):
        super().publish(snapshot, frame, tick)
        if snapshot["status"] == "ready":
            self.ready.set()
        if snapshot["status"] == "running" and snapshot["records"] and tick >= 12:
            self.playing.set()


def test_local_start_runs_real_engine_without_spending(tmp_path):
    view = WatchingView()
    store = FileStore(tmp_path)
    app_run = Run(decider=AsyncDecider(NoProvider()), store=store, view=view)
    try:
        assert view.ready.wait(timeout=30), app_run.error
        app = create_app(app_run)
        status, _ = request(app, "/api/start", body=b'{"controller":"local","map":"MAP01"}')
        assert status == 200
        assert view.playing.wait(timeout=30), app_run.error
        state = app_run.view()
        frame, tick = app_run.frame_snapshot()
        assert state["config"]["controller"] == "local"
        assert state["records"][-1]["accepted"]
        assert encode_frame(frame).startswith(b"\xff\xd8") and tick >= 12
        ledger = json.loads((tmp_path / "budget.json").read_text())
        assert ledger["calls"] == 0 and ledger["charged_usd"] == 0
    finally:
        app_run.shutdown()
