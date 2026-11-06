"""A bounded, restartable line-oriented gateway process."""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


class Gateway:
    def __init__(self, root: Path, command=None, timeout=25):
        self.root = root
        self.command = command or ["node", str(root / "gateway.mjs")]
        self.timeout = timeout
        self.process = None
        self.stderr = deque(maxlen=20)
        self.lines = None

    def _start(self):
        self.close()
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY")
            if key in os.environ
        }
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.root,
            env=env,
        )
        self.lines = queue.Queue()
        process = self.process
        lines = self.lines

        def read_stdout():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        def read_stderr():
            pending = ""
            secrets = [
                env.get(key) for key in ("TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY") if env.get(key)
            ]

            def keep(line):
                for secret in secrets:
                    line = line.replace(secret, "[REDACTED]")
                self.stderr.append(line[-4096:])

            try:
                while chunk := os.read(process.stderr.fileno(), 4096):
                    pending += chunk.decode("utf-8", errors="replace")
                    lines = pending.split("\n")
                    pending = lines.pop()
                    for line in lines:
                        keep(line)
                    pending = pending[-8192:]
                if pending:
                    keep(pending)
            except (OSError, ValueError):
                pass

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    def ask(self, job):
        started = time.monotonic()
        try:
            if self.process is None or self.process.poll() is not None:
                self._start()
            payload = {
                "provider": job.get("provider", "gateway"),
                "state": job["state"],
                "questions": job["questions"],
            }
            deadline = started + self.timeout
            sent = queue.Queue(maxsize=1)
            process = self.process

            def send():
                try:
                    process.stdin.write(json.dumps(payload) + "\n")
                    process.stdin.flush()
                    sent.put(None)
                except Exception as exc:
                    sent.put(exc)

            threading.Thread(target=send, daemon=True).start()
            try:
                error = sent.get(timeout=max(0, deadline - time.monotonic()))
                if error is not None:
                    raise error
                line = self.lines.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                logging.getLogger(__name__).warning(
                    "Gateway deadline: request %s, elapsed %.3f s, deadline %.3f s",
                    job.get("id"),
                    time.monotonic() - started,
                    self.timeout,
                )
                raise TimeoutError("Gateway request timeout") from exc
            if line is None:
                raise ConnectionError("Gateway adapter exited")
            result = json.loads(line)
            result["end_to_end_ms"] = (time.monotonic() - started) * 1000
            return result
        except Exception as exc:
            self.close()
            return {
                "ok": False,
                "error": str(exc)[:300],
                "error_name": type(exc).__name__,
                "cost_usd": None,
            }

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
