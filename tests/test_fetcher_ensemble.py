"""Pure tests for the Open-Meteo ENSEMBLE client (v0.16, SPEC §6).

No Home Assistant needed — ``build_ensemble_params`` / ``validate_ensemble`` /
``parse_ensemble`` are pure, and ``async_fetch_ensemble_raw`` is driven with a
monkeypatched ``_request_once`` (aiohttp stays lazy). The trimmed live fixture
(tests/fixtures/ensemble_icon_seamless.json, icon_seamless, captured 2026-07-11)
pins the recorded member-key shape: the control member under the bare
``shortwave_radiation`` key plus ``shortwave_radiation_member01`` .. ``_member39``
= 40 members, hourly stamps at the interval END (parser shifts −1 h).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from balcony_solar_forecast.const import (
    ENSEMBLE_MODEL,
    ENSEMBLE_URL,
)
from balcony_solar_forecast.fetcher import (
    FetchError,
    OpenMeteoFetcher,
    build_ensemble_params,
    parse_ensemble,
    validate_ensemble,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ensemble_icon_seamless.json"


@pytest.fixture
def live_fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _ensemble_payload(time, base, **members) -> dict:
    """Build a minimal ensemble payload: bare base member + named members."""
    hourly = {"time": list(time), "shortwave_radiation": list(base)}
    for name, arr in members.items():
        hourly[name] = list(arr)
    return {"hourly": hourly}


# ---------------------------------------------------------------------------
# build_ensemble_params
# ---------------------------------------------------------------------------


def test_build_ensemble_params_shape():
    p = build_ensemble_params(48.547853, 12.187272)
    assert p["hourly"] == "shortwave_radiation"
    assert p["models"] == ENSEMBLE_MODEL
    assert p["forecast_days"] == "3"
    assert p["timezone"] == "UTC"
    assert p["latitude"] == "48.547853"
    assert p["longitude"] == "12.187272"
    # No transposition params on the boundary (raw GHI members only).
    joined = " ".join(f"{k}={v}" for k, v in p.items())
    assert "azimuth" not in joined
    assert "tilt" not in joined


# ---------------------------------------------------------------------------
# Live fixture: recorded member shape + horizon
# ---------------------------------------------------------------------------


def test_fixture_records_40_members_incl_control(live_fixture):
    hourly = live_fixture["hourly"]
    member_keys = [k for k in hourly if k.startswith("shortwave_radiation")]
    # Control member under the bare key + member01..member39.
    assert "shortwave_radiation" in member_keys
    assert "shortwave_radiation_member01" in member_keys
    assert "shortwave_radiation_member39" in member_keys
    assert len(member_keys) == 40


def test_parse_fixture_extracts_all_members_per_hour(live_fixture):
    parsed = parse_ensemble(live_fixture)
    # Six trimmed hours, every hour carries all 40 members.
    assert len(parsed) == 6
    for members in parsed.values():
        assert len(members) == 40


def test_parse_fixture_shifts_stamp_minus_one_hour(live_fixture):
    """Hourly radiation is stamped at the interval END; key = stamp − 1 h."""
    parsed = parse_ensemble(live_fixture)
    # First trimmed stamp is 03:00 -> hour-start key 02:00.
    assert "2026-07-11T02:00:00+00:00" in parsed
    # Stamp 05:00 -> key 04:00; its control member (list[0]) is the base value 99.
    assert parsed["2026-07-11T04:00:00+00:00"][0] == 99.0


# ---------------------------------------------------------------------------
# parse_ensemble — inline shape / edge cases
# ---------------------------------------------------------------------------


def test_parse_minimal_shift_and_member_order():
    payload = _ensemble_payload(
        ["2026-07-11T05:00"],
        [100.0],
        shortwave_radiation_member01=[90.0],
    )
    parsed = parse_ensemble(payload)
    # Stamp 05:00 -> hour-start 04:00; control first, then member01.
    assert list(parsed) == ["2026-07-11T04:00:00+00:00"]
    assert parsed["2026-07-11T04:00:00+00:00"] == [100.0, 90.0]


def test_parse_drops_none_members_per_hour():
    payload = _ensemble_payload(
        ["2026-07-11T05:00"],
        [100.0],
        shortwave_radiation_member01=[None],
        shortwave_radiation_member02=[80.0],
    )
    parsed = parse_ensemble(payload)
    # The None member is dropped from that hour's list (control + member02 left).
    assert parsed["2026-07-11T04:00:00+00:00"] == [100.0, 80.0]


def test_parse_clamps_negative_to_zero():
    payload = _ensemble_payload(
        ["2026-07-11T05:00"],
        [-5.0],
        shortwave_radiation_member01=[10.0],
    )
    parsed = parse_ensemble(payload)
    assert parsed["2026-07-11T04:00:00+00:00"] == [0.0, 10.0]


# ---------------------------------------------------------------------------
# validate_ensemble — SHAPE, not just HTTP 200
# ---------------------------------------------------------------------------


def test_validate_accepts_fixture(live_fixture):
    validate_ensemble(live_fixture)  # must not raise


def test_validate_rejects_non_dict():
    with pytest.raises(FetchError) as e:
        validate_ensemble([1, 2, 3])
    assert not e.value.retryable


def test_validate_rejects_missing_hourly():
    with pytest.raises(FetchError):
        validate_ensemble({"latitude": 48.5})


def test_validate_rejects_empty_time():
    payload = _ensemble_payload([], [], shortwave_radiation_member01=[])
    with pytest.raises(FetchError) as e:
        validate_ensemble(payload)
    assert "empty" in str(e.value)


def test_validate_rejects_no_members():
    # Only a time array, no shortwave_radiation* members at all.
    with pytest.raises(FetchError) as e:
        validate_ensemble({"hourly": {"time": ["2026-07-11T05:00"]}})
    assert not e.value.retryable


def test_validate_rejects_member_length_mismatch():
    payload = _ensemble_payload(
        ["2026-07-11T05:00", "2026-07-11T06:00"],
        [100.0, 200.0],
        shortwave_radiation_member01=[90.0],  # shorter than time
    )
    with pytest.raises(FetchError):
        validate_ensemble(payload)


def test_parse_validates_before_parsing():
    with pytest.raises(FetchError):
        parse_ensemble({"hourly": {"time": []}})


# ---------------------------------------------------------------------------
# async_fetch_ensemble_raw — targets the ensemble endpoint, validates the body
# ---------------------------------------------------------------------------


async def test_async_fetch_ensemble_targets_ensemble_url(monkeypatch, live_fixture):
    fetcher = OpenMeteoFetcher(None)
    captured: dict = {}

    async def _stub(_aiohttp, params, *, url=None):
        captured["url"] = url
        captured["params"] = params
        return live_fixture

    monkeypatch.setattr(fetcher, "_request_once", _stub)

    result = await fetcher.async_fetch_ensemble_raw(48.5, 12.2)

    assert result is live_fixture
    assert captured["url"] == ENSEMBLE_URL
    assert captured["params"]["hourly"] == "shortwave_radiation"


async def test_async_fetch_ensemble_validates_malformed(monkeypatch):
    fetcher = OpenMeteoFetcher(None)

    async def _stub(_aiohttp, _params, *, url=None):
        return {"hourly": {"time": []}}  # empty time -> non-retryable FetchError

    monkeypatch.setattr(fetcher, "_request_once", _stub)

    with pytest.raises(FetchError):
        await fetcher.async_fetch_ensemble_raw(48.5, 12.2)


def test_utc_awareness_of_keys():
    payload = _ensemble_payload(
        ["2026-07-11T05:00"], [100.0], shortwave_radiation_member01=[90.0]
    )
    parsed = parse_ensemble(payload)
    key = next(iter(parsed))
    # Round-trips to an aware UTC datetime (the coordinator keys det GHI the same).
    assert datetime.fromisoformat(key).tzinfo == UTC
