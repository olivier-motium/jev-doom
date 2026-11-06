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
