"""Exercise the late-reply and projectile contracts through a real game."""

import pytest
from conftest import Case, ORDER_TEXT, require_hash_seed

pytestmark = pytest.mark.engine


def test_order_change_cancels_option_and_rejects_late_reply(play):
    require_hash_seed()
    run = play(Case("order_contract", delay=20, order_at=200, ticks=500))
    order_index = next(i for i, e in enumerate(run.events) if e["type"] == "orders")
    order = run.events[order_index]
    assert order["text"] == ORDER_TEXT
    assert any(
        e["type"] == "tactic_outcome" and e["outcome"]["end_reason"] == "invalidated"
        for e in run.events[:order_index]
    )
    after = run.events[order_index + 1 :]
    response = next(e["record"] for e in after if e["type"] == "response")
    assert not response["accepted"]
    assert (
        "Order or objective expired while request in flight; request fresh state"
        in response["override_reasons"]
    )
    assert any(e["type"] == "request" and e["order_rev"] == order["revision"] for e in after)
    assert any(
        "Selected option is no longer in the current menu" in r["override_reasons"]
        for r in run.records()
    )


def test_closing_projectile_requests_a_choice(play):
    require_hash_seed()

    class Projectile:
        inserted = False

        def install(self, patch, run, case):
            capture = run.capture

            def inject():
                capture()
                if self.inserted or not run.active_tactic or run.busy or run.sim_ticks < 50:
                    return
                self.inserted = True
                run.active_tactic["prefetch_requested"] = True
                run.decision_tick = run.sim_ticks
                run.decision_health = run.observation["player"]["health"]
                run.decision_enemies = {e["id"] for e in run.observation["enemies"]}
                p = run.observation["player"]
                run.observation["projectiles"] = [
                    {
                        "id": "test-projectile",
                        "label": "closing projectile",
                        "visible": True,
                        "x": p["x"] - 100,
                        "y": p["y"],
                        "velocity": [10, 0],
                        "closest_approach_units_if_player_stays": 5,
                        "closest_approach_seconds_if_player_stays": 1,
                    }
                ]

            patch.setattr(run, "capture", inject)

    projectile = Projectile()
    run = play(Case("projectile_contract", ticks=200), projectile)
    assert projectile.inserted
    request = next(
        e
        for e in run.events
        if e["type"] == "request"
        and any(p["id"] == "test-projectile" for p in e["state"]["projectiles"])
    )
    assert "dodge" in request["questions"]
    response = next(
        e["record"]
        for e in run.events
        if e["type"] == "response" and e["record"]["id"] == request["id"]
    )
    assert response["response"]["answers"]["dodge"]["choice"] == "carry_on"


def test_new_projectile_requests_choice_while_first_is_still_closer(play):
    require_hash_seed()

    class TwoProjectiles:
        phase = 0

        def install(self, patch, run, case):
            capture = run.capture

            def inject():
                capture()
                if self.phase >= 2 or not run.active_tactic or run.busy or run.sim_ticks < 50:
                    return
                if self.phase == 1 and "first-projectile" not in run.decision_projectiles:
                    return
                p = run.observation["player"]
                first = {
                    "id": "first-projectile",
                    "label": "first projectile",
                    "visible": True,
                    "x": p["x"] - 100,
                    "y": p["y"],
                    "velocity": [10, 0],
                    "closest_approach_units_if_player_stays": 5,
                    "closest_approach_seconds_if_player_stays": 1,
                }
                second = dict(
                    first,
                    id="second-projectile",
                    label="second projectile",
                    closest_approach_seconds_if_player_stays=2,
                )
                run.active_tactic["prefetch_requested"] = True
                run.decision_tick = run.sim_ticks
                run.decision_health = p["health"]
                run.decision_enemies = {e["id"] for e in run.observation["enemies"]}
                run.observation["projectiles"] = [first] if self.phase == 0 else [first, second]
                self.phase += 1

            patch.setattr(run, "capture", inject)

    run = play(Case("two_projectiles", ticks=300), TwoProjectiles())
    requests = [event for event in run.events if event["type"] == "request"]
    second = next(
        event
        for event in requests
        if any(
            projectile["id"] == "second-projectile" for projectile in event["state"]["projectiles"]
        )
    )
    assert "first projectile" in second["questions"]["dodge"]["instructions"]
