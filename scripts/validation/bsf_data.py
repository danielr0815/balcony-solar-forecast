"""bsf_data -- Laden + Normalisieren der HA-Datenpakete (Format wie hadata/).

Nur Python-stdlib. Alle Zeitstempel intern als aware datetime (UTC).
Lokale Zeit = Europe/Berlin (zoneinfo, Fallback: eigene EU-DST-Regel).

Semantik-Fallen, die hier zentral behandelt werden:
  * recorder-WS liefert start/end als epoch-MILLISEKUNDEN (auto-detektiert).
  * history/period minimal_response: erstes Element voll, danach {"s","lc"}.
  * get_issued_forecast: hourly_wh/raw_hourly_wh sind bis v0.20.6 DC-basiert;
    ab v0.21.0 kommt hourly_wh_ac dazu. Der Loader erkennt das Feld und setzt
    Feature-Flags, die Umrechnung DC->AC (eta) macht erst die Check-Schicht.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

UTC = dt.UTC

# ---------------------------------------------------------------------------
# Zeitzone
# ---------------------------------------------------------------------------


class _BerlinFallback(dt.tzinfo):
    """EU-DST-Regel (letzter So Maerz/Okt, 01:00 UTC) falls zoneinfo fehlt."""

    @staticmethod
    def _last_sunday(year: int, month: int) -> dt.datetime:
        d = dt.date(year, month, 31)
        d -= dt.timedelta(days=(d.weekday() + 1) % 7)  # Mon=0..Sun=6
        return dt.datetime(d.year, d.month, d.day, 1, 0, 0)

    def _is_dst_utc(self, naive_utc: dt.datetime) -> bool:
        return (
            self._last_sunday(naive_utc.year, 3)
            <= naive_utc
            < self._last_sunday(naive_utc.year, 10)
        )

    def fromutc(self, d: dt.datetime) -> dt.datetime:
        off = (
            dt.timedelta(hours=2)
            if self._is_dst_utc(d.replace(tzinfo=None))
            else dt.timedelta(hours=1)
        )
        return d + off

    def utcoffset(self, d: dt.datetime | None) -> dt.timedelta:
        if d is None:
            return dt.timedelta(hours=1)
        approx = d.replace(tzinfo=None) - dt.timedelta(hours=1)
        return (
            dt.timedelta(hours=2) if self._is_dst_utc(approx) else dt.timedelta(hours=1)
        )

    def dst(self, d: dt.datetime | None) -> dt.timedelta:
        return self.utcoffset(d) - dt.timedelta(hours=1)

    def tzname(self, d: dt.datetime | None) -> str:
        return "CEST" if self.utcoffset(d) == dt.timedelta(hours=2) else "CET"


def local_tz() -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Berlin")
    except Exception:  # noqa: BLE001 - Windows ohne tzdata-Paket
        return _BerlinFallback()


LOC = local_tz()

# ---------------------------------------------------------------------------
# Parsing-Helfer
# ---------------------------------------------------------------------------


def parse_epoch(v: float) -> dt.datetime:
    """epoch s ODER ms -> aware UTC datetime (WS-API liefert ms!)."""
    if v > 1e11:  # ms
        v = v / 1000.0
    return dt.datetime.fromtimestamp(v, UTC)


def parse_iso(s: str) -> dt.datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def to_float(s: Any) -> float | None:
    if s is None or s in ("unknown", "unavailable", ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

FILE_HOURLY = "actuals_hourly_stats.json"
FILE_5MIN = "fiveminute_stats.json"
FILE_ENTITIES = "entities_now.json"
FILE_HISTORY = "forecast_sensor_history.json"
FILE_ISSUED = "issued_forecasts_and_diag.json"

SID_SERVED_AC = "sensor.balcony_solar_forecast_power_production_now"
SID_SERVED_DC = "sensor.balcony_solar_forecast_power_production_now_dc"
SID_ACT_AC = "sensor.balcony_solar_forecast_measured_ac_power"
SID_ACT_DC = "sensor.balcony_solar_forecast_measured_dc_power_total"
SID_SCALAR = "sensor.balcony_solar_forecast_intraday_correction_scalar"
SID_M4 = "sensor.inverter_port_2_dc_power_2"
SID_M8 = "sensor.inverter_port_2_dc_power_4"

EID_TODAY = "sensor.balcony_solar_forecast_energy_production_today"
EID_TOMORROW = "sensor.balcony_solar_forecast_energy_production_tomorrow"
EID_TODAY_P10 = "sensor.balcony_solar_forecast_energy_production_today_p10"
EID_TODAY_P90 = "sensor.balcony_solar_forecast_energy_production_today_p90"
EID_BIAS = "sensor.balcony_solar_forecast_day_ahead_bias_status"


@dataclass
class Bundle:
    data_dir: str
    # {stat_id: {utc_start: mean}} Stundenmittel W (Mittel W x 1h = Wh)
    hourly: dict[str, dict[dt.datetime, float]] = field(default_factory=dict)
    # {stat_id: [(utc_start, mean), ...]} sortiert, 5-min
    five: dict[str, list[tuple[dt.datetime, float]]] = field(default_factory=dict)
    entities: dict[str, dict] = field(default_factory=dict)
    # {entity_id: [(utc_ts, state_str)]}
    history_min: dict[str, list[tuple[dt.datetime, str]]] = field(default_factory=dict)
    # {entity_id: [(utc_ts, state_str, attributes)]}
    history_attrs: dict[str, list[tuple[dt.datetime, str, dict]]] = field(
        default_factory=dict
    )
    # {local_date: result-dict}
    issued: dict[dt.date, dict] = field(default_factory=dict)
    diagnostics: dict | None = None

    # abgeleitet
    days: list[dt.date] = field(default_factory=list)  # lokale Tage mit Ist-Daten
    partial_days: set[dt.date] = field(default_factory=set)
    features: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # ---------------- Convenience ----------------
    def hourly_series(self, sid: str) -> dict[dt.datetime, float]:
        return self.hourly.get(sid, {})

    def day_sum_wh(
        self,
        sid: str,
        day: dt.date,
        loc_hours: tuple[int, int] | None = None,
        utc_hours: tuple[int, int] | None = None,
    ) -> float:
        """Summe der Stundenmittel (== Wh) eines lokalen Tages, opt. Fenster."""
        tot = 0.0
        for t, v in self.hourly_series(sid).items():
            tl = t.astimezone(LOC)
            if tl.date() != day:
                continue
            if loc_hours and not (loc_hours[0] <= tl.hour < loc_hours[1]):
                continue
            if utc_hours and not (utc_hours[0] <= t.hour < utc_hours[1]):
                continue
            tot += v
        return tot

    def day_max_w(
        self, sid: str, day: dt.date, loc_hours: tuple[int, int]
    ) -> float:
        best = 0.0
        for t, v in self.hourly_series(sid).items():
            tl = t.astimezone(LOC)
            if tl.date() == day and loc_hours[0] <= tl.hour < loc_hours[1]:
                best = max(best, v)
        return best

    def five_day(
        self, sid: str, day: dt.date, loc_hours: tuple[float, float] | None = None
    ) -> list[tuple[dt.datetime, float]]:
        out = []
        for t, v in self.five.get(sid, []):
            tl = t.astimezone(LOC)
            if tl.date() != day:
                continue
            hh = tl.hour + tl.minute / 60.0
            if loc_hours and not (loc_hours[0] <= hh < loc_hours[1]):
                continue
            out.append((t, v))
        return out

    def full_days(self) -> list[dt.date]:
        return [d for d in self.days if d not in self.partial_days]

    def daily_actual_ac_wh(self, day: dt.date) -> float:
        return self.day_sum_wh(SID_ACT_AC, day)

    def daily_actual_dc_wh(self, day: dt.date) -> float:
        return self.day_sum_wh(SID_ACT_DC, day)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_stats_file(path: str) -> dict[str, list[dict]]:
    raw = _load_json(path)
    stats = raw.get("stats", raw) if isinstance(raw, dict) else {}
    return stats if isinstance(stats, dict) else {}


def _expand_history(rows: list[dict]) -> tuple[str, list[tuple[dt.datetime, str, dict]]]:
    """HA history/period-Liste (voll ODER minimal_response) expandieren."""
    if not rows:
        return "", []
    eid = rows[0].get("entity_id", "")
    out = []
    for e in rows:
        state = e.get("state", e.get("s"))
        ts = e.get("last_changed", e.get("lc")) or e.get("last_updated", e.get("lu"))
        if state is None or ts is None:
            continue
        out.append((parse_iso(ts), str(state), e.get("attributes") or {}))
    out.sort(key=lambda x: x[0])
    return eid, out


def _dig_issued_result(obj: Any) -> dict | None:
    """{'result': {...}} / direkte Response / entry-gekeyt -> Result-Dict."""
    if not isinstance(obj, dict):
        return None
    if "hourly_wh" in obj or "available" in obj:
        return obj
    for key in ("result", "service_response", "response"):
        if key in obj:
            r = _dig_issued_result(obj[key])
            if r is not None:
                return r
    # entry-gekeyte Response: eine Ebene tiefer suchen
    for v in obj.values():
        if isinstance(v, dict) and ("hourly_wh" in v or "available" in v):
            return v
    return None


def load_bundle(data_dir: str) -> Bundle:
    b = Bundle(data_dir=data_dir)

    def _path(name: str) -> str | None:
        p = os.path.join(data_dir, name)
        return p if os.path.isfile(p) else None

    # --- hourly stats ---
    p = _path(FILE_HOURLY)
    if p:
        for sid, rows in _load_stats_file(p).items():
            ser = {}
            for r in rows:
                if r.get("mean") is None or r.get("start") is None:
                    continue
                ser[parse_epoch(r["start"])] = float(r["mean"])
            if ser:
                b.hourly[sid] = ser
    else:
        b.notes.append(f"FEHLT: {FILE_HOURLY} - Stunden-Checks werden uebersprungen.")

    # --- 5minute stats ---
    p = _path(FILE_5MIN)
    if p:
        for sid, rows in _load_stats_file(p).items():
            ser = [
                (parse_epoch(r["start"]), float(r["mean"]))
                for r in rows
                if r.get("mean") is not None and r.get("start") is not None
            ]
            ser.sort(key=lambda x: x[0])
            if ser:
                b.five[sid] = ser
    else:
        b.notes.append(f"FEHLT: {FILE_5MIN} - 5-min-Checks werden uebersprungen.")

    # --- entities ---
    p = _path(FILE_ENTITIES)
    if p:
        raw = _load_json(p)
        if isinstance(raw, dict):
            b.entities = raw
    else:
        b.notes.append(f"FEHLT: {FILE_ENTITIES} - Band-/Bias-Snapshot-Checks entfallen.")

    # --- history ---
    p = _path(FILE_HISTORY)
    if p:
        raw = _load_json(p)
        for rows in raw.get("minimal", []) or []:
            eid, ex = _expand_history(rows)
            if eid:
                b.history_min[eid] = [(t, s) for t, s, _a in ex]
        for rows in raw.get("with_attrs", []) or []:
            eid, ex = _expand_history(rows)
            if eid:
                b.history_attrs[eid] = ex
    else:
        b.notes.append(f"FEHLT: {FILE_HISTORY} - Verlaufs-Checks (Jojo, p10) entfallen.")

    # --- issued + diagnostics ---
    p = _path(FILE_ISSUED)
    if p:
        raw = _load_json(p)
        for dstr, obj in (raw.get("issued") or {}).items():
            r = _dig_issued_result(obj)
            if r is None:
                continue
            try:
                day = dt.date.fromisoformat(str(r.get("date") or dstr))
            except ValueError:
                continue
            b.issued[day] = r
        b.diagnostics = raw.get("diagnostics")
    else:
        b.notes.append(f"FEHLT: {FILE_ISSUED} - issued-Checks (C3/C7/C8c) entfallen.")

    _derive(b)
    return b


def _derive(b: Bundle) -> None:
    # lokale Tage + partial-Erkennung ueber Ist-AC-Stundenreihe
    ref = b.hourly_series(SID_ACT_AC) or b.hourly_series(SID_ACT_DC)
    days = sorted({t.astimezone(LOC).date() for t in ref})
    b.days = days
    for day in days:
        # Tageslicht-Slots 04:00Z..18:00Z (15 Stueck) vorhanden?
        n = sum(
            1
            for t in ref
            if t.astimezone(LOC).date() == day and 4 <= t.hour < 19
        )
        if n < 14:
            b.partial_days.add(day)

    # Feature-Erkennung (v0.21.0-Felder)
    feats: dict[str, Any] = {}
    any_issued = next(iter(b.issued.values()), {})
    feats["issued_has_hourly_wh_ac"] = "hourly_wh_ac" in any_issued
    feats["issued_has_cloud_class"] = "cloud_class_by_hour" in any_issued
    cells = (
        (b.entities.get(EID_BIAS) or {}).get("attributes", {}).get("bias_cells") or {}
    )
    feats["bias_has_clamped_flag"] = any(
        isinstance(c, dict) and "clamped" in c for c in cells.values()
    )
    ver = None
    if b.diagnostics:
        ver = (b.diagnostics.get("integration_manifest") or {}).get("version")
        if not ver:
            ver = (
                (b.diagnostics.get("custom_components") or {})
                .get("balcony_solar_forecast", {})
                .get("version")
            )
    feats["integration_version"] = ver
    qd = None
    if b.diagnostics:
        qd = ((b.diagnostics.get("data") or {}).get("quantiles") or {}).get("bins")
    feats["quantile_bins_available"] = qd is not None
    b.features = feats

    if not feats["issued_has_hourly_wh_ac"]:
        b.notes.append(
            "issued liefert KEIN hourly_wh_ac (Integration < v0.21.0?) - "
            "AC-Vergleiche nutzen hourly_wh (DC) x eta."
        )
    if not feats["issued_has_cloud_class"]:
        b.notes.append(
            "issued liefert KEIN cloud_class_by_hour - 'klare Morgen' werden "
            "heuristisch aus den Ist-Daten bestimmt."
        )
    if not feats["bias_has_clamped_flag"]:
        b.notes.append(
            "bias_cells ohne 'clamped'-Flag (Integration < v0.21.0?) - "
            "Clamp-Naehe wird ersatzweise aus theta abgeleitet."
        )
