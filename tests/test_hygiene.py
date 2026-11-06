"""Repo hygiene: ruff is clean and nothing private or binary is tracked."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tracked(*patterns):
    result = subprocess.run(
        ["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.splitlines()


def ruff(*args):
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args], cwd=ROOT, capture_output=True, text=True
    )


def test_ruff_check_is_clean():
    result = ruff("check", ".")
    assert result.returncode == 0, result.stdout + result.stderr


def test_ruff_format_is_clean():
    result = ruff("format", "--check", ".")
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_game_data_recordings_runs_or_env_are_tracked():
    banned = [
        name
        for name in tracked()
        if name.endswith((".wad", ".zip", ".lmp"))
        or name.startswith("runs/")
        or Path(name).name == ".env"
    ]
    assert banned == []
