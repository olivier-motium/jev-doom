"""The single provider request in flight, including its control-lease state."""

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True)
class Pending:
    kind: Literal["decision", "probe"]
    request_id: int | None
    since: float | None
    billed: bool
    stalled: bool = False


def submit(
    current: Pending | None,
    kind: Literal["decision", "probe"],
    request_id: int | None,
    since: float | None,
    billed: bool,
) -> Pending:
    if current is not None:
        raise ValueError("A provider request is already pending")
    return Pending(kind, request_id, since, billed)


def mark_stalled(current: Pending) -> Pending:
    return replace(current, stalled=True)


def clear(current: Pending | None) -> tuple[None, bool]:
    return None, current.stalled if current is not None else False
