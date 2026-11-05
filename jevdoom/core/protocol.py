"""Prepare model input and validate structured replies."""

import copy
import math

from .catalog import EVALUATOR_FIELDS


def state_for_model(observation, objective, exploration_target, tactic_outcomes, order, order_rev):
    """Actor context: what Jev, option generation and the motors may see. Evaluator counters are removed; outcome feedback carries statuses, not counter deltas."""
    state = copy.deepcopy(observation)
    state.pop("captured_mono", None)
    state["objective"] = copy.deepcopy(objective)
    for k in EVALUATOR_FIELDS:
        state["player"].pop(k, None)
    state["mode"] = "observable"
    state.get("survival", {}).pop("escape_guard", None)
    state.get("survival", {}).pop("active_escape", None)
    state["exploration_target"] = exploration_target
    state["recent_tactic_outcomes"] = [
        {k: v for k, v in o.items() if k != "measured"} for o in list(tactic_outcomes)[-5:]
    ]
    state["assumptions"] = (
        "Structured sensing of entities currently in view with exact positions and identities; remembered entities with ages; floor cells and wall pieces only where the view has reached them; wall faces classified by the texture the player sees; heights only for sectors in view; no static map, no line specials, no lift facts out of view. Hit, damage, kill, item and secret counters are evaluator evidence and are not shown; outcome feedback carries HUD health and armor changes only."
    )
    state["standing_order"] = order
    state["order_revision"] = order_rev
    state["measurement_context"] = dict(
        distance="Doom map units; player width32. Contact<64; close64-256; medium256-768; far>=768.",
        bearing="Relative degrees: 0ahead, positive LEFT, negative RIGHT. +90left -90right.",
        time="35 game ticks per second. World continues during your calls. Aiming, movement and firing run simultaneously; controls persist until replaced or lease expires.",
        aim="Vertical aim automatic. TRIGGER fire grants permission during the control lease; the motor waits until the exact FACE enemy is currently visible and aligned before firing. FACE and TRIGGER are independent choices in this call. Hitscan stops at first solid obstruction.",
        physics="Max running speed about292 map units/second. Destination navigation follows collision-aware geometry. A gun must face its target to hit.",
        uncertainty="Entity health and keys unknown. remembered positions can be stale.",
    )
    state["weapon_context"] = {
        "pistol": "Hitscan, one bullet per shot. Accurate at modest range when aimed.",
        "shotgun": "Hitscan, seven pellets, about1shot/second. Strong under500units; pellet spread wastes shots at long range.",
        "rocket launcher": "Explosive projectile: dangerous to the player at contact range.",
        "fist": "Melee requires contact range.",
    }
    state["obstacle_context"] = (
        "Observed explosive barrels block movement and can explode if shot. Navigation routes around remembered barrel positions; destruction is unknown until observations change. Avoid firing through a barrel near the player."
    )
    state["strategy"] = (
        "Reach the exit alive. Fight visible threats while moving if useful. Use pistol or shotgun appropriately. Do not chase unneeded pickups. Explore distinct frontiers; avoid repeatedly failed destinations. Interact with doors and switches when close. Model chooses destinations; geometry supplies waypoints."
    )
    return state


def validate_answers(job, r):
    if not r.get("ok"):
        raise ValueError(str(r.get("error", "Provider failed")))
    if not isinstance(r.get("answers"), dict) or set(r["answers"]) != set(job["questions"]):
        raise ValueError("Answer keys do not match the question battery")
    decimals = (r.get("rounding") or {}).get("probabilityDecimals")
    if decimals is not None and (type(decimals) is not int or not 0 <= decimals <= 15):
        raise ValueError("Invalid declared probability rounding")
    rounding_basis = "provider-declared" if decimals is not None else "exact-sum"
    if decimals is None:
        values = [p for a in r["answers"].values() for p in a.get("probabilities", {}).values()]
        if values and all(
            type(p) in (float, int) and math.isfinite(p) and abs(p * 100 - round(p * 100)) < 1e-8
            for p in values
        ):
            decimals = 2
            rounding_basis = "adapter assumption from observed two-decimal wire values; provider did not declare precision"
    # A choice within one rounding unit of the maximum is a near tie under the declared or assumed precision, not a protocol violation (observed three times on 18 September: 0.42/0.43, 0.38/0.39, 0.47/0.48).
    argmax_tolerance = 0 if decimals is None else 10**-decimals
    r["validation"] = dict(
        probability_decimals=decimals,
        rounding_basis=rounding_basis,
        probabilities_rewritten=False,
        argmax_tolerance=argmax_tolerance,
        near_tie_accepted=[],
    )
    rounding_error = 0 if decimals is None else 0.5 * 10**-decimals
    for key, q in job["questions"].items():
        ans = r["answers"][key]
        probs = ans["probabilities"]
        if (
            ans.get("type") != q["type"]
            or ans["choice"] not in q["criteria"]
            or set(probs) != set(q["criteria"])
            or any(
                type(p) not in (float, int) or not math.isfinite(p) or p < 0 or p > 1
                for p in probs.values()
            )
            or abs(sum(probs.values()) - 1) > 1e-6 + len(probs) * rounding_error
        ):
            raise ValueError("Invalid Choice distribution: " + key)
        if max(probs.values()) > probs[ans["choice"]] + argmax_tolerance + 1e-9:
            raise ValueError("Choice is not a highest-probability option: " + key)
        if max(probs.values()) > probs[ans["choice"]] + 1e-9:
            r["validation"]["near_tie_accepted"].append(
                dict(
                    question=key,
                    choice=ans["choice"],
                    choice_probability=probs[ans["choice"]],
                    maximum=max(probs.values()),
                )
            )
