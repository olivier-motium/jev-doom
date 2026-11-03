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
from navigation import Navigator, dist, MANUAL_DOORS, line_kind
from tactics import (
    MODES,
    build_menu,
    tactic_questions,
    fixed_select,
    measure,
    investigation_questions,
)

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
RUNS.mkdir(exist_ok=True)
RESERVE = 0.05
CAP = float(os.getenv("JEV_BUDGET_USD", "15.00"))
if not math.isfinite(CAP) or CAP < RESERVE:
    raise ValueError("JEV_BUDGET_USD must be finite and at least 0.05")
ASSISTS = (
    "escape_guard",
    "threat_turn",
    "cover_seek",
    "barrel_clearance",
    "lift_assist",
    "exit_activation",
)
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


def choice(instructions, criteria):
    return dict(type="choice", instructions=instructions, criteria=criteria)


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
            assists=[],
            controller="channels",
            start_type="Fresh pistol start",
            provider="Vercel AI Gateway",
            provider_key="gateway",
            model="typesafe-ai/jev",
            budget_usd=CAP,
            assistance="Observable mode: structured visible entities, seen floor cells and wall pieces, remembered observations; no static-map route access and no tactical assists",
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
        self.last_goal = -100
        self.call_seq = 0
        # HTTP can read state before the engine finishes loading the WAD.
        self.active_tactic = None
        self.tactic_menu = []
        self.omitted_tactics = []
        self.tactic_outcomes = deque(maxlen=12)
        self.investigation = None
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
        self.last_goal = -100
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
        self.nav = Navigator(
            ROOT / "assets/freedoom2.wad",
            self.config["map"],
            observable=self.config.get("mode", "observable") == "observable",
        )
        self.memory = {}
        self.previous_health = 100
        self.last_damage_tick = -1000
        self.position_history = deque(maxlen=35)
        self.last_failed_tick = -1000
        self.navigation_feedback = None
        self.objective = None
        self.last_action = None
        self.controls = {}
        self.last_goal = -100
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
        self.continued_from = None
        self.last_checkpoint_tick = 0
        self.door_observations = {}
        self.use_latches = {}
        self.use_pulses = {}
        self.lift_observations = {}
        self.lift_hold = None
        self.lift_assist_ticks = 0
        self.lift_calls = 0
        self.lift_lowers = 0
        self.threat_turn = None
        self.threat_turn_ticks = 0
        self.threat_turns = 0
        self.approach_facing_ticks = 0
        self.prediction_statuses = {}
        self.tactic_continuations = 0
        self.barrel_clear = None
        self.barrel_clear_ticks = 0
        self.barrel_clears = 0
        self.cover = None
        self.cover_ticks = 0
        self.covers = 0
        self.cover_hold = None
        self.cover_holding = False
        self.cover_holds = 0
        self.cover_hold_logged = None
        if self.investigation is None:
            previous = sorted((RUNS / "investigations").glob("*.json"))
            self.investigation = json.loads(previous[-1].read_text()) if previous else None
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
        self.escape = None
        self.escape_count = 0
        self.escape_ticks = 0
        self.control_rev = 0
        self.capture()
        self.status = "ready"

    def checkpoint(self):
        if self.game.is_episode_finished() or self.game.is_player_dead():
            return
        native = RUNS / "checkpoint.zds"
        staging = RUNS / "checkpoint-next.zds"
        native_arg = min((str(staging), os.path.relpath(staging)), key=len)
        if len(native_arg.encode()) > 100:
            raise ValueError("Native save path exceeds the engine command limit")
        self.game.set_action([0] * len(BUTTONS))
        if staging.exists():
            staging.unlink()
        before = self.game.get_state().tic
        self.game.save(native_arg)
        # save() advances two native ticks without refreshing the public state.
        self.game.advance_action(1)
        current = self.game.get_state()
        self.sim_ticks += max(0, current.tic - before) if current is not None else 3
        self.last_checkpoint_tick = self.sim_ticks
        if self.game.is_episode_finished() or self.game.is_player_dead():
            self.finish(
                "dead"
                if self.game.is_player_dead()
                else "engine_timeout"
                if self.game.is_episode_timeout_reached()
                else "level_complete"
                if self.game.get_last_reward() >= 1
                else "episode_ended_unverified"
            )
            return
        self.capture()
        if not staging.exists() or staging.stat().st_size < 1000:
            self.log(
                "checkpoint_failed",
                reason="Engine did not create native save; prior checkpoint retained",
                game_tick=self.observation["tick"],
            )
            return
        staging.replace(native)
        data = dict(
            at=utc(),
            source_run=str(getattr(self, "run_dir", "")),
            config=self.config,
            sim_ticks=self.sim_ticks,
            tick=self.game.get_state().tic,
            player=self.observation["player"],
            memory=self.memory,
            visits=[[x, y, n] for (x, y), n in self.nav.visits.items() if n > 0],
            seen=list(self.nav.seen),
            trail=self.nav.trail,
            failed=dict(self.nav.failed),
            used=dict(self.nav.used),
            distance=self.distance,
            order=self.order,
            order_rev=self.order_rev,
        )
        data["known_exits"] = sorted(self.nav.known_exits)
        tmp = RUNS / "checkpoint.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(RUNS / "checkpoint.json")
        self.last_checkpoint_tick = self.sim_ticks
        self.log("checkpoint_saved", game_tick=data["tick"], path=str(RUNS / "checkpoint.zds"))

    def begin_run(self, args, restore=False):
        if restore and not (RUNS / "checkpoint.zds").is_file():
            self.status = "checkpoint_error"
            self.error = "Native saved game is missing; load canceled before any Jev dispatch"
            self.controls = {"released": True, "reason": self.error}
            return
        saved = json.loads((RUNS / "checkpoint.json").read_text()) if restore else None
        selected_map = (
            saved["config"]["map"] if restore else str(args.get("map", self.config["map"])).upper()
        )
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
            "controller", self.config.get("controller", "channels")
        )
        if self.config["controller"] not in MODES:
            self.error = "Unknown controller"
            return
        self.config["map"] = selected_map
        self.config["start_type"] = (
            "Native checkpoint continuation" if restore else "Fresh pistol start"
        )
        if not restore:
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
            mode = args.get("mode", "observable")
            timing = args.get("timing", "real_time")
            if mode not in ("observable", "assisted"):
                self.error = "mode must be observable (default) or assisted (historical)"
                return
            if timing not in ("real_time", "pause_assisted"):
                self.error = "timing must be real_time (default) or pause_assisted (labelled)"
                return
            assists = args.get("assists", [] if mode == "observable" else list(ASSISTS))
            if not isinstance(assists, list) or not set(assists) <= set(ASSISTS):
                self.error = "assists must be a list drawn from " + ", ".join(ASSISTS)
                return
            if mode == "observable" and assists:
                self.error = (
                    "observable mode has no tactical assists; select mode=assisted to use them"
                )
                return
            self.config["mode"] = mode
            self.config["timing"] = timing
            self.config["assists"] = list(assists)
            self.config["assistance"] = (
                "Observable mode: seen floor cells and seen wall pieces only, no static map, no line specials, no lift facts out of view, no tactical assist; execution interlocks of the selected option only (fire when the faced target is visible and aligned, USE when the selected face is in reach, approach facing toward the selected face, the dodge Jev chose)"
                if mode == "observable"
                else "Assisted mode (historical): static map navigation plus observed barrels; assists as named: "
                + ", ".join(assists)
            )
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
            self.config["start_type"] += (
                ", observable mode" if mode == "observable" else ", assisted mode (historical)"
            )
            if timing == "pause_assisted":
                self.config["start_type"] += ", pause-assisted timing"
        self.new_game()
        self.episode += 1
        self.max_seconds = min(600, max(5, float(args.get("seconds", 90))))
        self.config["revision"] = args.get("revision", self.config["revision"])[:40]
        if restore:
            saved = json.loads((RUNS / "checkpoint.json").read_text())
            self.game.set_action([0] * len(BUTTONS))
            native = RUNS / "checkpoint.zds"
            self.game.load(min((str(native), os.path.relpath(native)), key=len))
            now_tick = self.game.get_state().tic
            self.capture()
            actual = self.observation["player"]
            expected = saved["player"]
            expected_ammo = {w["slot"]: w["ammo"] for w in expected["carrying"]}
            actual_ammo = {w["slot"]: w["ammo"] for w in actual["carrying"]}
            inventory_matches = expected_ammo.keys() == actual_ammo.keys() and all(
                abs(actual_ammo[k] - v) <= 1 for k, v in expected_ammo.items()
            )
            if (
                dist([actual["x"], actual["y"]], [expected["x"], expected["y"]]) > 16
                or any(actual[k] != expected[k] for k in ("health", "armor"))
                or not inventory_matches
            ):
                self.status = "checkpoint_error"
                self.error = "Native restored position/health/inventory did not match checkpoint; no Jev dispatch"
                self.controls = {"released": True, "reason": self.error}
                return
            self.sim_ticks = saved["sim_ticks"]
            self.memory = saved["memory"]
            for e in self.memory.values():
                e["seen_tick"] = now_tick - (saved["tick"] - e["seen_tick"])
            self.nav.visits.update({(x, y): n for x, y, n in saved["visits"]})
            self.nav.seen = {tuple(c) for c in saved["seen"]}
            self.nav.trail = saved["trail"]
            self.nav.failed.update(saved["failed"])
            self.nav.used.update(saved["used"])
            self.distance = saved["distance"]
            self.last_position = None
            self.order = saved["order"]
            self.order_rev = saved["order_rev"]
            self.nav.known_exits.update(saved.get("known_exits", []))
            self.last_nav = 0
            self.position_history.clear()
            if "known_exits" not in saved:
                # Upgrade an older checkpoint from its recorded observations only.
                prior_trace = Path(saved["source_run"]) / "trace.jsonl"
                if prior_trace.exists():
                    for row in prior_trace.read_text().splitlines():
                        event = json.loads(row)
                        if event.get("type") == "request":
                            self.nav.known_exits.update(
                                e["id"] for e in event["state"]["navigation"] if e["kind"] == "exit"
                            )
            self.continued_from = dict(
                source_run=saved["source_run"],
                at=saved["at"],
                game_seconds=round(saved["sim_ticks"] / 35, 2),
                saved_player=expected,
                restored_player=actual,
                label="Native saved-game continuation; not a fresh-seed attempt",
                counter_note="Hit/damage counters restart on native load; prior counters retained in saved_player",
            )
            self.last_checkpoint_tick = self.sim_ticks
            self.capture()
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
            continued_from=self.continued_from,
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
                self.last_goal = -100
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
            and not self.lift_hold
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
                self.last_goal = -100
                self.navigation_feedback = dict(
                    target=attempted["id"],
                    reason="Destination movement commanded but less than8units displacement over about2.5seconds",
                    tick=s.tic,
                    failed_attempts=self.nav.failed[attempted["id"]],
                )
        if time.monotonic() - self.last_nav > 0.5:
            if self.nav.observable:
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
            else:
                self.nav.observe(pos, s.tic)
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
        if self.nav.observable:
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
        for line in [] if self.nav.observable else self.nav.switches:
            hit = ray.intersection(LineString([line["a"], line["b"]]))
            if not hit.is_empty:
                # ViZDoom1.3.0 reports floor_height with its sign flipped against the WAD (runs/investigations/door-tracer-v29/line167-native-result.json). Doors move ceilings only, so the opening is runtime ceiling minus WAD floor.
                gap = min(
                    (
                        s.sectors[sec].ceiling_height - self.nav.raw_sectors[sec][0]
                        for sec in (line["r"], line["l"])
                        if sec is not None
                    ),
                    default=None,
                )
                kind = line_kind(line["special"])
                interaction = dict(
                    id=line["id"],
                    kind=kind,
                    distance=round(Point(pos).distance(hit), 1),
                    special=line["special"],
                    opening_height=round(gap, 1) if kind == "door" and gap is not None else None,
                    door_passable=(gap >= 56) if kind == "door" and gap is not None else None,
                    needs_use=kind != "door" or (gap is not None and gap < 56),
                )
                if line["special"] in MANUAL_DOORS and gap is not None:
                    previous = self.door_observations.get(line["id"], {})
                    changed_tick = (
                        s.tic
                        if previous and gap != previous["opening_height"]
                        else previous.get("changed_tick", -1000)
                    )
                    sector = min(
                        (sec for sec in (line["r"], line["l"]) if sec is not None),
                        key=lambda sec: (
                            s.sectors[sec].ceiling_height - self.nav.raw_sectors[sec][0]
                        ),
                    )
                    interaction.update(
                        moving=s.tic - changed_tick <= 6, use_group="door_sector_" + str(sector)
                    )
                    if previous and previous["passable"] != (gap >= 56):
                        self.log(
                            "door_transition",
                            line=line["id"],
                            game_tick=s.tic,
                            opening_height=gap,
                            passable=gap >= 56,
                            player=[p["x"], p["y"]],
                        )
                    self.door_observations[line["id"]] = dict(
                        opening_height=gap,
                        passable=gap >= 56,
                        changed_tick=changed_tick,
                        seen_tick=s.tic,
                    )
                interactions.append(interaction)
        lifts = []
        for sec, info in ({} if self.nav.observable else self.nav.lift_sectors).items():
            floor = -s.sectors[sec].floor_height
            previous = self.lift_observations.get(sec)
            changed_tick = (
                s.tic
                if previous is None or previous["floor"] != floor
                else previous["changed_tick"]
            )
            self.lift_observations[sec] = dict(floor=floor, changed_tick=changed_tick)
            state = (
                "moving"
                if s.tic - changed_tick <= 6
                else "down"
                if abs(floor - info["low"]) <= 4
                else "up"
                if abs(floor - info["high"]) <= 4
                else "between"
            )
            centre = (
                [
                    sum(self.nav.xy(c)[0] for c in self.nav.by_sector[sec])
                    / max(1, len(self.nav.by_sector[sec])),
                    sum(self.nav.xy(c)[1] for c in self.nav.by_sector[sec])
                    / max(1, len(self.nav.by_sector[sec])),
                ]
                if self.nav.by_sector[sec]
                else pos
            )
            if dist(pos, centre) < 640 and not self.nav.walls.intersects(LineString([pos, centre])):
                lifts.append(
                    dict(
                        id="lift_" + str(sec),
                        sector=sec,
                        floor=floor,
                        low=info["low"],
                        high=info["high"],
                        state=state,
                        distance=round(dist(pos, centre)),
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
        completed_doors = {id for id, d in self.door_observations.items() if d["passable"]}
        navigation = [
            n
            for n in self.nav_candidates
            if n["kind"] != "door_or_switch" or n["id"] not in completed_doors
        ]
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
            navigation=navigation,
            completed_door_approaches=sorted(completed_doors),
            navigation_feedback=self.navigation_feedback,
            interaction_ahead_within_USE_range=interactions,
            lifts=lifts,
            lift_assistance=self.lift_hold,
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
                map_assistance=(
                    "seen floor cells and seen wall pieces only; frontier candidates at the edge of seen floor; wall faces classified by their visible texture; no static map geometry"
                    if self.nav.observable
                    else "static full geometry; discovered frontier candidates; observed barrel collision boxes"
                ),
                keys="not exposed by this adapter",
            ),
        )
        subject = (self.objective or {}).get("subject") or {}
        if subject.get("kind") == "door_or_switch" and subject["id"] in completed_doors:
            self.end_tactic("completed")
            self.log(
                "objective_complete",
                subject=subject,
                reason="Observed door is passable; interaction approach is complete",
            )
            self.objective = None
            self.last_action = None
            self.last_goal = -100
            self.control_rev += 1
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
            active_escape=self.escape,
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
        observable = self.config.get("mode", "observable") == "observable"
        state["mode"] = self.config.get("mode", "observable")
        state["exploration_target"] = self.exploration_target
        state["recent_tactic_outcomes"] = [
            {
                k: v
                for k, v in o.items()
                if k != "measured" and not (observable and k == "damage_taken")
            }
            for o in list(self.tactic_outcomes)[-5:]
        ]
        if observable:
            state["assumptions"] = (
                "Structured sensing of entities currently in view with exact positions and identities; remembered entities with ages; floor cells and wall pieces only where the view has reached them; wall faces classified by the texture the player sees; heights only for sectors in view; no static map, no line specials, no lift facts out of view. Hit, damage, kill, item and secret counters are evaluator evidence and are not shown; outcome feedback carries HUD health and armor changes only."
            )
        else:
            state["assumptions"] = (
                "Structured sensing of currently visible entities with exact positions and identities; remembered entities with ages; static map geometry for routes; door and lift states only when in view or line of sight. Hit, damage, kill, item and secret counters are evaluator evidence and are not shown."
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

    def questions(self, phase, state):
        entities = state["enemies"] + state["items"] + state["remembered"]
        entities = {e["id"]: e for e in entities}
        if phase == "goal":
            nav = {
                e[
                    "id"
                ]: f"{e['description']}; {e['distance']} units path; failed {e['failed']}; activated {e.get('uses', 0)} times"
                for e in state["navigation"]
            }
            qs = {}
            goals = {}
            p = state["player"]
            survival = f"Health {p['health']}/100, armor {p['armor']}, current weapon {p['weapon']} with {p['ammo']} ammo, {len(state['enemies'])} visible enemies, recent damage {state['just_took_damage']}. Carried weapons: {', '.join(w['name'] for w in p['carrying'])}."
            intents = {
                "enemy": "Deal with the known hostile threat while staying alive",
                "health": "Get meaningful healing; urgent when health is low. A +1 bonus alone cannot repair serious damage",
                "ammo": "Get ammunition compatible with carried weapons when running low",
                "weapon": "Acquire a stronger weapon if still relying on a pistol; nearby shotguns are valuable before entering crowded rooms",
                "armor": "Get protective armor when current armor is low",
                "key": "Collect a key to unlock further progress",
            }
            for kind, goal in [
                ("enemy", "kill_enemies"),
                ("health", "restore_health"),
                ("ammo", "stock_ammo"),
                ("weapon", "upgrade_weapon"),
                ("armor", "add_armor"),
                ("key", "collect_key"),
            ]:
                options = {}
                for id, e in entities.items():
                    pickup_name = id.rsplit("_", 1)[0]
                    owned_weapon = e["kind"] == "weapon" and PICKUP_WEAPON_SLOTS.get(
                        pickup_name
                    ) in {w["slot"] for w in p["carrying"]}
                    effective_kind = "ammo" if owned_weapon else e["kind"]
                    if effective_kind != kind:
                        continue
                    # Memory is evidence of a former sighting, not a currently attackable target.
                    if kind == "enemy" and not e["visible"]:
                        continue
                    path = self.nav.path([p["x"], p["y"]], [e["x"], e["y"]])
                    if kind != "enemy" and (not path or dist(path[-1], [e["x"], e["y"]]) > 32):
                        continue
                    route = (
                        round(sum(dist(a, b) for a, b in zip(path, path[1:])))
                        if path
                        else "unavailable"
                    )
                    effect = (
                        "Already carried weapon; duplicate supplies ammunition only, no weapon upgrade"
                        if owned_weapon
                        else PICKUP_EFFECTS.get(pickup_name, kind)
                    )
                    options[id] = (
                        f"{e['label']}: {effect}; {e['distance']} units straight-line, estimated geometric route {route} units (doors may need opening), bearing{e['bearing']}, visible{e['visible']}, last seen{e['last_seen_seconds']}sec"
                    )
                if options:
                    qs["which_" + kind] = choice(
                        f"If pursuing {goal}, which known {kind} is best? {survival} Compare actual benefit, route length and threats. Prefer meaningful healing over tiny bonuses when hurt.",
                        options,
                    )
                    goals[goal] = intents[kind] + ". Known options: " + "; ".join(options.values())
            if nav:
                qs["navigation"] = choice(
                    "Choose the next route to advance through the level. Seek unexplored connected territory or the exit. Penalize repeated failed or already activated targets. This is navigation only, combat may temporarily take priority.",
                    nav,
                )
                goals["advance"] = "Explore the navigation choice, open doors and reach exit"
            exits = {
                e[
                    "id"
                ]: f"Known exit: {e['distance']} units reachable path; failed {e['failed']}; {e['description']}"
                for e in state["navigation"]
                if e["kind"] == "exit"
            }
            if exits:
                qs["exit"] = choice(
                    "Which known exit should end this level? Choose a reachable exit using route length and failures.",
                    exits,
                )
                goals["reach_exit"] = (
                    "Finish this level by reaching and activating a known exit. Combat can still aim and fire while moving there. No need to kill every enemy or collect every item. Known exit routes: "
                    + "; ".join(exits.values())
                )
            goals["scout"] = "Look around to discover a missing route or enemy without moving"
            qs["goal"] = choice(
                f"What is the highest priority NOW? {survival} Reach the exit alive. Reconsider exploration when wounded or under-equipped. Seek useful healing at low health and a stronger known weapon when using only a pistol. Combat channels can aim/fire while moving toward supplies. Choose from facts and standing_order; do not consume answers to the other questions in this same call.",
                goals,
            )
            return qs
        objective = state.get("objective") or {}
        qs = {}
        destination = objective.get("subject")
        p = state["player"]
        has_route = bool(
            destination and self.nav.path([p["x"], p["y"]], [destination["x"], destination["y"]])
        )
        faces = {
            "hold": "Keep current facing",
            "scan_left": "Look left to discover what is missing",
            "scan_right": "Look right to discover what is missing",
        }
        if has_route:
            faces = {"path": "Face the next waypoint toward the committed objective", **faces}
        faces.update(
            {
                "door_"
                + e[
                    "id"
                ]: f"Face nearby interactive line {e['id']} at ({e['x']},{e['y']}), path distance{e['distance']}units; turn to inspect/use this door or switch"
                for e in state["navigation"]
                if e["kind"] in ("door_or_switch", "exit") and e["distance"] < 150
            }
        )
        faces.update(
            {
                e["id"]: f"Face visible {e['label']}, bearing{e['bearing']} distance{e['distance']}"
                for e in state["enemies"][:6]
            }
        )
        recent_enemies = [
            e for e in state["remembered"] if e["kind"] == "enemy" and e["last_seen_seconds"] <= 5
        ][:3]
        faces.update(
            {
                "recent_"
                + e[
                    "id"
                ]: f"Turn toward last seen {e['label']}, bearing{e['bearing']} distance{e['distance']}, seen {e['last_seen_seconds']}s ago; it may be shooting from outside the view"
                for e in recent_enemies
            }
        )
        visible_summary = (
            "; ".join(
                f"{e['id']} bearing{e['bearing']}degrees distance{e['distance']}units"
                for e in state["enemies"]
            )
            or "none"
        )
        qs["face"] = choice(
            f"Where should eyes and gun point NOW? Visible hostile monster types: {visible_summary}. Aim at a visible hostile while movement can independently continue toward the navigation or pickup objective. The goal advance does not mean ignore enemies. If no enemy is visible but you are taking damage, turn toward the most recently seen enemy (recent_ options); otherwise face an offered PATH or nearby DOOR, and scan when neither exists. Do not keep an old combat bearing while walking blindly. When stuck beside a door, face that door. Unknown enemy HP is not evidence of death. Respect standing_order.",
            faces,
        )
        moves = {
            "hold": "Stay in place while aiming or firing",
            "back": "Retreat opposite the current facing",
            "strafe_left": "Strafe left while aiming",
            "strafe_right": "Strafe right while aiming",
        }
        if has_route:
            moves = {
                "destination": "Walk toward the committed objective using path geometry",
                **moves,
            }
        qs["move"] = choice(
            "How should the player move given the COMMITTED objective? Movement and aiming are independent. "
            + (
                "destination follows the reachable committed objective; for a visible enemy it stops at firing range (pistol384units, shotgun240units), not at the monster body."
                if has_route
                else "There is no reachable committed destination; choose a local movement or hold while scanning."
            ),
            moves,
        )
        qs["dodge"] = choice(
            "Is an immediate dodge needed for a projectile or close melee threat? Carry on unless this exact moment requires it.",
            {
                "carry_on": "Keep normal movement",
                "left": "Dodge left",
                "right": "Dodge right",
                "back_left": "Dodge backward and left",
                "back_right": "Dodge backward and right",
            },
        )
        barrels = (
            "; ".join(
                f"{e['id']} distance{e['distance']} bearing{e['bearing']}"
                for e in state.get("obstacles", [])
                if e["visible"]
            )
            or "none"
        )
        qs["trigger"] = choice(
            f"Should the gun trigger be held NOW? Weapon {state['player']['weapon']}, ammo {state['player']['ammo']}. Visible hostile monster types: {visible_summary}. Choose FIRE to permit attacking a visible threat during this control lease, including while turning to aim. You need not wait for a later already-aligned observation: the motor blocks shots until the independently selected FACE target is visible and aligned. This does not consume the FACE answer in this call. Navigation is not a reason to withhold firing permission. Nearby explosive barrels: {barrels}. Avoid firing through a barrel close to the player. Enemy HP is unavailable, not zero. Respect standing_order. Never shoot only a remembered enemy.",
            {
                "fire": "Permit fire at the selected visible enemy once aligned; motor waits while turning",
                "hold_fire": "Do not permit any shots during this control lease",
            },
        )
        p = state["player"]
        weapons = {"keep": f"Keep {p['weapon']} ({p['ammo']} ammo)"}
        weapons.update(
            {
                str(w["slot"]): f"Switch to {w['name']} ({w['ammo']} ammo)"
                for w in p["carrying"]
                if w["slot"] >= 2 and w["slot"] != p["weapon_slot"] and w["ammo"] > 0
            }
        )
        qs["weapon"] = choice(
            f"Which carried weapon suits NOW? Current {p['weapon']} with {p['ammo']} ammo. Visible enemies: {visible_summary}. Shotgun is powerful within500units; pistol is precise farther away. Switching lowers/raises the gun and briefly prevents firing. Keep the current weapon when no visible threat or clear ammo/range reason warrants switching. Do not alternate weapons just because a target briefly disappears.",
            weapons,
        )
        lines_ahead = state.get("interaction_ahead_within_USE_range", [])
        ahead = (
            "; ".join(
                f"{e['id']} kind {e['kind']} at {e['distance']} units"
                + (
                    f", opening {e['opening_height']} units, passable {e['door_passable']}"
                    if e["kind"] == "door"
                    else ", needs USE to activate"
                )
                for e in lines_ahead
            )
            or "none"
        )
        qs["use"] = choice(
            f"Should USE be pressed NOW? Interactive lines directly ahead within USE reach: {ahead}. An exit switch, switch or lift switch only works when USE is pressed while facing it; a closed door needs USE to open; an open door is walked through without USE. Do not press USE when no eligible line is ahead.",
            {"use": "Pulse USE on the interactive line ahead", "no_use": "Do not press USE"},
        )
        return qs

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

    def save_investigation(self):
        path = Path(self.investigation["path"])
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(self.investigation, indent=2))

    def request_investigation(self, args):
        if self.busy or self.status in (
            "running",
            "warming",
            "waiting_provider",
            "recovering",
            "paused",
        ):
            return
        capsule = args.get("capsule")
        if not capsule:
            capsule = dict(
                source_run=str(getattr(self, "run_dir", "")),
                result=self.result,
                recent_outcomes=list(self.tactic_outcomes)[-5:],
                hypotheses=[
                    dict(id="stale_evidence", claim="Decision facts changed before execution"),
                    dict(
                        id="route_blocked", claim="A proposed route collided or missed its endpoint"
                    ),
                    dict(
                        id="tactic_wrong",
                        claim="Facts and route were usable but selected behavior exposed the player",
                    ),
                ],
                tests=[
                    dict(
                        id="snapshot_compare",
                        label="Compare request observations with subsequent execution events",
                        hypotheses=["stale_evidence", "tactic_wrong"],
                        outcomes="Changed facts before actuation versus correct facts with ineffective choice",
                    ),
                    dict(
                        id="route_inspection",
                        label="Reconstruct the failed route with recorded obstacles and actual positions",
                        hypotheses=["route_blocked", "tactic_wrong"],
                        outcomes="Collision or unreachable endpoint versus geometrically clear route",
                    ),
                    dict(
                        id="native_fixture",
                        label="Replay the selected motor in an isolated native fixture",
                        hypotheses=["route_blocked", "tactic_wrong"],
                        outcomes="Engine blocks the same motor versus movement succeeds under controlled conditions; fixture cannot prove combat safety",
                    ),
                ],
            )
        questions = investigation_questions(capsule)
        state = dict(
            tick=self.observation.get("tick", 0),
            purpose="Choose an unpaid discriminating test, not an in-game action",
            failure_capsule=capsule,
        )
        if len(json.dumps(dict(state=state, questions=questions)).encode()) > 48000:
            self.error = "Investigation capsule exceeds request bound"
            return
        if self.cap_blocks():
            self.error = "Budget prevents investigation call"
            return
        self.reserve_call(self.config.get("provider_key", "gateway"))
        self.call_seq += 1
        now = time.monotonic()
        job = dict(
            provider=self.config.get("provider_key", "gateway"),
            id=self.call_seq,
            episode=self.episode,
            order_rev=self.order_rev,
            control_rev=self.control_rev,
            phase="investigation",
            state=state,
            questions=questions,
            snapshot_mono=now,
            sent_mono=now,
            sent_at=utc(),
        )
        path = RUNS / "investigations" / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        self.investigation = dict(
            status="requesting",
            path=str(path),
            capsule=capsule,
            questions=questions,
            selected_test=None,
            findings=None,
        )
        self.save_investigation()
        self.log("request", **{k: v for k, v in job.items() if not k.endswith("_mono")})
        self.jobs.put(job)
        self.busy = True

    def dispatch(self):
        if self.busy or self.status not in ("warming", "running"):
            return
        now = time.monotonic()
        if now - self.last_dispatch < 0.12:
            return
        phase = (
            ("goal" if self.objective is None or now - self.last_goal > 3 else "combat")
            if self.config.get("controller", "channels") == "channels"
            else "tactic"
        )
        state = self.state_for_model()
        if phase == "tactic":
            self.tactic_menu, self.omitted_tactics = build_menu(state, self.nav)
            state["tactic_candidates"] = self.tactic_menu
            state["omitted_tactics"] = self.omitted_tactics
            questions = tactic_questions(self.tactic_menu, state)
        else:
            questions = self.questions(phase, state)
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
        if job["phase"] == "investigation":
            try:
                self.validate_answers(job, r)
                selected = r["answers"]["test"]["choice"]
                self.investigation.update(
                    status="test_selected",
                    selected_test=selected,
                    response=r,
                    request=job["id"],
                    findings=None,
                )
                record["accepted"] = True
                record["composed_intent"] = {"selected_unpaid_test": selected, "actuation": False}
            except Exception as e:
                self.investigation.update(status="request_failed", error=str(e), response=r)
            self.records.append(record)
            self.log("response", record=record)
            self.save_investigation()
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
                self.checkpoint()
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
                    if job["phase"] == "goal":
                        goal = picks["goal"]
                        kind = {
                            "kill_enemies": "enemy",
                            "restore_health": "health",
                            "stock_ammo": "ammo",
                            "upgrade_weapon": "weapon",
                            "add_armor": "armor",
                            "collect_key": "key",
                        }.get(goal)
                        target = (
                            picks.get("which_" + kind)
                            if kind
                            else picks.get("exit")
                            if goal == "reach_exit"
                            else picks.get("navigation")
                            if goal == "advance"
                            else None
                        )
                        pool = (
                            job["state"]["enemies"]
                            + job["state"]["items"]
                            + job["state"]["remembered"]
                            + job["state"]["navigation"]
                        )
                        subject = next((x for x in pool if x["id"] == target), None)
                        self.objective = dict(
                            goal=goal,
                            subject=subject,
                            committed_tick=self.observation["tick"],
                            request=job["id"],
                        )
                        self.last_goal = time.monotonic()
                    else:
                        selected = None
                        if job["phase"] == "tactic":
                            selected = (
                                picks["tactic"]
                                if self.config["controller"] == "semantic_tactics"
                                else fixed_select(job["state"]["tactic_candidates"])
                            )
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
                                if self.config["controller"] == "fixed_tactics":
                                    record["override_reasons"].append(
                                        "Fixed comparison selector ignores Jev answers for actuation; paid call retained for matched timing policy"
                                    )
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
                        if job["phase"] == "tactic":
                            option = copy.deepcopy(
                                next(
                                    t
                                    for t in job["state"]["tactic_candidates"]
                                    if t["id"] == selected
                                )
                            )
                            if picks.get("dodge") in (
                                "left",
                                "right",
                                "back_left",
                                "back_right",
                                "carry_on",
                            ):
                                option["controls"]["dodge"] = picks["dodge"]
                            if self.config["controller"] == "fixed_tactics":
                                record["override_reasons"].append(
                                    "Fixed comparison selector ignores Jev answers for actuation; paid call retained for matched timing policy"
                                )
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
                        else:
                            self.last_action = dict(
                                **picks,
                                source_request=job["id"],
                                source_tick=job["state"]["tick"],
                                control_rev=self.control_rev,
                                committed_mono=time.monotonic(),
                                snapshot=job["state"],
                            )
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
                        self.objective
                        if job["phase"] == "goal"
                        else {
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
            if self.nav.observable and destination.get("through"):
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
        # Threat-check turn: damage with no enemy in view and a remembered enemy seen within5seconds outside the view; the route continues, aim and fire stay the model's.
        recent = (
            [
                m
                for m in self.observation.get("remembered", [])
                if m["kind"] == "enemy" and m["last_seen_seconds"] <= 5
            ]
            if self.observation.get("just_took_damage") and not self.observation.get("enemies")
            else []
        )
        turn_target = None
        if (
            recent
            and "threat_turn" in set(self.config.get("assists", []))
            and (a["face"] in ("path", "hold") or a["face"].startswith("door_"))
        ):
            current = next(
                (m for m in recent if self.threat_turn and m["id"] == self.threat_turn["id"]), None
            )
            turn_target = current or min(recent, key=lambda m: m["last_seen_seconds"])
        if turn_target:
            face = math.degrees(math.atan2(turn_target["y"] - p["y"], turn_target["x"] - p["x"]))
            overrides.append(
                "Threat check: damage with no enemy in view; turning toward last seen "
                + turn_target["id"]
            )
            self.threat_turn_ticks += 1
            if not self.threat_turn or self.threat_turn["id"] != turn_target["id"]:
                self.threat_turn = dict(id=turn_target["id"], since=self.sim_ticks)
                self.threat_turns += 1
                self.log(
                    "threat_turn_start",
                    game_tick=self.sim_ticks,
                    target=turn_target["id"],
                    last_seen_seconds=turn_target["last_seen_seconds"],
                    model_face=a["face"],
                    player=[round(p["x"], 1), round(p["y"], 1)],
                )
        elif self.threat_turn:
            self.log(
                "threat_turn_end",
                game_tick=self.sim_ticks,
                target=self.threat_turn["id"],
                ticks=self.sim_ticks - self.threat_turn["since"],
                enemy_visible=bool(self.observation.get("enemies")),
            )
            self.threat_turn = None
        turn = angle(face - p["facing"])
        values[2] = max(-7, min(7, turn))
        # One-tick check after startup: +7delta changes0degrees to353degrees (right).
        values[2] = -values[2]
        hold = None
        if (
            move == "destination"
            and waypoint
            and self.nav.lift_sectors
            and "lift_assist" in set(self.config.get("assists", []))
        ):
            here = self.nav.sector_at(pos)
            there = self.nav.sector_at(waypoint)
            z = p.get("z", 0)
            if (
                there in self.nav.lift_sectors
                and here != there
                and there in self.lift_observations
                and self.lift_observations[there]["floor"] - z > 24
            ):
                hold = dict(kind="call", sector=there, floor=self.lift_observations[there]["floor"])
            elif (
                here in self.nav.lift_sectors
                and there != here
                and there is not None
                and here in self.lift_observations
                and self.nav.sectors[there][0] - self.lift_observations[here]["floor"] > 24
            ):
                hold = dict(kind="ride", sector=here, floor=self.lift_observations[here]["floor"])
            elif (
                here in self.nav.lift_sectors
                and there != here
                and there is not None
                and here in self.lift_observations
                and self.lift_observations[here]["floor"] - self.nav.sectors[there][0] > 24
            ):
                # Lower (v49): the lift is up and the route leaves below it. A switch line on the lift edge is inert from the lift side, so cross the nearest walk-over lowering line of this tag into the neighbour; the route then brings the player back onto the lowering lift.
                walkovers = [
                    line
                    for line in self.nav.lift_lines
                    if line["special"] in (10, 88, 120, 121) and here in (line["r"], line["l"])
                ]
                if walkovers:
                    walkover = min(
                        walkovers,
                        key=lambda line: LineString([line["a"], line["b"]]).distance(Point(pos)),
                    )
                    mx, my = (
                        (walkover["a"][0] + walkover["b"][0]) / 2,
                        (walkover["a"][1] + walkover["b"][1]) / 2,
                    )
                    dx, dy = (
                        walkover["b"][0] - walkover["a"][0],
                        walkover["b"][1] - walkover["a"][1],
                    )
                    n = math.hypot(dx, dy) or 1
                    nx, ny = -dy / n, dx / n
                    side = (p["x"] - mx) * nx + (p["y"] - my) * ny
                    tx, ty = (
                        mx - 40 * nx * (1 if side > 0 else -1),
                        my - 40 * ny * (1 if side > 0 else -1),
                    )
                    hold = dict(
                        kind="lower",
                        sector=here,
                        floor=self.lift_observations[here]["floor"],
                        line=walkover["id"],
                        target=[round(tx, 1), round(ty, 1)],
                    )
        if (hold or {}).get("kind") != (self.lift_hold or {}).get("kind") or (hold or {}).get(
            "sector"
        ) != (self.lift_hold or {}).get("sector"):
            if hold:
                self.lift_lowers += int(hold["kind"] == "lower")
                self.log(
                    "lift_assist_start",
                    game_tick=self.sim_ticks,
                    hold=hold,
                    player=[round(p["x"], 1), round(p["y"], 1), round(p.get("z", 0), 1)],
                    source_request=a["source_request"],
                    model_move=a["move"],
                )
            elif self.lift_hold:
                self.log(
                    "lift_assist_end",
                    game_tick=self.sim_ticks,
                    hold=self.lift_hold,
                    player=[round(p["x"], 1), round(p["y"], 1), round(p.get("z", 0), 1)],
                )
            self.lift_hold = hold
        # Cover-seeking under unseen fire: damage with no enemy in view and a recent remembered enemy; move up to1.5s to the nearest seen cell whose line to every recent remembered position is wall-blocked. Aim, trigger and weapon stay the model's.
        if self.cover and (
            self.sim_ticks >= self.cover["until_tick"]
            or dist(pos, [self.cover["x"], self.cover["y"]]) < 16
            or self.observation.get("enemies")
        ):
            reached_cover = dist(pos, [self.cover["x"], self.cover["y"]]) < 16
            self.log(
                "cover_end",
                game_tick=self.sim_ticks,
                ticks=self.sim_ticks - self.cover["since"],
                reached=reached_cover,
                enemy_visible=bool(self.observation.get("enemies")),
                player=[round(p["x"], 1), round(p["y"], 1)],
            )
            # Cover hold (v52): a reached covered cell is held for the rest of the budget instead of restarting toward a neighbour.
            self.cover_hold = (
                dict(x=self.cover["x"], y=self.cover["y"], until_tick=self.cover["until_tick"])
                if reached_cover and not self.observation.get("enemies")
                else None
            )
            self.cover = None
        self.cover_holding = False
        if self.cover_hold and (
            self.sim_ticks >= self.cover_hold["until_tick"]
            or self.observation.get("enemies")
            or not recent
            or dist(pos, [self.cover_hold["x"], self.cover_hold["y"]]) >= 48
            or not all(
                self.nav.walls.intersects(
                    LineString([[self.cover_hold["x"], self.cover_hold["y"]], [m["x"], m["y"]]])
                )
                for m in recent
            )
        ):
            self.log(
                "cover_hold_end",
                game_tick=self.sim_ticks,
                hold=self.cover_hold,
                player=[round(p["x"], 1), round(p["y"], 1)],
            )
            self.cover_hold = None
        if (
            self.cover is None
            and recent
            and "cover_seek" in set(self.config.get("assists", []))
            and not self.escape
            and not hold
            and move == "destination"
            and self.cover_hold
        ):
            self.cover_holding = True
            if self.cover_hold_logged != self.cover_hold["until_tick"]:
                self.cover_hold_logged = self.cover_hold["until_tick"]
                self.cover_holds += 1
                self.log(
                    "cover_hold_start",
                    game_tick=self.sim_ticks,
                    hold=self.cover_hold,
                    threats=[m["id"] for m in recent],
                    player=[round(p["x"], 1), round(p["y"], 1)],
                )
        if (
            self.cover is None
            and recent
            and "cover_seek" in set(self.config.get("assists", []))
            and not self.escape
            and not hold
            and move == "destination"
            and not self.cover_holding
        ):
            spot = self.nav.cover_point(pos, recent)
            if spot:
                self.cover = dict(**spot, since=self.sim_ticks, until_tick=self.sim_ticks + 52)
                self.covers += 1
                self.log(
                    "cover_start",
                    game_tick=self.sim_ticks,
                    target=spot,
                    threats=[m["id"] for m in recent],
                    model_move=a["move"],
                    player=[round(p["x"], 1), round(p["y"], 1)],
                )
        if hold and hold["kind"] == "call":
            line = min(
                (line for line in self.nav.lift_lines if hold["sector"] in (line["r"], line["l"])),
                key=lambda line: LineString([line["a"], line["b"]]).distance(Point(pos)),
                default=None,
            )
            if line:
                ax, ay = line["a"]
                bx, by = line["b"]
                dx, dy = bx - ax, by - ay
                t = max(0, min(1, ((p["x"] - ax) * dx + (p["y"] - ay) * dy) / (dx * dx + dy * dy)))
                face = math.degrees(math.atan2(ay + t * dy - p["y"], ax + t * dx - p["x"]))
                turn = angle(face - p["facing"])
                values[2] = -max(-7, min(7, turn))
            overrides.append(
                "Lift assistance: waiting at the edge and calling the lift on the committed route"
            )
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
            if hold:
                if hold["kind"] == "lower":
                    tgt = hold["target"]
                    theta = math.atan2(tgt[1] - p["y"], tgt[0] - p["x"]) - math.radians(p["facing"])
                    speed = min(35, dist(pos, tgt) * 2)
                    values[0] = math.cos(theta) * speed
                    values[1] = -math.sin(theta) * speed
                    move = "lift_lower"
                    overrides.append(
                        "Lift assistance: crossing walk-over line "
                        + hold["line"]
                        + " to lower the raised lift on the committed route"
                    )
                else:
                    values[0] = values[1] = 0
                    move = "lift_wait" if hold["kind"] == "call" else "lift_ride"
                self.lift_assist_ticks += 1
            if self.cover and not hold:
                theta = math.atan2(
                    self.cover["y"] - p["y"], self.cover["x"] - p["x"]
                ) - math.radians(p["facing"])
                speed = min(35, dist(pos, [self.cover["x"], self.cover["y"]]) * 2)
                values[0] = math.cos(theta) * speed
                values[1] = -math.sin(theta) * speed
                move = "cover_seek"
                self.cover_ticks += 1
                overrides.append(
                    "Cover-seeking under unseen fire: moving to a wall-covered cell from the remembered shooter; aim, trigger and weapon unchanged"
                )
            if self.cover_holding and not hold and not self.cover:
                hd = dist(pos, [self.cover_hold["x"], self.cover_hold["y"]])
                if hd > 8:
                    theta = math.atan2(
                        self.cover_hold["y"] - p["y"], self.cover_hold["x"] - p["x"]
                    ) - math.radians(p["facing"])
                    speed = min(20, hd * 2)
                    values[0] = math.cos(theta) * speed
                    values[1] = -math.sin(theta) * speed
                else:
                    values[0] = values[1] = 0
                move = "cover_hold"
                self.cover_ticks += 1
                overrides.append(
                    "Cover hold under unseen fire: staying in the wall-covered cell for the rest of the cover budget; aim, trigger and weapon unchanged"
                )
            if hold and hold["kind"] == "ride":
                overrides.append("Lift assistance: riding the lift up before continuing the route")
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
        # Barrel clearance: never stand within96units of a visible barrel (own and enemy fire both detonate them, fixture barrel-v36); a stop or hold there becomes a move to the nearest clear cell.
        near_barrels = [
            b
            for b in self.observation.get("obstacles", [])
            if b.get("visible") and dist(pos, [b["x"], b["y"]]) < 96
        ]
        stopped = (values[0] == 0 and values[1] == 0) or move in ("hold", "enemy_in_firing_range")
        clear = None
        if (
            near_barrels
            and stopped
            and "barrel_clearance" in set(self.config.get("assists", []))
            and not self.escape
            and not hold
        ):
            clear = self.nav.clear_point(pos, self.observation.get("obstacles", []))
            if clear:
                theta = math.atan2(clear["y"] - p["y"], clear["x"] - p["x"]) - math.radians(
                    p["facing"]
                )
                speed = min(35, dist(pos, [clear["x"], clear["y"]]) * 2)
                values[0] = math.cos(theta) * speed
                values[1] = -math.sin(theta) * speed
                move = "barrel_clearance"
                self.barrel_clear_ticks += 1
                overrides.append(
                    "Barrel clearance: visible barrel within96units; moving to a clear cell instead of standing; aim, trigger and weapon unchanged"
                )
        if clear and not self.barrel_clear:
            self.barrel_clears += 1
            self.barrel_clear = dict(since=self.sim_ticks, target=clear)
            self.log(
                "barrel_clear_start",
                game_tick=self.sim_ticks,
                barrels=[b["id"] for b in near_barrels],
                target=clear,
                model_move=a["move"],
                player=[round(p["x"], 1), round(p["y"], 1)],
            )
        elif not clear and self.barrel_clear:
            self.log(
                "barrel_clear_end",
                game_tick=self.sim_ticks,
                ticks=self.sim_ticks - self.barrel_clear["since"],
                player=[round(p["x"], 1), round(p["y"], 1)],
            )
            self.barrel_clear = None
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
        if self.escape and (
            self.sim_ticks >= self.escape["until_tick"]
            or dist(pos, [self.escape["x"], self.escape["y"]]) < 16
            or p["health"] > 35
        ):
            self.log("escape_end", game_tick=self.sim_ticks, player=p)
            self.escape = None
            self.invalidate_objective(
                "Escape finished; replan from new position instead of resuming the previous route"
            )
            values[0] = values[1] = 0
            move = "expired_objective"
            overrides.append("Escape finished; previous movement released")
        enabled = set(self.config.get("assists", []))
        if (
            self.escape is None
            and "escape_guard" in enabled
            and p["health"] <= 30
            and self.sim_ticks - self.stationary_since >= 21
            and self.observation["enemies"]
        ):
            point = self.nav.escape_point(
                pos, self.observation["enemies"], self.observation["projectiles"]
            )
            if point:
                self.escape = dict(**point, until_tick=self.sim_ticks + 42)
                self.escape_count += 1
                self.last_goal = -100
                self.log(
                    "escape_start",
                    game_tick=self.sim_ticks,
                    player=p,
                    target=self.escape,
                    source_request=a["source_request"],
                    model_move=a["move"],
                    reason="Low health and stationary under visible threat",
                )
        if self.escape:
            waypoint = [self.escape["x"], self.escape["y"]]
            theta = math.atan2(waypoint[1] - p["y"], waypoint[0] - p["x"]) - math.radians(
                p["facing"]
            )
            speed = min(35, dist(pos, waypoint) * 2)
            values[0] = math.cos(theta) * speed
            values[1] = -math.sin(theta) * speed
            move = "escape_guard"
            self.escape_ticks += 1
            overrides.append(
                "Low-health stationary escape guard overrides MOVE; Jev aim, trigger and weapon retained"
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
        if (
            hold
            and hold["kind"] == "call"
            and eligible
            and any(eligible["id"] == line["id"] for line in self.nav.lift_lines)
            and self.sim_ticks - self.lift_observations[hold["sector"]]["changed_tick"] > 6
        ):
            since = self.sim_ticks - self.use_latches.get("lift_" + str(hold["sector"]), -1000)
            if since >= 35:
                values[4] = 1
                self.use_latches["lift_" + str(hold["sector"])] = self.sim_ticks
                self.lift_calls += 1
                self.use_pulses[eligible["id"]] = self.use_pulses.get(eligible["id"], 0) + 1
                self.log(
                    "use_pulse",
                    line=eligible["id"],
                    special=eligible.get("special"),
                    game_tick=self.sim_ticks,
                    opening_height=eligible.get("opening_height"),
                    player=[round(p["x"], 1), round(p["y"], 1)],
                    source_request=a["source_request"],
                    lift_assistance=True,
                )
        elif a["use"] == "use" or (
            eligible
            and "exit_activation" in set(self.config.get("assists", []))
            and eligible.get("kind") == "exit_switch"
            and (a["snapshot"].get("objective") or {}).get("goal") == "reach_exit"
            and eligible["id"]
            == ((a["snapshot"].get("objective") or {}).get("subject") or {}).get("id")
        ):
            if a["use"] != "use":
                overrides.append(
                    "Exit activation follows the committed reach_exit goal; combat-battery USE answer was "
                    + str(a["use"])
                )
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
                    special=eligible.get("special"),
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
        self.result["continued_from"] = self.continued_from
        self.result["cost_final"] = not self.busy
        self.result["pending_reservation_usd"] = RESERVE if self.busy else 0
        self.result["controller_metrics"] = dict(
            controller=self.config.get("controller", "channels"),
            failed_route_seconds=round(self.failed_route_ticks / 35, 3),
            motor_intervention_seconds=round(self.intervention_ticks / 35, 3),
            tactic_decisions=self.tactic_decisions,
            predictions_met=self.predictions_met,
            prediction_statuses=dict(self.prediction_statuses),
            tactic_continuations=self.tactic_continuations,
            damage_taken=self.observation.get("player", {}).get("damage_taken"),
            measurement="Native damage counter; failed-route time means commanded destination with stuck displacement; intervention time means a named motor override",
        )
        self.result["escape_guard"] = dict(
            activations=self.escape_count,
            game_seconds=round(self.escape_ticks / 35, 2),
            attribution="Scripted movement assistance; aim/fire/weapon remain Jev choices",
        )
        self.result["mode"] = self.config.get("mode", "observable")
        self.result["boundary"] = dict(
            mode=self.config.get("mode", "observable"),
            timing=self.config.get("timing", "real_time"),
            geometry_source=(
                "seen floor cells and seen wall pieces from the depth buffer"
                if self.nav.observable
                else "static full WAD geometry"
            ),
            line_semantics=(
                "visible face textures, effects observed"
                if self.nav.observable
                else "WAD line specials"
            ),
            lift_facts=(
                "none out of view; heights only for sectors in view"
                if self.nav.observable
                else "all lift sectors within static line of sight"
            ),
            outcome_feedback=(
                "HUD health and armor changes only"
                if self.nav.observable
                else "native damage_taken and hit-derived statuses"
            ),
            assists_enabled=list(self.config.get("assists", [])),
            evaluator_fields_hidden_from_actor=list(EVALUATOR_FIELDS),
            uninterrupted_real_time=(
                self.paused_seconds == 0
                and self.pause_started is None
                and self.stall_total == 0
                and self.recovery_total == 0
            ),
            timing_note="Real-time mode advances with released controls during provider stalls and recovery; pause-assisted mode stops simulation during those states",
        )
        self.result["cover_seek"] = dict(
            activations=self.covers,
            holds=self.cover_holds,
            game_seconds=round(self.cover_ticks / 35, 2),
            attribution="Scripted movement assistance toward static cover from remembered shooters when damaged with no enemy in view; aim, trigger and weapon remain the model or selector choice",
        )
        self.result["barrel_clearance"] = dict(
            activations=self.barrel_clears,
            game_seconds=round(self.barrel_clear_ticks / 35, 2),
            attribution="Scripted movement assistance away from visible barrels when the model or selector would stand; aim, trigger and weapon remain the model or selector choice",
        )
        self.result["approach_facing"] = dict(
            game_seconds=round(self.approach_facing_ticks / 35, 2),
            attribution="Motor facing toward the selected exit or switch line within48units of its approach point; the selection, USE-range rule and exit-activation gate are unchanged",
        )
        self.result["threat_turn"] = dict(
            activations=self.threat_turns,
            game_seconds=round(self.threat_turn_ticks / 35, 2),
            attribution="Scripted facing assistance when damaged with no enemy in view; movement, trigger and weapon remain the model or selector choice",
        )
        self.result["lift_assist"] = dict(
            game_seconds=round(self.lift_assist_ticks / 35, 2),
            lift_calls=self.lift_calls,
            lift_lowers=self.lift_lowers,
            attribution="Scripted wait, call and ride on the committed route; the destination remains the model or selector choice",
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
                        if cmd in ("start", "load"):
                            if self.busy or self.status in (
                                "running",
                                "warming",
                                "recovering",
                                "waiting_provider",
                            ):
                                continue
                            if cmd == "load" and not (RUNS / "checkpoint.json").exists():
                                continue
                            self.begin_run(args, restore=cmd == "load")
                        elif cmd == "checkpoint":
                            self.checkpoint()
                        elif cmd == "investigate":
                            self.request_investigation(args)
                        elif cmd == "probe":
                            self.request_probe(args)
                        elif cmd == "investigation_result":
                            if self.investigation and args.get("test") == self.investigation.get(
                                "selected_test"
                            ):
                                self.investigation.update(
                                    status="test_recorded", findings=args.get("findings")
                                )
                                self.save_investigation()
                                self.log("investigation_result", investigation=self.investigation)
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
                            self.last_goal = -100
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
                            if self.sim_ticks - self.last_checkpoint_tick >= 1050:
                                self.checkpoint()
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
                investigation=self.investigation,
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
