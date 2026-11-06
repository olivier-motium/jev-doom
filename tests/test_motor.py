"""The motor returns controls without changing the supplied game state."""

import copy
from collections import defaultdict
from types import SimpleNamespace

from jevdoom.core.motor import calculate_motor, same_door
from jevdoom.core.navigation import Navigator
from shapely.geometry import Point


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


def test_only_matching_door_faces_share_an_interaction():
    nav = SimpleNamespace(
        observed_lines={name: {"kind": "door"} for name in ("a", "b", "other")},
        lines=[
            {"id": "a", "a": (0, 0), "b": (0, 64), "r": 1, "l": 2},
            {"id": "b", "a": (0, 64), "b": (0, 0), "r": 2, "l": 1},
            {"id": "other", "a": (100, 0), "b": (100, 64), "r": 1, "l": 3},
        ],
    )
    assert same_door(nav, "a", "b")
    assert not same_door(nav, "a", "other")


def test_closing_one_door_span_rebuilds_the_wall():
    nav = Navigator.__new__(Navigator)
    nav.observed_lines = {
        "door": {
            "id": "door",
            "a": (0, 0),
            "b": (0, 100),
            "kind": "door",
            "spans": [(0, 0.25), (0.75, 1)],
            "open_spans": [(0, 0.25)],
        }
    }
    nav.cache, nav._nb_cache, nav._edge_cache = {}, {}, {}
    nav._reach, nav._geom_version = None, 0
    nav._rebuild_walls()
    assert nav._observed_union.distance(Point(0, 10)) > 1
    assert nav._observed_union.distance(Point(0, 90)) == 0
    assert nav.close_door("door", (0, 10))
    assert nav._observed_union.distance(Point(0, 10)) == 0
    assert nav._geom_version == 1
    midpoint = nav._span_mid(nav.observed_lines["door"])
    assert midpoint == [0, 12.5]


def test_stationary_combat_does_not_close_door_but_blocked_push_does():
    nav = Navigator.__new__(Navigator)
    nav.observed_lines = {
        "door": {
            "id": "door",
            "a": (0, 0),
            "b": (0, 64),
            "kind": "door",
            "spans": [(0, 1)],
            "open_spans": [(0, 1)],
            "open_tick": 1,
        }
    }
    nav.cache, nav._nb_cache, nav._edge_cache = {}, {}, {}
    nav._reach, nav._geom_version = None, 0
    nav.used, nav.lines = defaultdict(int), []
    nav.door_push_started = {"door": 0}
    nav.waypoint = lambda _start, end: end
    nav._rebuild_walls()
    target = {
        "id": "door",
        "kind": "door_or_switch",
        "x": -10,
        "y": 32,
        "line_x": 0,
        "line_y": 32,
        "through": [72, 32],
    }
    action = {
        "face": "hold",
        "move": "hold",
        "dodge": "carry_on",
        "trigger": "hold_fire",
        "weapon": "keep",
        "use": "no_use",
        "snapshot": {"objective": {"subject": target}},
        "control_rev": 0,
        "source_request": 1,
    }
    player = {"x": -10, "y": 32, "facing": 0, "weapon": "pistol"}
    observation = {
        "enemies": [],
        "previous_controls": {"MOVE": {"destination": target, "forward": 20, "right": 0}},
    }
    held = calculate_motor(action, observation, player, nav, {}, 50, 0, 0, {}, {}, 0)
    assert held.nav.point_is_open(held.nav.observed_lines["door"], (0, 32))
    action["move"] = "destination"
    pushed = calculate_motor(action, observation, player, nav, {}, 50, 0, 0, {}, {}, 0)
    assert not pushed.nav.point_is_open(pushed.nav.observed_lines["door"], (0, 32))
    assert any(name == "door_push_blocked" for name, _ in pushed.events)
