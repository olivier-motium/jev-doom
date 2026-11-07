"""Exercise run and replay transitions without starting ViZDoom."""

import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import IdleThread, MemoryStore, TickClock, fixed_answer
from jevdoom.adapters.files import FileStore
from jevdoom.adapters.replay import ReplaySession
from jevdoom.ports import Step
from jevdoom.run import RESERVE, Run
from test_local_journey import FakeEngine, ImmediateLocal


class TrackingEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.releases = 0

    def release(self):
        self.releases += 1


class HeldDecider(ImmediateLocal):
    def __init__(self):
        super().__init__()
        self.released = False

    def submit(self, job):
        if job["phase"] == "probe":
            reply = {"ok": True, "answers": {}, "cost_usd": 0.01}
        elif job["controller"] == "local":
            super().submit(job)
            return
        else:
            reply = fixed_answer(job)
        self.result = (job, reply)

    def poll(self, timeout=0):
        if not self.released:
            return None
        self.released = False
        return super().poll(timeout)

    def release(self):
        self.released = True


def make_run(monkeypatch, decider=None, store=None):
    clock = TickClock()
    with monkeypatch.context() as patch:
        patch.setattr(threading, "Thread", IdleThread)
        run = Run(
            engine=TrackingEngine(),
            decider=decider or ImmediateLocal(),
            store=store or MemoryStore(),
            clock=clock.monotonic,
        )
    run.new_game()
    return run, clock


def command(run, name, **args):
    run.commands.put((name, args))
    run.tick()


def test_pause_and_resume_releases_controls_and_returns_to_running(monkeypatch):
    run, clock = make_run(monkeypatch)
    command(run, "start", controller="local")
    run.tick()
    assert run.status == "running"
    revision = run.control_rev

    command(run, "pause")
    assert run.status == "paused"
    assert run.control_rev == revision + 1
    assert run.engine.releases == 1
    assert run.controls == {"released": True, "reason": "paused"}

    clock.now += 1
    command(run, "resume")
    assert run.status == "warming" and run.busy
    run.tick()
    assert run.status == "running" and run.records[-1]["accepted"]
    assert run.error is None


def test_resume_discards_reply_from_before_pause_and_accepts_fresh_reply(monkeypatch):
    decider = HeldDecider()
    run, clock = make_run(monkeypatch, decider)
    command(run, "start", controller="local")
    assert run.status == "warming" and run.busy
    old_id = run.pending.request_id
    revision = run.control_rev

    command(run, "pause")
    assert run.status == "paused" and run.control_rev == revision + 1
    assert run.engine.releases == 1
    assert run.controls == {"released": True, "reason": "paused"}
    command(run, "resume")
    assert run.status == "warming" and run.pending.request_id == old_id

    clock.now += 1
    decider.release()
    run.tick()
    record = run.records[-1]
    assert record["id"] == old_id and not record["accepted"]
    assert (
        "Order or objective expired while request in flight; request fresh state"
        in record["override_reasons"]
    )
    assert run.status == "warming" and run.busy and run.pending.request_id != old_id
    decider.release()
    run.tick()
    assert run.status == "running" and run.records[-1]["accepted"]
    assert run.error is None


def test_start_after_stopped_run_reinitializes_to_ready(monkeypatch):
    run, _ = make_run(monkeypatch)
    command(run, "start", controller="local")
    run.tick()
    command(run, "stop")
    assert run.status == "stopped" and not run.busy
    episode = run.episode
    states = []
    new_game = run.new_game

    def observe_new_game():
        new_game()
        states.append(run.status)

    monkeypatch.setattr(run, "new_game", observe_new_game)
    command(run, "start", controller="local")
    assert states == ["ready"]
    assert run.status == "warming" and run.episode == episode + 1
    assert run.error is None


def objective_run(monkeypatch):
    run, _ = make_run(monkeypatch)
    run.store.open_run("objective")
    run.objective = {"goal": "upgrade_weapon", "subject": {"id": "shotgun"}}
    run.last_action = {"trigger": "fire"}
    run.controls = {"MOVE": {"model_choice": "destination"}}
    run.active_tactic = {
        "option": {
            "id": "switch",
            "label": "Switch",
            "prediction": {"kind": "switch"},
            "expiry_seconds": 1,
        },
        "before": copy.deepcopy(run.observation),
        "started_tick": run.sim_ticks,
        "request": 1,
        "selector": "local",
    }
    return run


def test_clear_objective_new_game_resets_action_and_controls(monkeypatch):
    run = objective_run(monkeypatch)
    tactic = run.active_tactic
    revision = run.control_rev
    run.clear_objective("new_game")
    assert run.objective is None and run.last_action is None and run.controls == {}
    assert run.active_tactic is tactic and run.control_rev == revision
    assert run.store.events == []


def test_clear_objective_invalidated_keeps_action_and_controls(monkeypatch):
    run = objective_run(monkeypatch)
    objective, action, controls = run.objective, run.last_action, run.controls
    revision = run.control_rev
    run.clear_objective("invalidated", "pickup vanished")
    assert run.objective is None and run.active_tactic is None
    assert run.last_action is action and run.controls is controls
    assert run.control_rev == revision + 1
    outcome, event = run.store.events
    assert outcome["type"] == "tactic_outcome"
    assert outcome["outcome"]["end_reason"] == "invalidated"
    assert event["type"] == "objective_invalidated"
    assert event["objective"] == objective and event["reason"] == "pickup vanished"
    assert event["game_tick"] == run.sim_ticks


def test_clear_objective_weapon_complete_releases_controls(monkeypatch):
    run = objective_run(monkeypatch)
    tactic = run.active_tactic
    revision = run.control_rev
    run.clear_objective("weapon_complete", "weapon acquired")
    assert run.objective is None and run.last_action is None and run.active_tactic is tactic
    assert run.control_rev == revision + 1
    assert run.controls == {
        "released": True,
        "reason": "Weapon objective finished or invalid; request fresh goal",
    }
    (event,) = run.store.events
    assert event["type"] == "objective_complete"
    assert event["goal"] == "upgrade_weapon" and event["subject"] == {"id": "shotgun"}
    assert event["reason"] == "weapon acquired"


@pytest.mark.parametrize("reason,bump", [("completed", 0), ("interrupted", 1)])
def test_clear_objective_tactic_end_revision_depends_on_reason(monkeypatch, reason, bump):
    run = objective_run(monkeypatch)
    controls, revision = run.controls, run.control_rev
    run.clear_objective("tactic_end", reason)
    assert run.objective is None and run.last_action is None and run.active_tactic is None
    assert run.controls is controls and run.control_rev == revision + bump
    (event,) = run.store.events
    assert event["type"] == "tactic_outcome" and event["outcome"]["end_reason"] == reason


def test_clear_objective_orders_invalidates_tactic(monkeypatch):
    run = objective_run(monkeypatch)
    controls, revision = run.controls, run.control_rev
    run.clear_objective("orders")
    assert run.objective is None and run.last_action is None and run.active_tactic is None
    assert run.controls is controls and run.control_rev == revision + 1
    (event,) = run.store.events
    assert event["type"] == "tactic_outcome" and event["outcome"]["end_reason"] == "invalidated"


def test_billed_probe_is_busy_until_reply_settles(monkeypatch):
    decider = HeldDecider()
    run, _ = make_run(monkeypatch, decider)
    run.request_probe({"provider": "gateway", "state": {}, "questions": {}})
    assert run.busy and run.pending.billed
    assert run.ledger["charged_usd"] == RESERVE
    decider.release()
    run.tick()
    assert not run.busy and run.pending is None
    assert run.probes[-1]["accepted"] and run.provider_health["gateway"]
    assert run.ledger["charged_usd"] == pytest.approx(0.01)


@pytest.mark.parametrize(
    "controller,reservation,settled_cost,calls",
    [("semantic_tactics", RESERVE, 0.001, 1), ("local", 0, 0, 0)],
)
def test_finish_with_pending_request_records_reservation(
    monkeypatch, controller, reservation, settled_cost, calls
):
    decider = HeldDecider()
    run, _ = make_run(monkeypatch, decider)
    run.provider_health["gateway"] = True
    command(run, "start", controller=controller, provider="gateway")
    assert run.busy and run.pending.billed == (controller != "local")
    command(run, "stop")
    assert run.status == "stopped" and run.result["cost_final"] is (controller == "local")
    assert run.result["pending_reservation_usd"] == reservation
    assert run.store.result["pending_reservation_usd"] == reservation

    decider.release()
    run.tick()
    assert run.status == "stopped"
    assert not run.busy and run.pending is None
    for result in (run.result, run.store.result):
        assert result["reason"] == "stopped"
        assert result["cost_final"] is True
        assert result["pending_reservation_usd"] == 0
        assert result["budget"]["charged_usd"] == pytest.approx(settled_cost)
    for ledger in (run.ledger, run.store.ledger):
        assert ledger["charged_usd"] == pytest.approx(settled_cost)
        assert ledger["reported_usd"] == pytest.approx(settled_cost)
        assert ledger["calls"] == calls


@pytest.mark.parametrize("release_on_close", [False, True])
def test_shutdown_drains_waiting_billed_reply(monkeypatch, release_on_close):
    decider = HeldDecider()
    run, _ = make_run(monkeypatch, decider)
    run.provider_health["gateway"] = True
    command(run, "start", controller="semantic_tactics", provider="gateway")
    command(run, "stop")
    assert run.store.result["cost_final"] is False
    assert run.store.result["pending_reservation_usd"] == RESERVE

    if release_on_close:
        decider.close = decider.release
    else:
        decider.release()
    run.shutdown()

    assert not run.busy
    result, ledger = run.store.result, run.store.ledger
    assert result["cost_final"] is True
    assert result["pending_reservation_usd"] == 0
    assert result["budget"]["charged_usd"] == pytest.approx(0.001)
    assert result["budget"] == ledger
    assert "open_results" not in ledger
    assert run.store.events[-2]["type"] == "provider_return"
    assert run.store.events[-1]["type"] == "response"


def test_restart_recovers_result_after_settlement_write(monkeypatch, tmp_path):
    store = FileStore(tmp_path)
    decider = HeldDecider()
    run, _ = make_run(monkeypatch, decider, store)
    run.provider_health["gateway"] = True
    command(run, "start", controller="semantic_tactics", provider="gateway")
    command(run, "stop")
    result_path = run.run_dir / "result.json"
    result = json.loads(result_path.read_text())
    assert result["cost_final"] is False
    assert result["pending_reservation_usd"] == RESERVE

    real_replace = Path.replace
    failed = False

    def fail_result_replace(path, target):
        nonlocal failed
        if path.name == "result.tmp" and not failed:
            failed = True
            raise OSError("result write interrupted")
        return real_replace(path, target)

    decider.release()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_result_replace)
        with pytest.raises(OSError, match="result write interrupted"):
            run.tick()

    make_run(monkeypatch, store=FileStore(tmp_path))
    result = json.loads(result_path.read_text())
    ledger = json.loads((tmp_path / "budget.json").read_text())
    assert result["cost_final"] is True
    assert result["pending_reservation_usd"] == 0
    assert result["budget"] == ledger
    assert ledger["charged_usd"] == pytest.approx(0.001)
    assert "open_results" not in ledger


class ReplayEngine:
    def __init__(self, stop_event, *, error=None, wait_for_stop=False):
        self.stop_event = stop_event
        self.error = error
        self.wait_for_stop = wait_for_stop
        self.started = False
        self.published = threading.Event()

    def start(self, *args, **kwargs):
        self.started = True
        if self.error:
            raise RuntimeError(self.error)

    def frame(self):
        return SimpleNamespace(
            tic=1, variables={}, screen_buffer=np.zeros((2, 2, 3), dtype=np.uint8)
        )

    def replay_step(self):
        self.published.set()
        if self.wait_for_stop:
            assert self.stop_event.wait(timeout=5)
        return Step(None, not self.wait_for_stop, False, False, 0, False)

    def close(self):
        pass


def make_replay(tmp_path, **engine_args):
    (tmp_path / "episode.lmp").touch()
    (tmp_path / "result.json").write_text('{"reason": "stopped"}')
    events = [{"type": "episode_start", "config": {"map": "MAP01", "skill": 3, "seed": 17}}]
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(event) for event in events))
    session = ReplaySession(tmp_path, frame_interval=0)
    session.engine = ReplayEngine(session.stop_event, **engine_args)
    return session


def test_replay_shutdown_before_start_is_stopped(tmp_path):
    session = make_replay(tmp_path)
    session.shutdown()
    assert session.view()["status"] == "replay_stopped"
    assert not session.engine.started and session.thread.ident is None


def test_replay_start_error_is_published(tmp_path):
    session = make_replay(tmp_path, error="replay start failed")
    session.view()
    session.thread.join(timeout=5)
    assert not session.thread.is_alive()
    view = session.view()
    assert view["status"] == "replay_error" and view["error"] == "replay start failed"


def test_replay_stop_after_frame_is_stopped(tmp_path):
    session = make_replay(tmp_path, wait_for_stop=True)
    session.view()
    try:
        assert session.engine.published.wait(timeout=5)
        assert session.view()["observation"]["tick"] == 1
    finally:
        session.shutdown()
    assert not session.thread.is_alive()
    assert session.view()["status"] == "replay_stopped"


def test_replay_normal_finish_is_published(tmp_path):
    session = make_replay(tmp_path)
    session.view()
    session.thread.join(timeout=5)
    assert not session.thread.is_alive()
    view = session.view()
    assert view["status"] == "replay_finished"
    assert view["observation"]["tick"] == 1 and view["result"]["reason"] == "stopped"
