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
    def __init__(self, run_dir):
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
        self.records = sorted(
            (event["record"] for event in events if event["type"] == "response"),
            key=lambda record: record.get("tick", 0) + record.get("decision_age_ticks", 0),
        )
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.view_port = SnapshotView()
        self.engine = VizdoomEngine(Path(__file__).resolve().parents[2] / "assets/freedoom2.wad")
        self.recording = recording
        self.error = None
        self.stopped = False
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
        snapshot = dict(
            status=status,
            error=None,
            config=self.config,
            observation=observation,
            records=copy.deepcopy(shown),
            objective=None,
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
        self.view_port.publish(snapshot, image.getvalue(), frame.tic)

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
                self.stop_event.wait(1 / 35)
            self.stopped = self.stop_event.is_set()
            self._publish(
                frame, "replay_stopped" if self.stop_event.is_set() else "replay_finished", shown
            )
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.engine.close()

    def view(self):
        with self.start_lock:
            if self.thread.ident is None and not self.stop_event.is_set():
                self.thread.start()
        snapshot = self.view_port.read()
        if self.error is not None:
            state = copy.deepcopy(snapshot[0]) if snapshot else {"config": self.config}
            state.update(status="replay_error", error=self.error, replay=True)
            return state
        if self.stopped or (self.stop_event.is_set() and self.thread.ident is None):
            state = copy.deepcopy(snapshot[0]) if snapshot else {"config": self.config}
            state.update(status="replay_stopped", replay=True)
            return state
        return (
            copy.deepcopy(snapshot[0]) if snapshot else {"status": "booting", "config": self.config}
        )

    def frame_snapshot(self):
        snapshot = self.view_port.read()
        return (snapshot[1], snapshot[2]) if snapshot else (b"", 0)

    def shutdown(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=3)
