"""Layering constraints, enforced statically.

The package is a layered pipeline: urls -> client -> extract -> transform ->
backend, with `schema`, `catalog` and `identity` as shared leaves. These tests
fail the build when an import points the wrong way, which is cheaper than
rediscovering it in review.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "orb_extreme_platformone"
PACKAGE = "orb_extreme_platformone"

# Leaves may not import any other first-party module (except each other).
LEAF_MODULES = ("schema.py", "catalog.py", "urls.py", "identity.py")
LEAF_NAMES = {f"{PACKAGE}.{name[:-3]}" for name in LEAF_MODULES}


def _imports(path: pathlib.Path) -> set[str]:
    """First-party modules `path` imports, as absolute dotted names."""
    module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
    package = module.rsplit(".", 1)[0] if path.name != "__init__.py" else module
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package if node.level == 1 else package.rsplit(".", node.level - 1)[0]
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if not target.startswith(PACKAGE):
                continue
            found.add(target)
            # `from orb_extreme_platformone import bootstrap` imports a
            # submodule, so record the names too — otherwise the exact form
            # that caused the original layering inversion slips through.
            found.update(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(PACKAGE))
    return found


def _modules_under(*parts: str) -> list[pathlib.Path]:
    return sorted((SRC.joinpath(*parts) if parts else SRC).rglob("*.py"))


@pytest.mark.parametrize("path", _modules_under("transform"), ids=lambda p: p.name)
def test_transform_does_not_import_io_layers(path: pathlib.Path) -> None:
    """Transform is pure mapping: it must not reach into bootstrap, client or extract.

    `transform.common` used to import `bootstrap` for six string constants,
    dragging the NetBox REST client (and `requests`) into every transform-only
    import graph. Those names live in `schema` now.
    """
    forbidden = {f"{PACKAGE}.bootstrap", f"{PACKAGE}.client"}
    offending = {
        imported
        for imported in _imports(path)
        if imported in forbidden or imported.startswith(f"{PACKAGE}.extract")
    }
    assert not offending, f"{path.name} imports an I/O layer: {sorted(offending)}"


@pytest.mark.parametrize("name", LEAF_MODULES)
def test_leaf_modules_import_no_other_first_party_module(name: str) -> None:
    """Leaves carry shared vocabulary and must stay importable on their own."""
    offending = _imports(SRC / name) - LEAF_NAMES
    assert not offending, f"{name} is a leaf but imports {sorted(offending)}"


def test_client_does_not_import_extract_or_transform() -> None:
    """The transport layer sits below everything that uses it."""
    offending = {
        imported
        for imported in _imports(SRC / "client.py")
        if imported.startswith((f"{PACKAGE}.extract", f"{PACKAGE}.transform"))
    }
    assert not offending, f"client.py imports an upper layer: {sorted(offending)}"


def test_no_import_cycles() -> None:
    """A cycle makes import order load-bearing and breaks the layer story."""
    graph: dict[str, set[str]] = {}
    for path in _modules_under():
        module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
        module = module.removesuffix(".__init__")
        graph.setdefault(module, set()).update(_imports(path))

    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str, trail: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            pytest.fail(f"import cycle: {' -> '.join([*trail, node])}")
        visiting.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt in graph:
                walk(nxt, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for module in sorted(graph):
        walk(module, [])
