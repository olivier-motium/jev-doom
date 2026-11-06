"""Derive visible and remembered entities from a frame."""

import copy
import math

from .catalog import category
from .geometry import angle, dist


def observe_entities(frame, player, previous_memory):
    memory = dict(previous_memory)
    pos = [player["x"], player["y"]]
    label_px = {label.object_id: int(label.width * label.height) for label in frame.labels}
    visible = {oid for oid, a in label_px.items() if a >= 3}
    entities = []
    seen = set()
    # Map kill count does not identify a victim or prove player attribution.
    for o in frame.objects:
        kind = category(o.name)
        id = f"{o.name}_{o.id}"
        if not kind or o.id not in visible:
            continue
        entity = dict(
            id=id,
            label=f"{o.name} {o.id}",
            kind=kind,
            x=round(o.position_x, 1),
            y=round(o.position_y, 1),
            z=round(o.position_z, 1),
            seen_tick=frame.tic,
            visible=True,
            label_px=label_px.get(o.id),
        )
        if kind == "projectile":
            entity["velocity"] = [round(o.velocity_x * 35, 1), round(o.velocity_y * 35, 1)]
        memory[id] = entity
        seen.add(id)
    # Nearby unseen pickups are no longer actionable; this does not prove collection.
    for id, e in list(memory.items()):
        if (
            e["kind"] not in ("enemy", "obstacle")
            and id not in seen
            and dist([e["x"], e["y"]], pos) < 40
        ):
            memory.pop(id, None)
            continue
        en = copy.copy(e)
        en["visible"] = id in seen
        en["last_seen_seconds"] = round((frame.tic - e["seen_tick"]) / 35, 1)
        en["distance"] = round(dist([e["x"], e["y"]], pos))
        en["band"] = (
            "contact"
            if en["distance"] < 64
            else "close"
            if en["distance"] < 256
            else "medium"
            if en["distance"] < 768
            else "far"
        )
        en["bearing"] = round(
            angle(
                math.degrees(math.atan2(e["y"] - player["y"], e["x"] - player["x"]))
                - player["facing"]
            ),
            1,
        )
        if en["kind"] == "projectile" and en.get("velocity"):
            vx, vy = en["velocity"]
            rx, ry = en["x"] - player["x"], en["y"] - player["y"]
            vv = vx * vx + vy * vy
            t = max(0, min(3, -(rx * vx + ry * vy) / vv)) if vv else None
            en["closest_approach_seconds_if_player_stays"] = round(t, 2) if t is not None else None
            en["closest_approach_units_if_player_stays"] = (
                round(math.hypot(rx + vx * t, ry + vy * t)) if t is not None else None
            )
        if en["last_seen_seconds"] < 45:
            entities.append(en)
    return memory, entities
