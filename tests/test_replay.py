"""Record local games and replay their decisions without a provider."""

import math
import tempfile
import threading
import time

import pytest
from conftest import Case, IdleThread, ScriptedJev, TickClock
from jevdoom.adapters.files import FileStore
from jevdoom.adapters.replay import ReplaySession
from jevdoom.run import Run

pytestmark = pytest.mark.engine


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
        replay = ReplaySession(run.run_dir)
        try:
            first_seen = {}
            deadline = time.monotonic() + 12
            while replay.thread.is_alive() and time.monotonic() < deadline:
                snapshot = replay.view()
                tick = snapshot.get("observation", {}).get("tick")
                if tick is not None:
                    for record in snapshot.get("records", []):
                        first_seen.setdefault(record["id"], tick)
                replay.thread.join(timeout=0.01)
            assert not replay.thread.is_alive(), "replay did not finish"
            view = replay.view()
            assert view["status"] == "replay_finished"
            assert view["result"]["reason"] == run.result["reason"]
            player = view["observation"]["player"]
            assert math.dist((player["x"], player["y"]), (final["x"], final["y"])) <= 16
            assert view["records"]
            assert first_seen
            assert all(
                first_seen[record["id"]] == record["tick"] + record.get("decision_age_ticks", 0)
                for record in view["records"]
                if record["id"] in first_seen
            )
            assert all(
                r["tick"] + r.get("decision_age_ticks", 0) <= view["observation"]["tick"]
                for r in view["records"]
            )
        finally:
            replay.shutdown()
