"""W1 parity proof: the pure core and the (fetch-mocked) CLI emit the SAME dict.

The 0.23 refactor split the backfill in two:

  * ``custom_components/balcony_solar_forecast/core/bootstrap_build.py`` — the
    HA-free reconstruction / bootstrap MATH (data in, bootstrap dict out), and
  * ``scripts/backfill.py`` — a thin CLI wrapper that fetches (aiohttp:
    Open-Meteo Previous-Runs + HA WebSocket LTS) and writes the JSON.

This test feeds ONE set of fixed synthetic inputs (a few clear days of weather +
per-module hourly actuals) through BOTH paths and asserts the emitted bootstrap
dicts are byte-identical — the only field that legitimately differs is the
wall-clock ``generated_at`` timestamp, which is popped before the comparison.
This pins the refactor as verhaltensneutral: the CLI is nothing more than
fetching + I/O around the shared core.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Same import bootstrap as test_backfill_math: the conftest registers the
# HA-free namespace packages; make the standalone dev script importable too.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import backfill as bf  # noqa: E402
from balcony_solar_forecast import const  # noqa: E402
from balcony_solar_forecast.core import bootstrap_build as bb  # noqa: E402
from balcony_solar_forecast.core import horizon  # noqa: E402
from balcony_solar_forecast.core.types import SiteConfig  # noqa: E402

_FIXED_GEN = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def site() -> SiteConfig:
    return SiteConfig.from_dict(const.DEFAULT_SITE)


def _svf(site: SiteConfig) -> dict[str, float]:
    return {p.name: horizon.sky_view_factor(p) for p in site.planes}


def _weather_days(n_days: int = 3) -> list[bb.HourlyWeather]:
    """Clear high-sun UTC hours across ``n_days`` (a mix of morning + afternoon).

    Enough distinct hours/days that all three learners (shademap, day-ahead RLS,
    quantile ring) accumulate real content, so the parity comparison is over a
    non-trivial bootstrap dict.
    """
    start = datetime(2025, 6, 16, tzinfo=UTC)
    out: list[bb.HourlyWeather] = []
    for d in range(n_days):
        day = start + timedelta(days=d)
        for h in (8, 9, 10, 14, 15, 16, 17):
            out.append(
                bb.HourlyWeather(
                    start=day.replace(hour=h),
                    ghi=780.0, dni=820.0, dhi=120.0, temp_c=24.0,
                )
            )
    return out


def _entity_lts(
    site: SiteConfig,
    weather: list[bb.HourlyWeather],
    svf: dict[str, float],
) -> dict[str, dict[str, float]]:
    """Build entity-keyed hourly LTS tracking the modeled plane totals.

    Mirrors what ``fetch_lts_hourly`` returns ({statistic_id: {iso_hour: wh}}).
    A small per-day factor spreads the measured/modeled ratio so the quantile
    ring gets a genuine empirical band. Each plane maps to a distinct entity in
    DEFAULT_SITE, so this is a faithful stand-in for the WebSocket pull.
    """
    out: dict[str, dict[str, float]] = {}
    for plane in site.planes:
        ent = plane.actual_entity
        for wx in weather:
            day_idx = (wx.start.date() - weather[0].start.date()).days
            factor = 0.7 + 0.08 * day_idx
            r = bb.reconstruct_plane_hour(
                plane, svf[plane.name], wx,
                latitude=site.latitude, longitude=site.longitude,
            )
            total = (r.beam_wh + r.diffuse_wh) * factor
            if total > 0.0:
                out.setdefault(ent, {})[wx.start.isoformat()] = total
    return out


def _run_cli_path(monkeypatch, site, weather, lts_by_entity, out_path) -> dict:
    """Drive scripts/backfill.run_backfill with the fetch layer mocked out."""

    async def _fake_weather(session, **kwargs):
        return list(weather), True

    async def _fake_lts(session, **kwargs):
        # Return only the statistic ids the driver asked for (as the real
        # WebSocket reader does), so entity->channel mapping is exercised.
        ids = set(kwargs["statistic_ids"])
        return {sid: dict(rows) for sid, rows in lts_by_entity.items() if sid in ids}

    monkeypatch.setattr(bf, "fetch_weather_range", _fake_weather)
    monkeypatch.setattr(bf, "fetch_lts_hourly", _fake_lts)

    args = bf.build_arg_parser().parse_args([
        "--ha-url", "http://homeassistant.local:8123",
        "--token", "dummy-token",
        "--start", weather[0].start.date().isoformat(),
        "--end", weather[-1].start.date().isoformat(),
        "--out", str(out_path),
        # The parity fixture IS DEFAULT_SITE, so this run takes the reference
        # site deliberately (since 0.23.1 --site is otherwise required).
        "--use-default-site",
    ])
    rc = asyncio.run(bf.run_backfill(args))
    assert rc == 0, f"run_backfill returned {rc}"
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_core_and_cli_emit_identical_bootstrap(site, monkeypatch, tmp_path):
    weather = _weather_days()
    svf = _svf(site)
    lts_by_entity = _entity_lts(site, weather, svf)

    # --- Path A: the pure core module directly. ---
    channel_actuals = bf._entity_to_channel_actuals(site, lts_by_entity)
    acc = bb.accumulate_days(
        site, weather, channel_actuals, svf_by_plane=svf, tz=None
    )
    dict_core = bb.build_bootstrap_json(acc, site, generated_at=_FIXED_GEN)

    # --- Path B: the fetch-mocked CLI wrapper (writes the JSON). ---
    out_path = tmp_path / "bootstrap.json"
    dict_cli = _run_cli_path(monkeypatch, site, weather, lts_by_entity, out_path)

    # Sanity: the fixture actually trained all three layers (a non-trivial dict).
    assert dict_core[const.BOOTSTRAP_KEY_SHADEMAP]["channels"], "shademap empty"
    assert dict_core[const.BOOTSTRAP_KEY_BIAS]["cells"], "bias empty"
    assert dict_core[const.BOOTSTRAP_KEY_QUANTILE]["bins"], "quantile empty"

    # The wall-clock generated_at is the only field allowed to differ; both must
    # be valid ISO-UTC, and everything else byte-identical.
    gen_core = dict_core.pop(const.BOOTSTRAP_KEY_GENERATED_AT)
    gen_cli = dict_cli.pop(const.BOOTSTRAP_KEY_GENERATED_AT)
    assert datetime.fromisoformat(gen_core).tzinfo is not None
    assert datetime.fromisoformat(gen_cli).tzinfo is not None

    assert json.dumps(dict_core, sort_keys=True) == json.dumps(
        dict_cli, sort_keys=True
    )


def test_core_accumulate_is_deterministic(site):
    """accumulate_days over the same inputs is order-stable and repeatable."""
    weather = _weather_days()
    svf = _svf(site)
    channel_actuals = bf._entity_to_channel_actuals(
        site, _entity_lts(site, weather, svf)
    )
    a = bb.build_bootstrap_json(
        bb.accumulate_days(site, weather, channel_actuals, svf_by_plane=svf),
        site, generated_at=_FIXED_GEN,
    )
    b = bb.build_bootstrap_json(
        bb.accumulate_days(site, weather, channel_actuals, svf_by_plane=svf),
        site, generated_at=_FIXED_GEN,
    )
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a[const.BOOTSTRAP_KEY_SITE_SIGNATURE] == bb.site_signature(site)
