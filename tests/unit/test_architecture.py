"""Layering rules, enforced: dependencies point inwards (adapters -> app -> domain)."""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "optimizer"


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", sorted((PKG / "domain").glob("*.py")), ids=lambda p: p.name)
def test_domain_depends_on_nothing_but_itself_and_pure_stdlib(path):
    allowed_stdlib = {"__future__", "dataclasses", "enum", "typing"}
    for name in imports_of(path):
        assert name in allowed_stdlib or name.startswith("optimizer.domain"), f"{path.name} imports {name}"


ADAPTERS = ("optimizer.storage", "optimizer.observability", "optimizer.probing", "prometheus_client", "sqlite3")


@pytest.mark.parametrize("module", ["service.py", "mode.py", "events.py"])
def test_application_core_does_not_know_concrete_adapters(module):
    for name in imports_of(PKG / "app" / module):
        assert not name.startswith(ADAPTERS), f"app/{module} imports {name}"


def test_only_bootstrap_wires_concrete_classes():
    wiring = {p.name for p in (PKG / "app").glob("*.py") if any(n.startswith(ADAPTERS) for n in imports_of(p))}
    assert wiring <= {"bootstrap.py"}
