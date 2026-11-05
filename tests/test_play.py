"""Compare the model-free policy on held-out maps and seeds."""

import json
from collections import Counter

import pytest
from conftest import Case, require_hash_seed

pytestmark = pytest.mark.engine


class IdleRecorder:
    def __init__(self):
        self.counts = Counter()

    def install(self, patch, h, case):
        original = h.engine.step

        def step(buttons):
            if h.last_action is None or h.controls.get("released"):
                ends = [e for e in h.store.events if e["type"] == "tactic_outcome"]
                reason = ends[-1]["outcome"]["end_reason"] if ends else "before_first_option"
                self.counts[reason] += 1
            return original(buttons)

        patch.setattr(h.engine, "step", step)


def test_idle_causes(play):
    """Count released-control engine steps after the last recorded option end."""
    recorder = IdleRecorder()
    run = play(Case("diagnostic", seed=17, delay=20, ticks=700), recorder)
    print(
        "IDLE_BY_REASON",
        json.dumps(recorder.counts, sort_keys=True),
        "ticks",
        run.harness.sim_ticks,
    )
    print(
        "RESPONSES",
        dict(
            Counter("; ".join(record["override_reasons"]) or "accepted" for record in run.records())
        ),
    )
    print(
        "ENDS",
        dict(
            Counter(
                (event["outcome"]["end_reason"], event["outcome"]["tactic"])
                for event in run.events
                if event["type"] == "tactic_outcome"
            )
        ),
    )


@pytest.mark.parametrize("map_name", ["MAP01", "MAP02"])
@pytest.mark.parametrize("seed", [41, 42])
@pytest.mark.parametrize("delay", [1, 20])
def test_held_out_play(play, map_name, seed, delay):
    require_hash_seed()
    recorder = IdleRecorder()
    run = play(
        Case(
            "heldout",
            map=map_name,
            seed=seed,
            delay=delay,
            ticks=1040,
            controller="local" if delay == 1 else "semantic_tactics",
        ),
        recorder,
    )
    print(
        "HELDOUT",
        json.dumps(
            {
                "map": map_name,
                "seed": seed,
                "delay": delay,
                "health": run.rows[-1][5],
                "released_ticks_by_end_reason": dict(recorder.counts),
                **run.metrics(),
            },
            sort_keys=True,
        ),
    )
