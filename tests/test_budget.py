"""Reservations survive unknown costs and settle against reported charges."""

import pytest

from jevdoom.core.budget import blocks, reserve, settle


def ledger():
    return {"charged_usd": 0, "reported_usd": 0, "calls": 0, "unknown_cost_calls": 0}


def test_shared_reservations_replace_only_valid_reported_costs():
    shared = ledger()
    first, second = ledger(), ledger()
    reserve(shared, first, 0.05)
    assert shared["charged_usd"] == pytest.approx(0.05)
    settle(shared, first, {"cost_usd": None, "estimated_cost_usd": 0.001}, 0.05)
    assert shared["charged_usd"] == pytest.approx(0.05)
    assert shared["unknown_cost_calls"] == 1
    reserve(shared, second, 0.05)
    settle(shared, second, {"cost_usd": 0.01}, 0.05)
    assert shared["charged_usd"] == pytest.approx(0.06)
    assert shared["reported_usd"] == pytest.approx(0.01)
    assert blocks(shared, 0.10, 0.05)
