"""Negative examples make the architecture and release guards falsifiable."""

import json
from pathlib import Path

import pytest

from scripts.check_core_imports import forbidden_imports
from scripts.release_guard import release_notes, validate_runs, validate_versions


@pytest.mark.parametrize("source", [
    "import homeassistant.helpers",
    "def calculate():\n    import numpy as np",
    "from .. import coordinator",
    "from ..coordinator import BalconySolarCoordinator",
])
def test_core_boundary_rejects_framework_and_lazy_dependency_imports(source):
    assert forbidden_imports(source, "core/engine.py")


def test_network_exception_is_only_lazy_aiohttp_in_backfill():
    lazy = "async def fetch():\n    import aiohttp"
    assert forbidden_imports(lazy, "core/openmeteo_backfill.py") == []
    assert forbidden_imports("import aiohttp", "core/openmeteo_backfill.py")
    assert forbidden_imports(lazy, "core/engine.py")
    assert forbidden_imports("from datetime import UTC\nfrom .. import const", "core/engine.py") == []
    assert forbidden_imports("from .coordinator import X", "const.py")


def _run(**changes):
    return {"id": 1, "run_number": 5, "run_attempt": 1,
            "head_sha": "a" * 40, "head_branch": "main", "event": "push",
            "path": ".github/workflows/validate.yml", "status": "completed",
            "conclusion": "success", **changes}


@pytest.mark.parametrize("changes", [
    {"head_sha": "b" * 40}, {"head_branch": "feature"}, {"event": "pull_request"},
    {"status": "in_progress"}, {"conclusion": "failure"},
    {"path": ".github/workflows/unrelated.yml"},
])
def test_release_requires_success_on_the_exact_main_commit(changes):
    with pytest.raises(ValueError):
        validate_runs({"workflow_runs": [_run(**changes)]}, "a" * 40)


def test_failed_rerun_cannot_reuse_older_green_result():
    with pytest.raises(ValueError):
        validate_runs([{"workflow_runs": [_run(), _run(run_attempt=2, conclusion="failure")]}], "a" * 40)
    validate_runs({"workflow_runs": [_run()]}, "a" * 40)


def test_mismatched_release_version_rejected_before_any_publish_step():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "custom_components/balcony_solar_forecast/manifest.json").read_text())
    with pytest.raises(ValueError, match="differs"):
        validate_versions(root, manifest["version"] + "0")


def test_release_notes_are_only_the_requested_dated_release():
    notes = "## [Unreleased]\nfuture\n## [1.2.3] - 2026-09-26\nFixed\n## [1.2.2] - 2026-09-25\nOld\n"
    assert release_notes(notes, "1.2.3") == "Fixed\n"
    with pytest.raises(ValueError):
        release_notes(notes, "1.2.4")


def test_type_ratchet_rejects_new_duplicate_and_requires_removing_fixed_debt():
    from scripts.check_mypy_baseline import compare

    known = ["core/types.py::from_dict [arg-type] invalid argument"]
    assert compare(known, known) == ([], [])
    assert compare(known * 2, known) == (known, [])
    assert compare([], known) == ([], known)


def test_type_baseline_uses_symbol_not_shifting_line_number(tmp_path):
    from scripts.check_mypy_baseline import diagnostics

    path = tmp_path / "example.py"
    path.write_text("class Model:\n    def from_dict(self):\n        return missing\n")
    message = 'example.py:3: error: Name "missing" is not defined  [name-defined]'
    assert diagnostics(message, tmp_path) == [
        'example.py::Model.from_dict [name-defined] Name "missing" is not defined',
    ]


def test_missing_golden_artifact_fails_instead_of_skipping(tmp_path):
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    component = Path("custom_components/balcony_solar_forecast")
    for relative in (component / "const.py", component / "core/solpos.py",
                     component / "core/transpose.py", Path("tests/core/test_golden.py")):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    result = subprocess.run(
        [sys.executable, "-c", "import runpy; runpy.run_path('tests/core/test_golden.py')"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "FileNotFoundError" in result.stderr
    assert "reference_vectors.json" in result.stderr


@pytest.mark.parametrize("changed", [None, "manifest", "project", "const", "spec", "changelog"])
def test_release_metadata_requires_one_consistent_dated_version(tmp_path, changed):
    component = tmp_path / "custom_components/balcony_solar_forecast"
    component.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    files = {
        "manifest": (component / "manifest.json", '{"version": "1.2.3"}'),
        "project": (tmp_path / "pyproject.toml", '[project]\nversion = "1.2.3"'),
        "const": (component / "const.py", 'INTEGRATION_VERSION = "1.2.3"'),
        "spec": (tmp_path / "docs/SPEC.md", '**Gilt für Version: 1.2.3**'),
        "changelog": (tmp_path / "CHANGELOG.md", '## [1.2.3] - 2026-09-26\nFixed'),
    }
    for name, (path, content) in files.items():
        path.write_text(content.replace("1.2.3", "1.2.4") if name == changed else content)
    if changed is None:
        validate_versions(tmp_path, "1.2.3")
    else:
        with pytest.raises(ValueError):
            validate_versions(tmp_path, "1.2.3")


def test_type_baseline_normalizes_windows_paths(tmp_path):
    from scripts.check_mypy_baseline import diagnostics

    (tmp_path / "core").mkdir()
    (tmp_path / "core/model.py").write_text("missing\n")
    assert diagnostics(r'core\model.py:1: error: Unknown name  [name-defined]', tmp_path) == [
        'core/model.py::<module> [name-defined] Unknown name',
    ]
