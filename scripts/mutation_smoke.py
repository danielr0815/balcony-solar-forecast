#!/usr/bin/env python3
"""Three semantic mutations in temporary copies; the checkout is never edited."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "custom_components/balcony_solar_forecast/"
CASES = (
    ("group-clamp", COMPONENT + "core/electrical.py", "clamp_groups",
     "return dict(plane_watts)",
     "tests/core/test_electrical.py::test_clamp_two_430w_modules_cannot_exceed_800"),
    ("stale-label", COMPONENT + "_glue_util.py", "_usable_power",
     "return float(state.state) if state is not None else None",
     "tests/test_coordinator_learning.py::test_usable_power_rejects_frozen_stale_sensor"),
    ("catchup-gap", COMPONENT + "_nightly.py", "catchup_days", "return [latest]",
     "tests/test_review_orchestration.py::test_nightly_retries_older_gap_after_newer_actuals_were_recorded"),
)


def mutate(source: str, symbol: str, body: str) -> str:
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == symbol)
    lines = source.splitlines(keepends=True)
    lines[node.body[0].lineno - 1:node.end_lineno] = ["    " + body + "\n"]
    result = "".join(lines)
    compile(result, symbol, "exec")
    return result


def run_tests(root: Path, targets: list[str], report: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *targets, "-p", "no:homeassistant",
         "--junitxml", str(report)], cwd=root, text=True, capture_output=True,
        timeout=120,
    )


def semantic_failure(report: Path) -> bool:
    tree = ET.parse(report)
    failures = tree.findall(".//failure")
    return bool(failures) and not tree.findall(".//error") and all(
        "AssertionError" in (failure.text or "")
        or failure.get("message", "").startswith("assert ") for failure in failures
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bsf-mutations-") as tmp:
        root = Path(tmp)
        for name in ("custom_components", "tests", "scripts"):
            shutil.copytree(ROOT / name, root / name,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
        report = root / "result.xml"
        baseline = run_tests(root, [case[-1] for case in CASES], report)
        if baseline.returncode:
            print("Mutation baseline failed:\n" + baseline.stdout + baseline.stderr)
            return 1
        for name, relative, symbol, body, test in CASES:
            path = root / relative
            original = path.read_text()
            path.write_text(mutate(original, symbol, body))
            result = run_tests(root, [test], report)
            path.write_text(original)
            if result.returncode != 1 or not semantic_failure(report):
                print(f"Mutation {name} survived or failed non-semantically:\n"
                      + result.stdout + result.stderr)
                return 1
            print(f"Killed {name}: {test}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
