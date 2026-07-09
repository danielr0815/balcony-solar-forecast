"""Pure tests for the Open-Meteo fetcher: URL, SHAPE validation, parse.

No Home Assistant and no aiohttp are needed — the network coroutine imports
aiohttp lazily, and these tests only touch ``build_params`` /
``validate_payload`` / ``parse_weather``. The package is bootstrapped by
``tests/conftest.py`` so relative imports resolve without the HA-importing
root ``__init__``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from balcony_solar_forecast.const import (
    OPEN_METEO_HOURLY,
    OPEN_METEO_MINUTELY_15,
)
from balcony_solar_forecast.fetcher import (
    FetchError,
    build_params,
    parse_weather,
    validate_payload,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minutely_block(n: int, start_iso: str = "2026-07-05T00:15") -> dict:
    """Build ``n`` 15-min samples with distinguishable values."""
    base = datetime.fromisoformat(start_iso)
    from datetime import timedelta

    times = [(base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {
        "time": times,
        "shortwave_radiation": [float(i) for i in range(n)],
        "direct_normal_irradiance": [float(i) * 2 for i in range(n)],
        "diffuse_radiation": [float(i) * 0.5 for i in range(n)],
        "temperature_2m": [10.0 + i * 0.1 for i in range(n)],
    }


def _hourly_block(hours: int, start_iso: str = "2026-07-05T00:00") -> dict:
    base = datetime.fromisoformat(start_iso)
    from datetime import timedelta

    times = [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    return {
        "time": times,
        "cloud_cover_low": [80.0] * hours,
        "cloud_cover_mid": [10.0] * hours,
        "cloud_cover_high": [0.0] * hours,
        "visibility": [20000.0] * hours,
        "snowfall": [0.0] * hours,
        "snow_depth": [0.0] * hours,
    }


@pytest.fixture
def good_payload() -> dict:
    """4 quarter-hours spanning one full hour + a second hour of context."""
    return {
        "minutely_15": _minutely_block(4),
        "hourly": _hourly_block(2),
    }


# ---------------------------------------------------------------------------
# build_params
# ---------------------------------------------------------------------------


def test_build_params_no_azimuth_conversion_and_fixed_model():
    p = build_params(48.547853, 12.187272, 3)
    # We fetch raw radiation components and transpose locally — no GTI/tilt
    # param, hence NO azimuth conversion in the fetch URL.
    joined = " ".join(f"{k}={v}" for k, v in p.items())
    assert "azimuth" not in joined
    assert "tilt" not in joined
    assert p["timezone"] == "UTC"
    assert p["models"] == "icon_seamless"
    assert p["forecast_days"] == "3"
    assert p["latitude"] == "48.547853"
    assert p["longitude"] == "12.187272"


def test_build_params_lists_every_requested_variable():
    p = build_params(1.0, 2.0, 3)
    for var in OPEN_METEO_MINUTELY_15:
        assert var in p["minutely_15"].split(",")
    for var in OPEN_METEO_HOURLY:
        assert var in p["hourly"].split(",")


# ---------------------------------------------------------------------------
# validate_payload — SHAPE, not just HTTP 200
# ---------------------------------------------------------------------------


def test_validate_accepts_well_formed(good_payload):
    validate_payload(good_payload)  # must not raise


def test_validate_rejects_non_dict():
    with pytest.raises(FetchError) as e:
        validate_payload([1, 2, 3])
    assert not e.value.retryable


def test_validate_rejects_missing_minutely_block(good_payload):
    del good_payload["minutely_15"]
    with pytest.raises(FetchError) as e:
        validate_payload(good_payload)
    assert not e.value.retryable


def test_validate_rejects_missing_variable(good_payload):
    del good_payload["minutely_15"]["diffuse_radiation"]
    with pytest.raises(FetchError):
        validate_payload(good_payload)


def test_validate_rejects_length_mismatch_minutely(good_payload):
    # One radiation array shorter than its time array.
    good_payload["minutely_15"]["shortwave_radiation"] = [0.0, 1.0]
    with pytest.raises(FetchError) as e:
        validate_payload(good_payload)
    assert not e.value.retryable
    assert "length" in str(e.value)


def test_validate_rejects_length_mismatch_hourly(good_payload):
    good_payload["hourly"]["visibility"] = [20000.0]  # hourly has 2 rows
    with pytest.raises(FetchError):
        validate_payload(good_payload)


def test_validate_rejects_empty_time(good_payload):
    good_payload["minutely_15"]["time"] = []
    for var in OPEN_METEO_MINUTELY_15:
        good_payload["minutely_15"][var] = []
    with pytest.raises(FetchError) as e:
        validate_payload(good_payload)
    assert "empty" in str(e.value)


def test_validate_rejects_array_not_a_list(good_payload):
    good_payload["minutely_15"]["temperature_2m"] = "not-a-list"
    with pytest.raises(FetchError):
        validate_payload(good_payload)


def test_validate_rejects_all_null_radiation_window(good_payload):
    """HTTP-200-with-nulls (model outage): an all-null near-term radiation
    window must be rejected (retryable), not accepted as a zero-PV day."""
    n = len(good_payload["minutely_15"]["time"])
    good_payload["minutely_15"]["shortwave_radiation"] = [None] * n
    with pytest.raises(FetchError) as e:
        validate_payload(good_payload)
    assert e.value.retryable
    assert "null" in str(e.value)


def test_validate_accepts_null_tail_beyond_near_term():
    """A null tail beyond the near-term window is normal (model horizon) and
    must NOT trip the null-window guard as long as the near term has data."""
    from datetime import timedelta

    # 100 h of hourly context, 400 15-min samples (100 h) so the near-term
    # 24 h window (96 samples) is well within the array.
    n15 = 400
    base = datetime(2026, 7, 5, 0, 15)
    m15_times = [
        (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M")
        for i in range(n15)
    ]
    # Non-null for the first 24 h (96 samples), null tail afterwards.
    swr = [100.0 if i < 96 else None for i in range(n15)]
    payload = {
        "minutely_15": {
            "time": m15_times,
            "shortwave_radiation": swr,
            "direct_normal_irradiance": [0.0] * n15,
            "diffuse_radiation": [0.0] * n15,
            "temperature_2m": [10.0] * n15,
        },
        "hourly": _hourly_block(100),
    }
    validate_payload(payload)  # must not raise


def test_radiation_coverage_counts_non_null(good_payload):
    from balcony_solar_forecast.fetcher import radiation_coverage

    assert radiation_coverage(good_payload) == 4
    good_payload["minutely_15"]["shortwave_radiation"] = [1.0, None, 3.0, None]
    assert radiation_coverage(good_payload) == 2
    assert radiation_coverage({}) == 0


# ---------------------------------------------------------------------------
# parse_weather — typed parse, interval semantics, tz, carry-forward
# ---------------------------------------------------------------------------


def test_parse_basic_shape_and_length(good_payload):
    ws = parse_weather(good_payload)
    assert len(ws) == 4
    assert all(s.start.tzinfo == UTC for s in ws.slots)


def test_parse_shifts_stamp_to_interval_start(good_payload):
    """Open-Meteo stamps the interval END (backward mean); slot start is −15m."""
    ws = parse_weather(good_payload)
    # First stamp is 00:15 -> slot start 00:00.
    assert ws.slots[0].start == datetime(2026, 7, 5, 0, 0, tzinfo=UTC)
    assert ws.slots[1].start == datetime(2026, 7, 5, 0, 15, tzinfo=UTC)


def test_parse_midpoint_is_start_plus_7m30s(good_payload):
    ws = parse_weather(good_payload)
    assert ws.slots[0].midpoint == datetime(
        2026, 7, 5, 0, 7, 30, tzinfo=UTC
    )


def test_parse_carries_hourly_context_by_floored_hour(good_payload):
    ws = parse_weather(good_payload)
    # All four slots fall in hour 00:00 -> cloud_low 80 from that hour row.
    for s in ws.slots:
        assert s.cloud_low == 80.0
        assert s.visibility_m == 20000.0


def test_parse_snowfall_is_already_cm(good_payload):
    # Open-Meteo hourly snowfall is already centimetres (hourly_units.snowfall
    # == "cm"): it must pass through unchanged, NOT be multiplied by 100.
    good_payload["hourly"]["snowfall"] = [2.0, 0.0]  # 2 cm in the first hour
    good_payload["hourly"]["snow_depth"] = [0.15, 0.15]  # metres
    ws = parse_weather(good_payload)
    assert ws.slots[0].snowfall_cm == pytest.approx(2.0)
    assert ws.slots[0].snow_depth_m == pytest.approx(0.15)


def test_parse_clamps_negative_and_none_radiation(good_payload):
    good_payload["minutely_15"]["shortwave_radiation"] = [-5.0, None, 3.0, 4.0]
    ws = parse_weather(good_payload)
    assert ws.slots[0].ghi == 0.0  # negative clamped
    assert ws.slots[1].ghi == 0.0  # None -> 0
    assert ws.slots[2].ghi == 3.0


def test_parse_tolerates_none_temperature(good_payload):
    good_payload["minutely_15"]["temperature_2m"] = [None, 1.0, 2.0, 3.0]
    ws = parse_weather(good_payload)
    assert ws.slots[0].temp_c == 0.0
    assert ws.slots[1].temp_c == 1.0


def test_parse_validates_before_parsing():
    """parse_weather never trusts an unchecked body."""
    with pytest.raises(FetchError):
        parse_weather({"minutely_15": {}, "hourly": {}})


def test_parse_second_hour_context_applies_to_its_slots():
    payload = {
        "minutely_15": _minutely_block(8),  # 2 full hours
        "hourly": {
            "time": ["2026-07-05T00:00", "2026-07-05T01:00"],
            "cloud_cover_low": [80.0, 20.0],
            "cloud_cover_mid": [0.0, 0.0],
            "cloud_cover_high": [0.0, 0.0],
            "visibility": [1000.0, 30000.0],
            "snowfall": [0.0, 0.0],
            "snow_depth": [0.0, 0.0],
        },
    }
    ws = parse_weather(payload)
    # Slots 0..3 -> hour 00; slots 4..7 -> hour 01 (starts 00:45..01:30).
    assert ws.slots[3].cloud_low == 80.0  # start 00:45 -> hour 00
    assert ws.slots[4].cloud_low == 20.0  # start 01:00 -> hour 01
    assert ws.slots[4].visibility_m == 30000.0


def test_parse_utc_suffix_is_normalised():
    payload = {
        "minutely_15": {
            "time": ["2026-07-05T00:15Z"],
            "shortwave_radiation": [1.0],
            "direct_normal_irradiance": [1.0],
            "diffuse_radiation": [1.0],
            "temperature_2m": [1.0],
        },
        "hourly": _hourly_block(1),
    }
    ws = parse_weather(payload)
    assert ws.slots[0].start == datetime(2026, 7, 5, 0, 0, tzinfo=UTC)
