"""Tests for ``core.openmeteo_backfill`` — the ONE core module that fetches.

The pure parse helpers (``_num`` / ``_as_utc_hour`` / the uncovered
``parse_hourly_payload`` branches) run everywhere. The network orchestration
(``fetch_weather_range``: Previous-Runs first, degrade to the Historical
Forecast API on failure or an empty as-issued window; ``_get_json``: timeout +
HTTP-error mapping) is driven against a scripted fake aiohttp session — no
sockets. Those tests importorskip aiohttp (imported lazily by the module
under test) so the file stays green on the minimal plain-core path too.

Mirrors the SPEC §12.1/§12.4 contract: as-issued day-1-lead radiation when
the Previous-Runs API delivers, a LOUD degrade to analysis data when not.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import pytest
from balcony_solar_forecast.core import openmeteo_backfill as omb

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_as_utc_hour_naive_stamp_gets_utc():
    assert omb._as_utc_hour("2025-06-21T09:00") == datetime(
        2025, 6, 21, 9, 0, tzinfo=UTC
    )


def test_as_utc_hour_tz_aware_stamp_is_converted_to_utc():
    # 11:00 at +02:00 is 09:00 UTC — a foreign-offset stamp must be NORMALISED,
    # not trusted (Open-Meteo serves UTC when asked, but the parser is total).
    assert omb._as_utc_hour("2025-06-21T11:00:00+02:00") == datetime(
        2025, 6, 21, 9, 0, tzinfo=UTC
    )


def test_num_maps_garbage_to_none():
    assert omb._num(None) is None
    assert omb._num("not-a-number") is None
    assert omb._num(object()) is None
    # NaN / +-inf are not usable physics inputs either.
    assert omb._num(float("nan")) is None
    assert omb._num(float("inf")) is None
    assert omb._num(float("-inf")) is None
    assert omb._num("12.5") == 12.5
    assert omb._num(7) == 7.0


def test_parse_skips_non_string_time_stamps():
    payload = {
        "hourly": {
            "time": ["2025-06-21T09:00", None, 12345],
            "shortwave_radiation": [700.0, 710.0, 720.0],
            "direct_normal_irradiance": [800.0, 810.0, 820.0],
            "diffuse_radiation": [110.0, 120.0, 130.0],
            "temperature_2m": [22.0, 23.0, 24.0],
        }
    }
    recs = omb.parse_hourly_payload(payload, var_suffix="")
    # Only the well-formed stamp survives; the columns stay index-aligned.
    assert [r.ghi for r in recs] == [700.0]
    assert recs[0].start == datetime(2025, 6, 21, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Network layer (scripted fake aiohttp session, no sockets)
# ---------------------------------------------------------------------------


@pytest.fixture
def aiohttp_mod():
    return pytest.importorskip("aiohttp")


class _FakeResponse:
    def __init__(self, payload, *, status: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    """``aiohttp.ClientSession`` stand-in: scripted per-call outcomes, records
    every ``(url, params, timeout)`` so the test can assert the request shape."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, dict, object]] = []

    def get(self, url: str, *, params: dict, timeout: object):
        self.calls.append((url, params, timeout))
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _payload(times, *, suffix: str, ghi) -> dict:
    """Hourly payload with all four physics variables (suffixed or plain)."""
    n = len(times)
    return {
        "hourly": {
            "time": times,
            f"shortwave_radiation{suffix}": ghi,
            f"direct_normal_irradiance{suffix}": [800.0] * n,
            f"diffuse_radiation{suffix}": [110.0] * n,
            f"temperature_2m{suffix}": [22.0] * n,
            "cloud_cover_low": [5.0] * n,
            "visibility": [24000.0] * n,
        }
    }


async def test_get_json_returns_payload_with_timeout(aiohttp_mod):
    resp = _FakeResponse({"hourly": {"time": []}})
    session = _FakeSession([resp])
    out = await omb._get_json(session, "https://example.test", {"a": "b"})
    assert out == {"hourly": {"time": []}}
    url, params, timeout = session.calls[0]
    assert url == "https://example.test"
    assert params == {"a": "b"}
    # The bounded total timeout travels with every request (no hang forever).
    assert isinstance(timeout, aiohttp_mod.ClientTimeout)
    assert timeout.total == omb._HTTP_TIMEOUT_SECONDS


async def test_get_json_http_error_raises_with_body_snippet(aiohttp_mod):
    session = _FakeSession([_FakeResponse(None, status=500, text="upstream broke")])
    with pytest.raises(RuntimeError, match="HTTP 500") as excinfo:
        await omb._get_json(session, "https://example.test", {})
    assert "upstream broke" in str(excinfo.value)


async def test_fetch_range_previous_runs_success_is_as_issued(aiohttp_mod):
    times = ["2025-06-21T09:00", "2025-06-21T10:00"]
    session = _FakeSession(
        [_FakeResponse(_payload(times, suffix="_previous_day1", ghi=[700.0, 750.0]))]
    )
    records, is_as_issued = await omb.fetch_weather_range(
        session, latitude=51.1, longitude=10.4,
        start=date(2025, 6, 21), end=date(2025, 6, 22),
    )
    assert is_as_issued is True
    assert [r.ghi for r in records] == [700.0, 750.0]
    # Exactly one request: the Previous-Runs API with day-1-lead variables.
    assert len(session.calls) == 1
    url, params, _timeout = session.calls[0]
    assert url == omb.PREVIOUS_RUNS_URL
    assert params["latitude"] == "51.100000"
    assert params["longitude"] == "10.400000"
    assert params["start_date"] == "2025-06-21"
    assert params["end_date"] == "2025-06-22"
    assert params["timezone"] == "UTC"
    assert params["models"] == "icon_seamless"
    assert "shortwave_radiation_previous_day1" in params["hourly"]


async def test_fetch_range_degrades_to_historical_on_provider_failure(
    aiohttp_mod, caplog
):
    times = ["2025-06-21T09:00"]
    session = _FakeSession(
        [
            ConnectionError("dns gone"),
            _FakeResponse(_payload(times, suffix="", ghi=[701.0])),
        ]
    )
    with caplog.at_level(logging.WARNING, logger=omb.__name__):
        records, is_as_issued = await omb.fetch_weather_range(
            session, latitude=51.1, longitude=10.4,
            start=date(2025, 6, 21), end=date(2025, 6, 21),
        )
    assert is_as_issued is False
    assert [r.ghi for r in records] == [701.0]
    # Second request: Historical Forecast API, PLAIN (unsuffixed) variables.
    assert len(session.calls) == 2
    url, params, _ = session.calls[1]
    assert url == omb.HISTORICAL_FORECAST_URL
    assert "shortwave_radiation_previous_day1" not in params["hourly"]
    assert "shortwave_radiation" in params["hourly"]
    # The degrade is loud, never silent (SPEC §13).
    assert any("Historical Forecast API" in r.getMessage() for r in caplog.records)


async def test_fetch_range_degrades_when_previous_runs_window_empty(
    aiohttp_mod, caplog
):
    """A 200 with all-null suffixed radiation is NOT as-issued data: degrade
    with the dedicated warning instead of learning from an empty window."""
    times = ["2025-06-21T09:00"]
    session = _FakeSession(
        [
            _FakeResponse(_payload(times, suffix="_previous_day1", ghi=[None])),
            _FakeResponse(_payload(times, suffix="", ghi=[702.0])),
        ]
    )
    with caplog.at_level(logging.WARNING, logger=omb.__name__):
        records, is_as_issued = await omb.fetch_weather_range(
            session, latitude=51.1, longitude=10.4,
            start=date(2025, 6, 21), end=date(2025, 6, 21),
        )
    assert is_as_issued is False
    assert [r.ghi for r in records] == [702.0]
    assert any("no as-issued radiation" in r.getMessage() for r in caplog.records)


async def test_fetch_range_propagates_when_both_apis_fail(aiohttp_mod):
    """The degrade is a fallback, not a swallow: if the Historical Forecast API
    also fails, the caller (bootstrap) sees the error and skips the range."""
    session = _FakeSession(
        [
            ConnectionError("dns gone"),
            _FakeResponse(None, status=503, text="service unavailable"),
        ]
    )
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await omb.fetch_weather_range(
            session, latitude=51.1, longitude=10.4,
            start=date(2025, 6, 21), end=date(2025, 6, 21),
        )
