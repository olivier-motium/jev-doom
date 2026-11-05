"""Start a local run through HTTP with a fake engine and no provider."""

import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from conftest import MemoryStore
from jevdoom.adapters.local import LocalDecider
from jevdoom.core.catalog import VARIABLES
from jevdoom.adapters.web import create_app
from jevdoom.ports import Frame, Step
from jevdoom.run import Run
from test_web import request


class FakeEngine:
    def __init__(self):
        self.tick = 0
        self.buttons = []
        self.ended = threading.Event()

    def navigation_data(self):
        return (Path(__file__).resolve().parents[1] / "assets/freedoom2.wad").read_bytes()

    def start(self, map_name, skill, seed, record_path=None):
        self.tick = 0
        return self.frame()

    def frame(self):
        values = {name: 0.0 for name in VARIABLES}
        values.update(
            HEALTH=100.0, SELECTED_WEAPON=2.0, SELECTED_WEAPON_AMMO=50.0, WEAPON2=1.0, AMMO2=50.0
        )
        return Frame(
            self.tick,
            values,
            (),
            (),
            tuple(SimpleNamespace(floor_height=0, ceiling_height=128) for _ in range(1024)),
            None,
            np.zeros((480, 640, 3), dtype=np.uint8),
        )

    def variable(self, name):
        return self.frame().variables[name]

    def status(self):
        return Step(None, False, False, False, 0, False)

    def step(self, buttons):
        self.buttons.append(buttons)
        self.tick += 1
        return self.status()

    def release(self):
        pass

    def close(self):
        self.ended.set()


class ImmediateLocal:
    def __init__(self):
        self.result = None

    def submit(self, job):
        assert job["controller"] == "local"
        self.result = (job, LocalDecider().ask(job))

    def poll(self, timeout=0):
        result, self.result = self.result, None
        return result

    def close(self):
        pass


def test_local_start_reaches_engine_without_charges():
    ready = threading.Event()
    owner = SimpleNamespace(run=None, tick=0)
    engine = FakeEngine()
    store = MemoryStore()

    def clock():
        return 1000 + owner.tick / 35

    def sleep(_):
        if not ready.wait(timeout=3):
            raise RuntimeError("test start not delivered")
        owner.tick += 1
        if owner.tick >= 12:
            owner.run.stop_event.set()

    run = Run(engine=engine, decider=ImmediateLocal(), store=store, clock=clock, sleep=sleep)
    owner.run = run
    app = create_app(run)
    status, _ = request(app, "/api/start", body=b'{"map":"MAP01"}')
    assert status == 200
    ready.set()
    assert engine.ended.wait(timeout=10)
    assert run.error is None
    assert engine.buttons
    assert store.ledger["calls"] == 0
    assert store.ledger["charged_usd"] == 0
    assert any(
        event["type"] == "response" and event["record"]["accepted"] for event in store.events
    )
    snapshot = run.view()
    assert snapshot["config"]["controller"] == "local"
    assert snapshot["records"][-1]["provider"] == "local"
    assert run.episode_costs()["provider_episode_cost"]["provider"] == "local"
    assert all(event["provider"] == "local" for event in store.events if event["type"] == "request")
    assert run.frame_snapshot()[1] == snapshot["observation"]["tick"]


def test_engine_start_failure_is_published():
    class BrokenEngine(FakeEngine):
        def start(self, map_name, skill, seed, record_path=None):
            raise RuntimeError("engine start failed")

    engine = BrokenEngine()
    run = Run(engine=engine, decider=ImmediateLocal(), store=MemoryStore())
    assert engine.ended.wait(timeout=5)
    assert run.view()["status"] == "engine_error"
    assert run.view()["error"] == "engine start failed"


def test_demo_surfaces_engine_start_error(monkeypatch, tmp_path):
    import sys
    import pytest
    from jevdoom import app, run as run_module

    class BrokenEngine(FakeEngine):
        def start(self, map_name, skill, seed, record_path=None):
            raise RuntimeError("engine start failed")

    make_run = run_module.Run
    monkeypatch.setattr(
        run_module,
        "Run",
        lambda: make_run(engine=BrokenEngine(), decider=ImmediateLocal(), store=MemoryStore()),
    )
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["jevdoom", "--demo"])
    with pytest.raises(RuntimeError, match="Demo recording failed: engine start failed"):
        app.main()
