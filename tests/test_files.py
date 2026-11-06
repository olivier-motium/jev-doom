"""Run storage never reuses a recording directory."""

import pytest
from jevdoom.adapters.files import FileStore


def test_run_directory_is_unique_and_cannot_escape(tmp_path):
    store = FileStore(tmp_path)
    first = store.open_run("same-second")
    (first / "episode.lmp").write_bytes(b"first recording")
    second = store.open_run("same-second")
    assert second != first
    assert (first / "episode.lmp").read_bytes() == b"first recording"
    with pytest.raises(ValueError):
        store.open_run("../outside")
