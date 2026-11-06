"""Exercise the engine contract with a fake native game."""

from types import SimpleNamespace

import pytest

pytest.importorskip("vizdoom")

from jevdoom.adapters import vizdoom_engine
from jevdoom.core.catalog import PICKUP_WEAPON_SLOTS, VARIABLES, owns_pickup
from jevdoom.ports import end_reason


def test_shared_weapon_slots_do_not_claim_an_unknown_pickup():
    assert PICKUP_WEAPON_SLOTS["Chainsaw"] == 1
    assert PICKUP_WEAPON_SLOTS["SuperShotgun"] == 3
    assert "WEAPON1" in VARIABLES and "WEAPON3" in VARIABLES
    assert not owns_pickup("Chainsaw", [{"slot": 1, "count": 1}])
    assert owns_pickup("Chainsaw", [{"slot": 1, "count": 2}])
    assert not owns_pickup("Shotgun", [{"slot": 3, "count": 0}])
    assert owns_pickup("Shotgun", [{"slot": 3, "count": 1}])
    assert not owns_pickup("SuperShotgun", [{"slot": 3, "count": 1}])
    assert owns_pickup("SuperShotgun", [{"slot": 3, "count": 2}])


class FakeGame:
    def __init__(self):
        self.done = False
        self.dead = False
        self.frame = SimpleNamespace(
            tic=5,
            labels=[SimpleNamespace(object_id=3, width=4, height=5)],
            objects=[
                SimpleNamespace(
                    name="DoomImp",
                    id=3,
                    position_x=10,
                    position_y=20,
                    position_z=0,
                    velocity_x=0,
                    velocity_y=0,
                )
            ],
            sectors=[SimpleNamespace(floor_height=0, ceiling_height=128)],
            depth_buffer=None,
            screen_buffer=bytearray(b"abc"),
        )

    def __getattr__(self, name):
        if name.startswith("set_") or name in ("init", "new_episode", "close"):
            return lambda *args: None
        raise AttributeError(name)

    def get_state(self):
        return self.frame

    def get_game_variable(self, name):
        return 42

    def make_action(self, buttons, ticks):
        self.done = True
        self.dead = True

    def is_episode_finished(self):
        return self.done

    def is_player_dead(self):
        return self.dead

    def is_episode_timeout_reached(self):
        return False

    def get_last_reward(self):
        return 0

    def get_total_reward(self):
        return 0

    def get_map_exit_reward(self):
        return 1


def test_frame_is_owned_and_step_reports_death(monkeypatch):
    game = FakeGame()
    monkeypatch.setattr(vizdoom_engine.vzd, "DoomGame", lambda: game)
    engine = vizdoom_engine.VizdoomEngine("example.wad")
    frame = engine.start("MAP01", 3, 17)
    game.frame.screen_buffer[0] = 0
    assert frame.screen_buffer == bytearray(b"abc")
    assert frame.objects[0].name == "DoomImp"
    step = engine.step([0] * 11)
    assert step.terminal and step.dead
    assert end_reason(step) == "dead"
    engine.close()


def test_terminal_reason_uses_the_step_contract():
    from jevdoom.ports import Step

    assert end_reason(Step(None, True, False, True, 0, False)) == "engine_timeout"
    assert end_reason(Step(None, True, False, False, 1, True)) == "level_complete"
    assert end_reason(Step(None, True, False, False, 0, False)) == "episode_ended_unverified"
    assert end_reason(Step(None, False, False, False, 0, False)) is None


def test_record_and_replay_use_short_temporary_paths(monkeypatch, tmp_path):
    from pathlib import Path

    class RecordingGame(FakeGame):
        def __init__(self):
            super().__init__()
            self.path = None
            self.recording = False

        def new_episode(self, path):
            self.path = Path(path)
            self.recording = True

        def replay_episode(self, path):
            self.path = Path(path)
            assert self.path.read_bytes() == b"recording"

        def close(self):
            if self.recording:
                self.path.write_bytes(b"recording")

    games = []

    def make_game():
        game = RecordingGame()
        games.append(game)
        return game

    monkeypatch.setattr(vizdoom_engine.vzd, "DoomGame", make_game)
    engine = vizdoom_engine.VizdoomEngine("example.wad")
    destination = tmp_path / ("long-" * 25) / "episode.lmp"
    destination.parent.mkdir()
    engine.start("MAP01", 3, 17, record_path=destination)
    recorded_temp = games[-1].path
    assert len(str(recorded_temp)) < len(str(destination))
    assert not destination.exists()
    engine.close()
    assert destination.read_bytes() == b"recording" and not recorded_temp.exists()

    engine.start("MAP01", 3, 17, replay_path=destination)
    replay_temp = games[-1].path
    assert replay_temp != destination and len(str(replay_temp)) < len(str(destination))
    engine.close()
    assert destination.read_bytes() == b"recording" and not replay_temp.exists()

    existing = tmp_path / "existing.lmp"
    existing.write_bytes(b"previous complete recording")
    engine.start("MAP01", 3, 17, record_path=existing)
    failed_temp = games[-1].path
    with monkeypatch.context() as patch:

        def fail_copy(source, target):
            Path(target).write_bytes(b"partial")
            raise OSError("copy failed")

        patch.setattr(vizdoom_engine.shutil, "copyfile", fail_copy)
        with pytest.raises(OSError, match="copy failed"):
            engine.close()
    assert existing.read_bytes() == b"previous complete recording"
    assert not failed_temp.exists()
    assert not list(tmp_path.glob(".episode-*.tmp"))
