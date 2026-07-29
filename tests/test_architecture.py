"""Layering constraint, enforced statically.

`transform` is pure mapping. It used to import `bootstrap` for six string
constants, which dragged the NetBox REST client — and `requests` — into every
transform-only import graph. Those names live in `schema` now; this keeps them
there.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = "orb_extreme_platformone"
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / PACKAGE
TRANSFORM = sorted((SRC / "transform").glob("*.py"))


def _first_party_imports(path: pathlib.Path) -> set[str]:
    """Absolute dotted names of the first-party modules `path` imports."""
    module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
    package = module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            base = package if node.level else ""
            target = f"{base}.{node.module}" if node.module and base else (node.module or base)
            if not target.startswith(PACKAGE):
                continue
            found.add(target)
            # `from orb_extreme_platformone import bootstrap` imports a submodule
            # — the exact form the original inversion used.
            found.update(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(PACKAGE))
    return found


@pytest.mark.parametrize("path", TRANSFORM, ids=lambda p: p.name)
def test_transform_does_not_import_io_layers(path: pathlib.Path) -> None:
    forbidden = {f"{PACKAGE}.bootstrap", f"{PACKAGE}.client", f"{PACKAGE}.http"}
    offending = {
        name
        for name in _first_party_imports(path)
        if name in forbidden or name.startswith(f"{PACKAGE}.extract")
    }
    assert not offending, f"{path.name} imports an I/O layer: {sorted(offending)}"
