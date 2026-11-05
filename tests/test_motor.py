"""The motor returns controls without changing the supplied game state."""

import copy
from collections import defaultdict
from types import SimpleNamespace

from jevdoom.core.motor import calculate_motor


def test_fire_requires_the_selected_visible_enemy():
    nav = SimpleNamespace(
        observed_lines={}, cache={}, _nb_cache={}, _edge_cache={}, used=defaultdict(int), lines=[]
    )
    action = {
        "face": "imp",
        "move": "hold",
        "dodge": "carry_on",
        "trigger": "fire",
        "weapon": "keep",
        "use": "no_use",
        "snapshot": {"objective": None},
        "control_rev": 0,
        "source_request": 7,
    }
    player = {"x": 0, "y": 0, "facing": 0, "weapon": "pistol"}
    observation = {"enemies": [{"id": "imp", "x": 100, "y": 0}]}
    original = copy.deepcopy((action, observation, player))
    result = calculate_motor(action, observation, player, nav, {}, 35, 0, 0, {}, {}, 0)
    assert result.buttons[3] == 1
    assert (action, observation, player) == original
    observation["enemies"] = []
    result = calculate_motor(action, observation, player, nav, {}, 35, 0, 0, {}, {}, 0)
    assert result.buttons[3] == 0
