"""Observation fixtures: the decision functions on recorded game observations, without the engine.

Each fixture holds one request from a golden run: the harness fields the model state is built
from, the Navigator state, and what state_for_model, build_menu, measure, tactic_questions and
validate_answers returned at that moment.
"""

import json

import pytest
from conftest import GOLDEN, ROOT, model_context, normal, restore_nav, validation_outcomes

FIXTURES = sorted((GOLDEN / "observations").glob("*.json"))
WAD = ROOT / "assets" / "freedoom2.wad"


@pytest.fixture(scope="module")
def navigator():
    """A fresh Navigator per map, built once; each test restores its own recorded state."""
    if not WAD.exists():
        pytest.skip("run ./setup.sh first: the fixtures need assets/freedoom2.wad")
    from jevdoom.core.navigation import Navigator

    built = {}

    def get(map_name):
        if map_name not in built:
            built[map_name] = Navigator(WAD.read_bytes(), map_name)
        return built[map_name]

    return get


def test_successful_observations_do_not_hide_unexplained_damage(navigator):
    from jevdoom.run import Run
    from jevdoom.core.tactics import build_menu

    fixture = json.loads((GOLDEN / "observations" / "map01_seed17-request077.json").read_text())
    nav = navigator(fixture["map"])
    restore_nav(nav, fixture["nav"])
    context = model_context(fixture["harness"])
    context.observation["enemies"] = []
    context.observation["just_took_damage"] = True
    context.tactic_outcomes = [
        {"tactic": "observe", "prediction": {"kind": "observation"},
         "prediction_status": "met", "measured": {"new_visible": ["enemy"], "new_navigation": []}}
        for _ in range(2)
    ]
    state = Run.state_for_model(context)
    menu, _ = build_menu(state, nav)
    assert any(option["id"] == "observe" for option in menu)


def test_pickups_above_normal_health_and_armor_limits(navigator, monkeypatch):
    from jevdoom.run import Run
    from jevdoom.core.tactics import build_menu

    fixture = json.loads((GOLDEN / "observations" / "map01_seed17-request077.json").read_text())
    nav = navigator(fixture["map"])
    restore_nav(nav, fixture["nav"])
    state = Run.state_for_model(model_context(fixture["harness"]))
    p = state["player"]
    p["health"], p["armor"] = 100, 80
    state.update(enemies=[], remembered=[], navigation=[], projectiles=[])
    state["items"] = [
        {"id": "Soulsphere_1", "kind": "health", "label": "Soulsphere", "x": p["x"] + 60, "y": p["y"]},
        {"id": "BlueArmor_1", "kind": "armor", "label": "Blue armor", "x": p["x"], "y": p["y"] + 60},
    ]
    monkeypatch.setattr(nav, "path", lambda start, end: [start, end])
    menu, _ = build_menu(state, nav)
    assert {"health_Soulsphere_1", "armor_BlueArmor_1"} <= {o["id"] for o in menu}


@pytest.mark.parametrize("path", FIXTURES, ids=[path.stem for path in FIXTURES])
def test_decision_functions_repeat_the_recording(path, navigator):
    from jevdoom.run import Run
    from jevdoom.core.tactics import build_menu, measure, tactic_questions

    fixture = json.loads(path.read_text())
    expected = fixture["expected"]
    nav = navigator(fixture["map"])
    restore_nav(nav, fixture["nav"])

    state = Run.state_for_model(model_context(fixture["harness"]))
    assert normal(state) == expected["state"]
    assert "lifts" not in state["measurement_context"]
    assert "escape_guard" not in state.get("survival", {})

    menu, omitted = build_menu(state, nav)
    assert normal(menu) == expected["menu"]
    assert normal(omitted) == expected["omitted"]

    active = fixture["active"]
    if active:
        after = fixture["harness"]["observation"]
        outcome = measure(
            active["option"], active["before"], after, nav, "replaced", active["elapsed"]
        )
        assert normal(outcome) == expected["measure"]

    state["tactic_candidates"] = menu
    state["omitted_tactics"] = omitted
    questions = tactic_questions(menu, state)
    assert normal(questions) == expected["questions"]
    assert validation_outcomes(questions) == expected["validation"]


def point_off_floor(nav, here, gap):
    """A point `gap` units straight out from a reachable edge cell of seen floor, with that cell
    still the nearest seen cell, so a route to the point ends exactly `gap` units short."""
    for cell in sorted(nav.seen):
        x, y = nav.xy(cell)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if (cell[0] + dx, cell[1] + dy) in nav.obs_cells:
                continue
            spot = [x + dx * gap, y + dy * gap]
            if nav.nearest(spot) == cell and nav.path(here, spot):
                return spot
    raise AssertionError(f"no seen floor edge with a free {gap}-unit margin")


def test_combat_route_must_end_within_32_units(navigator):
    """tactics.py:40: a visible enemy whose route ends more than 32 units short of it is fought
    from where the player stands; a nearer one is walked to."""
    from jevdoom.run import Run
    from jevdoom.core.tactics import build_menu

    fixture = json.loads((GOLDEN / "observations" / "map01_seed17-request077.json").read_text())
    nav = navigator(fixture["map"])
    restore_nav(nav, fixture["nav"])
    state = Run.state_for_model(model_context(fixture["harness"]))
    here = [state["player"]["x"], state["player"]["y"]]
    for gap, move in ((28, "destination"), (40, "hold")):
        spot = point_off_floor(nav, here, gap)
        enemy = dict(state["enemies"][0], x=spot[0], y=spot[1])
        menu, _ = build_menu(dict(state, enemies=[enemy]), nav)
        [combat] = [option for option in menu if option["kind"] == "combat"]
        assert combat["controls"]["move"] == move, f"enemy {gap} units off the route"
