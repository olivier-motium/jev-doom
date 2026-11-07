"""Run states and legal transitions.

booting          Initial state; engine initialization leads to ready.
ready            A game is loaded; a start leads to warming.
warming          Awaiting a decision after start, resume, retry or a stall.
                 started_play is false only before the first accepted decision;
                 once true, real-time play can continue with controls released.
running          An accepted decision is controlling play.
waiting_provider A real-time request has stalled; its reply is discarded or
                 enters recovery, and a pause or terminal outcome can intervene.
paused           An operator pause; resume returns to warming.
recovering       Provider failure; retry returns to warming after backoff.
terminal         A finish reason; a new game returns to ready. engine_error
                 ends the engine loop, so it cannot be restarted there.

pause_started tracks elapsed operator or assisted-timing pauses independently
of status. retry_at sets the recovery deadline; recovery_attempts counts
consecutive provider failures until an accepted warming decision resets it.
An engine error can also occur during initialization or after a finish. A
same-state move records no change, including the final engine_error assignment.
"""

BOOTING = "booting"
READY = "ready"
WARMING = "warming"
RUNNING = "running"
WAITING_PROVIDER = "waiting_provider"
PAUSED = "paused"
RECOVERING = "recovering"
ENGINE_ERROR = "engine_error"

LIVE = frozenset({WARMING, RUNNING, WAITING_PROVIDER, PAUSED, RECOVERING})
ACCEPTS_REPLY = frozenset({WARMING, RUNNING, WAITING_PROVIDER})
CAN_PAUSE = LIVE - {PAUSED}
DISPATCHES = frozenset({WARMING, RUNNING})
TERMINAL = frozenset(
    {
        "stopped",
        "dead",
        "engine_timeout",
        "level_complete",
        "episode_ended_unverified",
        "duration_limit",
        "invalid_answer",
        "provider_error",
        "request_limit",
        "budget_limit",
        ENGINE_ERROR,
    }
)

TRANSITIONS = {
    BOOTING: frozenset({READY, ENGINE_ERROR}),
    READY: frozenset({READY, WARMING, ENGINE_ERROR}),
    WARMING: frozenset({WARMING, RUNNING, WAITING_PROVIDER, PAUSED, RECOVERING}) | TERMINAL,
    RUNNING: frozenset({WAITING_PROVIDER, PAUSED, RECOVERING}) | TERMINAL,
    WAITING_PROVIDER: frozenset({WARMING, PAUSED, RECOVERING}) | TERMINAL,
    PAUSED: frozenset({WARMING}) | TERMINAL,
    RECOVERING: frozenset({WARMING, PAUSED}) | TERMINAL,
    **{status: frozenset({READY, ENGINE_ERROR}) for status in TERMINAL - {ENGINE_ERROR}},
    ENGINE_ERROR: frozenset({ENGINE_ERROR}),
}


def transition(current: str, target: str, why: str) -> str:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"Illegal run transition {current!r} -> {target!r}: {why}")
    return target
