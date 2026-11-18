"""Anonymous prospective capture exercises same-axis forecast and DC measurement."""

from datetime import timedelta

import pytest

from scripts.validation.capture_lead_time import (
    HOUR,
    _binding_sha256,
    _capture_path,
    _closed_reference_targets,
    _fetch_measurements,
    _write,
    actual_capture,
    export_bundle,
    forecast_capture,
    load_plan,
    reference_capture,
)
from scripts.validation.replay_benchmark import compare, load_verified, timestamp

DAY = "2026-06-21"
START = timestamp(DAY + "T10:00:00Z")
PLAN = {
    "timezone": "Europe/Berlin",
    "entry_id": "synthetic",
    "dc_sources": {"A": "sensor.dc_a", "B": "sensor.dc_b"},
    "targets": [START.isoformat(), (START + HOUR).isoformat()],
    "development_end": "2026-06-19",
    "selection_end": "2026-06-20",
    "licence": "CC0-1.0 synthetic example",
}


def _entry(at):
    slots = [(START + timedelta(minutes=15 * i)).isoformat() for i in range(8)]
    return {
        "published_at": at.isoformat(),
        "dc_bindings_sha256": _binding_sha256(PLAN),
        "forecast_dc_bindings_sha256": _binding_sha256(PLAN),
        "issued_at": (at - timedelta(seconds=2)).isoformat(),
        "raw_hourly_wh": dict.fromkeys(PLAN["targets"], 310),
        "total_hourly": dict.fromkeys(PLAN["targets"], 270),
        "slot_starts": slots,
        "planes": {"A": [], "B": []},
        "invalid_weather_slots": [],
        "night_zero_slots": [],
        "band_evidence_by_slot": {stamp: "trained" if i % 2 else "neutral"
                                  for i, stamp in enumerate(slots)},
        "p10": {"wh_period": dict.fromkeys(slots, 50)},
        "p50": {"wh_period": dict.fromkeys(slots, 67)},
        "p90": {"wh_period": dict.fromkeys(slots, 75)},
    }


def _stats():
    starts = [(START + timedelta(minutes=5 * i)).timestamp() * 1000 for i in range(24)]
    return {
        "sensor.dc_a": [{"start": t, "mean": 150} for t in starts],
        # A missing 5-minute bucket leaves the second target unusable.
        "sensor.dc_b": [{"start": t, "mean": 100} for t in starts[:-1]],
    }


def test_prospective_capture_replays_complete_common_dc_axis(tmp_path):
    plan_file = tmp_path / "plan.json"
    _write(plan_file, PLAN)
    plan, digest = load_plan(plan_file)
    folder = tmp_path / "captures"
    folder.mkdir()
    issues = [
        ("previous_evening", None, "2026-06-20T18:00:00Z"),
        ("early_morning", None, "2026-06-21T02:00:00Z"),
        ("intraday", START.isoformat(), "2026-06-21T09:45:00Z"),
    ]
    for phase, target, stamp in issues:
        published = timestamp(stamp)
        image = forecast_capture(
            plan, digest, day=DAY, phase=phase, target=target,
            entry=_entry(published), captured_at=published + timedelta(seconds=2),
        )
        path = _capture_path(folder, image)
        _write(path, image)
        with pytest.raises(FileExistsError):
            _write(path, image)

    after = START + 3 * HOUR
    capture = actual_capture(plan, digest, DAY, _stats(), captured_at=after,
                             dc_bindings_sha256=_binding_sha256(plan))
    _write(_capture_path(folder, capture), capture)
    observation = tmp_path / "observations.json"
    _write(observation, {"source": "synthetic independent reference", "records": [{
        "target_start": START.isoformat(), "available_at": after.isoformat(),
        "observed_weather": "clear",
    }]})
    manifest = export_bundle(plan, digest, folder, tmp_path / "replay", observation)
    bundle = load_verified(manifest)
    with pytest.raises(FileExistsError):
        export_bundle(plan, digest, folder, tmp_path / "replay")
    common = [row for row in bundle["records"] if row["target_start"] == START.isoformat()]
    assert len(common) == 3
    assert all(row["actual_wh"] == 250 and row["quality"] == "valid" for row in common)
    assert all(row["intervals"]["served"] == [200, 268, 300] for row in common)
    assert all(timestamp(row["issued_at"]) < timestamp(row["forecast_captured_at"]) < START
               for row in common)
    assert all(row["regime"] == {"observed_weather": "clear", "band_learning_state": "mixed"}
               for row in common)
    assert "sensor.dc_a" not in (manifest.parent / "bundle.json").read_text()
    missing = [row for row in bundle["records"] if row["quality"] == "missing_channels"]
    assert len(missing) == 2 and all(row["actual_wh"] is None and row["missing_modules"] == ["B"]
                                     for row in missing)
    assert bundle["missing_forecast_captures"]["intraday"] == PLAN["targets"][1:]
    report = compare(bundle)
    phases = report["strata"]["test"]["issue_period"]
    assert set(phases) == {"previous_evening", "early_morning", "intraday"}
    assert phases["intraday"]["served"]["mae_wh"] == 20
    assert phases["intraday"]["raw"]["mae_wh"] == 60


def test_capture_rejects_late_issue_and_incomplete_forecast_hour():
    issued = START - timedelta(minutes=20)
    entry = _entry(issued)
    with pytest.raises(ValueError, match="planned phase"):
        forecast_capture(PLAN, "demo", day=DAY, phase="intraday", target=START.isoformat(),
                         entry=entry, captured_at=START)
    with pytest.raises(ValueError, match="closed"):
        actual_capture(PLAN, "demo", DAY, _stats(), captured_at=START + HOUR,
                       dc_bindings_sha256=_binding_sha256(PLAN))

    entry["invalid_weather_slots"] = [entry["slot_starts"][1]]
    image = forecast_capture(PLAN, "demo", day=DAY, phase="intraday", target=START.isoformat(),
                             entry=entry, captured_at=START - timedelta(minutes=10))
    assert image["records"] == [{"target_start": START.isoformat(),
                                  "forecasts": {"raw": None, "served": None}}]


def test_export_rejects_changed_plan(tmp_path):
    phase = "intraday"
    published = START - timedelta(minutes=20)
    image = forecast_capture(
        PLAN, "original", day=DAY, phase=phase, target=START.isoformat(),
        entry=_entry(published), captured_at=published,
    )
    _write(_capture_path(tmp_path, image), image)
    with pytest.raises(ValueError, match="plan or filename mismatch"):
        export_bundle(PLAN, "changed", tmp_path, tmp_path / "replay")


@pytest.mark.parametrize(("field", "message"), [
    ("dc_bindings_sha256", "differ from the selected HA config entry"),
    ("forecast_dc_bindings_sha256", "does not match the current DC sensor bindings"),
])
def test_capture_rejects_forecast_or_plan_from_different_dc_bindings(field, message):
    at = START - timedelta(minutes=15)
    entry = _entry(at)
    entry[field] = _binding_sha256(dict(PLAN, dc_sources={"A": "sensor.some_other_site"}))
    with pytest.raises(ValueError, match=message):
        forecast_capture(PLAN, "digest", day=DAY, phase="intraday", target=START.isoformat(),
                         entry=entry, captured_at=at)


def test_recorder_binding_is_checked_before_and_after_read(monkeypatch):
    from scripts.validation import capture_lead_time as capture

    bindings = _binding_sha256(PLAN)
    seen = []
    alternate = _binding_sha256(dict(PLAN, dc_sources={"A": "sensor.other"}))
    observed = iter([bindings, alternate])
    monkeypatch.setattr(capture, "_now", lambda: START + 2 * HOUR)
    monkeypatch.setattr(capture, "_fetch_entry", lambda *args: {"dc_bindings_sha256": next(observed)})

    class DummyWS:
        def __init__(self, url, token):
            pass

        def connect(self):
            pass

        def call(self, request):
            if request["type"] == "recorder/get_statistics_metadata":
                return [
                    {"statistic_id": source, "unit_class": "power", "statistics_unit_of_measurement": "W"}
                    for source in request["statistic_ids"]
                ]
            seen.append(request["statistic_ids"])
            return {}

        def close(self):
            pass

    monkeypatch.setattr(capture, "MiniWS", DummyWS)
    with pytest.raises(ValueError, match="differ from the selected HA config entry"):
        _fetch_measurements(object(), "https://ha.example", "synthetic", PLAN, [START])
    assert seen == [["sensor.dc_a", "sensor.dc_b"]]


@pytest.mark.parametrize(("complete", "has_gap", "expected"), [
    (True, False, 0.0),
    (False, False, None),
    (True, True, None),
])
def test_missing_hour_is_zero_only_with_four_confirmed_valid_night_slots(complete, has_gap, expected):
    at = START - timedelta(minutes=15)
    entry = _entry(at)
    entry["raw_hourly_wh"] = {}
    entry["total_hourly"] = {}
    slots = entry["slot_starts"][:4]
    entry["night_zero_slots"] = slots if complete else slots[:-1]
    if has_gap:
        entry["invalid_weather_slots"] = slots[:1]
    image = forecast_capture(PLAN, "digest", day=DAY, phase="intraday", target=START.isoformat(),
                             entry=entry, captured_at=at)
    assert image["records"][0]["forecasts"] == {"raw": expected, "served": expected}


def test_previous_day_reference_requires_real_capture_before_publication():
    at = timestamp("2026-06-20T16:15:00Z")  # 18:15 local previous evening
    late_target = START + 9 * HOUR  # Its previous-day occurrence has not closed at 18:15.
    plan = {**PLAN, "targets": [START.isoformat(), late_target.isoformat()]}
    sources = _closed_reference_targets(plan, DAY, at)
    assert sources == {START: START - timedelta(days=1)}
    starts = [(sources[START] + timedelta(minutes=5 * i)).timestamp() * 1000 for i in range(12)]
    stats = {
        "sensor.dc_a": [{"start": t, "mean": 52} for t in starts],
        "sensor.dc_b": [{"start": t, "mean": 83} for t in starts],
    }
    reference = reference_capture(plan, "digest", DAY, "previous_evening", sources, stats,
                                  captured_at=at, dc_bindings_sha256=_binding_sha256(plan))
    issue = forecast_capture(plan, "digest", day=DAY, phase="previous_evening", target=None,
                             entry=_entry(at + timedelta(seconds=30)),
                             captured_at=at + timedelta(minutes=1), reference=reference)
    primary, late = issue["records"]
    frozen = primary["references"]["previous_day"]
    assert frozen == {"wh": 135.0, "target_start": sources[START].isoformat(),
                      "target_end": (sources[START] + HOUR).isoformat(),
                      "available_at": at.isoformat()}
    assert "references" not in late

    publication = at - timedelta(seconds=10)
    issued_earlier = forecast_capture(plan, "digest", day=DAY, phase="previous_evening", target=None,
                                      entry=_entry(publication), captured_at=at + timedelta(minutes=1),
                                      reference=reference)
    assert all("references" not in row for row in issued_earlier["records"])
