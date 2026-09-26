"""Scripted weather inputs shared by coordinator unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.helpers.coordinator import _FakeStore, _make_coordinator

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
FETCH_INTERVAL = timedelta(seconds=1800)


# ---------------------------------------------------------------------------
# Payload / fetcher fakes
# ---------------------------------------------------------------------------


def _om_payload(n_quarters: int = 8, start_iso: str = "2026-07-05T10:15") -> dict:
    """A minimal valid Open-Meteo payload (mirrors tests/test_fetcher_shapes)."""
    base = datetime.fromisoformat(start_iso)
    times = [
        (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M")
        for i in range(n_quarters)
    ]
    hours = max(1, n_quarters // 4 + 1)
    hbase = base.replace(minute=0)
    htimes = [
        (hbase + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
        for i in range(hours)
    ]
    return {
        "minutely_15": {
            "time": times,
            "shortwave_radiation": [500.0] * n_quarters,
            "direct_normal_irradiance": [600.0] * n_quarters,
            "diffuse_radiation": [150.0] * n_quarters,
            "temperature_2m": [22.0] * n_quarters,
        },
        "hourly": {
            "time": htimes,
            "cloud_cover_low": [10.0] * hours,
            "cloud_cover_mid": [0.0] * hours,
            "cloud_cover_high": [0.0] * hours,
            "visibility": [30000.0] * hours,
            "snowfall": [0.0] * hours,
            "snow_depth": [0.0] * hours,
        },
    }


def _sparse_payload(**kw) -> dict:
    """A payload with LESS radiation coverage (nulled radiation samples)."""
    p = _om_payload(**kw)
    n = len(p["minutely_15"]["time"])
    p["minutely_15"]["shortwave_radiation"] = [None] * n
    p["minutely_15"]["direct_normal_irradiance"] = [None] * n
    p["minutely_15"]["diffuse_radiation"] = [None] * n
    return p


class _PayloadStore(_FakeStore):
    """FakeStore that actually holds a last-good payload."""

    def __init__(self) -> None:
        super().__init__()
        self.last_payload: dict | None = None

    def get_last_payload(self):
        return self.last_payload

    def set_last_payload(self, payload, fetched_at_iso):
        self.last_payload = {"payload": payload, "fetched_at": fetched_at_iso}


class _FakeFetcher:
    """Scripted fetcher: pops the next behaviour per call."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def async_fetch_raw(self, _lat, _lon, _days):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _coord(store: _PayloadStore | None = None):
    c = _make_coordinator(store or _PayloadStore())
    c._fetch_interval = FETCH_INTERVAL
    return c
