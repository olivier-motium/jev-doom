"""Contracts between the application and its runtime adapters."""

from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class Frame:
    tic: int
    variables: dict[str, float]
    labels: tuple[Any, ...]
    objects: tuple[Any, ...]
    sectors: tuple[Any, ...]
    depth_buffer: Any
    screen_buffer: Any


@dataclass(frozen=True)
class Step:
    frame: Frame | None
    terminal: bool
    dead: bool
    timeout: bool
    reward: float
    level_done: bool
    total_reward: float = 0
    exit_reward: float = 1
    finished: bool = False


class Engine(Protocol):
    def start(
        self, map_name: str, skill: int, seed: int, record_path: str | None = None
    ) -> Frame: ...
    def navigation_data(self) -> bytes: ...
    def step(self, buttons: list[float]) -> Step: ...
    def frame(self) -> Frame | None: ...
    def variable(self, name: str) -> float: ...
    def status(self) -> Step: ...
    def release(self) -> None: ...
    def close(self) -> None: ...


class Decider(Protocol):
    def submit(self, job: dict) -> None: ...
    def poll(self, timeout: float = 0) -> tuple[dict, dict] | None: ...
    def close(self) -> None: ...


class Store(Protocol):
    def load_ledger(self) -> dict | None: ...
    def save_ledger(self, ledger: dict) -> None: ...
    def open_run(self, revision: str) -> Any: ...
    def record_path(self) -> Any: ...
    def append_trace(self, event: dict) -> None: ...
    def append_probe(self, record: dict) -> None: ...
    def read_result(self, run_name: str) -> dict: ...
    def write_result(self, result: dict, run_name: str | None = None) -> None: ...


class View(Protocol):
    def publish(self, snapshot: dict, frame: Any, tick: int) -> None: ...
    def read(self) -> tuple[dict, Any, int] | None: ...


Clock = Callable[[], float]


def end_reason(step: Step) -> str | None:
    if step.dead:
        return "dead"
    if not step.terminal:
        return None
    if step.timeout:
        return "engine_timeout"
    return "level_complete" if step.level_done else "episode_ended_unverified"
