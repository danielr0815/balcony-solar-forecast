"""Retry + Retry-After tests for the Open-Meteo fetcher.

Two layers are exercised WITHOUT real aiohttp (imported lazily inside the
network coroutine, and stubbed here):

  * ``_request_once`` status/error classification — driven by a tiny fake
    ``aiohttp`` module and a fake session whose ``.get`` yields a settable
    async-context-manager response;
  * the ``_async_fetch_payload`` retry loop — driven by monkeypatching
    ``_request_once`` and ``fetcher.asyncio.sleep`` so no wall-clock passes
    and the requested delays are recorded.

The package is bootstrapped by ``tests/conftest.py`` (see the note in
tests/test_fetcher_shapes.py); no Home Assistant import is needed.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta

import pytest
from balcony_solar_forecast import fetcher as fetcher_module
from balcony_solar_forecast.fetcher import (
    FetchError,
    OpenMeteoFetcher,
    validate_payload,
)

# ---------------------------------------------------------------------------
# Fakes: aiohttp module + session/response (no real network, no HA)
# ---------------------------------------------------------------------------


class _FakeContentTypeError(Exception):
    """Stand-in for ``aiohttp.ContentTypeError`` (json() on a non-JSON body)."""


class _FakeClientError(Exception):
    """Stand-in for ``aiohttp.ClientError`` (connection-level failure)."""


def _fake_aiohttp() -> types.SimpleNamespace:
    """The subset of the aiohttp module ``_request_once`` touches."""
    return types.SimpleNamespace(
        ClientTimeout=lambda total=None: types.SimpleNamespace(total=total),
        ContentTypeError=_FakeContentTypeError,
        ClientError=_FakeClientError,
    )


class _FakeResp:
    """A response with settable status/headers and a scriptable ``json()``."""

    def __init__(self, *, status, headers=None, json_value=None, json_exc=None):
        self.status = status
        self.headers = headers or {}
        self._json_value = json_value
        self._json_exc = json_exc

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_value


class _FakeCtx:
    """Async context manager yielding ``resp``, or raising on __aenter__."""

    def __init__(self, *, resp=None, enter_exc=None):
        self._resp = resp
        self._enter_exc = enter_exc

    async def __aenter__(self):
        if self._enter_exc is not None:
            raise self._enter_exc
        return self._resp

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    """Session whose ``.get(...)`` returns a fixed context manager."""

    def __init__(self, ctx: _FakeCtx):
        self._ctx = ctx

    def get(self, _url, params=None, timeout=None):
        return self._ctx


class _RaisingGetSession:
    """Session whose ``.get(...)`` raises before any context manager exists."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def get(self, *_a, **_kw):
        raise self._exc


def _fetcher_for(session) -> OpenMeteoFetcher:
    return OpenMeteoFetcher(session)


# ---------------------------------------------------------------------------
# _request_once — status / error classification
# ---------------------------------------------------------------------------


async def test_request_once_500_is_retryable():
    fetcher = _fetcher_for(_FakeSession(_FakeCtx(resp=_FakeResp(status=500))))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is True
    assert e.value.retry_after is None


async def test_request_once_404_is_not_retryable():
    fetcher = _fetcher_for(_FakeSession(_FakeCtx(resp=_FakeResp(status=404))))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is False


async def test_request_once_429_numeric_retry_after_is_parsed():
    resp = _FakeResp(status=429, headers={"Retry-After": "7"})
    fetcher = _fetcher_for(_FakeSession(_FakeCtx(resp=resp)))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is True
    assert e.value.retry_after == 7.0


async def test_request_once_429_http_date_retry_after_is_none():
    # An HTTP-date Retry-After is unparsable as seconds -> plain retryable.
    resp = _FakeResp(
        status=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    )
    fetcher = _fetcher_for(_FakeSession(_FakeCtx(resp=resp)))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is True
    assert e.value.retry_after is None


async def test_request_once_429_without_header_has_no_retry_after():
    resp = _FakeResp(status=429, headers={})
    fetcher = _fetcher_for(_FakeSession(_FakeCtx(resp=resp)))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is True
    assert e.value.retry_after is None


async def test_request_once_non_json_body_is_retryable():
    resp = _FakeResp(status=200, json_exc=_FakeContentTypeError("not json"))
    fetcher = _fetcher_for(_FakeSession(_FakeCtx(resp=resp)))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is True


async def test_request_once_client_error_from_get_is_retryable():
    fetcher = _fetcher_for(_RaisingGetSession(_FakeClientError("no route")))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is True


async def test_request_once_timeout_from_context_is_retryable():
    # TimeoutError is the (asyncio) timeout alias caught by the coroutine.
    fetcher = _fetcher_for(_FakeSession(_FakeCtx(enter_exc=TimeoutError("slow"))))
    with pytest.raises(FetchError) as e:
        await fetcher._request_once(_fake_aiohttp(), {})
    assert e.value.retryable is True


# ---------------------------------------------------------------------------
# _async_fetch_payload — retry loop behaviour
# ---------------------------------------------------------------------------


def _good_payload() -> dict:
    """A minimal well-formed payload (mirrors tests/test_fetcher_shapes)."""
    n = 4
    base = datetime(2026, 7, 5, 0, 15)
    m15_times = [
        (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M")
        for i in range(n)
    ]
    hbase = datetime(2026, 7, 5, 0, 0)
    h_times = [
        (hbase + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(2)
    ]
    return {
        "minutely_15": {
            "time": m15_times,
            "shortwave_radiation": [float(i) for i in range(n)],
            "direct_normal_irradiance": [0.0] * n,
            "diffuse_radiation": [0.0] * n,
            "temperature_2m": [10.0] * n,
        },
        "hourly": {
            "time": h_times,
            "cloud_cover_low": [10.0] * 2,
            "cloud_cover_mid": [0.0] * 2,
            "cloud_cover_high": [0.0] * 2,
            "visibility": [30000.0] * 2,
            "snowfall": [0.0] * 2,
            "snow_depth": [0.0] * 2,
        },
    }


class _ScriptedOnce:
    """Callable drop-in for ``_request_once``: pops a scripted result/error."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls = 0

    async def __call__(self, _aiohttp, _params):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def recorded_sleeps(monkeypatch) -> list[float]:
    """Replace ``fetcher.asyncio.sleep`` with a no-wait recorder of delays."""
    sleeps: list[float] = []

    async def _record(delay):
        sleeps.append(delay)

    # fetcher.py does ``import asyncio`` at module level; patch its attribute.
    monkeypatch.setattr(fetcher_module.asyncio, "sleep", _record)
    return sleeps


def _patch_once(monkeypatch, fetcher, script) -> _ScriptedOnce:
    scripted = _ScriptedOnce(script)
    monkeypatch.setattr(fetcher, "_request_once", scripted)
    return scripted


async def test_loop_retry_after_short_sleeps_exact_value(
    monkeypatch, recorded_sleeps
):
    fetcher = _fetcher_for(None)
    scripted = _patch_once(
        monkeypatch,
        fetcher,
        [
            FetchError("rate limited", retryable=True, retry_after=5.0),
            _good_payload(),
        ],
    )

    payload = await fetcher._async_fetch_payload(1.0, 2.0, 3)

    validate_payload(payload)  # the success body actually made it through
    assert scripted.calls == 2
    # Exactly the server-requested wait, NOT a jittered backoff value.
    assert recorded_sleeps == [5.0]


async def test_loop_retry_after_too_long_raises_immediately(
    monkeypatch, recorded_sleeps
):
    fetcher = _fetcher_for(None)
    scripted = _patch_once(
        monkeypatch,
        fetcher,
        [FetchError("rate limited", retryable=True, retry_after=120.0)],
    )

    with pytest.raises(FetchError) as e:
        await fetcher._async_fetch_payload(1.0, 2.0, 3)

    assert e.value.retry_after == 120.0
    assert scripted.calls == 1  # one attempt: handed straight back to the ladder
    assert recorded_sleeps == []


async def test_loop_non_retryable_raises_immediately(monkeypatch, recorded_sleeps):
    fetcher = _fetcher_for(None)
    scripted = _patch_once(
        monkeypatch, fetcher, [FetchError("client error", retryable=False)]
    )

    with pytest.raises(FetchError):
        await fetcher._async_fetch_payload(1.0, 2.0, 3)

    assert scripted.calls == 1
    assert recorded_sleeps == []


async def test_loop_jittered_backoff_exhausts_all_tries(
    monkeypatch, recorded_sleeps
):
    fetcher = _fetcher_for(None)
    scripted = _patch_once(
        monkeypatch,
        fetcher,
        [
            FetchError(f"boom {i}", retryable=True)
            for i in range(fetcher_module.MAX_TRIES)
        ],
    )

    with pytest.raises(FetchError):
        await fetcher._async_fetch_payload(1.0, 2.0, 3)

    assert scripted.calls == fetcher_module.MAX_TRIES
    assert len(recorded_sleeps) == fetcher_module.MAX_TRIES - 1
    # Full-jitter backoff: each delay lies within [0, ceiling(attempt)].
    for attempt, delay in enumerate(recorded_sleeps, start=1):
        ceiling = min(
            fetcher_module._MAX_BACKOFF_SECONDS,
            fetcher_module._BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
        )
        assert 0.0 <= delay <= ceiling


# ---------------------------------------------------------------------------
# _async_fetch_payload — success payload is still SHAPE-validated
# ---------------------------------------------------------------------------


async def test_loop_validates_malformed_success_payload(
    monkeypatch, recorded_sleeps
):
    fetcher = _fetcher_for(None)
    scripted = _patch_once(
        monkeypatch, fetcher, [{"minutely_15": {}, "hourly": {}}]
    )

    with pytest.raises(FetchError):
        await fetcher._async_fetch_payload(1.0, 2.0, 3)

    assert scripted.calls == 1  # validation failure is non-retryable, fails fast


async def test_loop_wellformed_success_returns_payload(
    monkeypatch, recorded_sleeps
):
    fetcher = _fetcher_for(None)
    good = _good_payload()
    scripted = _patch_once(monkeypatch, fetcher, [good])

    result = await fetcher._async_fetch_payload(1.0, 2.0, 3)

    assert result is good
    assert scripted.calls == 1
    assert recorded_sleeps == []
