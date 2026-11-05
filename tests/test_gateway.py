"""Exercise gateway lifecycle with a local fake child and no provider calls."""

import sys
from pathlib import Path

from jevdoom.adapters.gateway import Gateway


FAKE = r"""
import json, os, sys, time
for line in sys.stdin:
    request = json.loads(line)
    action = request['state']['action']
    if action == 'exit':
        sys.exit(9)
    if action == 'hang':
        time.sleep(5)
    if action == 'flood':
        sys.stderr.write('x' * 1048576 + '\n')
        sys.stderr.flush()
    print(json.dumps({'ok': True, 'visible': sorted(os.environ)}), flush=True)
"""


def job(action):
    return {"state": {"action": action}, "questions": {}}


def test_gateway_drains_stderr_and_restarts_after_child_exit(monkeypatch):
    monkeypatch.setenv("JEV_TEST_PRIVATE", "not-for-child")
    gateway = Gateway(Path.cwd(), [sys.executable, "-u", "-c", FAKE], timeout=5)
    try:
        reply = gateway.ask(job("flood"))
        assert reply["ok"]
        assert "JEV_TEST_PRIVATE" not in reply["visible"]
        assert len(gateway.stderr) <= 20
        assert not gateway.ask(job("exit"))["ok"]
        assert gateway.ask(job("ok"))["ok"]
    finally:
        gateway.close()


def test_gateway_times_out_and_recovers():
    gateway = Gateway(Path.cwd(), [sys.executable, "-u", "-c", FAKE], timeout=0.2)
    try:
        reply = gateway.ask(job("hang"))
        assert reply["error_name"] == "TimeoutError"
        gateway.timeout = 5
        assert gateway.ask(job("ok"))["ok"]
    finally:
        gateway.close()
