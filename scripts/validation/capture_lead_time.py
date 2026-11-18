"""Freeze prospective HA issues and closed DC observations for replay_benchmark."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .bsf_data import parse_epoch
from .bsf_fetch import DOMAIN, MiniWS, Rest
from .replay_benchmark import issue_period, number, timestamp

HOUR = timedelta(hours=1)
FIVE_MIN = timedelta(minutes=5)
PHASES = ("previous_evening", "early_morning", "intraday")


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> tuple[dict, str]:
    data = path.read_bytes()
    return json.loads(data), _sha(data)


def _write(path: Path, value: dict) -> str:
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(data)
    return _sha(data)


def load_plan(path: Path) -> tuple[dict, str]:
    plan, digest = _load(path)
    tz = ZoneInfo(plan["timezone"])
    if date.fromisoformat(plan["development_end"]) >= date.fromisoformat(plan["selection_end"]):
        raise ValueError("Chronological split dates must be ordered")
    targets = [timestamp(start) for start in plan["targets"]]
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("Plan requires unique target starts")
    if any(start != start.replace(minute=0, second=0, microsecond=0) for start in targets):
        raise ValueError("Targets start on the UTC hour")
    if any(start.astimezone(tz).date() != (start + HOUR - timedelta(microseconds=1)).astimezone(tz).date()
           for start in targets):
        raise ValueError("A target hour may not cross the local day boundary")
    sources = plan["dc_sources"]
    if (not isinstance(sources, dict) or not sources
            or any(not name or not isinstance(source, str) or not source for name, source in sources.items())
            or len(set(sources.values())) != len(sources)):
        raise ValueError("Map each stable module name to its own DC measurement entity")
    if any(not isinstance(plan[key], str) or not plan[key].strip() for key in ("entry_id", "licence")):
        raise ValueError("Plan requires a config entry and data licence")
    return plan, digest


def _binding_sha256(plan: dict) -> str:
    binding = {"entry_id": plan["entry_id"], "dc_sources": plan["dc_sources"]}
    return _sha(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode())


def _verify_bindings(plan: dict, entry: dict) -> str:
    expected = _binding_sha256(plan)
    if entry.get("dc_bindings_sha256") != expected:
        raise ValueError("DC sensor bindings differ from the selected HA config entry")
    return expected


def _day_targets(plan: dict, day: str) -> list[datetime]:
    tz = ZoneInfo(plan["timezone"])
    local_day = date.fromisoformat(day)
    if local_day.isoformat() != day:
        raise ValueError("Target day must use YYYY-MM-DD")
    return sorted(
        start for stamp in plan["targets"]
        if (start := timestamp(stamp)).astimezone(tz).date() == local_day
    )


def _within_phase(at: datetime, start: datetime, phase: str, tz: ZoneInfo) -> bool:
    if at >= start or issue_period(at, start, tz) != phase:
        return False
    return phase != "intraday" or start - at <= timedelta(minutes=30)


def _hour_curve(value: dict) -> dict[datetime, float]:
    if not isinstance(value, dict):
        raise ValueError("Forecast hourly curve must be an object")
    curve = {timestamp(at): watts for at, watts in value.items()}
    if len(curve) != len(value):
        raise ValueError("Duplicate forecast timestamp")
    return curve


def forecast_capture(
    plan: dict, plan_sha256: str, *, day: str, phase: str,
    target: str | None, entry: dict, captured_at: datetime, reference: dict | None = None,
) -> dict:
    """Reduce one snapshot to its predeclared future targets."""
    bindings = _verify_bindings(plan, entry)
    if entry.get("forecast_dc_bindings_sha256") != bindings:
        raise ValueError("The latest forecast does not match the current DC sensor bindings")
    starts = _day_targets(plan, day)
    if phase == "intraday":
        if target is None or timestamp(target) not in starts:
            raise ValueError("Intraday requires one predeclared --target timestamp")
        starts = [timestamp(target)]
    elif target is not None:
        raise ValueError("--target applies only to intraday")
    if not starts:
        raise ValueError("No planned target on this local date")
    tz = ZoneInfo(plan["timezone"])
    published = timestamp(entry["published_at"])
    computed = timestamp(entry["issued_at"])  # get_forecast's historical field name
    if not computed <= published <= captured_at:
        raise ValueError("Forecast compute/publication/capture timestamps are inconsistent")
    if any(not _within_phase(at, start, phase, tz)
           for at in (published, captured_at) for start in starts):
        raise ValueError("Forecast publication and capture must be in the planned phase before all targets")

    raw = _hour_curve(entry["raw_hourly_wh"])
    served = _hour_curve(entry["total_hourly"])
    available_slots = (
        {timestamp(stamp) for stamp in entry["slot_starts"]}
        - {timestamp(stamp) for stamp in entry["invalid_weather_slots"]}
    )
    night = {timestamp(stamp) for stamp in entry["night_zero_slots"]}
    evidence = {timestamp(stamp): state for stamp, state in entry.get("band_evidence_by_slot", {}).items()}
    reference_rows = _reference_rows(plan, plan_sha256, day, phase, published, reference)
    planes = set(entry["planes"])
    if not planes or set(plan["dc_sources"]) - planes:
        raise ValueError("Configured DC sources do not match the forecast modules")
    unmetered = sorted(planes - plan["dc_sources"].keys())
    bands = {key: _hour_curve(entry.get(key, {}).get("wh_period", {}))
             for key in ("p10", "p50", "p90")}
    records = []
    for start in starts:
        slots = [start + timedelta(minutes=15 * i) for i in range(4)]
        complete = available_slots.issuperset(slots)
        night_zero = complete and night.issuperset(slots)
        record = {
            "target_start": start.isoformat(),
            "forecasts": {key: number(curve.get(start, 0.0 if night_zero else None)) if complete else None
                          for key, curve in (("raw", raw), ("served", served))},
        }
        if start in reference_rows:
            record["references"] = {"previous_day": reference_rows[start]}
        if record["forecasts"]["served"] is not None:
            totals = []
            for curve in bands.values():
                values = [number(curve.get(slot)) for slot in slots]
                if all(value is not None for value in values):
                    totals.append(sum(values))
            if len(totals) == len(bands) and totals == sorted(totals):
                record["intervals"] = {"served": totals}
                states = {evidence.get(slot, "unknown") for slot in slots}
                known = {"trained", "neutral", "ensemble", "mixed"}
                state = (next(iter(states)) if len(states) == 1 else "mixed") if states <= known else "unknown"
                record["regime"] = {"band_learning_state": state}
        records.append(record)
    return {
        "kind": "forecast", "phase": phase, "day": day, "plan_sha256": plan_sha256,
        "computed_at": computed.isoformat(), "published_at": published.isoformat(),
        "captured_at": captured_at.isoformat(), "unmetered_modules": unmetered, "records": records,
        "dc_bindings_sha256": bindings,
    }


def _hourly_measurements(plan: dict, starts: list[datetime], stats: dict) -> list[dict]:
    series = {}
    for module, entity in plan["dc_sources"].items():
        values = {}
        rows = stats.get(entity)
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict):
                continue
            stamp = row.get("start")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                continue
            try:
                at = parse_epoch(stamp)
                value = number(row.get("mean"))
            except (OverflowError, ValueError, OSError):
                continue
            # A duplicate bucket cannot attest a single measurement.
            values[at] = value if at not in values else None
        series[module] = values

    records = []
    for start in starts:
        required = [start + FIVE_MIN * i for i in range(12)]
        missing, complete = [], []
        for module, values in series.items():
            means = [values.get(at) for at in required]
            if any(mean is None for mean in means):
                missing.append(module)
            else:
                complete.append(sum(means) / len(required))
        records.append({"target_start": start.isoformat(), "missing_modules": missing,
                        "actual_wh": None if missing else sum(complete)})
    return records


def actual_capture(plan: dict, plan_sha256: str, day: str, stats: dict, *,
                   captured_at: datetime, dc_bindings_sha256: str) -> dict:
    starts = _day_targets(plan, day)
    if not starts or starts[-1] + HOUR > captured_at:
        raise ValueError("All measurement targets must have closed before capture")
    if dc_bindings_sha256 != _binding_sha256(plan):
        raise ValueError("Measurement bindings differ from the frozen plan")
    return {"kind": "actual", "day": day, "plan_sha256": plan_sha256,
            "captured_at": captured_at.isoformat(), "dc_bindings_sha256": dc_bindings_sha256,
            "records": _hourly_measurements(plan, starts, stats)}


def _previous_hour(target: datetime, tz: ZoneInfo) -> datetime | None:
    """Match the previous local day's wall-clock hour only where unambiguous."""
    local = target.astimezone(tz).replace(tzinfo=None) - timedelta(days=1)
    candidates = {local.replace(tzinfo=tz, fold=fold).astimezone(UTC) for fold in (0, 1)}
    candidates = {start for start in candidates if start.astimezone(tz).replace(tzinfo=None) == local}
    if len(candidates) != 1:
        return None
    start = candidates.pop()
    end_local = (start + HOUR - timedelta(microseconds=1)).astimezone(tz)
    if start.minute or end_local.date() != local.date():
        return None
    return start


def _closed_reference_targets(plan: dict, day: str, at: datetime) -> dict[datetime, datetime]:
    tz = ZoneInfo(plan["timezone"])
    return {
        start: previous
        for start in _day_targets(plan, day)
        if (previous := _previous_hour(start, tz)) is not None and previous + HOUR <= at
    }


def reference_capture(plan: dict, plan_sha256: str, day: str, phase: str,
                      sources: dict[datetime, datetime], stats: dict, *,
                      captured_at: datetime, dc_bindings_sha256: str) -> dict:
    if dc_bindings_sha256 != _binding_sha256(plan):
        raise ValueError("Reference bindings differ from the frozen plan")
    closed = _closed_reference_targets(plan, day, captured_at)
    if not sources or any(closed.get(start) != previous for start, previous in sources.items()):
        raise ValueError("Previous-day measurements must match closed planned target hours")
    measurements = {timestamp(row["target_start"]): row["actual_wh"] for row in
                    _hourly_measurements(plan, sorted(set(sources.values())), stats)}
    records = [{
        "target_start": start.isoformat(), "wh": measurements[previous],
        "source_start": previous.isoformat(),
    } for start, previous in sorted(sources.items())]
    return {"kind": "reference", "day": day, "phase": phase, "plan_sha256": plan_sha256,
            "captured_at": captured_at.isoformat(), "dc_bindings_sha256": dc_bindings_sha256,
            "records": records}


def _reference_rows(plan, plan_sha256, day, phase, published, reference):
    if reference is None:
        return {}
    if (reference.get("kind"), reference.get("day"), reference.get("phase"),
            reference.get("plan_sha256"), reference.get("dc_bindings_sha256")) != (
            "reference", day, phase, plan_sha256, _binding_sha256(plan)):
        raise ValueError("Reference file disagrees with this capture plan, day, phase or binding")
    available = timestamp(reference["captured_at"])
    if available > published:
        return {}  # the current HA publication predates this independent acquisition
    closed = _closed_reference_targets(plan, day, available)
    results, seen = {}, set()
    for row in reference["records"]:
        start, previous = timestamp(row["target_start"]), timestamp(row["source_start"])
        wh = number(row["wh"])
        if closed.get(start) != previous or start in seen:
            raise ValueError("Invalid or duplicate frozen previous-day target")
        seen.add(start)
        if wh is not None:
            results[start] = {"wh": wh, "target_start": previous.isoformat(),
                              "target_end": (previous + HOUR).isoformat(),
                              "available_at": available.isoformat()}
    return results


def _capture_path(folder: Path, snapshot: dict) -> Path:
    if snapshot["kind"] == "reference":
        return folder / f"{snapshot['day']}-reference-{snapshot['phase']}.json"
    stem = f"{snapshot['day']}-{snapshot.get('phase', snapshot['kind'])}"
    if snapshot.get("phase") == "intraday":
        start = timestamp(snapshot["records"][0]["target_start"])
        stem += start.strftime("-%H%MZ")
    return folder / f"{stem}.json"


def _external_weather(path: Path | None, targets: set[datetime]) -> tuple[dict, dict]:
    if path is None:
        return {}, {}
    data, digest = _load(path)
    source = data["source"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Report the independent weather observation source")
    by_start = {}
    for row in data["records"]:
        start = timestamp(row["target_start"])
        label = row["observed_weather"]
        if (start in by_start or start not in targets or label not in ("clear", "mixed", "overcast")
                or timestamp(row["available_at"]) < start + HOUR):
            raise ValueError("Invalid, duplicated or unclosed weather observation")
        by_start[start] = label
    return by_start, {"source": source, "sha256": digest}


def export_bundle(plan: dict, plan_sha256: str, folder: Path, output: Path,
                  observations: Path | None = None) -> Path:
    targets = {timestamp(value) for value in plan["targets"]}
    by_start, weather_source = _external_weather(observations, targets)
    hashes, actuals, forecasts = {}, {}, {}
    for path in sorted(folder.glob("*.json")):
        snapshot, digest = _load(path)
        if (snapshot["plan_sha256"] != plan_sha256 or _capture_path(folder, snapshot) != path
                or snapshot.get("dc_bindings_sha256") != _binding_sha256(plan)):
            raise ValueError(f"Capture plan or filename mismatch in {path.name}")
        hashes[path.name] = digest
        if snapshot["kind"] == "reference":
            continue  # reference values are bound and frozen in each issue
        captured = timestamp(snapshot["captured_at"])
        day_targets = set(_day_targets(plan, snapshot["day"]))
        starts = [timestamp(row["target_start"]) for row in snapshot["records"]]
        is_intraday = snapshot.get("phase") == "intraday"
        if (is_intraday and (len(starts) != 1 or starts[0] not in day_targets)
                or not is_intraday and (set(starts) != day_targets or len(starts) != len(day_targets))):
            raise ValueError("Capture target axis differs from plan")
        for row, start in zip(snapshot["records"], starts, strict=True):
            if snapshot["kind"] == "actual" and start not in actuals and captured >= start + HOUR:
                actuals[start] = (row, captured)
            elif snapshot["kind"] == "forecast":
                phase = snapshot["phase"]
                published = timestamp(snapshot["published_at"])
                if ((phase, start) in forecasts or phase not in PHASES
                        or not timestamp(snapshot["computed_at"]) <= published <= captured
                        or any(not _within_phase(at, start, phase, ZoneInfo(plan["timezone"]))
                               for at in (published, captured))):
                    raise ValueError("Forecast targets or acquisition clocks overlap or violate the plan")
                forecasts[phase, start] = (row, snapshot)
            else:
                raise ValueError("Repeated or unclosed measurement target")

    if not forecasts:
        raise ValueError("No frozen forecasts to export")
    records = []
    missing_actual = set()
    for (_, start), (forecast, snapshot) in sorted(forecasts.items(), key=lambda item: (item[0][1], item[0][0])):
        if start not in actuals:
            missing_actual.add(start)
            continue
        actual, observed = actuals[start]
        missing = sorted(set(actual["missing_modules"]) | set(snapshot["unmetered_modules"]))
        record = {
            **forecast,
            "target_end": (start + HOUR).isoformat(),
            "issued_at": snapshot["published_at"], "available_at": snapshot["published_at"],
            "forecast_captured_at": snapshot["captured_at"],
            "actual_available_at": observed.isoformat(),
            "actual_wh": None if missing else actual["actual_wh"],
            "quality": "missing_channels" if missing else "valid",
            "missing_modules": missing,
        }
        if start in by_start:
            record.setdefault("regime", {})["observed_weather"] = by_start[start]
        records.append(record)
    if missing_actual:
        raise ValueError("Capture closed measurements for every frozen target before export")
    missing_forecasts = {
        phase: [start.isoformat() for start in sorted(targets) if (phase, start) not in forecasts]
        for phase in PHASES
    }
    bundle = {"schema_version": 1, "basis": "DC", "timezone": plan["timezone"],
              "models": ["raw", "served"], "records": records,
              "missing_forecast_captures": missing_forecasts,
              "development_end": plan["development_end"], "selection_end": plan["selection_end"]}
    output.mkdir()
    digest = _write(output / "bundle.json", bundle)
    manifest = {"basis": "DC", "timezone": plan["timezone"], "licence": plan["licence"],
                "input": {"path": "bundle.json", "sha256": digest}, "captured_at": _now().isoformat(),
                "source_role": "prospective_ha_forecast_and_closed_recorder_dc",
                "plan_sha256": plan_sha256, "captures_sha256": hashes,
                "dc_bindings_sha256": _binding_sha256(plan)}
    if weather_source:
        manifest["observed_weather"] = weather_source
    _write(output / "manifest.json", manifest)
    return output / "manifest.json"


def _fetch_entry(rest: Rest, plan: dict) -> dict:
    response = rest.post(f"/api/services/{DOMAIN}/get_forecast?return_response",
                         {"entry_id": plan["entry_id"], "include_capture_metadata": True})
    return response["service_response"]["entries"][plan["entry_id"]]


def _recorder_power_units(rows: object, sources: set[str]) -> dict[str, str]:
    """Only use statistics whose class and stored unit support W conversion."""
    units: dict[str, str] = {}
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        source = row.get("statistic_id")
        if not isinstance(source, str) or source not in sources:
            continue
        unit = row.get("statistics_unit_of_measurement")
        if source not in seen and row.get("unit_class") == "power" and unit in ("W", "kW"):
            units[source] = unit
        else:
            units.pop(source, None)
        seen.add(source)
    return units


def _fetch_measurements(rest: Rest, ha_url: str, token: str, plan: dict,
                        starts: list[datetime]) -> tuple[dict, datetime, str]:
    if not starts or starts[-1] + HOUR > _now():
        raise ValueError("Capture recorder data only after the requested targets have closed")
    before = _verify_bindings(plan, _fetch_entry(rest, plan))
    sources = set(plan["dc_sources"].values())
    metadata = {"type": "recorder/get_statistics_metadata", "statistic_ids": sorted(sources)}
    ws = MiniWS(ha_url, token)
    ws.connect()
    try:
        before_units = _recorder_power_units(ws.call(metadata), sources)
        stats = {}
        if before_units:
            rows = ws.call({"type": "recorder/statistics_during_period", "period": "5minute",
                            "start_time": starts[0].isoformat(), "end_time": (starts[-1] + HOUR).isoformat(),
                            "statistic_ids": sorted(before_units),
                            "types": ["mean"], "units": {"power": "W"}})
            if not isinstance(rows, dict):
                raise ValueError("Recorder statistics must report a per-channel mapping")
            # Recorder applies the requested unit only to convertible statistics.
            # Treat missing, incompatible or changed metadata as a channel gap.
            after_units = _recorder_power_units(ws.call(metadata), sources)
            stats = {source: rows[source] for source, unit in before_units.items()
                     if source in rows and after_units.get(source) == unit}
    finally:
        ws.close()
    _verify_bindings(plan, _fetch_entry(rest, plan))
    return stats, _now(), before


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("captures", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("day", help="target day in the plan timezone")
    issue.add_argument("phase", choices=PHASES)
    issue.add_argument("--target", help="one planned timestamp for intraday")
    actual = sub.add_parser("actual")
    actual.add_argument("day")
    reference = sub.add_parser("reference")
    reference.add_argument("day")
    reference.add_argument("phase", choices=PHASES)
    export = sub.add_parser("export")
    export.add_argument("output", type=Path)
    export.add_argument("--observations", type=Path)
    args = parser.parse_args()
    plan, digest = load_plan(args.plan)
    if args.command == "export":
        print(export_bundle(plan, digest, args.captures, args.output, args.observations))
        return
    ha_url, token = os.environ["BSF_HA_URL"], os.environ["BSF_HA_TOKEN"]
    rest = Rest(ha_url, token)
    if rest.get("/api/config")["time_zone"] != plan["timezone"]:
        parser.error("HA timezone differs from the fixed plan timezone")
    if args.command == "issue":
        if not _day_targets(plan, args.day):
            parser.error("No planned target on this local date")
        ref_path = _capture_path(args.captures, {"kind": "reference", "day": args.day, "phase": args.phase})
        prior = _load(ref_path)[0] if ref_path.is_file() else None
        entry = _fetch_entry(rest, plan)
        captured_at = _now()
        snapshot = forecast_capture(plan, digest, day=args.day, phase=args.phase,
                                    target=args.target, entry=entry, captured_at=captured_at,
                                    reference=prior)
    elif args.command == "reference":
        sources = _closed_reference_targets(plan, args.day, _now())
        stats, captured_at, bindings = _fetch_measurements(
            rest, ha_url, token, plan, sorted(set(sources.values())),
        )
        snapshot = reference_capture(plan, digest, args.day, args.phase, sources, stats,
                                     captured_at=captured_at, dc_bindings_sha256=bindings)
    else:
        stats, captured_at, bindings = _fetch_measurements(
            rest, ha_url, token, plan, _day_targets(plan, args.day),
        )
        snapshot = actual_capture(plan, digest, args.day, stats, captured_at=captured_at,
                                  dc_bindings_sha256=bindings)
    args.captures.mkdir(parents=True, exist_ok=True)
    path = _capture_path(args.captures, snapshot)
    _write(path, snapshot)
    print(path)


if __name__ == "__main__":
    main()
