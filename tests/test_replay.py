"""Record local games and replay their decisions without a provider."""

import math
import json
import tempfile
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import Case, IdleThread, ScriptedJev, TickClock
from jevdoom.adapters.files import FileStore
from jevdoom.adapters.replay import ReplaySession
from jevdoom.run import Run

pytestmark = pytest.mark.engine


def test_replay_uses_recorded_objective_and_latest_applied_option(tmp_path):
    events = [
        {"type": "episode_start", "config": {}},
        {"type": "request", "state": {"tick": 2, "objective": {"goal": "reach_exit"}}},
        {"type": "tactic_start", "started_tick": 3, "option": {"label": "Explore"}},
        {
            "type": "response",
            "record": {
                "id": 1,
                "tick": 3,
                "accepted": True,
                "option": {"label": "Explore"},
                "response": {},
            },
        },
        {"type": "request", "state": {"tick": 4, "objective": {"subject": {"label": "Door"}}}},
        {
            "type": "response",
            "record": {
                "id": 2,
                "tick": 5,
                "accepted": True,
                "option": {"label": "Fight"},
                "response": {},
            },
        },
        {
            "type": "response",
            "record": {
                "id": 3,
                "tick": 6,
                "accepted": False,
                "option": {"label": "Rejected"},
                "response": {},
            },
        },
    ]
    (tmp_path / "episode.lmp").touch()
    (tmp_path / "result.json").write_text("{}")
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(event) for event in events))
    replay = ReplaySession(tmp_path, frame_interval=0)

    def snapshot(tick):
        frame = SimpleNamespace(
            tic=tick, variables={}, screen_buffer=np.zeros((2, 2, 3), dtype=np.uint8)
        )
        shown = [record for record in replay.records if record["tick"] <= tick]
        replay._publish(frame, "replaying", shown)
        return replay.view_port.read()[0]

    assert snapshot(1)["objective"] is None and snapshot(1)["replay_option"] is None
    assert snapshot(2)["objective"] == {"goal": "reach_exit"}
    assert snapshot(3)["objective"] is None and snapshot(3)["replay_option"]["label"] == "Explore"
    assert snapshot(4)["objective"] == {"subject": {"label": "Door"}}
    assert snapshot(6)["objective"] is None and snapshot(6)["replay_option"]["label"] == "Fight"


@pytest.mark.parametrize("map_name,seed", [("MAP01", 17), ("MAP01", 18), ("MAP02", 19)])
def test_record_and_replay(map_name, seed, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="jd-", dir="/tmp") as folder:
        clock = TickClock()
        decider = ScriptedJev(Case("record", map=map_name, seed=seed, controller="local"))
        store = FileStore(__import__("pathlib").Path(folder))
        with monkeypatch.context() as patch:
            patch.setattr(threading, "Thread", IdleThread)
            run = Run(store=store, decider=decider, clock=clock.monotonic, sleep=clock.sleep)
        run.commands.put(
            ("start", {"map": map_name, "seed": seed, "seconds": 5, "controller": "local"})
        )

        def advance():
            decider.tick()
            if run.result.get("reason"):
                run.stop_event.set()

        clock.after_sleep = advance
        run.engine_loop()
        assert run.result.get("reason") == "duration_limit", run.error
        assert (run.run_dir / "episode.lmp").stat().st_size > 0
        final = run.result["player"]
        replay = ReplaySession(run.run_dir, frame_interval=0)
        try:
            replay.view()
            started = time.monotonic()
            limit = 60
            tick = None
            while replay.thread.is_alive() and time.monotonic() - started < limit:
                snapshot = replay.view()
                tick = snapshot.get("observation", {}).get("tick", tick)
                if tick is not None:
                    assert all(
                        record["tick"] + record.get("decision_age_ticks", 0) <= tick
                        for record in snapshot.get("records", [])
                    )
                replay.thread.join(timeout=0.01)
            elapsed = time.monotonic() - started
            assert not replay.thread.is_alive(), (
                f"replay {map_name} seed {seed} timed out after {elapsed:.1f}s "
                f"(limit {limit}s, last tick {tick})"
            )
            view = replay.view()
            assert view["status"] == "replay_finished"
            assert view["result"]["reason"] == run.result["reason"]
            player = view["observation"]["player"]
            assert math.dist((player["x"], player["y"]), (final["x"], final["y"])) <= 16
            assert view["records"]
            assert all(
                r["tick"] + r.get("decision_age_ticks", 0) <= view["observation"]["tick"]
                for r in view["records"]
            )
        finally:
            replay.shutdown()
