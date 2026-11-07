"""Local storage for budget, run events, and results."""

import json
from pathlib import Path


class FileStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(exist_ok=True)
        self.trace = None
        self.run_dir = None

    def load_ledger(self):
        path = self.root / "budget.json"
        return json.loads(path.read_text()) if path.exists() else None

    def save_ledger(self, ledger):
        path = self.root / "budget.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, indent=2))
        tmp.replace(path)

    def open_run(self, name):
        if not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("Invalid run name")
        suffix = 0
        while True:
            candidate = self.root / (name if suffix == 0 else f"{name}-{suffix}")
            try:
                candidate.mkdir()
                self.run_dir = candidate
                break
            except FileExistsError:
                suffix += 1
        if self.trace is not None:
            self.trace.close()
        self.trace = (self.run_dir / "trace.jsonl").open("a")
        return self.run_dir

    def append_trace(self, event):
        if self.trace is not None:
            self.trace.write(json.dumps(event, separators=(",", ":")) + "\n")
            self.trace.flush()

    def record_path(self):
        return self.run_dir / "episode.lmp" if self.run_dir is not None else None

    def append_probe(self, record):
        with (self.root / "provider-probes.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def result_path(self, run_name):
        if not run_name or Path(run_name).name != run_name or run_name in (".", ".."):
            raise ValueError("Invalid run name")
        return self.root / run_name / "result.json"

    def read_result(self, run_name):
        return json.loads(self.result_path(run_name).read_text())

    def write_result(self, result, run_name=None):
        if run_name is None:
            if self.run_dir is None:
                return
            run_name = self.run_dir.name
        path = self.result_path(run_name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2))
        tmp.replace(path)
