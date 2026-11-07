import pytest

from jevdoom.core.lifecycle import ENGINE_ERROR, TERMINAL, TRANSITIONS, transition
from jevdoom.core.pending import clear, mark_stalled, submit


def test_illegal_run_transitions_are_rejected():
    for terminal in TERMINAL:
        with pytest.raises(ValueError, match="Illegal run transition"):
            transition(terminal, "running", "start or resume")
    with pytest.raises(ValueError, match="Illegal run transition"):
        transition("ready", "paused", "pause")
    for target in TRANSITIONS.keys() - {ENGINE_ERROR}:
        with pytest.raises(ValueError, match="Illegal run transition"):
            transition(ENGINE_ERROR, target, "restart")
    assert transition(ENGINE_ERROR, ENGINE_ERROR, "engine error") == ENGINE_ERROR


def test_pending_request_lifecycle():
    pending = submit(None, 7, 12.0, True)
    assert pending.request_id == 7 and not pending.stalled
    pending = mark_stalled(pending)
    assert pending.stalled
    pending, stalled = clear(pending)
    assert pending is None and stalled

    probe = submit(None, None, None, True)
    assert probe.since is None and not probe.stalled
    assert clear(probe) == (None, False)
