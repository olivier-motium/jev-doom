"""Drive the unmodified Harness in one thread, with a tick clock and a scripted, model-free Jev.

The real engine runs. Threads are not started, `time` is a tick clock, every DoomGame is wrapped
to record one row per engine tick, and `Harness.jobs` answers each request itself. No test can
reach a provider: both keys are blank and starting gateway.mjs fails the test.
"""

import copy
import hashlib
import json
import os
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
RECORD = os.environ.get("JEVDOOM_RECORD_GOLDEN") == "1"
ORDER_TEXT = "Hold position and stay alive. Retreat from close threats."
TIMEOUT_REPLY = dict(
    ok=False, error="Gateway request timeout", error_name="TimeoutError", cost_usd=None
)


@pytest.fixture(autouse=True)
def no_billed_calls(monkeypatch):
    """Blank both provider keys and refuse to start the Node gateway."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "")
    real_popen = subprocess.Popen

    def popen(args, *rest, **kwargs):
        words = args if isinstance(args, (list, tuple)) else [args]
        if any("gateway.mjs" in str(word) for word in words):
            raise AssertionError("A test started gateway.mjs; give it a fake child instead")
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)


def require_hash_seed():
    if os.environ.get("PYTHONHASHSEED") != "0":
        pytest.skip("engine goldens need PYTHONHASHSEED=0 so string-set order repeats")


def normal(value):
    """The JSON form of a value: tuples become lists, so recorded and live values compare."""
    return json.loads(json.dumps(value))


@dataclass
class Case:
    name: str
    map: str = "MAP01"
    seed: int = 17
    skill: int = 3
    ticks: int = 1040
    seconds: float = 90
    delay: int = 1  # engine ticks from request to reply; 1 means the next tick
    controller: str = "semantic_tactics"
    timing: str = "real_time"
    fail: tuple = ()  # request ids answered with a provider timeout
    order_at: int | None = None  # game tick after which the order changes while a call is pending


class TickClock:
    """Stands in for the time module inside harness.py. Each sleep ends one engine loop."""

    def __init__(self):
        self.now = 1000.0
        self.after_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.after_sleep()


class IdleThread:
    """Stands in for threading.Thread while the Harness is built: nothing starts."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass


class RecordingGame:
    """A real DoomGame that records (tic, buttons, x, y, angle, health) for every tick advanced."""

    def __init__(self, game, rows):
        self._game = game
        self._rows = rows
        self._buttons = None

    def __getattr__(self, name):
        return getattr(self._game, name)

    def set_action(self, buttons):
        self._buttons = list(buttons)
        return self._game.set_action(buttons)

    def make_action(self, buttons, tics=1):
        reward = self._game.make_action(buttons, tics)
        self._record(buttons)
        return reward

    def advance_action(self, *args):
        self._game.advance_action(*args)
        self._record(self._buttons)

    def _record(self, buttons):
        import vizdoom

        def variable(name):
            return round(
                float(self._game.get_game_variable(getattr(vizdoom.GameVariable, name))), 3
            )

        self._rows.append(
            [
                self._game.get_episode_time(),
                None if buttons is None else [round(float(b), 4) for b in buttons],
                variable("POSITION_X"),
                variable("POSITION_Y"),
                variable("ANGLE"),
                variable("HEALTH"),
            ]
        )


def fixed_answer(job):
    """Model-free Jev: fixed_select for every tactic question, carry_on for dodge, one-hot."""
    from jevdoom.core.tactics import fixed_select

    pick = fixed_select(job["state"]["tactic_candidates"])
    answers = {}
    for key, question in job["questions"].items():
        choice = {"dodge": "carry_on", "fire": "allow"}.get(key, pick)
        probabilities = {option: float(option == choice) for option in question["criteria"]}
        answers[key] = dict(type="choice", choice=choice, probabilities=probabilities)
    return dict(ok=True, answers=answers, cost_usd=0.001, elapsed_ms=50.0)


class ScriptedJev:
    """Stands in for Harness.jobs and the provider thread. Each reply lands `delay` ticks later."""

    def __init__(self, case):
        self.case = case
        self.pending = []
        self.completed = []

    def submit(self, job):
        if job["id"] in self.case.fail:
            reply = TIMEOUT_REPLY
        elif self.case.controller == "local":
            from jevdoom.adapters.local import LocalDecider

            reply = LocalDecider().ask(job)
        else:
            reply = fixed_answer(job)
        self.pending.append([self.case.delay, job, copy.deepcopy(reply)])

    def poll(self, timeout=0):
        return self.completed.pop(0) if self.completed else None

    def close(self):
        pass

    def tick(self):
        for item in list(self.pending):
            item[0] -= 1
            if item[0] <= 0:
                self.pending.remove(item)
                _, job, reply = item
                self.completed.append((job, reply))


@dataclass
class Run:
    harness: object
    rows: list
    events: list

    def metrics(self):
        h = self.harness
        return dict(
            end=h.status,
            game_ticks=h.sim_ticks,
            requests=h.call_seq,
            distance=round(h.distance, 1),
            seen_cells=len(h.nav.seen),
            use_pulses=sum(h.use_pulses.values()),
            deaths=int(h.status == "dead"),
            idle_ticks=sum(not any(row[1] or ()) for row in self.rows),
        )

    def summary(self):
        rows = json.dumps(self.rows, separators=(",", ":"))
        return dict(
            metrics=self.metrics(),
            rows_sha256=hashlib.sha256(rows.encode()).hexdigest(),
            every_35th_row=[json.dumps(row, separators=(",", ":")) for row in self.rows[::35]],
        )

    def records(self):
        return [e["record"] for e in self.events if e["type"] == "response"]


def check_golden(name, run):
    """Compare a run with tests/golden/<name>.json, or write it when JEVDOOM_RECORD_GOLDEN=1."""
    path = GOLDEN / f"{name}.json"
    actual = run.summary()
    if RECORD:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=1) + "\n")
        return
    expected = json.loads(path.read_text())
    assert actual["metrics"] == expected["metrics"]
    for got, want in zip(actual["every_35th_row"], expected["every_35th_row"]):
        assert got == want, "first sampled row that differs"
    assert actual["rows_sha256"] == expected["rows_sha256"]


class MemoryStore:
    def __init__(self):
        self.events = []
        self.result = None
        self.started = False
        self.ledger = None

    def load_ledger(self):
        return copy.deepcopy(self.ledger)

    def save_ledger(self, ledger):
        self.ledger = copy.deepcopy(ledger)

    def open_run(self, name):
        self.started = True
        self.events = []
        return Path("memory") / name

    def append_trace(self, event):
        if self.started:
            self.events.append(normal(event))

    def record_path(self):
        return None

    def append_probe(self, record):
        pass

    def write_result(self, result):
        self.result = normal(result)


@pytest.fixture
def play(monkeypatch):
    """Run a scripted decider with a real engine, memory store, and tick clock."""
    from jevdoom import run as harness
    import vizdoom

    real_game = vizdoom.DoomGame

    def run(case, recorder=None):
        rows = []
        clock = TickClock()
        jev = ScriptedJev(case)
        store = MemoryStore()
        monkeypatch.setattr(vizdoom, "DoomGame", lambda: RecordingGame(real_game(), rows))
        with monkeypatch.context() as patch:
            patch.setattr(threading, "Thread", IdleThread)
            h = harness.Run(decider=jev, store=store, clock=clock.monotonic, sleep=clock.sleep)
        h.provider_health["gateway"] = True
        start = dict(
            map=case.map,
            seed=case.seed,
            skill=case.skill,
            seconds=case.seconds,
            controller=case.controller,
            timing=case.timing,
            provider="gateway",
        )
        h.commands.put(("start", start))
        if recorder:
            recorder.install(monkeypatch, h, case)
        loops = 0
        ordered = False

        def after_sleep():
            nonlocal loops, ordered
            jev.tick()
            loops += 1
            if (
                case.order_at is not None
                and not ordered
                and h.sim_ticks >= case.order_at
                and jev.pending
            ):
                h.commands.put(("orders", dict(text=ORDER_TEXT)))
                ordered = True
            if h.result or h.sim_ticks >= case.ticks or loops > 3 * case.ticks + 700:
                h.stop_event.set()

        clock.after_sleep = after_sleep
        h.engine_loop()
        assert h.status != "engine_error", h.error
        assert h.result or h.sim_ticks >= case.ticks, f"run stalled at tick {h.sim_ticks}"
        return Run(h, rows, store.events)

    return run


# Observation fixtures: the pure decision functions on recorded observations, without the engine.


def nav_snapshot(nav, position):
    """What build_menu and measure read from the Navigator, including cached routes from here."""
    here = nav.nearest(position)
    return dict(
        observed_lines=list(nav.observed_lines.values()),
        observed_heights=[[sector, *value] for sector, value in nav.observed_heights.items()],
        cells=[[x, y, sector] for (x, y), sector in nav.obs_cells.items()],
        seen=[list(cell) for cell in nav.seen],
        failed={key: count for key, count in nav.failed.items() if count},
        used={key: count for key, count in nav.used.items() if count},
        obstacles=[list(item) for item in nav.obstacle_signature],
        blocked_cells=[list(cell) for cell in nav.blocked_cells],
        walls_wkb=nav._observed_union.wkb_hex,
        blocked_wkb=nav._observed_blocked.wkb_hex,
        routes=[[list(a), list(b), path] for (a, b), path in nav.cache.items() if a == here],
    )


def restore_nav(nav, snap):
    """Put a fresh Navigator for the same map into the recorded state."""
    from shapely import wkb
    from shapely.prepared import prep

    nav.observed_lines = {line["id"]: line for line in copy.deepcopy(snap["observed_lines"])}
    nav.observed_heights = {
        sector: (floor, ceiling, tick) for sector, floor, ceiling, tick in snap["observed_heights"]
    }
    nav.obs_cells = {(x, y): sector for x, y, sector in snap["cells"]}
    nav.seen = {tuple(cell) for cell in snap["seen"]}
    nav.failed = defaultdict(int, snap["failed"])
    nav.used = defaultdict(int, snap["used"])
    nav._nb_cache = {}
    nav._edge_cache = {}
    nav.obstacle_signature = None
    nav.update_obstacles([dict(id=i, x=x, y=y) for i, x, y in snap["obstacles"]])
    nav.blocked_cells = {tuple(cell) for cell in snap["blocked_cells"]}
    nav._observed_union = wkb.loads(snap["walls_wkb"], hex=True)
    nav._observed_blocked = wkb.loads(snap["blocked_wkb"], hex=True)
    nav._union_prep = prep(nav._observed_union)
    nav._blocked_prep = prep(nav._observed_blocked)
    nav._reach = None
    nav.cache = {(tuple(a), tuple(b)): copy.deepcopy(path) for a, b, path in snap["routes"]}


def model_context(recorded):
    """The Harness fields state_for_model reads, as a stand-in object."""
    return SimpleNamespace(
        observation=copy.deepcopy(recorded["observation"]),
        objective=copy.deepcopy(recorded["objective"]),
        config=dict(mode=recorded["mode"]),
        exploration_target=recorded["exploration_target"],
        tactic_outcomes=copy.deepcopy(recorded["tactic_outcomes"]),
        order=recorded["order"],
        order_rev=recorded["order_rev"],
    )


def reply_variants(questions):
    """Replies to a question battery: valid, and broken in each way validate_answers checks."""
    one_hot = {}
    for key, question in questions.items():
        choice = next(iter(question["criteria"]))
        probabilities = {option: float(option == choice) for option in question["criteria"]}
        one_hot[key] = dict(type="choice", choice=choice, probabilities=probabilities)
    yield "one_hot", dict(ok=True, answers=one_hot)
    yield "provider_error", dict(ok=False, error="Gateway request timeout")
    yield "missing_answer", dict(ok=True, answers=dict(list(one_hot.items())[1:]))
    wide = next((k for k, q in questions.items() if len(q["criteria"]) >= 2), None)
    if wide is None:
        return
    first, second, *rest = questions[wide]["criteria"]

    def spread(top, low):
        answers = copy.deepcopy(one_hot)
        rest_share = round((1 - top - low) / len(rest), 6) if rest else 0
        probabilities = {first: low, second: top, **{option: rest_share for option in rest}}
        answers[wide] = dict(type="choice", choice=first, probabilities=probabilities)
        return answers

    near_tie = dict(
        ok=True, answers=spread(0.5, 0.49 if rest else 0.5), rounding=dict(probabilityDecimals=2)
    )
    yield "near_tie", near_tie
    yield (
        "not_highest",
        dict(
            ok=True,
            answers=spread(0.6, 0.4 if not rest else 0.3),
            rounding=dict(probabilityDecimals=2),
        ),
    )
    bad_sum = copy.deepcopy(one_hot)
    bad_sum[wide]["probabilities"][bad_sum[wide]["choice"]] = 0.8
    yield "bad_sum", dict(ok=True, answers=bad_sum)


def validation_outcomes(questions):
    """validate_answers on each reply variant: its validation record or its error text."""
    from jevdoom.core.protocol import validate_answers

    out = {}
    for name, reply in reply_variants(questions):
        try:
            validate_answers(dict(questions=questions), reply)
            out[name] = dict(validation=reply["validation"])
        except ValueError as error:
            out[name] = dict(error=str(error))
    return normal(out)


class DecisionRecorder:
    """Records decision fixtures at chosen requests. The run itself must not change."""

    def __init__(self, picks):
        self.picks = set(picks)
        self.calls = 0
        self.current = None
        self.fixtures = []

    def install(self, monkeypatch, h, case):
        from jevdoom import run as harness

        real_menu, real_questions, real_measure = (
            harness.build_menu,
            harness.tactic_questions,
            harness.measure,
        )

        def build_menu(state, nav):
            self.calls += 1
            if self.calls not in self.picks:
                return real_menu(state, nav)
            player = h.observation["player"]
            fixture = dict(
                source=case.name,
                request=h.call_seq + 1,
                map=case.map,
                harness=normal(
                    dict(
                        observation=h.observation,
                        objective=h.objective,
                        mode=h.config["mode"],
                        exploration_target=getattr(h, "exploration_target", None),
                        tactic_outcomes=list(h.tactic_outcomes),
                        order=h.order,
                        order_rev=h.order_rev,
                    )
                ),
                nav=normal(nav_snapshot(nav, [player["x"], player["y"]])),
                active=None,
                expected=dict(state=normal(state)),
            )
            menu, omitted = real_menu(state, nav)
            fixture["expected"].update(menu=normal(menu), omitted=normal(omitted), measure=None)
            active = h.active_tactic
            if active:
                # Measuring here fills route caches; put them back so the run is unchanged.
                routes, neighbours = dict(nav.cache), dict(nav._nb_cache)
                elapsed = (h.sim_ticks - active["started_tick"]) / 35
                outcome = real_measure(
                    active["option"], active["before"], h.observation, nav, "replaced", elapsed
                )
                nav.cache.clear()
                nav.cache.update(routes)
                nav._nb_cache.clear()
                nav._nb_cache.update(neighbours)
                fixture["active"] = normal(
                    dict(option=active["option"], before=active["before"], elapsed=elapsed)
                )
                fixture["expected"]["measure"] = normal(outcome)
            self.current = fixture
            return menu, omitted

        def tactic_questions(menu, state=None):
            questions = real_questions(menu, state)
            if self.current is not None:
                self.current["expected"]["questions"] = normal(questions)
                self.current["expected"]["validation"] = validation_outcomes(questions)
                self.fixtures.append(self.current)
                self.current = None
            return questions

        monkeypatch.setattr(harness, "build_menu", build_menu)
        monkeypatch.setattr(harness, "tactic_questions", tactic_questions)
