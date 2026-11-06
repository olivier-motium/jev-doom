"""Feasible, inspectable options. Selection is Jev or a labeled fixed rule."""

import math
from shapely.geometry import LineString
from .catalog import PICKUP_WEAPON_SLOTS, owns_pickup
from .geometry import dist

MODES = ("semantic_tactics", "local")


def route_length(path):
    return sum(dist(a, b) for a, b in zip(path, path[1:]))


def choice(text, criteria):
    return dict(type="choice", instructions=text, criteria=criteria)


def projectile_sidestep(p, b):
    """Side to step, in the player frame, to leave the path of a closing projectile: away from its line, left when on the line."""
    vx, vy = b.get("velocity") or (0, 0)
    n = math.hypot(vx, vy)
    if not n:
        return None
    hx, hy = vx / n, vy / n
    rx, ry = p["x"] - b["x"], p["y"] - b["y"]
    cross = hx * ry - hy * rx
    wx, wy = (-hy, hx) if cross >= 0 else (hy, -hx)
    f = math.radians(p["facing"])
    lx, ly = -math.sin(f), math.cos(f)
    return "left" if wx * lx + wy * ly >= 0 else "right"


def closing_projectile(state):
    """The visible projectile that will pass within40units of the player between0.4and3seconds if the player stays; None otherwise."""
    balls = [
        b
        for b in state.get("projectiles", [])
        if b.get("visible", True)
        and b.get("closest_approach_units_if_player_stays") is not None
        and b["closest_approach_units_if_player_stays"] < 40
        and 0.4 <= (b.get("closest_approach_seconds_if_player_stays") or 0) <= 3
    ]
    return (
        min(balls, key=lambda x: x["closest_approach_seconds_if_player_stays"]) if balls else None
    )


def build_menu(state, nav):
    p = state["player"]
    pos = [p["x"], p["y"]]
    enemies = state["enemies"]
    nearest = min(enemies, key=lambda e: e["distance"], default=None)
    items = {
        e["id"]: e
        for e in state["items"] + state["remembered"]
        if e["kind"] in ("health", "armor", "ammo", "weapon")
    }

    def action(subject=None, face=None, move="destination", use="no_use"):
        target = face or (nearest["id"] if nearest else "path" if subject else "scan_left")
        weapon = "keep"
        if nearest:
            slot = (
                3
                if nearest["distance"] < 500
                and any(w["slot"] == 3 and w["ammo"] > 0 for w in p["carrying"])
                else 2
            )
            if slot != p["weapon_slot"] and any(
                w["slot"] == slot and w["ammo"] > 0 for w in p["carrying"]
            ):
                weapon = str(slot)
        return dict(
            face=target,
            move=move,
            dodge="carry_on",
            trigger="fire" if nearest and target == nearest["id"] else "hold_fire",
            weapon=weapon,
            use=use,
        )

    menu = []
    omitted = []
    known_open = set(state.get("completed_door_approaches", []))
    # No WAD special or tag is read: a door on the route is a seen piece whose visible texture reads as a door and which is not seen open now.
    doors = [
        dict(id=v["id"], a=v["a"], b=v["b"])
        for v in nav.observed_lines.values()
        if v["kind"] == "door" and any(not nav.span_open(v, span) for span in v["spans"])
    ]

    def add(
        id,
        label,
        kind,
        subject=None,
        face=None,
        move="destination",
        prediction=None,
        priority=50,
        needs_path=True,
    ):
        path = nav.path(pos, [subject["x"], subject["y"]]) if subject else []
        length = route_length(path)
        if subject and (not path or dist(path[-1], [subject["x"], subject["y"]]) > 32):
            if needs_path:
                omitted.append(
                    dict(id=id, reason="No path reaching the actual subject within 32 units")
                )
                return
            # A visible threat needs no route: face and fire from here. A retreat point was chosen over clear seen floor and is walked on a straight line.
            path = []
            length = dist(pos, [subject["x"], subject["y"]])
            if kind == "combat":
                move = "hold"
        if subject and kind in ("health", "armor", "weapon", "ammo", "advance"):
            blocking = [
                e["id"] for e in enemies if any(dist(q, [e["x"], e["y"]]) < 80 for q in path[1:])
            ]
            if blocking:
                omitted.append(
                    dict(
                        id=id,
                        reason="Route crosses a visible hostile within 80 units; clear or retreat first",
                        hostiles=blocking,
                    )
                )
                return
        exposure = [
            e["id"]
            for e in enemies
            if any(
                dist(q, [e["x"], e["y"]]) < 500
                and nav.walls.distance(LineString([q, [e["x"], e["y"]]])) > 0
                for q in path[:: max(1, len(path) // 10)]
            )
        ]
        interaction = next(
            (
                e
                for e in state["interaction_ahead_within_USE_range"]
                if subject and e["id"] == subject["id"]
            ),
            None,
        )
        # An interactive subject gets USE when its line is in reach; only a manual door already observed passable is walked through instead.
        use = "use" if interaction and interaction.get("needs_use", True) else "no_use"
        # A route through a closed manual door within its first segments carries USE for that door only; observed passability retires it.
        crossing = (
            [
                (i, line, LineString([a, b]).intersection(LineString([line["a"], line["b"]])))
                for i, (a, b) in enumerate(zip(path[:5], path[1:5]))
                for line in doors
                if line["id"] not in known_open
                and LineString([a, b]).intersects(LineString([line["a"], line["b"]]))
            ]
            if subject and kind not in ("exit", "door_or_switch")
            else []
        )
        route_door = (
            min(
                crossing,
                key=lambda c: (
                    c[0],
                    dist(pos, [c[2].centroid.x, c[2].centroid.y]) if not c[2].is_empty else 1e9,
                ),
            )
            if crossing
            else None
        )
        if route_door:
            use = "use"
            if (
                not nearest
                and not face
                and not route_door[2].is_empty
                and dist(pos, [route_door[2].centroid.x, route_door[2].centroid.y]) < 96
            ):
                face = "door_" + route_door[1]["id"]
        if (
            subject
            and subject["kind"] in ("exit", "door_or_switch")
            and dist(pos, [subject["x"], subject["y"]]) < 90
        ):
            face = "door_" + subject["id"]
        if (
            subject
            and subject["kind"] == "door_or_switch"
            and subject.get("open")
            and subject.get("line_a")
        ):
            # A door seen open: the option's intent is the crossing; facing follows the route through the doorway.
            prediction = dict(
                kind="door_through",
                target=subject["id"],
                a=subject["line_a"],
                b=subject["line_b"],
                text="The player crosses the seen door line while it is open",
            )
            face = "path"
        elif subject and subject["kind"] == "door_or_switch" and use == "use":
            ok = (nav.observed_lines.get(subject["id"]) or {}).get("kind")
            if ok == "door":
                prediction = dict(
                    kind="door",
                    target=subject["id"],
                    text="The seen door piece is observed open (the view passes through it) or passable within reach",
                )
            elif ok == "platform":
                prediction = dict(
                    kind="switch",
                    target=subject["id"],
                    text="Platform texture: an effect is known only if the moving floor is in view; otherwise the outcome stays unknown",
                )
            else:
                prediction = dict(
                    kind="switch",
                    target=subject["id"],
                    text="No observable effect is exposed for this face; outcome stays unknown until seen",
                )
        pred = prediction or dict(
            kind="route_progress",
            target=subject["id"],
            expected_decrease_units=max(8, length - 28),
            before_distance=round(length, 1),
            text="Reach the selected subject along the observed route",
        )
        controls = action(subject, face, move, use)
        if route_door:
            controls["use_target"] = route_door[1]["id"]
        menu.append(
            dict(
                id=id,
                label=label,
                kind=kind,
                subject=subject,
                controls=controls,
                door_on_route=route_door[1]["id"] if route_door else None,
                lift_on_route=None,
                prediction=pred,
                priority=priority,
                route_units=round(length),
                exposed_to=exposure,
                feasibility="Static box-clear path and observed barrels; dynamic danger is estimated, not guaranteed safe",
                expiry_seconds=3.0 if kind in ("combat", "guard", "retreat") else 6.0,
                completion="Prediction satisfied, target invalid, newly blocked route, or time limit",
            )
        )
        return True

    for e in state["navigation"]:
        # v48: the exit option states what activation does and the route as running seconds; ids, controls, priority and prediction unchanged.
        if e["kind"] == "exit":
            add(
                "exit_" + e["id"],
                f"Reach and activate known exit {e['id']}: USE at the line ends the level at once; route {e['distance']} units, about {round(e['distance'] / 292, 1)} s of running",
                "exit",
                e,
                prediction=dict(
                    kind="exit",
                    text="Native level-exit reward 1; otherwise record approach distance",
                ),
                priority=0,
            )
    health = []
    armor = []
    weapons = []
    ammunition = []
    slots = PICKUP_WEAPON_SLOTS
    effects = {
        "Medikit": 25,
        "Stimpack": 10,
        "HealthBonus": 1,
        "Soulsphere": 100,
        "Berserk": 100,
        "Megasphere": 200,
        "GreenArmor": 100,
        "BlueArmor": 200,
        "ArmorBonus": 1,
    }
    # Route planning is the costly step; plan only the four nearest candidates per kind by straight line.
    nearest_per_kind = {}
    for e in sorted(items.values(), key=lambda e: dist(pos, [e["x"], e["y"]])):
        nearest_per_kind.setdefault(e["kind"], []).append(e)
    budgeted = [e for group in nearest_per_kind.values() for e in group[:4]]
    for e in budgeted:
        name = e["id"].rsplit("_", 1)[0]
        path = nav.path(pos, [e["x"], e["y"]])
        length = route_length(path)
        if not path or dist(path[-1], [e["x"], e["y"]]) > 32:
            continue
        health_cap = 200 if name in ("HealthBonus", "Soulsphere", "Megasphere") else 100
        armor_cap = 200 if name in ("ArmorBonus", "BlueArmor", "Megasphere") else 100
        if name == "Megasphere":
            benefit = max(0, 200 - p["health"]) + max(0, 200 - p["armor"])
        elif e["kind"] == "health":
            benefit = min(effects.get(name, 0), max(0, health_cap - p["health"]))
        elif e["kind"] == "armor":
            benefit = (
                min(effects.get(name, 0), max(0, armor_cap - p["armor"]))
                if name == "ArmorBonus"
                else max(0, armor_cap - p["armor"])
            )
        else:
            benefit = 0
        score = benefit / max(48, length)
        if e["kind"] == "health" and p["health"] < health_cap:
            health.append((-score, e["id"], e))
        elif name == "Megasphere" and p["armor"] < 200:
            armor.append((-score, e["id"], e))
        if e["kind"] == "armor" and p["armor"] < armor_cap:
            armor.append((-score, e["id"], e))
        if e["kind"] == "weapon" and not owns_pickup(name, p["carrying"]):
            weapons.append((length, e["id"], e))
        useful_ammo = (
            name in ("Clip", "ClipBox", "Chaingun")
            and any(w["slot"] in (2, 4) and w["ammo"] < 20 for w in p["carrying"])
        ) or (
            name in ("Shell", "ShellBox", "Shotgun", "SuperShotgun")
            and any(w["slot"] == 3 and w["ammo"] < 4 for w in p["carrying"])
        )
        if useful_ammo:
            ammunition.append((length, e["id"], e))
    for options, kind in [
        (health, "health"),
        (armor, "armor"),
        (weapons, "weapon"),
        (ammunition, "ammo"),
    ]:
        for _, _, e in sorted(options):
            name = e["id"].rsplit("_", 1)[0]
            # v46: the option states the effect amount and the current level; the strategy and rubric wording are unchanged.
            amount = {
                "health": f"+{effects.get(name, 0)} health, now {p['health']}, cap {200 if name in ('HealthBonus', 'Soulsphere', 'Megasphere') else 100}",
                "armor": (
                    f"armor to {effects.get(name, 0)}"
                    if name in ("GreenArmor", "BlueArmor", "Megasphere")
                    else f"+{effects.get(name, 0)} armor"
                )
                + f", now {p['armor']}",
                "weapon": f"weapon {name} for slot {slots.get(name)}, carrying {', '.join(w['name'] for w in p['carrying'])}",
                "ammo": f"ammo {name}; current {p['weapon']} has {p['ammo']}",
            }[kind]
            if add(
                kind + "_" + e["id"],
                "Approach " + e["label"] + " (" + amount + ") while covering the route",
                kind,
                e,
                priority=(
                    2 if kind == "health" and p["health"] < 50 else 6 if kind == "armor" else 8
                ),
            ):
                break
    if nearest:
        add(
            "clear_" + nearest["id"],
            "Clear the nearest visible threat before crossing",
            "combat",
            nearest,
            prediction=dict(
                kind="target_gone",
                target=nearest["id"],
                text="The faced target leaves the view while faced; leaving view is not a confirmed kill",
            ),
            priority=3 if nearest["distance"] < 500 else 12,
            needs_path=False,
        )
        point = nav.escape_point(pos, enemies, state["projectiles"])
        if point:
            subject = dict(id="retreat_point", kind="retreat", x=point["x"], y=point["y"])
            add(
                "retreat",
                "Retreat along clear floor while aiming at " + nearest["label"],
                "retreat",
                subject,
                prediction=dict(
                    kind="separation",
                    enemy=nearest["id"],
                    enemy_position=[nearest["x"], nearest["y"]],
                    before_distance=nearest["distance"],
                    expected_increase_units=32,
                    text="Increase separation from the locked threat by 32 units; report actual damage separately",
                ),
                priority=1 if p["health"] <= 30 else 15,
                needs_path=False,
            )
    else:
        # No enemy in view but one remembered close and recent: hold facing where it was last seen, ready to fire when it shows (fire lands only on a visible, aligned target).
        threat = min(
            (
                m
                for m in state["remembered"]
                if m["kind"] == "enemy" and m["distance"] <= 256 and m["last_seen_seconds"] <= 3
            ),
            key=lambda m: m["distance"],
            default=None,
        )
        if threat:
            menu.append(
                dict(
                    id="guard_" + threat["id"],
                    label=f"Hold here facing where {threat['label']} was last seen ({threat['distance']} units, {threat['last_seen_seconds']} s ago), ready to fire when it shows",
                    kind="guard",
                    subject=None,
                    controls=dict(
                        face="recent_" + threat["id"],
                        move="hold",
                        dodge="carry_on",
                        trigger="fire",
                        weapon="keep",
                        use="no_use",
                    ),
                    prediction=dict(
                        kind="observation",
                        before_sequence=state["sequence"],
                        text="The remembered threat comes back into view or a new candidate appears",
                    ),
                    priority=12 if p["health"] <= 40 else 30,
                    route_units=0,
                    exposed_to=[],
                    feasibility="Stationary; observed memory only",
                    expiry_seconds=1.0,
                    completion="One game second or the threat reappears",
                )
            )
    # Advance options: the nearest fresh interactive line, then the nearest frontier, so an already activated switch never crowds out the way forward.
    advance = [
        e
        for e in state["navigation"]
        if e["kind"] in ("frontier", "door_or_switch") and e["failed"] < 3
    ]
    lines = [e for e in advance if e["kind"] == "door_or_switch"]
    fronts = [e for e in advance if e["kind"] == "frontier"]

    def is_used(e):
        return bool(e.get("uses"))

    fresh = [e for e in lines if not is_used(e)]
    used = [e for e in lines if is_used(e)]
    # Menu order: a door seen open, then seen door/switch/platform faces, then the frontier chosen before an interruption, then frontiers, then wall faces to test, then used lines. A wall test never crowds out the way forward.
    textured = [
        e for e in fresh if e.get("texture_kind") in ("door", "switch", "platform", "exit_sign")
    ]
    wall_tests = [e for e in fresh if e not in textured]
    resume = [e for e in fronts if e["id"] == state.get("exploration_target")]

    def offer(cands, limit):
        n = 0
        for e in cands:
            if any(c["id"] == "advance_" + e["id"] for c in menu):
                continue
            times = e.get("uses") or 0
            tk = e.get("texture_kind")
            if e["kind"] == "door_or_switch" and tk == "door":
                label = (
                    ("Door %s is seen open now: walk through before it closes" % e["id"])
                    if e.get("open")
                    else ("Open seen door %s with USE and walk through while it is open" % e["id"])
                    if not times
                    else (
                        "Door %s closed again after %d presses: press USE and walk through while it is open"
                        % (e["id"], times)
                    )
                )
            elif e["kind"] == "door_or_switch" and tk in ("switch", "exit_sign"):
                label = (
                    ("Press seen switch face %s; effect unknown until observed" % e["id"])
                    if not times
                    else (
                        "Press switch face %s again (%d presses, no effect seen yet)"
                        % (e["id"], times)
                    )
                )
            elif e["kind"] == "door_or_switch" and tk == "platform":
                label = (
                    "Press seen platform face %s; effect known only if the moving floor is in view"
                    % e["id"]
                ) + (" (%d presses)" % times if times else "")
            elif e["kind"] == "door_or_switch":
                label = (
                    "Test seen wall face %s with USE for a hidden door or switch" % e["id"]
                ) + (" (%d presses, nothing seen)" % times if times else "")
            elif e["kind"] == "frontier" and e["id"] == state.get("exploration_target"):
                label = (
                    "Resume exploring toward %s, the frontier chosen before the interruption"
                    % e["id"]
                )
            else:
                label = "Open the route and advance to " + e["id"]
            k = len(menu)
            add(
                "advance_" + e["id"],
                label,
                "advance",
                e,
                priority=8
                if e.get("open")
                else 20
                + e["failed"] * 10
                + (10 * min(times, 3) if e["kind"] == "door_or_switch" else 0),
            )
            if len(menu) > k:
                n += 1
                if n >= limit:
                    break

    offer([e for e in lines if e.get("open")], 1)
    offer(textured, 1)
    offer(resume, 1)
    offer(fronts, 1)
    offer(wall_tests, 1)
    offer(used, 1)
    if len([c for c in menu if c["kind"] == "advance"]) < 2:
        offer(fronts, 1)
    observe = dict(
        id="observe",
        label="Observe again and turn to refresh uncertain evidence",
        kind="observe",
        subject=None,
        controls=action(None, face="scan_left", move="hold"),
        prediction=dict(
            kind="observation",
            before_sequence=state["sequence"],
            text="Reveal a new visible entity or navigation candidate; a sequence increment alone is not information gain",
        ),
        priority=100,
        route_units=0,
        exposed_to=[],
        feasibility="No feasible progress option, or recent unexplained damage, or a contradicted route; stationary observation can still be dangerous",
        expiry_seconds=1.5,
        completion="New information appears or the observation window expires",
    )
    if len(menu) > 7:
        menu.sort(key=lambda t: (t["priority"], t["route_units"]))
        omitted.extend(
            dict(
                id=t["id"], reason="Eight-option menu limit; candidate remains in full observation"
            )
            for t in menu[7:]
        )
        menu = menu[:7]
    recent = state.get("recent_tactic_outcomes", [])
    contradiction = bool(
        recent
        and recent[-1]["prediction_status"] == "not_met"
        and recent[-1]["prediction"]["kind"] in ("route_progress", "door")
    )
    unexplained_damage = state.get("just_took_damage", False) and not enemies
    repeated_empty = len(recent) >= 2 and all(
        x["tactic"] == "observe"
        and x.get("measured", {}).get("new_visible") == []
        and x.get("measured", {}).get("new_navigation") == []
        for x in recent[-2:]
    )
    if not menu or ((contradiction or unexplained_damage) and not repeated_empty):
        menu.append(observe)
    else:
        omitted.append(
            dict(
                id="observe",
                reason="Fresh usable state and feasible progress; no unresolved information need, or repeated empty observations",
            )
        )
    # Activating a seen switch, door or exit face is the selected option's own intent; the motor pulses USE only when that face is within reach.
    for t_ in menu:
        if t_["kind"] == "exit" or (t_.get("subject") or {}).get("kind") in (
            "exit",
            "door_or_switch",
        ):
            t_["controls"]["use"] = "use"
    return menu, omitted


def dodge_question(state):
    """Independent choice in the same call when a visible projectile will pass close: the motor applies the chosen dodge with whichever tactic is selected; carry_on keeps the tactic movement."""
    ball = closing_projectile(state)
    if not ball:
        return None
    p = state["player"]
    clear = projectile_sidestep(p, ball) or "left"
    return choice(
        f"A visible projectile {ball['label']} will pass within {ball['closest_approach_units_if_player_stays']} units of the player in {ball['closest_approach_seconds_if_player_stays']} s if the player stays; fireballs fly about 10 units per tick and a 64-unit sidestep clears their path when time allows, hitscan cannot be dodged. The clear side by geometry is {clear}. Choose the dodge for this control lease; it combines with the selected tactic and overrides its movement for the lease.",
        {
            "carry_on": "Keep the tactic movement; no dodge",
            "left": "Sidestep left for the lease",
            "right": "Sidestep right for the lease",
            "back_left": "Dodge backward and left",
            "back_right": "Dodge backward and right",
        },
    )


def tactic_questions(menu, state=None):
    criteria = {
        t[
            "id"
        ]: f"{t['label']}. Controls {t['controls']}. Route {t['route_units']}units. Visible threats near route {t['exposed_to']}. Feasibility: {t['feasibility']}. Prediction: {t['prediction']}."
        for t in menu
    }
    dq = dodge_question(state) if state else None
    out = dict(
        progress=choice(
            "Which complete feasible tactic offers the most useful progress toward reaching the exit alive? Healing and equipment can support progress. Do not reward pointless travel or tiny pickups.",
            criteria,
        ),
        exposure=choice(
            "Which tactic best avoids immediate lethal exposure, given health, armor, visible threats and route? Static clearance is not safety. Retreat can preserve life; standing in a doorway can be dangerous.",
            criteria,
        ),
        evidence=choice(
            "Which tactic is best supported by fresh observations and measured previous outcomes? Penalize stale subjects, failed predictions and contradictions. Scores cannot recover omitted observations.",
            criteria,
        ),
        tactic=choice(
            "Choose one complete tactic. Priority: avoid OBSERVED immediate lethal exposure; when no such threat exists, make useful progress using reliable evidence. Do not choose inactivity merely because moving has hypothetical risk. Use the facts, standing order and prior outcomes. You cannot read the other rubric answers in this same call. These controls execute together; do not invent a new target or combination. Armor absorbs one third of every hit until it is used up and a shotgun ends close fights sooner than the pistol, so a 100-armor vest, a shotgun or a medikit near the route is useful progress for reaching the exit alive; a +1 bonus is a tiny pickup. Zombiemen, shotgun guys and chaingunners fire hitscan: the shot lands the instant it is fired at any distance in view, so retreating in the open or advancing past one in view does not reduce its hits; killing it, at close range with the shotgun when carried, or breaking line of sight does.",
            criteria,
        ),
    )  # v51: hitscan fact (probe C, runs/investigations/line332-v45/rubric-probe-c-result.json)  # v50: tactic instruction carries two Doom facts and the tiny-pickup definition (probe B, runs/investigations/line332-v45/rubric-probe-b-result.json)
    if dq:
        out["dodge"] = dq
    if state and state.get("order_revision", 0):
        out["fire"] = choice(
            "Given the standing order, may the selected tactic fire when its target is aligned?",
            {"allow": "Allow firing", "hold": "Do not fire"},
        )
    return out


def fixed_select(menu):
    return min(menu, key=lambda t: (t["priority"], t["route_units"], t["id"]))["id"]


def measure(option, before, after, nav, reason, elapsed):
    pred = option["prediction"]
    p = after["player"]
    bp = before["player"]
    metric = {}
    passed = False
    unknown = False
    if pred["kind"] == "route_progress":
        subject = option["subject"]
        path = nav.path([p["x"], p["y"]], [subject["x"], subject["y"]])
        remaining = route_length(path) if path else None
        decrease = pred["before_distance"] - remaining if remaining is not None else None
        metric = dict(
            remaining_route_units=round(remaining, 1) if remaining is not None else None,
            decrease_units=round(decrease, 1) if decrease is not None else None,
        )
        passed = decrease is not None and (
            decrease >= pred["expected_decrease_units"]
            or dist([p["x"], p["y"]], [subject["x"], subject["y"]]) < 28
        )
        unknown = remaining is None
    elif pred["kind"] == "target_gone":
        before_ids = {e["id"] for e in before.get("enemies", [])}
        after_ids = {e["id"] for e in after.get("enemies", [])}
        metric = dict(
            target_visible_before=pred["target"] in before_ids,
            target_visible_after=pred["target"] in after_ids,
        )
        passed = pred["target"] in before_ids and pred["target"] not in after_ids
        unknown = pred["target"] not in before_ids
    elif pred["kind"] == "separation":
        enemy = next((e for e in after["enemies"] if e["id"] == pred["enemy"]), None)
        position = [enemy["x"], enemy["y"]] if enemy else pred["enemy_position"]
        d = dist([p["x"], p["y"]], position)
        metric = dict(
            separation_units=round(d, 1),
            increase_units=round(d - pred["before_distance"], 1),
            enemy_position_current=bool(enemy),
        )
        passed = bool(enemy) and d - pred["before_distance"] >= pred["expected_increase_units"]
        unknown = not enemy
    elif pred["kind"] == "observation":
        metric = dict(
            sequence=after["sequence"],
            new_visible=sorted(
                {
                    e["id"]
                    for e in after["enemies"] + after["items"] + after.get("obstacles", [])
                    if e.get("visible", True)
                }
                - {
                    e["id"]
                    for e in before["enemies"] + before["items"] + before.get("obstacles", [])
                    if e.get("visible", True)
                }
            ),
            new_navigation=sorted(
                {e["id"] for e in after["navigation"]} - {e["id"] for e in before["navigation"]}
            ),
        )
        passed = bool(metric["new_visible"] or metric["new_navigation"])
    elif pred["kind"] == "door":
        door = next(
            (e for e in after["interaction_ahead_within_USE_range"] if e["id"] == pred["target"]),
            None,
        )
        metric = dict(
            opening_height=door.get("opening_height") if door else None, line_in_ray=bool(door)
        )
        passed = bool(door and door.get("door_passable"))
        unknown = door is None or door.get("opening_height") is None
    elif pred["kind"] == "door_through":
        ax, ay = pred["a"]
        bx, by = pred["b"]

        def side(q):
            return (bx - ax) * (q["y"] - ay) - (by - ay) * (q["x"] - ax)

        metric = dict(side_before=round(side(bp)), side_after=round(side(p)))
        passed = side(bp) * side(p) < 0
    elif pred["kind"] == "switch":
        metric = dict(evidence="none exposed")
        unknown = True
    elif pred["kind"] == "exit":
        metric = dict(terminal_reason=reason)
        passed = reason == "level_complete"
    # Four honest statuses: met, not_met (window elapsed or terminal), unknown (evidence unavailable), window_ended_early (replaced before the window with no decisive evidence).
    status = (
        "met"
        if passed
        else "unknown"
        if unknown
        else "not_met"
        if elapsed >= option["expiry_seconds"]
        or reason in ("dead", "level_complete", "invalidated")
        else "window_ended_early"
    )
    return dict(
        tactic=option["id"],
        label=option["label"],
        prediction=pred,
        measured=metric,
        prediction_status=status,
        end_reason=reason,
        game_seconds=round(elapsed, 3),
        health_change=p["health"] - bp["health"],
        armor_change=p["armor"] - bp["armor"],
        hud_loss=(bp["health"] + bp["armor"]) - (p["health"] + p["armor"]),
        from_tick=before["tick"],
        to_tick=after["tick"],
    )
