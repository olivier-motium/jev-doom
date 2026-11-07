"""The single provider request in flight, including its control-lease state."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Pending:
    request_id: int | None
    since: float | None
    billed: bool
    stalled: bool = False


def submit(
    current: Pending | None,
    request_id: int | None,
    since: float | None,
    billed: bool,
) -> Pending:
    if current is not None:
        raise ValueError("A provider request is already pending")
    return Pending(request_id, since, billed)


def mark_stalled(current: Pending) -> Pending:
    return replace(current, stalled=True)


def clear(current: Pending | None) -> tuple[None, bool]:
    return None, current.stalled if current is not None else False
