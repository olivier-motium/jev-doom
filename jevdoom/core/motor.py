"""Calculate controls from a captured observation and navigation state."""

import copy
import math
from dataclasses import dataclass

from .catalog import BUTTONS
from .geometry import angle, dist


@dataclass
class MotorResult:
    buttons: list
    controls: dict
    nav: object
    use_latches: dict
    use_pulses: dict
    approach_facing_ticks: int
    events: list


def same_door(nav, x, y):
    def sides(line_id):
        lines = [line for line in nav.lines if line["id"] == line_id]
        return {line[k] for line in lines for k in ("r", "l")} - {None}

    return bool(sides(x) & sides(y))


def calculate_motor(
    a,
    observation,
    p,
    nav,
    memory,
    sim_ticks,
    stationary_since,
    control_rev,
    use_latches,
    use_pulses,
    approach_facing_ticks,
):
    # Navigation caches and counters are returned with the result, leaving inputs owned by the caller.
    nav = copy.copy(nav)
    nav.observed_lines = {key: dict(value) for key, value in nav.observed_lines.items()}
    nav.cache = dict(nav.cache)
    nav._nb_cache = dict(nav._nb_cache)
    nav._edge_cache = dict(nav._edge_cache)
    nav.used = copy.copy(nav.used)
    use_latches = dict(use_latches)
    use_pulses = dict(use_pulses)
    values = [0] * len(BUTTONS)
    overrides = []
    events = []

    def emit(kind, **data):
        events.append((kind, data))

    pos = [p["x"], p["y"]]
    face = p["facing"]
    move = a["move"]
    destination = (a["snapshot"].get("objective") or {}).get("subject")
    waypoint = None
    if destination:
        if destination.get("through"):
            rec = nav.observed_lines.get(destination["id"])
            if rec and rec.get("open_tick") is not None:
                # The selected door option carries its own walk-through: while the piece is seen open, the route runs to the far side of the doorway.
                wp = nav.waypoint(pos, destination["through"])
                if (
                    wp
                    and sim_ticks - stationary_since > 35
                    and dist(
                        pos,
                        [
                            rec["a"][0] + (rec["b"][0] - rec["a"][0]) / 2,
                            rec["a"][1] + (rec["b"][1] - rec["a"][1]) / 2,
                        ],
                    )
                    < 96
                ):
                    # Pushed toward the doorway for a second without moving: the piece is not passable now, whatever the view said; it is a wall again until seen open.
                    rec["open_tick"] = None
                    nav._reach = None
                    nav.cache.clear()
                    emit(
                        "door_push_blocked",
                        line=destination["id"],
                        game_tick=sim_ticks,
                        player=[round(p["x"], 1), round(p["y"], 1)],
                    )
                    wp = None
                if wp:
                    destination = dict(
                        destination,
                        x=destination["through"][0],
                        y=destination["through"][1],
                        walking_through=True,
                    )
                    overrides.append(
                        "Door " + destination["id"] + " seen open: walking through to its far side"
                    )
        waypoint = nav.waypoint(pos, [destination["x"], destination["y"]])
        if (
            move == "destination"
            and waypoint is None
            and dist(pos, [destination["x"], destination["y"]]) <= 160
            and nav.segment_clear(pos, [destination["x"], destination["y"]])
        ):
            waypoint = [destination["x"], destination["y"]]
            overrides.append("No cell route; straight line over seen clear floor")
        if move == "destination" and waypoint is None:
            overrides.append("Selected destination has no geometric path")
            move = "hold"
    if (a["face"] == "path" or (destination and destination.get("walking_through"))) and waypoint:
        face = math.degrees(math.atan2(waypoint[1] - p["y"], waypoint[0] - p["x"]))
    elif a["face"].startswith("door_"):
        line = next(
            (candidate for candidate in nav.lines if candidate["id"] == a["face"][5:]),
            None,
        )
        if line:
            ax, ay = line["a"]
            bx, by = line["b"]
            dx, dy = bx - ax, by - ay
            t = max(0, min(1, ((p["x"] - ax) * dx + (p["y"] - ay) * dy) / (dx * dx + dy * dy)))
            tx, ty = ax + t * dx, ay + t * dy
            face = math.degrees(math.atan2(ty - p["y"], tx - p["x"]))
    elif a["face"].startswith("recent_"):
        m = memory.get(a["face"][7:])
        if m:
            face = math.degrees(math.atan2(m["y"] - p["y"], m["x"] - p["x"]))
        else:
            overrides.append("Remembered face target no longer in memory")
    elif a["face"] == "scan_left":
        face += 45
    elif a["face"] == "scan_right":
        face -= 45
    elif a["face"] not in ("hold", "path"):
        target = next((e for e in observation["enemies"] if e["id"] == a["face"]), None)
        if target:
            face = math.degrees(math.atan2(target["y"] - p["y"], target["x"] - p["x"]))
        else:
            overrides.append("Locked face target no longer visible; no substitute")
    # Approach facing (v47): within48units of a USE line approach point the eyes turn to the line itself, whatever the arrival direction, so the USE ray can reach it; the selected destination promised "face its line and USE".
    if (
        destination
        and not destination.get("walking_through")
        and destination.get("kind") in ("door_or_switch", "exit")
        and destination.get("activation", "use") == "use"
        and a["face"] in ("path", "hold")
        and dist(pos, [destination["x"], destination["y"]]) < 48
    ):
        line = next(
            (candidate for candidate in nav.lines if candidate["id"] == destination["id"]),
            None,
        )
        if line:
            ax, ay = line["a"]
            bx, by = line["b"]
            dx, dy = bx - ax, by - ay
            t = max(0, min(1, ((p["x"] - ax) * dx + (p["y"] - ay) * dy) / (dx * dx + dy * dy)))
            face = math.degrees(math.atan2(ay + t * dy - p["y"], ax + t * dx - p["x"]))
            approach_facing_ticks += 1
            overrides.append(
                "Approach facing: turning toward "
                + destination["kind"]
                + " line "
                + destination["id"]
                + " within48units of its approach point"
            )
    turn = angle(face - p["facing"])
    values[2] = max(-7, min(7, turn))
    # One-tick check after startup: +7delta changes0degrees to353degrees (right).
    values[2] = -values[2]
    if move == "destination" and waypoint:
        d = dist(pos, waypoint)
        theta = math.atan2(waypoint[1] - p["y"], waypoint[0] - p["x"]) - math.radians(p["facing"])
        speed = min(35, d * 2)
        values[0] = math.cos(theta) * speed
        values[1] = -math.sin(theta) * speed
        if (
            destination["kind"] in ("door_or_switch", "exit")
            and destination.get("activation", "use") == "use"
            and dist(pos, [destination["x"], destination["y"]]) < 12
        ):
            values[0] = values[1] = 0
        if destination["kind"] == "enemy":
            enemy = next((e for e in observation["enemies"] if e["id"] == destination["id"]), None)
            firing_range = (
                48
                if p["weapon"] in ("fist", "chainsaw")
                else 240
                if p["weapon"] == "shotgun"
                else 384
            )
            if enemy and dist(pos, [enemy["x"], enemy["y"]]) <= firing_range:
                values[0] = values[1] = 0
                move = "enemy_in_firing_range"
                overrides.append(
                    f"Enemy approach complete within{firing_range}units; stop translation instead of chasing through target"
                )
    elif move == "back":
        values[0] = -35
    elif move == "strafe_left":
        values[1] = -35
    elif move == "strafe_right":
        values[1] = 35
    if a["dodge"] != "carry_on":
        values[0] = -35 if a["dodge"].startswith("back") else 0
        values[1] = -35 if a["dodge"].endswith("left") else 35
        overrides.append("Model dodge takes priority over movement")
    if a.get("control_rev", 0) != control_rev:
        values[0] = values[1] = 0
        move = "expired_objective"
        overrides.append(
            "Objective expired; old MOVE and DODGE released while waiting for fresh decisions"
        )
    values[3] = int(a["trigger"] == "fire")
    if values[3]:
        aimed = next(
            (
                e
                for e in observation["enemies"]
                if e["id"] == a["face"] or "recent_" + e["id"] == a["face"]
            ),
            None,
        )
        if aimed is None:
            values[3] = 0
            overrides.append("Fire permission held: exact FACE enemy is not currently visible")
        else:
            aim_error = abs(
                angle(
                    math.degrees(math.atan2(aimed["y"] - p["y"], aimed["x"] - p["x"])) - p["facing"]
                )
            )
            tolerance = 6 if p["weapon"] == "shotgun" else 3
            if aim_error > tolerance:
                values[3] = 0
                overrides.append(
                    f"Fire permission held until selected enemy is aligned within{tolerance}degrees; error{aim_error:.1f}"
                )
    eligible = min(
        observation.get("interaction_ahead_within_USE_range", []),
        key=lambda e: e["distance"],
        default=None,
    )
    if a["use"] == "use":
        if eligible is None:
            overrides.append("USE permission held: no current interactive line in reach")
        elif (
            a.get("use_target")
            and eligible["id"] != a["use_target"]
            and not same_door(nav, eligible["id"], a["use_target"])
        ):
            overrides.append(
                f"USE permission held: ray line {eligible['id']} is not the route door {a['use_target']}"
            )
        elif eligible.get("kind") == "door":
            group = eligible.get("use_group", eligible["id"])
            since = sim_ticks - use_latches.get(group, -1000)
            if eligible.get("door_passable") or eligible.get("moving"):
                overrides.append("USE permission held: door is moving or already passable")
            elif since < 35:
                overrides.append(
                    "USE permission held: door activation latch awaits observed outcome"
                )
            else:
                values[4] = 1
                use_latches[group] = sim_ticks
        else:
            values[4] = int(sim_ticks % 14 == 0)
        if values[4]:
            use_pulses[eligible["id"]] = use_pulses.get(eligible["id"], 0) + 1
            emit(
                "use_pulse",
                line=eligible["id"],
                game_tick=sim_ticks,
                opening_height=eligible.get("opening_height"),
                player=[round(p["x"], 1), round(p["y"], 1)],
                source_request=a["source_request"],
            )
    if a["weapon"] != "keep":
        values[5 + int(a["weapon"]) - 2] = 1
    controls = dict(
        FACE=dict(choice=a["face"], bearing=round(face, 1), turn=round(values[2], 1)),
        MOVE=dict(
            choice=move,
            model_choice=a["move"],
            destination=destination,
            waypoint=waypoint,
            forward=round(values[0], 1),
            right=round(values[1], 1),
        ),
        TRIGGER=bool(values[3]),
        TRIGGER_PERMISSION=a["trigger"],
        WEAPON=a["weapon"],
        USE=bool(values[4]),
        USE_PERMISSION=a["use"],
        USE_RAY_LINE=eligible["id"] if values[4] and eligible else None,
        overrides=overrides,
        source_request=a["source_request"],
    )
    if values[4] and eligible:
        nav.used[eligible["id"]] += 1
    return MotorResult(
        values, controls, nav, use_latches, use_pulses, approach_facing_ticks, events
    )
