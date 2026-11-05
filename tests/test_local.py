"""Local choices use only the supplied menu and incur no model cost."""

import json
from pathlib import Path

from jevdoom.adapters.local import LocalDecider
from jevdoom.core.protocol import validate_answers


def test_local_decision_is_valid_and_free():
    fixture = json.loads(
        (Path(__file__).parent / "golden/observations/map01_seed17-request077.json").read_text()
    )
    state = fixture["expected"]["state"]
    state["tactic_candidates"] = fixture["expected"]["menu"]
    job = {"state": state, "questions": fixture["expected"]["questions"]}
    decision = LocalDecider().ask(job)
    validate_answers(job, decision)
    assert decision["cost_usd"] == 0
    assert decision["model"] == "Local rule, no model"
    assert not LocalDecider.billed
