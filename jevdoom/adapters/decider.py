"""One non-blocking decision at a time, routed to a local or gateway policy."""

import queue
import threading

from .local import LocalDecider


class AsyncDecider:
    def __init__(self, gateway):
        self.gateway = gateway
        self.local = LocalDecider()
        self.jobs = queue.Queue(maxsize=1)
        self.results = queue.Queue()
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def submit(self, job):
        if self.closed.is_set():
            raise RuntimeError("Decider is closed")
        self.jobs.put_nowait(job)

    def poll(self, timeout=0):
        try:
            return self.results.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None or self.closed.is_set():
                break
            try:
                result = (
                    self.local.ask(job)
                    if job.get("controller") == "local"
                    else self.gateway.ask(job)
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": str(exc)[:300],
                    "error_name": type(exc).__name__,
                    "cost_usd": None,
                }
            self.results.put((job, result))
        self.gateway.close()

    def close(self):
        self.closed.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        self.gateway.close()
        if self.worker.is_alive():
            self.worker.join(timeout=2)
