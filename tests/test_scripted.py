"""Scripted cases the golden runs do not reach: late replies, an order change during a call,
a provider failure with recovery, and a run that ends in death.

All but the provider failure are pinned like golden runs. Recovery is checked by its events.
"""

import pytest
from conftest import ORDER_TEXT, Case, check_golden, require_hash_seed

pytestmark = pytest.mark.engine


def events(run, kind):
    return [event for event in run.events if event["type"] == kind]


def test_replies_twenty_ticks_late(play):
    require_hash_seed()
    run = play(Case("map01_late20", delay=20, ticks=700))
    check_golden("map01_late20", run)
    records = run.records()
    assert any(record["accepted"] for record in records)
    assert any(
        "Selected option is no longer in the current menu" in record["override_reasons"]
        for record in records
    )
    assert all(record["decision_age_ticks"] >= 0 for record in records)


def test_provider_stall_discards_late_reply_and_requests_fresh_state(play):
    run = play(Case("provider_stall", delay=80, ticks=300))
    assert events(run, "provider_stall")
    discarded = [
        r for r in run.records() if any("Provider stall" in s for s in r["override_reasons"])
    ]
    assert discarded and not discarded[0]["accepted"]
    assert any(event["id"] > discarded[0]["id"] for event in events(run, "request"))


def test_order_change_while_a_call_is_pending(play):
    require_hash_seed()
    run = play(Case("map01_order_change", delay=20, order_at=200, ticks=500))
    check_golden("map01_order_change", run)
    [order] = events(run, "orders")
    assert order["revision"] == 1
    after = run.events[run.events.index(order) :]
    stale = next(event["record"] for event in after if event["type"] == "response")
    assert not stale["accepted"]
    assert stale["override_reasons"] == [
        "Order or objective expired while request in flight; request fresh state"
    ]
    fresh = next(event for event in after if event["type"] == "request")
    assert fresh["order_rev"] == 1
    assert fresh["state"]["standing_order"] == ORDER_TEXT


def test_provider_timeout_then_recovery(play):
    require_hash_seed()
    run = play(Case("map01_provider_timeout", fail=(4,), ticks=900))
    h = run.harness
    [pause] = events(run, "recovery_pause")
    assert (pause["request"], pause["attempt"], pause["retry_delay_seconds"]) == (4, 1, 15)
    [retry] = events(run, "recovery_request")
    # The 15 s backoff ends between observations. The fresh capture is one tick later.
    assert retry["game_tick"] - pause["game_tick"] == 15 * 35 + 1
    assert h.free_run_ticks == 15 * 35 + 1
    assert (h.status, h.recovery_total, h.recovery_attempts) == ("stopped", 1, 0)
    [after_retry] = [r for r in run.records() if r["id"] == 5]
    assert after_retry["accepted"]


def test_run_ends_in_death(play):
    require_hash_seed()
    run = play(Case("map02_seed25_skill5", map="MAP02", seed=25, skill=5))
    check_golden("map02_seed25_skill5", run)
    h = run.harness
    result = h.store.result
    assert (h.status, result["reason"]) == ("dead", "dead")
    assert result["terminal_evidence"]["dead"]
    assert result["player"]["health"] <= 0
    assert events(run, "episode_end")
