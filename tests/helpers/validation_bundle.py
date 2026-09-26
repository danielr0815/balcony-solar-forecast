"""Synthetic two-day post-deployment package; no operator data.

One accurate day and one 15 % overforecast day exercise both scalar directions.
All numbers are independent of the checker implementation and kept round so
the expected verdict is understandable without the old private baseline dump.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def write_validation_bundle(path: Path, *, morning_multiplier: float = 1.0) -> None:
    prefix = "sensor.balcony_solar_forecast_"
    actual_ac = prefix + "measured_ac_power"
    actual_dc = prefix + "measured_dc_power_total"
    served_ac = prefix + "power_production_now"
    served_dc = prefix + "power_production_now_dc"
    scalar = prefix + "intraday_correction_scalar"
    today = prefix + "energy_production_today"
    p10 = today + "_p10"
    hourly = {key: [] for key in (
        actual_ac, actual_dc, served_ac,
        "sensor.inverter_port_2_dc_power_2", "sensor.inverter_port_2_dc_power_4",
    )}
    five = {key: [] for key in (actual_dc, served_dc, scalar)}
    issued = {}
    history = {today: [], p10: []}
    for index in range(2):
        day = datetime(2026, 7, 20 + index, tzinfo=UTC)
        curve = {}
        raw = {}
        classes = {}
        for hour in range(4, 19):
            ts = day.replace(hour=hour)
            key = ts.isoformat()
            curve[key] = 360.0 * (1.15 if index else 1.0)
            raw[key] = 358.8 if hour in (11, 12) else 400.0
            classes[key] = "clear"
            values = {
                actual_ac: 360.0, actual_dc: 400.0,
                served_ac: 360.0 * (morning_multiplier if 5 <= hour < 8 else 1.0),
                "sensor.inverter_port_2_dc_power_2": 50.0,
                "sensor.inverter_port_2_dc_power_4": 50.0,
            }
            for sid, value in values.items():
                hourly[sid].append({"start": ts.timestamp(), "mean": value})
        for minute in range(0, 180, 5):
            ts = day.replace(hour=4) + timedelta(minutes=minute)
            for sid, value in ((actual_dc, 400.0), (served_dc, 400.0),
                               (scalar, 0.8 if index else 1.0)):
                five[sid].append({"start": ts.timestamp(), "mean": value})
        issued[day.date().isoformat()] = {
            "available": True, "hourly_wh_ac": curve,
            "raw_hourly_wh": raw, "cloud_class_by_hour": classes,
        }
        for eid, value in ((today, 5.4), (p10, 4.0)):
            for hour in (6, 7):
                history[eid].append({
                    "entity_id": eid, "state": str(value),
                    "last_changed": day.replace(hour=hour).isoformat(),
                })
    cells = {key: {"theta": 1.0, "applied": 1.0, "n": 10, "clamped": False}
             for key in ("clear|morning", "clear|afternoon",
                         "mixed|afternoon", "overcast|afternoon")}
    entities = {
        prefix + "day_ahead_bias_status": {"attributes": {"bias_cells": cells}},
        today: {"attributes": {
            "wh_period": {"slot": 360.0}, "wh_period_p10": {"slot": 300.0},
            "wh_period_p90": {"slot": 400.0},
        }},
    }
    files = {
        "actuals_hourly_stats.json": hourly,
        "fiveminute_stats.json": five,
        "entities_now.json": entities,
        "forecast_sensor_history.json": {"minimal": list(history.values())},
        "issued_forecasts_and_diag.json": {
            "issued": issued, "diagnostics": {
                "data": {"quantiles": {"bins": {"clear|morning": {"trained": True}}}},
            },
        },
    }
    for name, data in files.items():
        (path / name).write_text(json.dumps(data), encoding="utf-8")
