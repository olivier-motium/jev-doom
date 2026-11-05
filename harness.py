"""One real-time engine owner and one serial Jev worker. No frontend credentials."""

import copy
import hashlib
import io
import json
import math
import os
import queue
import subprocess
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import vizdoom as vzd
from PIL import Image
from shapely.geometry import LineString, Point
from navigation import Navigator, dist
from tactics import (
    MODES,
    build_menu,
    tactic_questions,
    measure,
)

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
RUNS.mkdir(exist_ok=True)
RESERVE = 0.05
CAP = float(os.getenv("JEV_BUDGET_USD", "15.00"))
if not math.isfinite(CAP) or CAP < RESERVE:
    raise ValueError("JEV_BUDGET_USD must be finite and at least 0.05")
EVALUATOR_FIELDS = (
    "hits",
    "damage_dealt",
    "kills",
    "items",
    "secrets",
    "damage_taken",
    "hits_taken",
)
CAP_ORDER = "Both providers share the cumulative charged-or-reserved budget. Unknown costs retain a 0.05 USD reservation."
BUTTONS = [
    "MOVE_FORWARD_BACKWARD_DELTA",
    "MOVE_LEFT_RIGHT_DELTA",
    "TURN_LEFT_RIGHT_DELTA",
    "ATTACK",
    "USE",
    "SELECT_WEAPON2",
    "SELECT_WEAPON3",
    "SELECT_WEAPON4",
    "SELECT_WEAPON5",
    "SELECT_WEAPON6",
    "SELECT_WEAPON7",
]
MONSTERS = {
    "DoomImp",
    "Zombieman",
    "ShotgunGuy",
    "ChaingunGuy",
    "Demon",
    "Spectre",
    "Cacodemon",
    "BaronOfHell",
    "HellKnight",
    "LostSoul",
    "Revenant",
    "Fatso",
    "Arachnotron",
    "Archvile",
    "PainElemental",
    "Cyberdemon",
    "SpiderMastermind",
    "WolfensteinSS",
}
HEALTH = {"Stimpack", "Medikit", "HealthBonus", "Soulsphere", "Megasphere", "Berserk"}
ARMOR = {"ArmorBonus", "GreenArmor", "BlueArmor"}
AMMO = {
    "Clip",
    "ClipBox",
    "Shell",
    "ShellBox",
    "RocketAmmo",
    "RocketBox",
    "Cell",
    "CellPack",
    "Backpack",
}
WEAPONS = {
    "Shotgun",
    "SuperShotgun",
    "Chaingun",
    "RocketLauncher",
    "PlasmaRifle",
    "BFG9000",
    "Chainsaw",
}
KEYS = {"BlueCard", "RedCard", "YellowCard", "BlueSkull", "RedSkull", "YellowSkull"}
WEAPON_NAMES = {
    1: "fist",
    2: "pistol",
    3: "shotgun",
    4: "chaingun",
    5: "rocket launcher",
    6: "plasma rifle",
    7: "BFG",
    8: "chainsaw",
    9: "super shotgun",
}
PICKUP_WEAPON_SLOTS = {
    "Shotgun": 3,
    "SuperShotgun": 9,
    "Chaingun": 4,
    "RocketLauncher": 5,
    "PlasmaRifle": 6,
    "BFG9000": 7,
    "Chainsaw": 8,
}
PICKUP_EFFECTS = {
    "HealthBonus": "Only +1 health; a small bonus, not a healing kit",
    "Stimpack": "+10 health up to100",
    "Medikit": "+25 health up to100",
    "Soulsphere": "+100 health up to200",
    "Megasphere": "Set health and armor to200",
    "Berserk": "Restore health to100 and strengthen fists",
    "ArmorBonus": "Only +1 armor",
    "GreenArmor": "Set armor to100",
    "BlueArmor": "Set armor to200",
    "Shotgun": "Acquire shotgun and some shells; much stronger than pistol at close/medium range",
    "SuperShotgun": "Acquire double-barrel shotgun and shells; strong close range",
    "Chaingun": "Acquire rapid-fire bullet weapon",
    "Clip": "Bullets for pistol or chaingun",
    "ClipBox": "50 bullets for pistol or chaingun",
    "Shell": "4 shotgun shells",
    "ShellBox": "20 shotgun shells",
    "RocketAmmo": "Rocket ammunition, not pistol bullets",
    "RocketBox": "Rocket ammunition, not pistol bullets",
    "Cell": "Energy ammunition, not pistol bullets",
    "CellPack": "Energy ammunition, not pistol bullets",
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def angle(x):
    return (x + 180) % 360 - 180


def category(name):
    if name in MONSTERS:
        return "enemy"
    if name == "ExplosiveBarrel":
        return "obstacle"
    if name in HEALTH:
        return "health"
    if name in ARMOR:
        return "armor"
    if name in AMMO:
        return "ammo"
    if name in WEAPONS:
        return "weapon"
    if name in KEYS:
        return "key"
    if any(x in name for x in ("Ball", "Rocket", "Plasma")):
        return "projectile"
    return None


class Harness:
    def __init__(self):
        self.commands = queue.Queue()
        self.replies = queue.Queue()
        self.jobs = queue.Queue(maxsize=1)
        self.lock = threading.RLock()
        self.status = "booting"
        self.error = None
        self.order = "Survive and advance through the level. Defeat visible threats, collect useful resources, explore unvisited routes, open doors and reach the exit."
        self.order_rev = 0
        self.config = dict(
            game="Freedoom Phase 2",
            engine=f"ViZDoom {vzd.__version__}",
            map="MAP01",
            skill=3,
            seed=17,
            revision="v64-observable-order",
            mode="observable",
            timing="real_time",
            controller="semantic_tactics",
            start_type="Fresh pistol start",
            provider="Vercel AI Gateway",
            provider_key="gateway",
            model="typesafe-ai/jev",
            budget_usd=CAP,
            assistance="Observable mode: seen floor cells and seen wall pieces only, no static map, no line specials, no lift facts out of view, no tactical assist; execution interlocks of the selected option only (fire when the faced target is visible and aligned, USE when the selected face is in reach, approach facing toward the selected face, the dodge Jev chose)",
        )
        self.ledger_path = RUNS / "budget.json"
        self.ledger = (
            json.loads(self.ledger_path.read_text())
            if self.ledger_path.exists()
            else dict(charged_usd=0, reported_usd=0, calls=0, unknown_cost_calls=0)
        )
        self.provider_ledgers = self.ledger.setdefault(
            "providers",
            {
                "gateway": {
                    k: self.ledger.get(k, 0)
                    for k in ("charged_usd", "reported_usd", "calls", "unknown_cost_calls")
                },
                "typesafe": dict(
                    charged_usd=0, reported_usd=0, calls=0, unknown_cost_calls=0, estimated_usd=0
                ),
            },
        )
        self.probes = deque(maxlen=30)
        self.provider_health = {"gateway": False, "typesafe": False}
        if CAP_ORDER not in self.ledger.setdefault("accounting_notes", []):
            self.ledger["accounting_notes"].append(CAP_ORDER)
            self.save_budget()
        self.observation = {}
        self.records = deque(maxlen=500)
        self.latencies = []
        self.jpeg = b""
        self.map = {}
        self.busy = False
        self.episode = 0
        self.seq = 0
        self.objective = None
        self.last_action = None
        self.controls = {}
        self.stop_event = threading.Event()
        self.result = {}
        self.recovery_total = 0
        self.paused_seconds = 0
        self.max_seconds = 90
        self.call_seq = 0
        # HTTP can read state before the engine finishes loading the WAD.
        self.active_tactic = None
        self.tactic_menu = []
        self.omitted_tactics = []
        self.tactic_outcomes = deque(maxlen=12)
        self.control_rev = 0
        self.worker = threading.Thread(target=self.provider_loop, daemon=True)
        self.worker.start()
        self.engine = threading.Thread(target=self.engine_loop, daemon=True)
        self.engine.start()

    def log(self, type, **data):
        event = dict(type=type, at=utc(), episode=self.episode)
        event.update(data)
        if hasattr(self, "trace"):
            self.trace.write(json.dumps(event, separators=(",", ":")) + "\n")
            self.trace.flush()
        return event

    def save_budget(self):
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ledger, indent=2))
        tmp.replace(self.ledger_path)

    def invalidate_objective(self, reason):
        self.end_tactic("invalidated")
        self.log(
            "objective_invalidated",
            objective=self.objective,
            reason=reason,
            game_tick=self.sim_ticks,
        )
        self.objective = None
        self.control_rev += 1

    def provider_loop(self):
        proc = None
        while not self.stop_event.is_set():
            job = self.jobs.get()
            if job is None:
                break
            try:
                if proc is None:
                    proc = subprocess.Popen(
                        ["node", str(ROOT / "gateway.mjs")],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        cwd=ROOT,
                    )
                self.gateway_process = proc
                start = time.monotonic()
                proc.stdin.write(
                    json.dumps(
                        {
                            "provider": job.get("provider", "gateway"),
                            "state": job["state"],
                            "questions": job["questions"],
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("Gateway adapter exited")
                result = json.loads(line)
                result["end_to_end_ms"] = (time.monotonic() - start) * 1000
            except Exception as e:
                result = {"ok": False, "error": str(e)[:300], "cost_usd": None}
            with self.lock:
                self.log(
                    "provider_return",
                    request=job["id"],
                    request_episode=job["episode"],
                    response=result,
                )
            self.replies.put((job, result))
        if proc:
            proc.terminate()

    def new_game(self):
        if hasattr(self, "game"):
            self.game.close()
        self.game = vzd.DoomGame()
        g = self.game
        g.set_doom_game_path(str(ROOT / "assets/freedoom2.wad"))
        g.set_doom_map(self.config["map"].lower())
        g.set_doom_skill(self.config["skill"])
        g.set_seed(self.config["seed"])
        g.set_window_visible(False)
        g.set_sound_enabled(False)
        g.set_screen_format(vzd.ScreenFormat.RGB24)
        g.set_screen_resolution(vzd.ScreenResolution.RES_640X480)
        g.set_labels_buffer_enabled(True)
        g.set_depth_buffer_enabled(True)
        g.set_objects_info_enabled(True)
        g.set_sectors_info_enabled(True)
        g.set_render_hud(True)
        g.set_render_weapon(True)
        g.set_available_buttons([getattr(vzd.Button, b) for b in BUTTONS])
        g.set_mode(vzd.Mode.PLAYER)
        g.set_episode_timeout(35 * 600)
        g.set_map_exit_reward(1)
        g.init()
        g.new_episode()
        self.nav = Navigator(ROOT / "assets/freedoom2.wad", self.config["map"])
        self.memory = {}
        self.previous_health = 100
        self.last_damage_tick = -1000
        self.position_history = deque(maxlen=35)
        self.last_failed_tick = -1000
        self.navigation_feedback = None
        self.objective = None
        self.last_action = None
        self.controls = {}
        self.seq = 0
        self.sim_ticks = 0
        self.start_wall = time.monotonic()
        self.last_dispatch = 0
        self.last_nav = 0
        self.distance = 0
        self.last_position = None
        self.started_play = False
        self.recovery_attempts = 0
        self.recovery_total = 0
        self.stall_total = 0
        self.retry_at = 0
        self.pause_started = None
        self.paused_seconds = 0
        self.free_run_ticks = 0
        self.use_latches = {}
        self.use_pulses = {}
        self.approach_facing_ticks = 0
        self.prediction_statuses = {}
        self.tactic_continuations = 0
        self.active_tactic = None
        self.tactic_outcomes = deque(maxlen=12)
        self.tactic_menu = []
        self.omitted_tactics = []
        self.failed_route_ticks = 0
        self.intervention_ticks = 0
        self.tactic_decisions = 0
        self.predictions_met = 0
        self.stationary_anchor = None
        self.stationary_since = 0
        self.exploration_target = None
        self.control_rev = 0
        self.capture()
        self.status = "ready"

    def begin_run(self, args):
        selected_map = str(args.get("map", self.config["map"])).upper()
        if selected_map not in ("MAP01", "MAP02"):
            self.error = "Supported maps are MAP01 and MAP02; MAP03 hangs this ViZDoom build at engine init (tested 18 September 2026), later maps untested"
            return
        provider = args.get("provider", self.config.get("provider_key", "gateway"))
        if provider not in ("gateway", "typesafe"):
            self.error = "Unknown provider"
            return
        if not self.provider_health[provider]:
            self.error = "Click Check provider before starting a run"
            return
        self.config.update(
            provider_key=provider,
            provider="TypeSafe direct" if provider == "typesafe" else "Vercel AI Gateway",
            model="jev-latest" if provider == "typesafe" else "typesafe-ai/jev",
        )
        self.config["controller"] = args.get(
            "controller", self.config.get("controller", "semantic_tactics")
        )
        if self.config["controller"] not in MODES:
            self.error = "Unknown controller"
            return
        self.config["map"] = selected_map
        seed = args.get("seed", 17)
        skill = args.get("skill", 3)
        if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
            self.error = "Seed must be an integer"
            return
        if type(skill) is not int or not 1 <= skill <= 5:
            self.error = "Skill must be an integer from 1 to 5"
            return
        self.config["seed"] = seed
        self.config["skill"] = skill
        timing = args.get("timing", "real_time")
        if timing not in ("real_time", "pause_assisted"):
            self.error = "timing must be real_time (default) or pause_assisted (labelled)"
            return
        self.config["timing"] = timing
        parts = []
        if selected_map != "MAP01":
            parts.append(selected_map)
        if skill != 3:
            parts.append("skill %d" % skill)
        if seed != 17:
            parts.append("unused seed %d" % seed)
        self.config["start_type"] = (
            "Fresh pistol start"
            if not parts
            else "Fresh pistol start on "
            + ", ".join(parts)
            + " ("
            + ("transfer check" if parts == ["unused seed %d" % seed] else "harder condition")
            + ")"
        )
        self.config["start_type"] += ", observable mode"
        if timing == "pause_assisted":
            self.config["start_type"] += ", pause-assisted timing"
        self.new_game()
        self.episode += 1
        self.max_seconds = min(600, max(5, float(args.get("seconds", 90))))
        self.config["revision"] = args.get("revision", self.config["revision"])[:40]
        self.run_dir = RUNS / (
            datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + self.config["revision"]
        )
        self.run_dir.mkdir(exist_ok=True)
        if hasattr(self, "trace"):
            self.trace.close()
        self.trace = open(self.run_dir / "trace.jsonl", "a")
        self.run_budget_start = copy.deepcopy(self.ledger)
        self.log(
            "episode_start",
            config=self.config,
            max_seconds=self.max_seconds,
        )
        self.records.clear()
        self.result = {}
        self.status = "warming"
        self.error = None

    def capture(self):
        s = self.game.get_state()
        if s is None:
            return
        g = self.game

        def var(name):
            return float(g.get_game_variable(getattr(vzd.GameVariable, name)))

        p = dict(
            x=round(var("POSITION_X"), 1),
            y=round(var("POSITION_Y"), 1),
            z=round(var("POSITION_Z"), 1),
            facing=round(var("ANGLE"), 1),
            health=int(var("HEALTH")),
            armor=int(var("ARMOR")),
            weapon=WEAPON_NAMES.get(int(var("SELECTED_WEAPON")), "unknown"),
            weapon_slot=int(var("SELECTED_WEAPON")),
            ammo=int(var("SELECTED_WEAPON_AMMO")),
            kills=int(var("KILLCOUNT")),
            items=int(var("ITEMCOUNT")),
            secrets=int(var("SECRETCOUNT")),
            hits=int(var("HITCOUNT")),
            damage_dealt=int(var("DAMAGECOUNT")),
            damage_taken=int(var("DAMAGE_TAKEN")),
            hits_taken=int(var("HITS_TAKEN")),
        )
        p["carrying"] = [
            dict(slot=i, name=WEAPON_NAMES[i], ammo=int(var("AMMO" + str(i))))
            for i in range(1, 8)
            if var("WEAPON" + str(i))
        ]
        if (self.objective or {}).get("goal") == "upgrade_weapon":
            subject = self.objective.get("subject") or {}
            slot = PICKUP_WEAPON_SLOTS.get(subject.get("id", "").rsplit("_", 1)[0])
            if slot in {w["slot"] for w in p["carrying"]} or subject.get("id") not in self.memory:
                self.log(
                    "objective_complete",
                    goal="upgrade_weapon",
                    subject=subject,
                    reason="Weapon now carried"
                    if slot in {w["slot"] for w in p["carrying"]}
                    else "Target no longer in observed memory",
                )
                self.objective = None
                self.last_action = None
                self.control_rev += 1
                self.controls = {
                    "released": True,
                    "reason": "Weapon objective finished or invalid; request fresh goal",
                }
        p["velocity"] = [round(var("VELOCITY_X") * 35, 1), round(var("VELOCITY_Y") * 35, 1)]
        pos = [p["x"], p["y"]]
        self.position_history.append(pos)
        if self.stationary_anchor is None or dist(pos, self.stationary_anchor) >= 8:
            self.stationary_anchor = pos
            self.stationary_since = self.sim_ticks
        if self.last_position:
            self.distance += dist(pos, self.last_position)
        self.last_position = pos
        label_px = {label.object_id: int(label.width * label.height) for label in s.labels}
        visible = {oid for oid, a in label_px.items() if a >= 3}
        entities = []
        seen = set()
        # Map kill count does not identify a victim or prove player attribution.
        for o in s.objects:
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
                seen_tick=s.tic,
                visible=True,
                label_px=label_px.get(o.id),
            )
            if kind == "projectile":
                entity["velocity"] = [round(o.velocity_x * 35, 1), round(o.velocity_y * 35, 1)]
            self.memory[id] = entity
            seen.add(id)
        # Nearby unseen pickups are no longer actionable; this does not prove collection.
        for id, e in list(self.memory.items()):
            if (
                e["kind"] not in ("enemy", "obstacle")
                and id not in seen
                and dist([e["x"], e["y"]], pos) < 40
            ):
                self.memory.pop(id, None)
                continue
            en = copy.copy(e)
            en["visible"] = id in seen
            en["last_seen_seconds"] = round((s.tic - e["seen_tick"]) / 35, 1)
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
                angle(math.degrees(math.atan2(e["y"] - p["y"], e["x"] - p["x"])) - p["facing"]), 1
            )
            if en["kind"] == "projectile" and en.get("velocity"):
                vx, vy = en["velocity"]
                rx, ry = en["x"] - p["x"], en["y"] - p["y"]
                vv = vx * vx + vy * vy
                t = max(0, min(3, -(rx * vx + ry * vy) / vv)) if vv else None
                en["closest_approach_seconds_if_player_stays"] = (
                    round(t, 2) if t is not None else None
                )
                en["closest_approach_units_if_player_stays"] = (
                    round(math.hypot(rx + vx * t, ry + vy * t)) if t is not None else None
                )
            if en["last_seen_seconds"] < 45:
                entities.append(en)
        subject = (self.objective or {}).get("subject") or {}
        if (
            subject.get("kind") in ("health", "ammo", "weapon", "armor", "key")
            and subject.get("id") not in self.memory
        ):
            self.invalidate_objective(
                "Pickup no longer observed after nearby approach; replan without inferring collection"
            )
        entities.sort(key=lambda e: (not e["visible"], e["distance"]))
        obstacles = [e for e in entities if e["kind"] == "obstacle"]
        self.nav.update_obstacles(obstacles)
        if (
            len(self.position_history) >= 30
            and dist(self.position_history[0], pos) < 8
            and self.last_action
            and self.last_action.get("move") == "destination"
            and s.tic - self.last_failed_tick > 105
        ):
            attempted = (self.last_action["snapshot"].get("objective") or {}).get("subject")
            # A USE target counts as failed too when the player stands at its approach point for the window with no pulse landing on it (nothing to press there).
            idle_use = (
                bool(attempted)
                and attempted.get("activation") == "use"
                and dist(pos, [attempted["x"], attempted["y"]]) <= 45
                and not self.use_pulses.get(attempted["id"])
                and self.sim_ticks - self.stationary_since > 88
            )  # measured from when the player stopped, so tactic restarts cannot reset the window
            if attempted and (dist(pos, [attempted["x"], attempted["y"]]) > 45 or idle_use):
                self.nav.failed[attempted["id"]] += 1
                self.last_failed_tick = s.tic
                self.navigation_feedback = dict(
                    target=attempted["id"],
                    reason="Destination movement commanded but less than8units displacement over about2.5seconds",
                    tick=s.tic,
                    failed_attempts=self.nav.failed[attempted["id"]],
                )
        if time.monotonic() - self.last_nav > 0.5:
            depth_map = s.depth_buffer
            cols = (
                [int(depth_map[215:226, c].min()) for c in range(0, depth_map.shape[1], 10)]
                if depth_map is not None
                else []
            )
            self.nav.observe_view(pos, p["facing"], cols, s.tic)
            for cell in self.nav.frame_seen:
                sec = self.nav.obs_cells.get(cell)
                if sec is not None:
                    self.nav.record_heights(
                        sec, -s.sectors[sec].floor_height, s.sectors[sec].ceiling_height, s.tic
                    )
            self.last_nav = time.monotonic()
            self.map = self.nav.ui()
            self.nav_candidates = self.nav.candidates(pos)
        self.seq += 1
        interactions = []

        ray = LineString(
            [
                pos,
                [
                    p["x"] + 64 * math.cos(math.radians(p["facing"])),
                    p["y"] + 64 * math.sin(math.radians(p["facing"])),
                ],
            ]
        )
        for lid, v in self.nav.observed_lines.items():
            if not v["spans"] or v.get("open_tick") is not None:
                continue
            base = LineString([v["a"], v["b"]])
            hit = None
            for s0, s1 in v["spans"]:
                a_ = base.interpolate(s0, normalized=True)
                b_ = base.interpolate(s1, normalized=True)
                x_ = ray.intersection(LineString([(a_.x, a_.y), (b_.x, b_.y)]))
                if not x_.is_empty:
                    hit = x_
                    break
            if hit is None:
                continue
            closed = lid in self.nav.frame_hits
            interactions.append(
                dict(
                    id=lid,
                    kind={
                        "switch": "switch",
                        "door": "door",
                        "platform": "lift_switch",
                        "exit_sign": "switch",
                        None: "wall",
                    }[v["kind"]],
                    texture_kind=v["kind"] or "wall",
                    texture=v.get("texture"),
                    distance=round(Point(pos).distance(hit), 1),
                    opening_height=None,
                    door_passable=(not closed) if v["kind"] == "door" else None,
                    needs_use=closed if v["kind"] == "door" else True,
                )
            )
        if p["health"] < self.previous_health:
            self.last_damage_tick = s.tic
        depth = s.depth_buffer
        walls = (
            {
                k: round(float(depth[190:230, lo:hi].mean()), 1)
                for k, lo, hi in [("left", 40, 100), ("ahead", 290, 350), ("right", 540, 600)]
            }
            if depth is not None
            else {}
        )
        remembered = [e for e in entities if not e["visible"]]
        # Reserve room for useful supplies so a crowd cannot evict every remembered medikit.
        priority = []
        for kind in ("health", "weapon", "ammo", "armor", "key"):
            priority.extend([e for e in remembered if e["kind"] == kind][:2])
        kept = {e["id"] for e in priority}
        priority.extend(
            [e for e in remembered if e["id"] not in kept][: max(0, 18 - len(priority))]
        )
        self.observation = dict(
            sequence=self.seq,
            tick=s.tic,
            captured_at=utc(),
            captured_mono=time.monotonic(),
            player=p,
            enemies=[e for e in entities if e["kind"] == "enemy" and e["visible"]],
            items=[
                e
                for e in entities
                if e["kind"] not in ("enemy", "projectile", "obstacle") and e["visible"]
            ],
            obstacles=obstacles[:8],
            projectiles=[e for e in entities if e["kind"] == "projectile" and e["visible"]],
            remembered=priority,
            walls_depth_buffer=walls,
            just_took_damage=s.tic - self.last_damage_tick < 35,
            navigation=list(self.nav_candidates),
            completed_door_approaches=[],
            navigation_feedback=self.navigation_feedback,
            interaction_ahead_within_USE_range=interactions,
            lifts=[],
            lift_assistance=None,
            objective=copy.deepcopy(self.objective),
            previous_controls=self.controls,
            progress=dict(
                game_seconds=round(self.sim_ticks / 35, 2),
                distance=round(self.distance),
                visited_cells=sum(n > 0 for n in self.nav.visits.values()),
                discovered_cells=len(self.nav.seen),
                stuck=len(self.position_history) > 15 and dist(self.position_history[0], pos) < 8,
            ),
            limits=dict(
                enemy_health="not exposed",
                kill_attribution="aggregate engine killcount; victim and player attribution unknown",
                map_assistance="seen floor cells and seen wall pieces only; frontier candidates at the edge of seen floor; wall faces classified by their visible texture; no static map geometry",
                keys="not exposed by this adapter",
            ),
        )
        self.previous_health = p["health"]
        if self.active_tactic:
            option = self.active_tactic["option"]
            target = option["subject"]
            arrived = (
                target
                and target["kind"] in ("frontier", "retreat")
                and dist(pos, [target["x"], target["y"]]) < 24
            )
            lost = option["kind"] == "combat" and not any(
                e["id"] == target["id"] for e in self.observation["enemies"]
            )
            if arrived or lost:
                self.end_tactic("completed" if arrived else "target_no_longer_visible")
                self.last_action = None
                self.control_rev += 1
                self.objective = None
        self.observation["survival"] = dict(
            stationary_seconds=round((self.sim_ticks - self.stationary_since) / 35, 2),
            escape_guard="At health30 or less, stationary under visible threat: bounded local escape may override MOVE; aiming and firing remain model-controlled",
            active_escape=None,
        )
        buf = io.BytesIO()
        Image.fromarray(s.screen_buffer).save(buf, format="JPEG", quality=82)
        self.jpeg = buf.getvalue()

    def state_for_model(self):
        """Actor context: what Jev, option generation and the motors may see. Evaluator counters are removed; outcome feedback carries statuses, not counter deltas."""
        state = copy.deepcopy(self.observation)
        state.pop("captured_mono", None)
        state["objective"] = copy.deepcopy(self.objective)
        for k in EVALUATOR_FIELDS:
            state["player"].pop(k, None)
        state["mode"] = "observable"
        state["exploration_target"] = self.exploration_target
        state["recent_tactic_outcomes"] = [
            {k: v for k, v in o.items() if k != "measured"} for o in list(self.tactic_outcomes)[-5:]
        ]
        state["assumptions"] = (
            "Structured sensing of entities currently in view with exact positions and identities; remembered entities with ages; floor cells and wall pieces only where the view has reached them; wall faces classified by the texture the player sees; heights only for sectors in view; no static map, no line specials, no lift facts out of view. Hit, damage, kill, item and secret counters are evaluator evidence and are not shown; outcome feedback carries HUD health and armor changes only."
        )
        state["standing_order"] = self.order
        state["order_revision"] = self.order_rev
        state["measurement_context"] = dict(
            distance="Doom map units; player width32. Contact<64; close64-256; medium256-768; far>=768.",
            bearing="Relative degrees: 0ahead, positive LEFT, negative RIGHT. +90left -90right.",
            time="35 game ticks per second. World continues during your calls. Aiming, movement and firing run simultaneously; controls persist until replaced or lease expires.",
            aim="Vertical aim automatic. TRIGGER fire grants permission during the control lease; the motor waits until the exact FACE enemy is currently visible and aligned before firing. FACE and TRIGGER are independent choices in this call. Hitscan stops at first solid obstruction.",
            physics="Max running speed about292 map units/second. Destination navigation follows collision-aware geometry. A gun must face its target to hit.",
            uncertainty="Entity health and keys unknown. remembered positions can be stale.",
            lifts="lifts lists lift sectors with their current floor and low/high positions. A route may cross a lift; the motor calls the lift and rides it as disclosed assistance.",
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

    def validate_answers(self, job, r):
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
                type(p) in (float, int)
                and math.isfinite(p)
                and abs(p * 100 - round(p * 100)) < 1e-8
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

    def end_tactic(self, reason):
        active = self.active_tactic
        if not active:
            return
        outcome = measure(
            active["option"],
            active["before"],
            self.observation,
            self.nav,
            reason,
            (self.sim_ticks - active["started_tick"]) / 35,
        )
        outcome.update(request=active["request"], selector=active["selector"])
        self.tactic_outcomes.append(outcome)
        if outcome["prediction_status"] == "met":
            self.predictions_met += 1
        self.prediction_statuses[outcome["prediction_status"]] = (
            self.prediction_statuses.get(outcome["prediction_status"], 0) + 1
        )
        self.log("tactic_outcome", outcome=outcome)
        self.active_tactic = None

    def cap_blocks(self):
        """Reserve the next call against the shared cumulative cap."""
        return self.ledger["charged_usd"] + RESERVE > CAP

    def reserve_call(self, provider):
        account = self.provider_ledgers[provider]
        self.ledger["charged_usd"] += RESERVE
        self.ledger["calls"] += 1
        account["charged_usd"] += RESERVE
        account["calls"] += 1
        self.save_budget()

    def request_probe(self, args):
        if self.busy or self.status in (
            "running",
            "warming",
            "waiting_provider",
            "recovering",
            "paused",
        ):
            return
        provider = args.get("provider", "gateway")
        state = args["state"]
        questions = args["questions"]
        if (
            provider not in self.provider_ledgers
            or len(json.dumps(dict(state=state, questions=questions)).encode()) > 48000
            or len(questions) > 12
        ):
            self.error = "Invalid probe request"
            return
        if self.cap_blocks():
            self.error = "Budget prevents provider probe"
            return
        self.reserve_call(provider)
        self.call_seq += 1
        now = time.monotonic()
        job = dict(
            provider=provider,
            id=self.call_seq,
            episode=self.episode,
            order_rev=self.order_rev,
            control_rev=self.control_rev,
            phase="probe",
            state=state,
            questions=questions,
            snapshot_mono=now,
            sent_mono=now,
            sent_at=utc(),
            label=str(args.get("label", "probe"))[:100],
            state_hash=hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
            questions_hash=hashlib.sha256(
                json.dumps(questions, sort_keys=True).encode()
            ).hexdigest(),
        )
        self.jobs.put(job)
        self.busy = True

    def dispatch(self):
        if self.busy or self.status not in ("warming", "running"):
            return
        now = time.monotonic()
        if now - self.last_dispatch < 0.12:
            return
        phase = "tactic"
        state = self.state_for_model()
        self.tactic_menu, self.omitted_tactics = build_menu(state, self.nav)
        state["tactic_candidates"] = self.tactic_menu
        state["omitted_tactics"] = self.omitted_tactics
        questions = tactic_questions(self.tactic_menu, state)
        if len(json.dumps({"state": state, "questions": questions}).encode()) > 48000:
            self.finish("request_limit")
            return
        if self.cap_blocks():
            self.finish("budget_limit")
            return
        self.reserve_call(self.config.get("provider_key", "gateway"))
        self.call_seq += 1
        job = dict(
            provider=self.config.get("provider_key", "gateway"),
            id=self.call_seq,
            episode=self.episode,
            order_rev=self.order_rev,
            control_rev=self.control_rev,
            phase=phase,
            state=state,
            questions=questions,
            snapshot_mono=self.observation["captured_mono"],
            sent_mono=now,
            sent_at=utc(),
        )
        self.log("request", **{k: v for k, v in job.items() if not k.endswith("_mono")})
        self.jobs.put(job)
        self.busy = True
        self.last_dispatch = now

    def accept_reply(self, job, r):
        self.busy = False
        cost = r.get("cost_usd")
        provider = job.get("provider", "gateway")
        account = self.provider_ledgers[provider]
        if (
            cost is not None
            and isinstance(cost, (float, int))
            and math.isfinite(cost)
            and cost >= 0
        ):
            self.ledger["charged_usd"] += cost - RESERVE
            self.ledger["reported_usd"] += cost
            account["charged_usd"] += cost - RESERVE
            account["reported_usd"] += cost
        else:
            self.ledger["unknown_cost_calls"] += 1
            account["unknown_cost_calls"] += 1
        estimate = r.get("estimated_cost_usd")
        if (
            estimate is not None
            and isinstance(estimate, (float, int))
            and math.isfinite(estimate)
            and estimate >= 0
        ):
            account["estimated_usd"] = account.get("estimated_usd", 0) + estimate
        self.save_budget()
        age = time.monotonic() - job["snapshot_mono"]
        self.latencies.append(r.get("elapsed_ms", 0))
        record = dict(
            provider=provider,
            id=job["id"],
            phase=job["phase"],
            tick=job["state"].get("tick", 0),
            at=utc(),
            snapshot_age_seconds=round(age, 3),
            questions=job["questions"],
            response=r,
            accepted=False,
            override_reasons=[],
        )
        if job["phase"] == "probe":
            try:
                self.validate_answers(job, r)
                record["accepted"] = True
                self.provider_health[provider] = True
            except Exception as e:
                self.provider_health[provider] = False
                record["override_reasons"].append(str(e))
            record.update(
                label=job.get("label"),
                state_hash=job.get("state_hash"),
                questions_hash=job.get("questions_hash"),
            )
            self.probes.append(record)
            with (RUNS / "provider-probes.jsonl").open("a") as f:
                f.write(json.dumps(record) + "\n")
            self.records.append(record)
            return
        validation_error = None
        if r.get("ok"):
            try:
                self.validate_answers(job, r)
            except Exception as e:
                validation_error = str(e)
        elif r.get("error_name") == "AI_InvalidResponseDataError":
            validation_error = r.get("error", "Invalid provider answer")
        if validation_error:
            record["validation_error"] = validation_error
        if job["episode"] != self.episode or self.status not in (
            "warming",
            "running",
            "waiting_provider",
        ):
            record["override_reasons"].append("Episode/order/state changed while request in flight")
        elif not r.get("ok") or validation_error:
            transient = (
                (validation_error and self.recovery_attempts < 2)
                or r.get("status") in (429, 500, 502, 503, 504, 529)
                or "timeout" in str(r.get("error", "")).lower()
            )
            if transient:
                self.recovery_attempts += 1
                self.recovery_total += 1
                self.status = "recovering"
                self.end_tactic("invalidated")
                self.last_action = None
                if (
                    self.pause_started is None
                    and self.config.get("timing", "real_time") == "pause_assisted"
                ):
                    self.pause_started = time.monotonic()
                delay = min(60, 15 * 2 ** min(self.recovery_attempts - 1, 2))
                self.retry_at = time.monotonic() + delay
                self.error = (
                    "Jev request failed. "
                    + (
                        "Game preserved and paused"
                        if self.config.get("timing", "real_time") == "pause_assisted"
                        else "World running on with controls released"
                    )
                    + f"; next fresh-state attempt in{delay}seconds. Consecutive failures:{self.recovery_attempts}."
                )
                self.controls = {
                    "released": True,
                    "reason": "Provider recovery: simulation explicitly paused"
                    if self.config.get("timing", "real_time") == "pause_assisted"
                    else "Provider recovery: controls released, world running",
                }
                self.log(
                    "recovery_pause",
                    request=job["id"],
                    error=validation_error or r.get("error"),
                    attempt=self.recovery_attempts,
                    total=self.recovery_total,
                    retry_delay_seconds=delay,
                    game_tick=self.observation["tick"],
                )
                record["override_reasons"].append(
                    "Provider failure; controls released; simulation paused for fresh-state recovery"
                )
            else:
                self.error = validation_error or r.get("error")
                self.finish("invalid_answer" if validation_error else "provider_error")
                record["override_reasons"].append(
                    "Provider failure; recovery bound exhausted or non-transient"
                )
        elif self.status == "waiting_provider":
            self.capture()
            self.status = "warming"
            record["override_reasons"].append(
                "Provider stall paused simulation; late answer discarded; fresh snapshot required"
            )
            self.log(
                "stall_answer_discarded", request=job["id"], game_tick=self.observation["tick"]
            )
        elif job["order_rev"] != self.order_rev or job.get("control_rev", 0) != self.control_rev:
            record["override_reasons"].append(
                "Order or objective expired while request in flight; request fresh state"
            )
        else:
            try:
                self.validate_answers(job, r)
                answers = r["answers"]
                if age > 2 and self.status == "running":
                    record["override_reasons"].append("Snapshot older than2seconds; discarded")
                else:
                    picks = {k: a["choice"] for k, a in answers.items()}
                    selected = picks["tactic"]
                    active = self.active_tactic
                    if (
                        active
                        and self.last_action is not None
                        and self.status == "running"
                        and active["option"]["id"] == selected
                        and (self.sim_ticks - active["started_tick"]) / 35
                        < active["option"]["expiry_seconds"]
                    ):
                        # Same option re-selected inside its window: one pending prediction continues against the original before-state.
                        active["request"] = job["id"]
                        active["rubrics"] = picks
                        self.last_action["committed_mono"] = time.monotonic()
                        self.last_action["source_request"] = job["id"]
                        self.tactic_continuations += 1
                        self.log(
                            "tactic_continue",
                            request=job["id"],
                            tactic=selected,
                            started_tick=active["started_tick"],
                            game_tick=self.sim_ticks,
                        )
                        record["accepted"] = True
                        record["composed_intent"] = {
                            k: v
                            for k, v in self.last_action.items()
                            if k not in ("snapshot", "committed_mono")
                        }
                        self.records.append(record)
                        self.log("response", record=record)
                        return
                    self.end_tactic("replaced")
                    option = copy.deepcopy(
                        next(t for t in job["state"]["tactic_candidates"] if t["id"] == selected)
                    )
                    if picks.get("dodge") in (
                        "left",
                        "right",
                        "back_left",
                        "back_right",
                        "carry_on",
                    ):
                        option["controls"]["dodge"] = picks["dodge"]
                    subject = option["subject"]
                    self.objective = dict(
                        goal={
                            "weapon": "upgrade_weapon",
                            "health": "restore_health",
                            "armor": "add_armor",
                            "combat": "kill_enemies",
                            "exit": "reach_exit",
                        }.get(option["kind"], option["kind"]),
                        subject=subject,
                        committed_tick=self.observation["tick"],
                        request=job["id"],
                    )
                    # The exploration objective persists across combat and pickups: the last frontier Jev chose is offered again as a resume option while it is still a frontier.
                    if (
                        option["kind"] == "advance"
                        and subject
                        and subject.get("kind") == "frontier"
                    ):
                        self.exploration_target = subject["id"]
                    elif option["kind"] == "advance" and subject:
                        self.exploration_target = None
                    snapshot = copy.deepcopy(job["state"])
                    snapshot["objective"] = copy.deepcopy(self.objective)
                    self.last_action = dict(
                        **option["controls"],
                        source_request=job["id"],
                        source_tick=job["state"]["tick"],
                        control_rev=self.control_rev,
                        committed_mono=time.monotonic(),
                        snapshot=snapshot,
                    )
                    self.active_tactic = dict(
                        option=option,
                        before=copy.deepcopy(self.observation),
                        started_tick=self.sim_ticks,
                        request=job["id"],
                        rubrics=picks,
                        selector=self.config["controller"],
                    )
                    self.tactic_decisions += 1
                    self.log("tactic_start", **self.active_tactic)
                    if self.status == "warming":
                        self.status = "running"
                        self.error = None
                        if not self.started_play:
                            self.start_wall = time.monotonic()
                            self.started_play = True
                        if self.pause_started is not None:
                            elapsed = time.monotonic() - self.pause_started
                            self.paused_seconds += elapsed
                            self.pause_started = None
                            self.log(
                                "simulation_resumed",
                                pause_seconds=round(elapsed, 3),
                                game_tick=self.observation["tick"],
                            )
                        self.recovery_attempts = 0
                    record["accepted"] = True
                    record["composed_intent"] = copy.deepcopy(
                        {
                            k: v
                            for k, v in self.last_action.items()
                            if k not in ("snapshot", "committed_mono")
                        }
                    )
            except Exception as e:
                self.error = str(e)
                self.finish("invalid_answer")
                record["override_reasons"].append(str(e))
        self.records.append(record)
        self.log("response", record=record)
        if self.result and self.result.get("episode") == job["episode"]:
            self.result["budget"] = copy.deepcopy(self.ledger)
            self.result["cost_final"] = True
            self.result["pending_reservation_usd"] = 0
            self.result.update(self.episode_costs())
            (self.run_dir / "result.json").write_text(json.dumps(self.result, indent=2))

    def motor(self):
        values = [0] * len(BUTTONS)
        overrides = []
        a = self.last_action
        p = self.observation.get("player", {})
        if self.status != "running" or not a:
            return values
        if (
            self.active_tactic
            and (self.sim_ticks - self.active_tactic["started_tick"]) / 35
            >= self.active_tactic["option"]["expiry_seconds"]
        ):
            self.end_tactic("expired")
            self.last_action = None
            self.controls = {
                "released": True,
                "reason": "Tactic execution window expired; await fresh decision",
            }
            return values
        if time.monotonic() - a["committed_mono"] > 2:
            self.controls = dict(released=True, reason="Control lease expired")
            return values
        p = copy.copy(p)
        p.update(
            x=float(self.game.get_game_variable(vzd.GameVariable.POSITION_X)),
            y=float(self.game.get_game_variable(vzd.GameVariable.POSITION_Y)),
            facing=float(self.game.get_game_variable(vzd.GameVariable.ANGLE)),
        )
        pos = [p["x"], p["y"]]
        face = p["facing"]
        move = a["move"]
        destination = (a["snapshot"].get("objective") or {}).get("subject")
        waypoint = None
        if destination:
            if destination.get("through"):
                rec = self.nav.observed_lines.get(destination["id"])
                if rec and rec.get("open_tick") is not None:
                    # The selected door option carries its own walk-through: while the piece is seen open, the route runs to the far side of the doorway.
                    wp = self.nav.waypoint(pos, destination["through"])
                    if (
                        wp
                        and self.sim_ticks - self.stationary_since > 35
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
                        self.nav._reach = None
                        self.nav.cache.clear()
                        self.log(
                            "door_push_blocked",
                            line=destination["id"],
                            game_tick=self.sim_ticks,
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
                            "Door "
                            + destination["id"]
                            + " seen open: walking through to its far side"
                        )
            waypoint = self.nav.waypoint(pos, [destination["x"], destination["y"]])
            if (
                move == "destination"
                and waypoint is None
                and dist(pos, [destination["x"], destination["y"]]) <= 160
                and self.nav.segment_clear(pos, [destination["x"], destination["y"]])
            ):
                waypoint = [destination["x"], destination["y"]]
                overrides.append("No cell route; straight line over seen clear floor")
            if move == "destination" and waypoint is None:
                overrides.append("Selected destination has no geometric path")
                move = "hold"
        if (
            a["face"] == "path" or (destination and destination.get("walking_through"))
        ) and waypoint:
            face = math.degrees(math.atan2(waypoint[1] - p["y"], waypoint[0] - p["x"]))
        elif a["face"].startswith("door_"):
            line = next(
                (candidate for candidate in self.nav.lines if candidate["id"] == a["face"][5:]),
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
            m = self.memory.get(a["face"][7:])
            if m:
                face = math.degrees(math.atan2(m["y"] - p["y"], m["x"] - p["x"]))
            else:
                overrides.append("Remembered face target no longer in memory")
        elif a["face"] == "scan_left":
            face += 45
        elif a["face"] == "scan_right":
            face -= 45
        elif a["face"] not in ("hold", "path"):
            target = next((e for e in self.observation["enemies"] if e["id"] == a["face"]), None)
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
                (candidate for candidate in self.nav.lines if candidate["id"] == destination["id"]),
                None,
            )
            if line:
                ax, ay = line["a"]
                bx, by = line["b"]
                dx, dy = bx - ax, by - ay
                t = max(0, min(1, ((p["x"] - ax) * dx + (p["y"] - ay) * dy) / (dx * dx + dy * dy)))
                face = math.degrees(math.atan2(ay + t * dy - p["y"], ax + t * dx - p["x"]))
                self.approach_facing_ticks += 1
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
            theta = math.atan2(waypoint[1] - p["y"], waypoint[0] - p["x"]) - math.radians(
                p["facing"]
            )
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
                enemy = next(
                    (e for e in self.observation["enemies"] if e["id"] == destination["id"]), None
                )
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
        if a.get("control_rev", 0) != self.control_rev:
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
                    for e in self.observation["enemies"]
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
                        math.degrees(math.atan2(aimed["y"] - p["y"], aimed["x"] - p["x"]))
                        - p["facing"]
                    )
                )
                tolerance = 6 if p["weapon"] == "shotgun" else 3
                if aim_error > tolerance:
                    values[3] = 0
                    overrides.append(
                        f"Fire permission held until selected enemy is aligned within{tolerance}degrees; error{aim_error:.1f}"
                    )
        eligible = min(
            self.observation.get("interaction_ahead_within_USE_range", []),
            key=lambda e: e["distance"],
            default=None,
        )
        if a["use"] == "use":
            if eligible is None:
                overrides.append("USE permission held: no current interactive line in reach")
            elif (
                a.get("use_target")
                and eligible["id"] != a["use_target"]
                and not self.same_door(eligible["id"], a["use_target"])
            ):
                overrides.append(
                    f"USE permission held: ray line {eligible['id']} is not the route door {a['use_target']}"
                )
            elif eligible.get("kind") == "door":
                group = eligible.get("use_group", eligible["id"])
                since = self.sim_ticks - self.use_latches.get(group, -1000)
                if eligible.get("door_passable") or eligible.get("moving"):
                    overrides.append("USE permission held: door is moving or already passable")
                elif since < 35:
                    overrides.append(
                        "USE permission held: door activation latch awaits observed outcome"
                    )
                else:
                    values[4] = 1
                    self.use_latches[group] = self.sim_ticks
            else:
                values[4] = int(self.sim_ticks % 14 == 0)
            if values[4]:
                self.use_pulses[eligible["id"]] = self.use_pulses.get(eligible["id"], 0) + 1
                self.log(
                    "use_pulse",
                    line=eligible["id"],
                    game_tick=self.sim_ticks,
                    opening_height=eligible.get("opening_height"),
                    player=[round(p["x"], 1), round(p["y"], 1)],
                    source_request=a["source_request"],
                )
        if a["weapon"] != "keep":
            values[5 + int(a["weapon"]) - 2] = 1
        self.controls = dict(
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
            self.nav.used[eligible["id"]] += 1
        return values

    def same_door(self, x, y):
        def sides(line_id):
            lines = [line for line in self.nav.lines if line["id"] == line_id]
            return {line[k] for line in lines for k in ("r", "l")} - {None}

        return bool(sides(x) & sides(y))

    def episode_costs(self):
        if not hasattr(self, "run_budget_start"):
            return {}
        provider = self.config.get("provider_key", "gateway")
        before = self.run_budget_start["providers"][provider]
        after = self.provider_ledgers[provider]
        return dict(
            reported_episode_cost_usd=self.ledger["reported_usd"]
            - self.run_budget_start["reported_usd"],
            episode_provider_calls=self.ledger["calls"] - self.run_budget_start["calls"],
            provider_episode_cost=dict(
                provider=provider,
                charged_or_reserved_usd=after["charged_usd"] - before["charged_usd"],
                reported_usd=after["reported_usd"] - before["reported_usd"],
                estimated_usd=after.get("estimated_usd", 0) - before.get("estimated_usd", 0),
                basis="Direct estimates use public input-token pricing; missing actual charges retain0.05USD reservations",
            ),
        )

    def finish(self, reason):
        if self.status not in ("running", "warming", "paused", "recovering", "waiting_provider"):
            return
        if reason == "dead" and self.observation.get("player"):
            for key, var in [
                ("health", "HEALTH"),
                ("armor", "ARMOR"),
                ("damage_taken", "DAMAGE_TAKEN"),
                ("hits_taken", "HITS_TAKEN"),
            ]:
                self.observation["player"][key] = int(
                    self.game.get_game_variable(getattr(vzd.GameVariable, var))
                )
        self.end_tactic(reason)
        self.status = reason
        self.controls = dict(released=True, reason=reason)
        self.last_action = None
        self.result = dict(
            reason=reason,
            at=utc(),
            episode=self.episode,
            config=self.config,
            game_seconds=round(getattr(self, "sim_ticks", 0) / 35, 2),
            wall_seconds=round(time.monotonic() - getattr(self, "start_wall", time.monotonic()), 2),
            player=self.observation.get("player"),
            progress=self.observation.get("progress"),
            budget=copy.deepcopy(self.ledger),
        )
        self.result["terminal_evidence"] = dict(
            finished=self.game.is_episode_finished(),
            dead=self.game.is_player_dead(),
            timeout=self.game.is_episode_timeout_reached(),
            last_reward=self.game.get_last_reward(),
            total_reward=self.game.get_total_reward(),
            configured_map_exit_reward=self.game.get_map_exit_reward(),
        )
        self.result["paused_seconds"] = round(
            self.paused_seconds
            + (time.monotonic() - self.pause_started if self.pause_started is not None else 0),
            3,
        )
        self.result["recovery_attempts"] = self.recovery_total
        self.result["provider_stalls"] = self.stall_total
        self.result["timing_mode"] = (
            "Real time: the world ran on with controls released during provider stalls and recoveries; no pause for inference"
            if self.config.get("timing", "real_time") == "real_time"
            else "Pause-assisted (labelled): simulation paused on provider/control-lease stalls and during bounded backoff"
        )
        self.result["free_run_seconds"] = round(self.free_run_ticks / 35, 2)
        self.result["cost_final"] = not self.busy
        self.result["pending_reservation_usd"] = RESERVE if self.busy else 0
        self.result["controller_metrics"] = dict(
            controller=self.config.get("controller", "semantic_tactics"),
            failed_route_seconds=round(self.failed_route_ticks / 35, 3),
            motor_intervention_seconds=round(self.intervention_ticks / 35, 3),
            tactic_decisions=self.tactic_decisions,
            predictions_met=self.predictions_met,
            prediction_statuses=dict(self.prediction_statuses),
            tactic_continuations=self.tactic_continuations,
            damage_taken=self.observation.get("player", {}).get("damage_taken"),
            measurement="Native damage counter; failed-route time means commanded destination with stuck displacement; intervention time means a named motor override",
        )
        self.result["boundary"] = dict(
            mode="observable",
            timing=self.config.get("timing", "real_time"),
            geometry_source="seen floor cells and seen wall pieces from the depth buffer",
            line_semantics="visible face textures, effects observed",
            lift_facts="none out of view; heights only for sectors in view",
            outcome_feedback="HUD health and armor changes only",
            evaluator_fields_hidden_from_actor=list(EVALUATOR_FIELDS),
            uninterrupted_real_time=(
                self.paused_seconds == 0
                and self.pause_started is None
                and self.stall_total == 0
                and self.recovery_total == 0
            ),
            timing_note="Real-time mode advances with released controls during provider stalls and recovery; pause-assisted mode stops simulation during those states",
        )
        self.result["approach_facing"] = dict(
            game_seconds=round(self.approach_facing_ticks / 35, 2),
            attribution="Motor facing toward the selected exit or switch line within48units of its approach point; the selection, USE-range rule and exit-activation gate are unchanged",
        )
        self.result["doors"] = dict(
            use_pulses=sum(self.use_pulses.values()),
            use_pulses_by_line=dict(self.use_pulses),
            measurement="Applied USE button ticks by nearest ray line, from use_pulse trace events; a pulse does not prove activation",
        )
        self.result.update(self.episode_costs())
        self.log("episode_end", result=self.result)
        if hasattr(self, "run_dir"):
            (self.run_dir / "result.json").write_text(json.dumps(self.result, indent=2))

    def engine_loop(self):
        try:
            self.new_game()
            deadline = time.monotonic()
            while not self.stop_event.is_set():
                with self.lock:
                    while not self.commands.empty():
                        cmd, args = self.commands.get()
                        if cmd == "start":
                            if self.busy or self.status in (
                                "running",
                                "warming",
                                "recovering",
                                "waiting_provider",
                            ):
                                continue
                            self.begin_run(args)
                        elif cmd == "probe":
                            self.request_probe(args)
                        elif cmd == "stop":
                            self.finish("stopped")
                        elif cmd == "pause" and self.status in (
                            "running",
                            "warming",
                            "recovering",
                            "waiting_provider",
                        ):
                            self.status = "paused"
                            self.pause_started = time.monotonic()
                            self.last_action = None
                            self.game.set_action([0] * len(BUTTONS))
                            self.controls = {"released": True, "reason": "paused"}
                        elif cmd == "resume" and self.status == "paused":
                            self.capture()
                            self.status = "warming"
                        elif cmd == "orders":
                            self.order = str(args["text"])[:1500]
                            self.order_rev += 1
                            self.last_action = None
                            self.log("orders", text=self.order, revision=self.order_rev)
                    while not self.replies.empty():
                        self.accept_reply(*self.replies.get())
                    if self.status == "recovering" and time.monotonic() >= self.retry_at:
                        self.capture()
                        self.status = "warming"
                        self.log(
                            "recovery_request",
                            attempt=self.recovery_attempts,
                            game_tick=self.observation["tick"],
                        )
                    if (
                        self.status == "running"
                        and self.busy
                        and time.monotonic() - self.last_dispatch > 2
                    ):
                        self.status = "waiting_provider"
                        self.end_tactic("invalidated")
                        self.last_action = None
                        self.stall_total += 1
                        self.game.set_action([0] * len(BUTTONS))
                        if (
                            self.pause_started is None
                            and self.config.get("timing", "real_time") == "pause_assisted"
                        ):
                            self.pause_started = time.monotonic()
                        self.controls = {
                            "released": True,
                            "reason": "Provider stalled over2seconds; "
                            + (
                                "simulation explicitly paused"
                                if self.config.get("timing", "real_time") == "pause_assisted"
                                else "controls released, world running"
                            ),
                        }
                        self.error = (
                            "Control lease expired while awaiting Jev. "
                            + (
                                "Simulation paused until the call settles"
                                if self.config.get("timing", "real_time") == "pause_assisted"
                                else "The world runs on with controls released"
                            )
                            + ", then fresh state is requested."
                        )
                        self.log(
                            "provider_stall_pause",
                            game_tick=self.observation["tick"],
                            stall=self.stall_total,
                        )
                    if self.status == "running":
                        buttons = self.motor()
                        if self.controls.get("MOVE", {}).get(
                            "model_choice"
                        ) == "destination" and self.observation.get("progress", {}).get("stuck"):
                            self.failed_route_ticks += 1
                        if self.controls.get("overrides"):
                            self.intervention_ticks += 1
                        self.game.make_action(buttons, 1)
                        self.sim_ticks += 1
                        if self.game.is_episode_finished():
                            self.finish(
                                "dead"
                                if self.game.is_player_dead()
                                else "engine_timeout"
                                if self.game.is_episode_timeout_reached()
                                else "level_complete"
                                if self.game.get_last_reward() >= 1
                                else "episode_ended_unverified"
                            )
                        elif self.game.is_player_dead():
                            self.capture()
                            self.finish("dead")
                        elif self.sim_ticks / 35 >= self.max_seconds:
                            self.capture()
                            self.finish("duration_limit")
                        elif self.sim_ticks % 2 == 0:
                            self.capture()
                            if self.sim_ticks % 14 == 0:
                                self.log(
                                    "execution",
                                    tick=self.observation["tick"],
                                    player=self.observation["player"],
                                    controls=self.controls,
                                    progress=self.observation["progress"],
                                )
                    elif (
                        self.config.get("timing", "real_time") == "real_time"
                        and self.status in ("recovering", "waiting_provider", "warming")
                        and self.started_play
                        and not self.game.is_episode_finished()
                    ):
                        # Real time: the world runs on with released controls while a decision is awaited; only the assisted timing mode may pause it.
                        self.game.make_action([0] * len(BUTTONS), 1)
                        self.sim_ticks += 1
                        self.free_run_ticks += 1
                        if self.game.is_episode_finished():
                            self.finish(
                                "dead"
                                if self.game.is_player_dead()
                                else "engine_timeout"
                                if self.game.is_episode_timeout_reached()
                                else "level_complete"
                                if self.game.get_last_reward() >= 1
                                else "episode_ended_unverified"
                            )
                        elif self.game.is_player_dead():
                            self.capture()
                            self.finish("dead")
                        elif self.sim_ticks / 35 >= self.max_seconds:
                            self.capture()
                            self.finish("duration_limit")
                        elif self.sim_ticks % 2 == 0:
                            self.capture()
                    self.dispatch()
                deadline += 1 / 35
                time.sleep(max(0, deadline - time.monotonic()))
                if deadline < time.monotonic() - 0.2:
                    deadline = time.monotonic()
        except Exception as e:
            traceback.print_exc()
            self.error = str(e)
            try:
                self.finish("engine_error")
            except Exception:
                pass
            self.status = "engine_error"
            # A game failure must not strand billing or discard a paid response.
            if self.busy:
                try:
                    self.accept_reply(*self.replies.get(timeout=21))
                except queue.Empty:
                    self.log("provider_unsettled_after_engine_error", reserved_usd=RESERVE)
        finally:
            if hasattr(self, "game"):
                self.game.close()

    def shutdown(self):
        self.stop_event.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        if hasattr(self, "gateway_process"):
            self.gateway_process.terminate()
        self.engine.join(timeout=3)

    def view(self):
        with self.lock:
            calls = list(self.records)
            lat = self.latencies[-300:]
            return dict(
                probes=list(self.probes),
                provider_health=self.provider_health,
                auth_providers={
                    "gateway": bool(os.getenv("AI_GATEWAY_API_KEY")),
                    "typesafe": bool(os.getenv("TYPESAFE_API_KEY")),
                },
                tactics=dict(
                    active=self.active_tactic,
                    menu=self.tactic_menu,
                    omitted=self.omitted_tactics,
                    outcomes=list(self.tactic_outcomes),
                ),
                status=self.status,
                error=self.error,
                config=self.config,
                episode=self.episode,
                order=self.order,
                order_revision=self.order_rev,
                observation=self.observation,
                objective=self.objective,
                controls=self.controls,
                map=self.map,
                records=calls[-30:],
                busy=self.busy,
                budget=self.ledger,
                latency=dict(
                    last_ms=lat[-1] if lat else None,
                    avg_ms=sum(lat) / len(lat) if lat else None,
                    p95_ms=sorted(lat)[int(len(lat) * 0.95)] if lat else None,
                    samples=lat,
                ),
                result=self.result,
                run_path=str(getattr(self, "run_dir", "")),
                recovery=dict(
                    total=self.recovery_total,
                    stalls=getattr(self, "stall_total", 0),
                    paused_seconds=round(self.paused_seconds, 3),
                    simulation_paused=self.status == "paused"
                    or (
                        self.status in ("recovering", "warming", "waiting_provider")
                        and (
                            self.config.get("timing") == "pause_assisted"
                            or not getattr(self, "started_play", False)
                        )
                    ),
                ),
                auth_available=bool(
                    os.getenv(
                        "TYPESAFE_API_KEY"
                        if self.config.get("provider_key") == "typesafe"
                        else "AI_GATEWAY_API_KEY"
                    )
                ),
            )
