#!/usr/bin/env python3
"""Enforce the HA-free, stdlib-only core, including function-local imports."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "balcony_solar_forecast"


def forbidden_imports(source: str, relative_path: str) -> list[str]:
    """Return violations without importing the module or its dependencies."""
    violations = []
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            # Within core, only siblings and the HA-free shared constants are
            # permitted. In particular `from .. import coordinator` is not.
            allowed = relative_path.startswith("core/") and (
                node.level == 1
                or (node.level == 2 and node.module == "const")
                or (node.level == 2 and node.module is None
                    and all(a.name == "const" for a in node.names))
            )
            if not allowed:
                violations.append(f"{relative_path}: forbidden relative import {ast.unparse(node)}")
            continue
        names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
        for name in names:
            if name.split(".")[0] in sys.stdlib_module_names:
                continue
            ancestor = parents.get(node)
            lazy = False
            while ancestor is not None:
                lazy |= isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef)
                ancestor = parents.get(ancestor)
            if name == "aiohttp" and relative_path == "core/openmeteo_backfill.py" and lazy:
                continue
            violations.append(f"{relative_path}: forbidden dependency {name}")
    return violations


def main() -> int:
    paths = [COMPONENT / "const.py", *sorted((COMPONENT / "core").rglob("*.py"))]
    errors = [error for path in paths for error in forbidden_imports(
        path.read_text(encoding="utf-8"), path.relative_to(COMPONENT).as_posix()
    )]
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"Core import boundary OK ({len(paths)} modules)")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
