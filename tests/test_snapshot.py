"""Dashboard consumers receive an owned state and the matching frame tick."""

from jevdoom.run import Run
from jevdoom.adapters.snapshot import SnapshotView


def test_published_state_and_frame_are_owned():
    run = Run.__new__(Run)
    original = {"observation": {"tick": 17, "player": {"health": 80}}}
    run._build_view = lambda: original
    run.frame_image = b"frame-at-17"
    run.view_port = SnapshotView()
    run.publish_snapshot()
    original["observation"]["player"]["health"] = 1
    first = run.view()
    assert first["observation"]["player"]["health"] == 80
    first["observation"]["player"]["health"] = 2
    assert run.view()["observation"]["player"]["health"] == 80
    assert run.frame_snapshot() == (b"frame-at-17", 17)
