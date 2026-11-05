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
    def load_ledger(self) -> dict: ...
    def save_ledger(self, ledger: dict) -> None: ...
    def open_run(self, revision: str) -> None: ...
    def append_trace(self, event: dict) -> None: ...
    def write_result(self, result: dict) -> None: ...


class View(Protocol):
    def publish(self, snapshot: dict, frame: bytes, tick: int) -> None: ...


Clock = Callable[[], float]


def end_reason(step: Step) -> str | None:
    if step.dead:
        return "dead"
    if step.finished:
        if step.timeout:
            return "engine_timeout"
        return "level_complete" if step.level_done else "episode_ended_unverified"
    return None
