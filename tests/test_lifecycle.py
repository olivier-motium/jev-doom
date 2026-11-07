import pytest

from jevdoom.core.lifecycle import ENGINE_ERROR, TERMINAL, TRANSITIONS, transition


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
