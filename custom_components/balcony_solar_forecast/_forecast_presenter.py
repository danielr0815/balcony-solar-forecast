"""Forecast response presentation, shared independently of entity lifecycle.

Historical sensor imports remain reexports; registration belongs to services.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.util import dt as dt_util

from ._forecast_access import current_forecast
from .const import (
    ATTR_WH_PERIOD,
    DATA_KEY_BAND_SOURCE,
    DATA_KEY_BAND_SOURCE_BY_DAY,
    DATA_KEY_QUANTILE_CURVES,
    DOMAIN,
    FORECAST_RESP_KEY_P10,
    FORECAST_RESP_KEY_P50,
    FORECAST_RESP_KEY_P90,
)

_KEY_SLOT_STARTS = "slot_starts"
_KEY_PLANE_WATTS = "plane_watts"
_KEY_HOURLY_WH = "hourly_wh"
_KEY_COMPUTED_AT = "computed_at"
_KEY_WATTS = "watts"
_Q_P10 = FORECAST_RESP_KEY_P10
_Q_P50 = FORECAST_RESP_KEY_P50
_Q_P90 = FORECAST_RESP_KEY_P90


def _build_forecast_response(
    hass: HomeAssistant, entry_id: str | None
) -> ServiceResponse:
    """Assemble the get_forecast response for one or all entries.

    Returns ``{entries: {entry_id: {planes, slot_starts, total_15min,
    total_hourly, issued_at}}}``. ``planes`` maps plane name -> list of 15-min
    watts aligned to ``slot_starts``. All read from the coordinator's flat
    ``self.data`` dict; a coordinator without a current forecast yields empty
    curves rather than a stale one (SPEC §13).
    """
    entries: dict[str, Any] = {}
    store = hass.data.get(DOMAIN, {})
    for eid, coordinator in store.items():
        if entry_id is not None and eid != entry_id:
            continue
        data = current_forecast(coordinator)
        if not data:
            entries[eid] = _empty_forecast_entry()
            continue
        entry_resp = {
            "slot_starts": list(data.get(_KEY_SLOT_STARTS, [])),
            "planes": {
                name: list(watts)
                for name, watts in (data.get(_KEY_PLANE_WATTS) or {}).items()
            },
            "total_15min": [
                w for _, w in _iter_curve(data)
            ],
            "total_hourly": dict(data.get(_KEY_HOURLY_WH) or {}),
            "issued_at": data.get(_KEY_COMPUTED_AT),
        }
        # v0.4 quantile bands (SPEC §11.2/§14.4): plane-agnostic TOTAL p10/p50/p90
        # 15-min + hourly Wh curves alongside the served (corrected) curve. Only
        # present when the engine issued bands this cycle; absent otherwise so a
        # quantiles-off / cold-start install simply omits the blocks rather than
        # fabricating a spread.
        bands = _band_blocks(data)
        if bands:
            entry_resp.update(bands)
            # Band provenance (SCT-4): the today-level source label + the compact
            # per-local-day count breakdown, so a consumer of the raw curves can
            # tell which days actually carry a trained band. Gated on ``bands``
            # like the curve blocks above: a quantiles-off / cold-start response
            # carries no band block at all, so it must not claim a band_source
            # either (would be a status lie — cf. EnergyBandSensor gating).
            entry_resp["band_source"] = data.get(DATA_KEY_BAND_SOURCE, "learned")
            by_day = data.get(DATA_KEY_BAND_SOURCE_BY_DAY)
            if isinstance(by_day, dict) and by_day:
                entry_resp["band_source_by_day"] = dict(by_day)
        readiness = data.get("quantile_readiness")
        if isinstance(readiness, dict):
            entry_resp["quantile_readiness"] = readiness
        entries[eid] = entry_resp
    return {"entries": entries}


def _band_blocks(data: dict[str, Any]) -> dict[str, Any]:
    """Assemble the p10/p50/p90 15-min + hourly forecast-response blocks.

    Reads the coordinator's ``DATA_KEY_QUANTILE_CURVES`` (15-min band Wh curves
    keyed by ISO-UTC slot start) and rolls each up to hourly Wh. Returns a dict
    ``{p10: {"wh_period": {...}, "hourly": {...}}, p50: ..., p90: ...}`` — or an
    empty dict when no bands were issued (quantiles off / cold start), so the
    caller omits the blocks entirely. Pure; never raises on a malformed curve.
    """
    curves = data.get(DATA_KEY_QUANTILE_CURVES)
    if not isinstance(curves, dict) or not curves:
        return {}
    out: dict[str, Any] = {}
    for key in (_Q_P10, _Q_P50, _Q_P90):
        curve = curves.get(key)
        if not isinstance(curve, dict) or not curve:
            continue
        out[key] = {
            ATTR_WH_PERIOD: dict(curve),
            "hourly": _hourly_from_slots(curve),
        }
    return out


def _hourly_from_slots(slot_wh: dict[str, float]) -> dict[str, float]:
    """Roll a 15-min ``{iso_slot: Wh}`` curve up to ``{iso_hour: Wh}``.

    Buckets each slot's Wh into its containing UTC hour (truncating the slot
    start to the hour). Malformed keys/values are skipped so a diagnostic curve
    can never crash the response.
    """
    hourly: dict[str, float] = {}
    for iso, wh in slot_wh.items():
        parsed = dt_util.parse_datetime(iso) if isinstance(iso, str) else None
        if parsed is None or not isinstance(wh, (int, float)):
            continue
        hour_key = parsed.replace(minute=0, second=0, microsecond=0).isoformat()
        hourly[hour_key] = round(hourly.get(hour_key, 0.0) + float(wh), 2)
    return hourly


def _empty_forecast_entry() -> dict[str, Any]:
    return {
        "planes": {},
        "slot_starts": [],
        "total_15min": [],
        "total_hourly": {},
        "issued_at": None,
    }


def _iter_curve(data: dict[str, Any]):
    """Yield ``(iso_start, watts)`` pairs of the site-total 15-min curve.

    Ordered by ``slot_starts`` when present (the ``watts`` dict is keyed by
    the same ISO strings); falls back to the dict's own order otherwise.
    """
    watts = data.get(_KEY_WATTS) or {}
    starts = data.get(_KEY_SLOT_STARTS)
    if starts:
        for iso in starts:
            if iso in watts:
                yield iso, watts[iso]
    else:
        yield from watts.items()
