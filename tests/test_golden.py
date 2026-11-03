"""Golden runs: the harness with a model-free Jev must repeat these runs tick for tick.

Record again only in the play-upgrade slice: JEVDOOM_RECORD_GOLDEN=1 PYTHONHASHSEED=0 pytest ...
Recording also writes the observation fixtures that tests/test_observations.py replays.
"""

import json

import pytest
from conftest import GOLDEN, RECORD, Case, DecisionRecorder, check_golden, require_hash_seed

pytestmark = pytest.mark.engine

# Requests kept as observation fixtures. Together they cover door, door walk-through and switch
# options, combat and retreat, guard, observe, health, weapon and ammo pickups, a dodge
# question, a long omitted list and a first request with no active tactic.
RUNS = [
    (Case("map01_seed17", map="MAP01", seed=17), (16, 77, 100, 121, 141, 144)),
    (Case("map02_seed18", map="MAP02", seed=18), (1, 30, 35, 77, 109, 139)),
]


@pytest.mark.parametrize("case,picks", RUNS, ids=[case.name for case, _ in RUNS])
def test_golden_run(play, case, picks):
    require_hash_seed()
    recorder = DecisionRecorder(picks) if RECORD else None
    run = play(case, recorder)
    assert run.harness.error is None
    check_golden(case.name, run)
    if recorder:
        assert len(recorder.fixtures) == len(picks)
        folder = GOLDEN / "observations"
        folder.mkdir(parents=True, exist_ok=True)
        for fixture in recorder.fixtures:
            path = folder / f"{case.name}-request{fixture['request']:03d}.json"
            path.write_text(json.dumps(fixture, separators=(",", ":")) + "\n")
