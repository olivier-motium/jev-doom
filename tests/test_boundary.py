"""The decision core may not import runtime, IO, or presentation adapters."""

import ast
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "jevdoom" / "core"
BANNED = {
    "vizdoom",
    "fastapi",
    "starlette",
    "uvicorn",
    "subprocess",
    "threading",
    "socket",
    "PIL",
    "time",
    "os",
}


def test_core_does_not_import_runtime_adapters():
    violations = []
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            violations.extend((str(path), name) for name in names if name.split(".")[0] in BANNED)
    assert not violations
