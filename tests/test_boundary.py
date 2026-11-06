"""The decision core may not import runtime, IO, or presentation adapters."""

import ast
import importlib.util
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


def imported_names(node):
    if isinstance(node, ast.Import):
        return [item.name for item in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    module = node.module or ""
    if node.level:
        module = importlib.util.resolve_name("." * node.level + module, "jevdoom.core")
    return [module] + [f"{module}.{item.name}" for item in node.names]


def is_forbidden(name):
    return (
        name.split(".")[0] in BANNED
        or name == "jevdoom.adapters"
        or name.startswith("jevdoom.adapters.")
    )


def test_core_does_not_import_runtime_adapters():
    violations = []
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            violations.extend(
                (str(path), name) for name in imported_names(node) if is_forbidden(name)
            )
    assert not violations


def test_boundary_rejects_absolute_and_relative_adapter_imports():
    for source in (
        "import jevdoom.adapters.gateway",
        "from jevdoom import adapters",
        "from jevdoom.adapters.gateway import Gateway",
        "from ..adapters import gateway",
        "from .. import adapters",
    ):
        assert any(is_forbidden(name) for name in imported_names(ast.parse(source).body[0]))
