"""One real-time engine owner and one serial Jev worker. No frontend credentials."""

import copy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
import logging
import os
import queue
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from shapely.geometry import LineString, Point
from jevdoom.core.navigation import Navigator
from jevdoom.adapters.gateway import Gateway
from jevdoom.adapters.files import FileStore
from jevdoom.adapters.snapshot import SnapshotView
from jevdoom.adapters.decider import AsyncDecider
from jevdoom.ports import end_reason
from jevdoom.core.protocol import MAX_ORDER_CHARS, state_for_model, validate_answers
from jevdoom.core.perception import observe_entities
from jevdoom.core.motor import calculate_motor
from jevdoom.core.budget import blocks, reserve, settle
from jevdoom.core.geometry import dist
from jevdoom.core.lifecycle import ACCEPTS_REPLY, CAN_PAUSE, DISPATCHES, LIVE, transition
from jevdoom.core.pending import clear, mark_stalled, submit
from jevdoom.core.catalog import (
    BUTTONS,
    EVALUATOR_FIELDS,
    WEAPON_NAMES,
    owns_pickup,
)
from jevdoom.core.tactics import (
    MODES,
    build_menu,
    closing_projectiles,
    fixed_select,
    tactic_questions,
    measure,
)

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
RUNS.mkdir(exist_ok=True)
RESERVE = 0.05
CAP = float(os.getenv("JEV_BUDGET_USD", "15.00"))
if not math.isfinite(CAP) or CAP < RESERVE:
    raise ValueError("JEV_BUDGET_USD must be finite and at least 0.05")
CAP_ORDER = "Both providers share the cumulative charged-or-reserved budget. Unknown costs retain a 0.05 USD reservation."


def utc():
    return datetime.now(timezone.utc).isoformat()


class Run:
    def __init__(self, engine=None, decider=None, store=None, view=None, clock=None, sleep=None):
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.store = store or FileStore(RUNS)
        self.view_port = view or SnapshotView()
        self.commands = queue.Queue()
        self.lock = threading.RLock()
        self._status = "booting"
        self.error = None
        self.order = "Survive and advance through the level. Defeat visible threats, collect useful resources, explore unvisited routes, open doors and reach the exit."
        self.order_rev = 0
        try:
            engine_version = version("vizdoom")
        except PackageNotFoundError:
            engine_version = "unavailable"
        self.config = dict(
            game="Freedoom Phase 2",
            engine=f"ViZDoom {engine_version}",
            map="MAP01",
            skill=3,
            seed=17,
            revision="v64-observable-order",
            mode="observable",
            timing="real_time",
            controller="local",
            start_type="Fresh pistol start",
            provider="Local rule, no model",
            provider_key="gateway",
            model="Local rule, no model",
            budget_usd=CAP,
            assistance="Observable mode: seen floor cells and seen wall pieces only, no static map, no line specials, no lift facts out of view, no tactical assist; execution interlocks of the selected option only (fire when the faced target is visible and aligned, USE when the selected face is in reach, approach facing toward the selected face, the dodge Jev chose)",
        )
        self.ledger = self.store.load_ledger() or dict(
            charged_usd=0, reported_usd=0, calls=0, unknown_cost_calls=0
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
        self.latencies = deque(maxlen=300)
        self.frame_image = None
        self.map = {}
        self.pending = None
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
        if engine is None:
            from jevdoom.adapters.vizdoom_engine import VizdoomEngine

            engine = VizdoomEngine(ROOT / "assets/freedoom2.wad")
        self.engine = engine
        self.decider = decider or AsyncDecider(Gateway(ROOT))
        self.engine_thread = threading.Thread(target=self.engine_loop, daemon=True)
        self.engine_thread.start()

    @property
    def status(self):
        return self._status

    def _move(self, target, why):
        self._status = transition(self.status, target, why)

    @property
    def busy(self):
        return self.pending is not None

    def log(self, type, **data):
        event = dict(type=type, at=utc(), episode=self.episode)
        event.update(data)
        self.store.append_trace(event)
        return event

    def save_budget(self):
        self.store.save_ledger(self.ledger)

    def clear_objective(self, reason, detail=None):
        if reason == "invalidated":
            self.end_tactic("invalidated")
            self.log(
                "objective_invalidated",
                objective=self.objective,
                reason=detail,
                game_tick=self.sim_ticks,
            )
            self.objective = None
            self.control_rev += 1
        elif reason == "new_game":
            self.objective = None
            self.last_action = None
            self.controls = {}
        elif reason == "weapon_complete":
            self.log(
                "objective_complete",
                goal="upgrade_weapon",
                subject=self.objective.get("subject") or {},
                reason=detail,
            )
            self.objective = None
            self.last_action = None
            self.control_rev += 1
            self.controls = {
                "released": True,
                "reason": "Weapon objective finished or invalid; request fresh goal",
            }
        elif reason == "tactic_end":
            self.end_tactic(detail)
            self.last_action = None
            if detail != "completed":
                self.control_rev += 1
            self.objective = None
        elif reason == "orders":
            self.end_tactic("invalidated")
            self.last_action = None
            self.objective = None
            self.control_rev += 1
        else:
            raise ValueError(f"Unknown objective clear reason: {reason!r}")

    def new_game(self):
        self.engine.start(
            self.config["map"], self.config["skill"], self.config["seed"], self.store.record_path()
        )
        self.nav = Navigator(self.engine.navigation_data(), self.config["map"])
        self.memory = {}
        self.previous_health = 100
        self.last_damage_tick = -1000
        self.position_history = deque(maxlen=35)
        self.last_failed_tick = -1000
        self.navigation_feedback = None
        self.clear_objective("new_game")
        self.seq = 0
        self.sim_ticks = 0
        self.start_wall = self.clock()
        self.last_dispatch = 0
        self.decision_tick = -105
        self.decision_health = 100
        self.decision_enemies = set()
        self.decision_projectiles = set()
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
        self._move("ready", "game initialized")

    def begin_run(self, args):
        selected_map = str(args.get("map", self.config["map"])).upper()
        if selected_map not in ("MAP01", "MAP02"):
            self.error = "Supported maps are MAP01 and MAP02; MAP03 hangs this ViZDoom build at engine init (tested 18 September 2026), later maps untested"
            return
        provider = args.get("provider", self.config.get("provider_key", "gateway"))
        if provider not in ("gateway", "typesafe"):
            self.error = "Unknown provider"
            return
        controller = args.get("controller", self.config.get("controller", "semantic_tactics"))
        if controller not in MODES:
            self.error = "Unknown controller"
            return
        if controller != "local" and not self.provider_health[provider]:
            self.error = "Click Check provider before starting a run"
            return
        self.config.update(
            provider_key=provider,
            provider=(
                "Local rule, no model"
                if controller == "local"
                else "TypeSafe direct"
                if provider == "typesafe"
                else "Vercel AI Gateway"
            ),
            model=(
                "Local rule, no model"
                if controller == "local"
                else "jev-latest"
                if provider == "typesafe"
                else "typesafe-ai/jev"
            ),
        )
        self.config["controller"] = controller
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
            parts.append("seed %d" % seed)
        self.config["start_type"] = (
            "Fresh pistol start" if not parts else "Fresh pistol start on " + ", ".join(parts)
        )
        self.config["start_type"] += ", observable mode"
        if timing == "pause_assisted":
            self.config["start_type"] += ", pause-assisted timing"
        self.max_seconds = min(600, max(5, float(args.get("seconds", 90))))
        self.config["revision"] = args.get("revision", self.config["revision"])[:40]
        revision = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in self.config["revision"]
        )
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + revision
        self.run_dir = self.store.open_run(name)
        self.new_game()
        self.episode += 1
        self.run_budget_start = copy.deepcopy(self.ledger)
        self.log(
            "episode_start",
            config=self.config,
            max_seconds=self.max_seconds,
        )
        self.records.clear()
        self.result = {}
        self._move("warming", "run started")
        self.error = None

    def capture(self):
        s = self.engine.frame()
        if s is None:
            return

        def var(name):
            return s.variables[name]

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
            dict(
                slot=i,
                name=WEAPON_NAMES[i],
                ammo=int(var("AMMO" + str(i))),
                count=int(var("WEAPON" + str(i))),
            )
            for i in WEAPON_NAMES
            if var("WEAPON" + str(i))
        ]
        if (self.objective or {}).get("goal") == "upgrade_weapon":
            subject = self.objective.get("subject") or {}
            carried = owns_pickup(subject.get("id", "").rsplit("_", 1)[0], p["carrying"])
            if carried or subject.get("id") not in self.memory:
                self.clear_objective(
                    "weapon_complete",
                    "Weapon now carried" if carried else "Target no longer in observed memory",
                )
        p["velocity"] = [round(var("VELOCITY_X") * 35, 1), round(var("VELOCITY_Y") * 35, 1)]
        pos = [p["x"], p["y"]]
        self.position_history.append(pos)
        if self.stationary_anchor is None or dist(pos, self.stationary_anchor) >= 8:
            self.stationary_anchor = pos
            self.stationary_since = self.sim_ticks
        if self.last_position:
            self.distance += dist(pos, self.last_position)
        self.last_position = pos
        self.memory, entities = observe_entities(s, p, self.memory)
        subject = (self.objective or {}).get("subject") or {}
        if (
            subject.get("kind") in ("health", "ammo", "weapon", "armor", "key")
            and subject.get("id") not in self.memory
        ):
            self.clear_objective(
                "invalidated",
                "Pickup no longer observed after nearby approach; replan without inferring collection",
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
        if self.clock() - self.last_nav > 0.5:
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
            self.last_nav = self.clock()
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
            if not v["spans"]:
                continue
            base = LineString([v["a"], v["b"]])
            hit = None
            for s0, s1 in v["spans"]:
                if self.nav.span_open(v, (s0, s1)):
                    continue
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
            captured_mono=self.clock(),
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
            complete = False
            unavailable = False
            if self.sim_ticks % 14 == 0:
                outcome = measure(
                    option,
                    self.active_tactic["before"],
                    self.observation,
                    self.nav,
                    "checking",
                    (self.sim_ticks - self.active_tactic["started_tick"]) / 35,
                )
                complete = outcome["prediction_status"] == "met"
                menu, _ = build_menu(self.state_for_model(), self.nav)
                unavailable = option["id"] not in {candidate["id"] for candidate in menu}
            stuck = (
                self.observation["progress"]["stuck"]
                and option["controls"]["move"] == "destination"
            )
            if arrived or lost or complete or unavailable or stuck:
                reason = (
                    "completed"
                    if arrived or complete
                    else "target_no_longer_visible"
                    if lost
                    else "blocked"
                    if stuck
                    else "unavailable"
                )
                self.clear_objective("tactic_end", reason)
        self.observation["survival"] = dict(
            stationary_seconds=round((self.sim_ticks - self.stationary_since) / 35, 2),
            escape_guard="At health30 or less, stationary under visible threat: bounded local escape may override MOVE; aiming and firing remain model-controlled",
            active_escape=None,
        )
        self.frame_image = s.screen_buffer

    def state_for_model(self):
        return state_for_model(
            self.observation,
            self.objective,
            self.exploration_target,
            self.tactic_outcomes,
            self.order,
            self.order_rev,
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
        return blocks(self.ledger, CAP, RESERVE)

    def reserve_call(self, provider):
        account = self.provider_ledgers[provider]
        reserve(self.ledger, account, RESERVE)
        self.save_budget()

    def request_probe(self, args):
        if self.busy or self.status in LIVE:
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
        now = self.clock()
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
        self.decider.submit(job)
        self.pending = submit(self.pending, None, None, True)

    def dispatch(self):
        if self.busy or self.status not in DISPATCHES:
            return
        now = self.clock()
        projectile_ids = {p["id"] for p in closing_projectiles(self.observation) if p.get("id")}
        new_projectile = bool(projectile_ids - self.decision_projectiles)
        if now - self.last_dispatch < 0.12 and not new_projectile:
            return
        enemies = {e["id"] for e in self.observation.get("enemies", [])}
        if self.last_action and not (
            enemies - self.decision_enemies
            or self.decision_health - self.observation["player"]["health"] >= 10
            or self.sim_ticks - self.decision_tick >= 105
            or new_projectile
            or (self.active_tactic and not self.active_tactic.get("prefetch_requested"))
        ):
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
        if self.config["controller"] != "local":
            if self.cap_blocks():
                self.finish("budget_limit")
                return
            self.reserve_call(self.config.get("provider_key", "gateway"))
        self.call_seq += 1
        job = dict(
            provider="local"
            if self.config["controller"] == "local"
            else self.config["provider_key"],
            controller=self.config["controller"],
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
        self.decider.submit(job)
        self.pending = submit(self.pending, job["id"], now, self.config["controller"] != "local")
        if (
            self.started_play
            and self.config.get("timing") == "pause_assisted"
            and self.pause_started is None
        ):
            self.pause_started = now
        self.last_dispatch = now
        if self.active_tactic:
            self.active_tactic["prefetch_requested"] = True
        self.decision_tick = self.sim_ticks
        self.decision_health = self.observation["player"]["health"]
        self.decision_enemies = enemies
        self.decision_projectiles.update(projectile_ids)

    def start_option(self, job, picks, selected, selector):
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
        if picks.get("fire") == "hold":
            option["controls"]["trigger"] = "hold_fire"
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
        if option["kind"] == "advance" and subject and subject.get("kind") == "frontier":
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
            committed_mono=self.clock(),
            snapshot=snapshot,
        )
        self.active_tactic = dict(
            option=option,
            before=copy.deepcopy(self.observation),
            started_tick=self.sim_ticks,
            request=job["id"],
            rubrics=picks,
            selector=selector,
            prefetch_requested=False,
        )
        self.tactic_decisions += 1
        self.log("tactic_start", **self.active_tactic)

    def accept_reply(self, job, r):
        self.pending, stalled = clear(self.pending)
        if self.status == "running" and self.pause_started is not None:
            self.paused_seconds += self.clock() - self.pause_started
            self.pause_started = None
        provider = job.get("provider", "gateway")
        if job.get("controller") != "local":
            settle(self.ledger, self.provider_ledgers[provider], r, RESERVE)
            self.save_budget()
        age = self.clock() - job["snapshot_mono"]
        self.latencies.append(r.get("elapsed_ms", 0))
        record = dict(
            provider=provider,
            id=job["id"],
            phase=job["phase"],
            tick=job["state"].get("tick", 0),
            at=utc(),
            snapshot_age_seconds=round(age, 3),
            decision_age_ticks=max(0, self.sim_ticks - job["state"].get("tick", 0)),
            questions=job["questions"],
            response=r,
            accepted=False,
            override_reasons=[],
        )
        if job["phase"] == "probe":
            try:
                validate_answers(job, r)
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
            self.store.append_probe(record)
            self.records.append(record)
            return
        validation_error = None
        if r.get("ok"):
            try:
                validate_answers(job, r)
            except Exception as e:
                validation_error = str(e)
        elif r.get("error_name") == "AI_InvalidResponseDataError":
            validation_error = r.get("error", "Invalid provider answer")
        if validation_error:
            record["validation_error"] = validation_error
        if job["episode"] != self.episode or self.status not in ACCEPTS_REPLY:
            record["override_reasons"].append("Episode/order/state changed while request in flight")
        elif not r.get("ok") or validation_error:
            transient = (
                (validation_error and self.recovery_attempts < 2)
                or r.get("status") in (429, 500, 502, 503, 504, 529)
                or r.get("error_name") in ("ConnectionError", "BrokenPipeError")
                or "timeout" in str(r.get("error", "")).lower()
            )
            if transient:
                self.recovery_attempts += 1
                self.recovery_total += 1
                self._move("recovering", "provider failure")
                self.end_tactic("invalidated")
                self.last_action = None
                if (
                    self.pause_started is None
                    and self.config.get("timing", "real_time") == "pause_assisted"
                ):
                    self.pause_started = self.clock()
                delay = min(60, 15 * 2 ** min(self.recovery_attempts - 1, 2))
                self.retry_at = self.clock() + delay
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
        elif stalled:
            self.capture()
            self._move("warming", "stalled reply discarded")
            self.decision_tick = self.sim_ticks - 105
            record["override_reasons"].append(
                "Provider stall: late answer discarded; fresh snapshot required"
            )
            self.log(
                "stall_answer_discarded", request=job["id"], game_tick=self.observation["tick"]
            )
        elif job["order_rev"] != self.order_rev or job.get("control_rev", 0) != self.control_rev:
            record["override_reasons"].append(
                "Order or objective expired while request in flight; request fresh state"
            )
            self.decision_tick = self.sim_ticks - 105
        else:
            try:
                answers = r["answers"]
                picks = {k: a["choice"] for k, a in answers.items()}
                selected = picks["tactic"]
                record["option"] = copy.deepcopy(
                    next(
                        (
                            option
                            for option in job["state"]["tactic_candidates"]
                            if option["id"] == selected
                        ),
                        None,
                    )
                )
                current_menu, self.omitted_tactics = build_menu(self.state_for_model(), self.nav)
                self.tactic_menu = current_menu
                if selected not in {option["id"] for option in current_menu}:
                    record["override_reasons"].append(
                        "Selected option is no longer in the current menu"
                    )
                    self.decision_tick = self.sim_ticks - 105
                    self.records.append(record)
                    self.log("response", record=record)
                    return
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
                    previous_selector = active["selector"]
                    active["request"] = job["id"]
                    active["selector"] = self.config["controller"]
                    active["rubrics"] = picks
                    self.last_action["committed_mono"] = self.clock()
                    self.last_action["source_request"] = job["id"]
                    if "dodge" in picks:
                        self.last_action["dodge"] = picks["dodge"]
                        active["option"]["controls"]["dodge"] = picks["dodge"]
                    if "fire" in picks:
                        requested_option = next(
                            option
                            for option in job["state"]["tactic_candidates"]
                            if option["id"] == selected
                        )
                        trigger = (
                            "hold_fire"
                            if picks["fire"] == "hold"
                            else requested_option["controls"]["trigger"]
                        )
                        self.last_action["trigger"] = trigger
                        active["option"]["controls"]["trigger"] = trigger
                    self.tactic_continuations += 1
                    self.log(
                        "tactic_continue",
                        request=job["id"],
                        tactic=selected,
                        previous_selector=previous_selector,
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
                self.start_option(job, picks, selected, self.config["controller"])
                if self.status == "warming":
                    self._move("running", "decision accepted")
                    self.error = None
                    if not self.started_play:
                        self.start_wall = self.clock()
                        self.started_play = True
                    if self.pause_started is not None:
                        elapsed = self.clock() - self.pause_started
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
            self.store.write_result(self.result)

    def start_local_fallback(self):
        state = self.state_for_model()
        menu, self.omitted_tactics = build_menu(state, self.nav)
        if not menu:
            return
        self.tactic_menu = menu
        state["tactic_candidates"] = menu
        selected = fixed_select(menu)
        job = {"id": f"local-fallback-{self.sim_ticks}", "state": state}
        questions = tactic_questions(menu, state)
        picks = {"tactic": selected}
        if "dodge" in questions:
            picks["dodge"] = "carry_on"
        if "fire" in questions:
            picks["fire"] = "hold"
        self.start_option(job, picks, selected, "local fallback")
        self.log("local_fallback", tick=self.sim_ticks, option=selected)

    def motor(self):
        values = [0] * len(BUTTONS)
        a = self.last_action
        p = self.observation.get("player", {})
        if self.status not in ("running", "waiting_provider"):
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
            self.start_local_fallback()
            a = self.last_action
            if not a:
                return values
        if not a:
            self.start_local_fallback()
            a = self.last_action
            if not a:
                return values
        p = copy.copy(p)
        p.update(
            x=self.engine.variable("POSITION_X"),
            y=self.engine.variable("POSITION_Y"),
            facing=self.engine.variable("ANGLE"),
        )
        result = calculate_motor(
            a,
            self.observation,
            p,
            self.nav,
            self.memory,
            self.sim_ticks,
            self.stationary_since,
            self.control_rev,
            self.use_latches,
            self.use_pulses,
            self.approach_facing_ticks,
        )
        self.nav = result.nav
        self.use_latches = result.use_latches
        self.use_pulses = result.use_pulses
        self.approach_facing_ticks = result.approach_facing_ticks
        self.controls = result.controls
        for kind, data in result.events:
            self.log(kind, **data)
        return result.buttons

    def episode_costs(self):
        if not hasattr(self, "run_budget_start"):
            return {}
        if self.config.get("controller") == "local":
            return dict(
                reported_episode_cost_usd=0,
                episode_provider_calls=0,
                provider_episode_cost=dict(
                    provider="local",
                    charged_or_reserved_usd=0,
                    reported_usd=0,
                    estimated_usd=0,
                    basis="Local rule uses no provider",
                ),
            )
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
        if self.status not in LIVE:
            return
        if reason == "dead" and self.observation.get("player"):
            for key, var in [
                ("health", "HEALTH"),
                ("armor", "ARMOR"),
                ("damage_taken", "DAMAGE_TAKEN"),
                ("hits_taken", "HITS_TAKEN"),
            ]:
                self.observation["player"][key] = int(self.engine.variable(var))
        self.end_tactic(reason)
        self._move(reason, "run finished")
        self.controls = dict(released=True, reason=reason)
        self.last_action = None
        self.result = dict(
            reason=reason,
            at=utc(),
            episode=self.episode,
            config=self.config,
            game_seconds=round(getattr(self, "sim_ticks", 0) / 35, 2),
            wall_seconds=round(self.clock() - getattr(self, "start_wall", self.clock()), 2),
            player=self.observation.get("player"),
            progress=self.observation.get("progress"),
            budget=copy.deepcopy(self.ledger),
        )
        terminal = self.engine.status()
        self.result["terminal_evidence"] = dict(
            finished=terminal.finished,
            dead=terminal.dead,
            timeout=terminal.timeout,
            last_reward=terminal.reward,
            total_reward=terminal.total_reward,
            configured_map_exit_reward=terminal.exit_reward,
        )
        self.result["paused_seconds"] = round(
            self.paused_seconds
            + (self.clock() - self.pause_started if self.pause_started is not None else 0),
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
        self.result["pending_reservation_usd"] = (
            RESERVE if self.pending and self.pending.billed else 0
        )
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
            self.store.write_result(self.result)
        if self.store.record_path() is not None:
            self.engine.close()

    def tick(self):
        with self.lock:
            while not self.commands.empty():
                cmd, args = self.commands.get()
                if cmd == "start":
                    if self.busy or self.status in LIVE:
                        continue
                    self.begin_run(args)
                elif cmd == "probe":
                    self.request_probe(args)
                elif cmd == "stop":
                    self.finish("stopped")
                elif cmd == "pause" and self.status in CAN_PAUSE:
                    self._move("paused", "operator pause")
                    self.control_rev += 1
                    if self.pause_started is None:
                        self.pause_started = self.clock()
                    self.last_action = None
                    self.engine.release()
                    self.controls = {"released": True, "reason": "paused"}
                elif cmd == "resume" and self.status == "paused":
                    self.capture()
                    self._move("warming", "operator resume")
                elif cmd == "orders":
                    text = args.get("text")
                    if not isinstance(text, str) or len(text) > MAX_ORDER_CHARS:
                        logging.getLogger(__name__).warning(
                            "Order rejected: submitted length %s; limit %d",
                            len(text) if isinstance(text, str) else "unavailable",
                            MAX_ORDER_CHARS,
                        )
                        self.error = f"Order must be text of at most {MAX_ORDER_CHARS} characters"
                        continue
                    self.order = text
                    self.order_rev += 1
                    self.clear_objective("orders")
                    self.log("orders", text=self.order, revision=self.order_rev)
            if (
                self.pending is not None
                and self.pending.since is not None
                and not self.pending.stalled
                and self.started_play
                and self.config.get("timing", "real_time") == "real_time"
                and self.status in DISPATCHES
                # pre-rework:harness.py:777 used 2 s as the real-time control lease, not a provider SLA.
                and self.clock() - self.pending.since >= 2
            ):
                self.pending = mark_stalled(self.pending)
                self.stall_total += 1
                self._move("waiting_provider", "provider stalled")
                self.log(
                    "provider_stall",
                    request=self.pending.request_id,
                    game_tick=self.observation.get("tick"),
                    seconds=2,
                    release="on reply: discard a stale success or enter provider error recovery",
                )
            while reply := self.decider.poll():
                job, response = reply
                self.log(
                    "provider_return",
                    request=job["id"],
                    request_episode=job["episode"],
                    response=response,
                )
                self.accept_reply(job, response)
            if self.status == "recovering" and self.clock() >= self.retry_at:
                self.capture()
                self._move("warming", "recovery retry")
                self.log(
                    "recovery_request",
                    attempt=self.recovery_attempts,
                    game_tick=self.observation["tick"],
                )
            if (
                self.status in ("running", "waiting_provider")
                and self.started_play
                and not (self.busy and self.config.get("timing") == "pause_assisted")
            ):
                buttons = self.motor()
                if self.controls.get("MOVE", {}).get(
                    "model_choice"
                ) == "destination" and self.observation.get("progress", {}).get("stuck"):
                    self.failed_route_ticks += 1
                if self.controls.get("overrides"):
                    self.intervention_ticks += 1
                step = self.engine.step(buttons)
                self.sim_ticks += 1
                if step.terminal:
                    if step.dead and not step.finished:
                        self.capture()
                    self.finish(end_reason(step))
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
                and self.status in ("recovering", "warming")
                and self.started_play
                and not self.engine.status().finished
            ):
                # Real time: the world runs on with released controls while a decision is awaited; only the assisted timing mode may pause it.
                step = self.engine.step([0] * len(BUTTONS))
                self.sim_ticks += 1
                self.free_run_ticks += 1
                if step.terminal:
                    if step.dead and not step.finished:
                        self.capture()
                    self.finish(end_reason(step))
                elif self.sim_ticks / 35 >= self.max_seconds:
                    self.capture()
                    self.finish("duration_limit")
                elif self.sim_ticks % 2 == 0:
                    self.capture()
            self.dispatch()
            self.publish_snapshot()

    def engine_loop(self):
        try:
            self.new_game()
            deadline = self.clock()
            while not self.stop_event.is_set():
                self.tick()
                deadline += 1 / 35
                self.sleep(max(0, deadline - self.clock()))
                if deadline < self.clock() - 0.2:
                    deadline = self.clock()
        except Exception as e:
            traceback.print_exc()
            self.error = str(e)
            try:
                self.finish("engine_error")
            except Exception:
                pass
            self._move("engine_error", "engine loop failed")
            # A game failure must not strand billing or discard a paid response.
            if self.busy:
                try:
                    reply = self.decider.poll(timeout=21)
                    if reply:
                        self.accept_reply(*reply)
                    else:
                        self.log(
                            "provider_unsettled_after_engine_error",
                            reserved_usd=RESERVE if self.pending and self.pending.billed else 0,
                        )
                except Exception:
                    self.log(
                        "provider_unsettled_after_engine_error",
                        reserved_usd=RESERVE if self.pending and self.pending.billed else 0,
                    )
        finally:
            try:
                with self.lock:
                    if self.stop_event.is_set():
                        self.finish("stopped")
                    self.publish_snapshot()
            finally:
                self.engine.close()

    def shutdown(self):
        self.stop_event.set()
        self.engine_thread.join(timeout=3)
        self.decider.close()

    def _build_view(self):
        with self.lock:
            calls = list(self.records)
            lat = list(self.latencies)
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
                auth_available=self.config.get("controller") == "local"
                or bool(
                    os.getenv(
                        "TYPESAFE_API_KEY"
                        if self.config.get("provider_key") == "typesafe"
                        else "AI_GATEWAY_API_KEY"
                    )
                ),
            )

    def publish_snapshot(self):
        state = self._build_view()
        tick = state.get("observation", {}).get("tick", 0)
        self.view_port.publish(state, self.frame_image, tick)

    def view(self):
        current = self.view_port.read()
        return copy.deepcopy(current[0] if current is not None else self._build_view())

    def frame_snapshot(self):
        current = self.view_port.read()
        return (current[1], current[2]) if current is not None else (b"", 0)
