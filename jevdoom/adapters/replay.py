"""Play a local ViZDoom recording and expose its recorded decisions to the dashboard."""

import copy
import io
import json
import queue
import threading
from pathlib import Path

from PIL import Image

from jevdoom.adapters.snapshot import SnapshotView
from jevdoom.adapters.vizdoom_engine import VizdoomEngine


class ReplaySession:
    def __init__(self, run_dir, frame_interval=1 / 35):
        self.run_dir = Path(run_dir).resolve()
        recording = self.run_dir / "episode.lmp"
        trace = self.run_dir / "trace.jsonl"
        result_path = self.run_dir / "result.json"
        if not recording.is_file() or not trace.is_file() or not result_path.is_file():
            raise ValueError(
                "Replay needs episode.lmp, trace.jsonl and result.json in the run directory"
            )
        events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
        start = next((event for event in events if event["type"] == "episode_start"), None)
        if start is None:
            raise ValueError("Replay trace has no episode_start")
        self.result = json.loads(result_path.read_text())
        self.config = dict(start["config"], controller="replay", provider="Replay", model="Replay")
        self.applied_options = []
        for index, event in enumerate(events):
            if event["type"] == "tactic_start":
                option = event.get("option")
                tick = event.get("started_tick", event.get("before", {}).get("tick"))
            elif event["type"] == "response" and event["record"].get("accepted"):
                record = event["record"]
                option = record.get("option")
                tick = record.get("tick", 0) + record.get("decision_age_ticks", 0)
            else:
                continue
            if isinstance(option, dict) and isinstance(tick, (int, float)):
                self.applied_options.append((tick, index, option))
        self.records = sorted(
            (event["record"] for event in events if event["type"] == "response"),
            key=lambda record: record.get("tick", 0) + record.get("decision_age_ticks", 0),
        )
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.view_port = SnapshotView()
        self.engine = VizdoomEngine(Path(__file__).resolve().parents[2] / "assets/freedoom2.wad")
        self.recording = recording
        self.frame_interval = frame_interval
        self.error = None
        self.state = "booting"
        self.start_lock = threading.Lock()
        self.thread = threading.Thread(target=self._play, daemon=True)

    def _publish(self, frame, status, shown):
        if frame is None:
            return
        image = io.BytesIO()
        Image.fromarray(frame.screen_buffer).save(image, format="JPEG", quality=82)
        values = frame.variables
        player = dict(
            x=values.get("POSITION_X"),
            y=values.get("POSITION_Y"),
            facing=values.get("ANGLE"),
            health=values.get("HEALTH"),
            armor=values.get("ARMOR"),
            ammo=values.get("SELECTED_WEAPON_AMMO"),
        )
        observation = dict(
            tick=frame.tic, player=player, progress={"game_seconds": round(frame.tic / 35, 2)}
        )
        applied = [entry for entry in self.applied_options if entry[0] <= frame.tic]
        latest = max(applied, key=lambda entry: entry[:2]) if applied else None
        snapshot = dict(
            status=status,
            error=None,
            config=self.config,
            observation=observation,
            records=copy.deepcopy(shown),
            objective=None,
            replay_option=copy.deepcopy(latest[2]) if latest else None,
            tactics={},
            map={},
            controls={},
            order="",
            busy=False,
            budget=self.result.get("budget", {}),
            latency={"last_ms": shown[-1]["response"].get("elapsed_ms") if shown else None},
            auth_providers={},
            provider_health={},
            result=self.result if status == "replay_finished" else {},
            replay=True,
        )
        with self.start_lock:
            self.view_port.publish(snapshot, image.getvalue(), frame.tic)
            self.state = status

    def _play(self):
        shown = []
        try:
            self.engine.start(
                self.config["map"],
                self.config["skill"],
                self.config["seed"],
                replay_path=self.recording,
            )
            frame = self.engine.frame()
            if frame is None and not self.stop_event.is_set():
                raise RuntimeError("Replay produced no initial frame")
            while not self.stop_event.is_set() and frame is not None:
                while (
                    self.records
                    and self.records[0].get("tick", 0)
                    + self.records[0].get("decision_age_ticks", 0)
                    <= frame.tic
                ):
                    shown.append(self.records.pop(0))
                self._publish(frame, "replaying", shown)
                step = self.engine.replay_step()
                if step.terminal:
                    break
                frame = self.engine.frame()
                if frame is None and not self.stop_event.is_set():
                    raise RuntimeError("Replay ended without a terminal step")
                self.stop_event.wait(self.frame_interval)
            if self.stop_event.is_set():
                with self.start_lock:
                    self.state = "replay_stopped"
            self._publish(
                frame, "replay_stopped" if self.stop_event.is_set() else "replay_finished", shown
            )
        except Exception as exc:
            with self.start_lock:
                self.error = str(exc)
                self.state = "replay_error"
        finally:
            self.engine.close()

    def view(self):
        with self.start_lock:
            if self.thread.ident is None and not self.stop_event.is_set():
                self.thread.start()
        with self.start_lock:
            snapshot = self.view_port.read()
            status = self.state
            error = self.error
        if status == "replay_error":
            state = copy.deepcopy(snapshot[0]) if snapshot else {"config": self.config}
            state.update(status=status, error=error, replay=True)
            return state
        if status == "replay_stopped":
            state = copy.deepcopy(snapshot[0]) if snapshot else {"config": self.config}
            state.update(status=status, replay=True)
            return state
        state = copy.deepcopy(snapshot[0]) if snapshot else {"config": self.config}
        state["status"] = status
        return state

    def frame_snapshot(self):
        snapshot = self.view_port.read()
        return (snapshot[1], snapshot[2]) if snapshot else (b"", 0)

    def shutdown(self):
        with self.start_lock:
            self.stop_event.set()
            started = self.thread.ident is not None
            if not started:
                self.state = "replay_stopped"
        if started:
            self.thread.join(timeout=3)
