"""Shared reservation accounting, independent of storage and providers."""

import math


def blocks(ledger, cap, reservation):
    return ledger["charged_usd"] + reservation > cap


def reserve(ledger, account, reservation):
    ledger["charged_usd"] += reservation
    ledger["calls"] += 1
    account["charged_usd"] += reservation
    account["calls"] += 1


def settle(ledger, account, response, reservation):
    cost = response.get("cost_usd")
    if cost is not None and isinstance(cost, (float, int)) and math.isfinite(cost) and cost >= 0:
        ledger["charged_usd"] += cost - reservation
        ledger["reported_usd"] += cost
        account["charged_usd"] += cost - reservation
        account["reported_usd"] += cost
    else:
        ledger["unknown_cost_calls"] += 1
        account["unknown_cost_calls"] += 1
    estimate = response.get("estimated_cost_usd")
    if (
        estimate is not None
        and isinstance(estimate, (float, int))
        and math.isfinite(estimate)
        and estimate >= 0
    ):
        account["estimated_usd"] = account.get("estimated_usd", 0) + estimate
