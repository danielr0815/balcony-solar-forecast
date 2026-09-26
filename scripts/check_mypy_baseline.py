#!/usr/bin/env python3
"""Check core and critical HA boundaries against reviewed legacy diagnostics.

No line numbers in the baseline: move-only refactors do not change the debt.
New errors fail; fixed errors also require removing their stale baseline entry.
Use --write-baseline only after reviewing the complete diagnostic diff.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "mypy_baseline.json"
HA_BOUNDARIES = (
    "_forecast_access.py", "_operations.py", "_weather_cache.py",
    "fetcher.py", "store.py", "coordinator.py",
)
ERROR = re.compile(r"^(.+\.py):(\d+): error: (.+)  \[([^]]+)\]$")


def symbol_at(path: Path, line: int) -> str:
    matches = [node for node in ast.walk(ast.parse(path.read_text()))
               if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
               and node.lineno <= line <= node.end_lineno]
    return ".".join(node.name for node in sorted(matches, key=lambda n: n.lineno)) or "<module>"


def diagnostics(output: str, root: Path) -> list[str]:
    result = []
    for line in output.splitlines():
        if match := ERROR.match(line):
            path, number, message, code = match.groups()
            path = path.replace("\\", "/")
            result.append(f"{path}::{symbol_at(root / path, int(number))} [{code}] {message}")
    return sorted(result)


def compare(current: list[str], baseline: list[str]) -> tuple[list[str], list[str]]:
    actual, known = Counter(current), Counter(baseline)
    return sorted((actual - known).elements()), sorted((known - actual).elements())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()
    config = (ROOT / "pyproject.toml").read_text()
    # The normal mypy command preserves its existing fast clean-module gate;
    # this complementary gate removes every legacy ignore_errors override.
    config = re.sub(r"(?ms)^\[\[tool\.mypy\.overrides\]\].*?(?=^\[|\Z)",
                    lambda m: "" if "ignore_errors = true" in m.group() else m.group(), config)
    targets = ["custom_components/balcony_solar_forecast/core"] + [
        f"custom_components/balcony_solar_forecast/{name}" for name in HA_BOUNDARIES
    ]
    with tempfile.TemporaryDirectory(prefix="bsf-mypy-") as tmp:
        path = Path(tmp) / "pyproject.toml"
        path.write_text(config)
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--config-file", str(path),
             "--no-incremental", "--no-error-summary", "--hide-error-context", *targets],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
    current = diagnostics(proc.stdout, ROOT)
    unparsed = [line for line in proc.stdout.splitlines() if "error:" in line and not ERROR.match(line)]
    if unparsed or proc.returncode not in (0, 1) or (proc.returncode == 1 and not current):
        print(proc.stdout + proc.stderr, file=sys.stderr)
        return 1
    if args.write_baseline:
        BASELINE.write_text(json.dumps(current, indent=2) + "\n")
        print(f"Wrote {len(current)} diagnostics; review the baseline diff before committing.")
        return 0
    new, fixed = compare(current, json.loads(BASELINE.read_text()))
    for label, entries in (("NEW type error", new), ("FIXED: remove baseline entry", fixed)):
        for entry in entries:
            print(f"{label}: {entry}")
    if new or fixed:
        return 1
    print(f"Core/HA-boundary type ratchet OK ({len(current)} explicit legacy diagnostics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
