"""Real offline validation must distinguish evidence, gaps and broken checks."""

import importlib
import json
import sys
from pathlib import Path

import pytest

from tests.helpers.validation_bundle import write_validation_bundle


@pytest.fixture
def validation(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "validation"
    monkeypatch.syspath_prepend(str(path))
    # The scripts intentionally support direct standalone invocation.
    yield importlib.import_module("validate")
    for name in ("validate", "bsf_checks", "bsf_data"):
        sys.modules.pop(name, None)


def test_missing_evidence_is_incomplete_not_a_validated_deployment(validation, tmp_path, capsys):
    data = {"sensor.balcony_solar_forecast_measured_ac_power": [
        {"start": 1784368800, "mean": 100.0},
    ]}
    (tmp_path / "actuals_hourly_stats.json").write_text(json.dumps(data))
    report = tmp_path / "report.json"
    assert validation.main(["--offline", "--data-dir", str(tmp_path),
                            "--json", str(report)]) != 0
    assert "Deployment validiert" not in capsys.readouterr().out
    assert json.loads(report.read_text())["summary"]["status"] == "INCOMPLETE"


@pytest.mark.parametrize("multiplier, expected", [(1.0, 0), (2.0, 2)])
def test_synthetic_full_package_passes_and_detects_morning_overforecast(
    validation, tmp_path, multiplier, expected,
):
    write_validation_bundle(tmp_path, morning_multiplier=multiplier)
    assert validation.main(["--offline", "--data-dir", str(tmp_path)]) == expected


def test_programming_error_is_error_not_a_missing_measurement(validation, tmp_path, monkeypatch):
    checks = importlib.import_module("bsf_checks")
    write_validation_bundle(tmp_path)

    def broken(_bundle, _eta):
        raise RuntimeError("deliberate check defect")

    monkeypatch.setattr(checks, "ALL_CHECKS", [broken])
    report = tmp_path / "report.json"
    assert validation.main(["--offline", "--data-dir", str(tmp_path),
                            "--json", str(report)]) == 2
    payload = json.loads(report.read_text())
    assert payload["checks"][0]["status"] == "ERROR"
    assert payload["summary"]["status"] == "ERROR"


def test_missing_subcheck_keeps_an_otherwise_passing_report_incomplete(validation, tmp_path):
    write_validation_bundle(tmp_path)
    (tmp_path / "forecast_sensor_history.json").unlink()
    report = tmp_path / "report.json"
    assert validation.main(["--offline", "--data-dir", str(tmp_path),
                            "--json", str(report)]) != 0
    assert json.loads(report.read_text())["summary"]["status"] == "INCOMPLETE"
