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
        self.run_dir = self.root / name
        self.run_dir.mkdir(exist_ok=True)
        if self.trace is not None:
            self.trace.close()
        self.trace = (self.run_dir / "trace.jsonl").open("a")
        return self.run_dir

    def append_trace(self, event):
        if self.trace is not None:
            self.trace.write(json.dumps(event, separators=(",", ":")) + "\n")
            self.trace.flush()

    def append_probe(self, record):
        with (self.root / "provider-probes.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def write_result(self, result):
        if self.run_dir is not None:
            (self.run_dir / "result.json").write_text(json.dumps(result, indent=2))
