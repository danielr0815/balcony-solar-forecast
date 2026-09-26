#!/usr/bin/env python3
"""Read-only release preflight. This script never creates tags or releases."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def release_notes(changelog: str, version: str) -> str:
    match = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", changelog, re.M)
    if match is None:
        raise ValueError(f"Missing dated CHANGELOG entry for {version}")
    rest = changelog[match.end():]
    return re.split(r"^## ", rest, maxsplit=1, flags=re.M)[0].strip() + "\n"


def validate_versions(root: Path, version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Release version must have the form X.Y.Z")
    component = root / "custom_components" / "balcony_solar_forecast"
    manifest = json.loads((component / "manifest.json").read_text())["version"]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    const = re.search(r'^INTEGRATION_VERSION\s*=\s*"([^"]+)"',
                      (component / "const.py").read_text(), re.M)
    spec = (root / "docs" / "SPEC.md").read_text()
    if not const or not version == manifest == project == const.group(1):
        raise ValueError("Release version differs from manifest, pyproject or const")
    if not re.search(rf"Gilt für Version:\s*`?{re.escape(version)}(?:[`*]|\s|$)", spec):
        raise ValueError("SPEC version stamp does not match the release")
    release_notes((root / "CHANGELOG.md").read_text(), version)


def validate_runs(pages: list[dict] | dict, sha: str) -> None:
    """Require success of the latest Validate push run for this exact main commit."""
    if isinstance(pages, dict):
        pages = [pages]
    runs = [run for page in pages for run in page.get("workflow_runs", [])
            if run.get("head_sha") == sha and run.get("head_branch") == "main"
            and run.get("event") == "push"
            and run.get("path") == ".github/workflows/validate.yml"]
    if not runs:
        raise ValueError("No Validate push run for the exact main commit")
    latest = max(runs, key=lambda r: (r.get("run_number", 0), r.get("run_attempt", 0), r["id"]))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError("The latest Validate run for this commit is not successful")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--runs-json", type=Path, required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.commit_sha):
            raise ValueError("A full, lowercase 40-character commit SHA is required")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if head != args.commit_sha:
            raise ValueError("Checkout does not match the requested commit")
        subprocess.run(["git", "merge-base", "--is-ancestor", head, "origin/main"],
                       cwd=ROOT, check=True)
        validate_versions(ROOT, args.version)
        validate_runs(json.loads(args.runs_json.read_text()), head)
        args.notes_file.write_text(release_notes((ROOT / "CHANGELOG.md").read_text(), args.version))
    except (ValueError, subprocess.CalledProcessError) as err:
        print(f"Release preflight rejected: {err}", file=sys.stderr)
        return 1
    print(f"Release preflight passed: v{args.version} at {head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
