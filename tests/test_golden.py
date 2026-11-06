"""Golden runs: the harness with a model-free Jev must repeat these runs tick for tick.

Record again only in the play-upgrade slice: JEVDOOM_RECORD_GOLDEN=1 PYTHONHASHSEED=0 pytest ...
The observation fixtures are maintained separately.
"""

import pytest
from conftest import Case, check_golden, require_hash_seed

pytestmark = pytest.mark.engine

RUNS = [
    Case("map01_seed17", map="MAP01", seed=17),
    Case("map02_seed18", map="MAP02", seed=18),
]


@pytest.mark.parametrize("case", RUNS, ids=[case.name for case in RUNS])
def test_golden_run(play, case):
    require_hash_seed()
    run = play(case)
    assert run.harness.error is None
    check_golden(case.name, run)
