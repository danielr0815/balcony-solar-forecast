"""Causal day-ahead physical learner ablation from original 15-minute weather.

Stationary site/config, independent bias/shademap/quantile states, original
variant-specific references. No HA, network, store writes or automatic activation.
The transient intraday layer, drift auto-disable and calibrated eta are excluded
explicitly; this is a persisted-learning experiment, not a full HA simulator.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .model_contract_impact import kernel
from .replay_benchmark import (
    compare,
    load_verified,
    local_day_bounds,
    number,
    timestamp,
    validate_output_paths,
)


@cache
def _core():
    types, engine, _horizon, digest = kernel(Path(__file__).resolve().parents[2])
    names = ('bias', 'shademap', 'quantiles', 'solpos', 'learning_inputs', 'daylight', 'measurement_quality')
    modules = {name: importlib.import_module(types.__package__+'.'+name) for name in names}
    package = types.__package__.rsplit('.', 1)[0]
    return SimpleNamespace(types=types, engine=engine, digest=digest, **modules,
                           const=importlib.import_module(package+'.const'),
                           validate_site=importlib.import_module(package+'._site_validation').validate_site)


PRESETS = {'raw': (False, False, False), 'bias': (False, True, False),
           'slow': (True, False, False), 'full': (True, True, True)}


def _classes(core, site, weather, tz):
    out = {}
    for slot in weather.slots:
        _az, elevation = core.solpos.sun_position(slot.start, site.latitude, site.longitude)
        out[slot.start] = core.bias.classify_cloud(
            cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid, cloud_high=slot.cloud_high,
            visibility_m=slot.visibility_m, month=slot.start.astimezone(tz).month,
            ghi=slot.ghi, elevation_deg=elevation)
    return out


def _predict(core, site, weather, issue, tz, state, flags):
    slow, bias, quantile = flags
    groups = {p.name: tuple(q.name for q in site.planes if q.shade_group == p.shade_group)
              + ((p.shade_group,) if p.shade_group in state['shade'].channels else ())
              if p.shade_group else (p.name,) for p in site.planes}
    def beam(channel, az, el, doy, prior):
        return core.shademap.effective_tau_pooled(state['shade'], channels=groups[channel],
                                                 sun_az=az, sun_el=el, doy=doy, static_prior=prior)
    classes = _classes(core, site, weather, tz)
    factors = {slot.start: core.bias.day_ahead_factor_solar(state['bias'], cloud_class=classes[slot.start],
                hours_from_noon=core.solpos.hours_from_solar_noon(slot.start, site.longitude)) for slot in weather.slots}
    bands = {}
    trained_by_hour = Counter()
    if quantile:
        for slot in weather.slots:
            band = core.quantiles.bands_for_bin(
                state['quantile'], cloud_class=classes[slot.start],
                day_part=core.bias.day_part_for_solar(
                    core.solpos.hours_from_solar_noon(slot.start, site.longitude)),
                as_of_date=issue.astimezone(tz).date().isoformat(),
            )
            if band.n:
                hour = slot.start.replace(minute=0, second=0, microsecond=0).isoformat()
                trained_by_hour[hour] += 1
            if (band.p10, band.p50, band.p90) != (1.0, 1.0, 1.0):
                bands[slot.start] = band
    hooks = core.engine.LearnerHooks(beam_tau=beam if slow else None,
                                    slot_factor=lambda t: factors[t] if bias else 1,
                                    band_by_slot=bands)
    result = core.engine.compute_forecast(site, weather, issue, tz=tz, hooks=hooks)
    if any(number(value) is None for curve in (result.hourly_wh, result.raw_hourly_wh, result.ac_hourly_wh) for value in curve.values()):
        raise ValueError('Physical model emitted invalid energy')
    iso = weather.slots[0].start.astimezone(tz).date().isoformat()
    per_plane = core.learning_inputs.per_plane_modeled(site, result, iso, tz)
    cloud = {}
    for slot in weather.slots:
        cloud.setdefault(slot.start.replace(minute=0).isoformat(), classes[slot.start])
    slow_curve = {}
    plane_watts = zip(*(plane.slow_watts for plane in result.plane_results), strict=True)
    for start, watts in zip(result.slot_starts, plane_watts, strict=True):
        hour = start.replace(minute=0, second=0, microsecond=0).isoformat()
        slow_curve[hour] = slow_curve.get(hour, 0.0) + sum(watts) * core.const.SLOT_HOURS
    # The engine omits zero-radiation night slots. This input axis was verified
    # complete and finite, so those omissions are confirmed zeros, not gaps.
    hours = {slot.start.replace(minute=0, second=0, microsecond=0).isoformat() for slot in weather.slots}
    def complete(curve):
        return {hour:curve.get(hour,0.0) for hour in sorted(hours)}
    slot_bands = {
        f'dc_{name}_slot_wh': {
            start.isoformat(): watts * core.const.SLOT_HOURS
            for start, watts in zip(result.slot_starts, values, strict=True)
        }
        for name, values in (('p10', result.p10_watts), ('p50', result.p50_watts),
                             ('p90', result.p90_watts))
        if values
    }
    snapshot = core.types.IssuedSnapshot(issued_at=issue.isoformat(), status='offline_recomputed',
        raw_hourly_wh=complete(result.raw_hourly_wh), corrected_hourly_wh=complete(result.hourly_wh),
        slow_only_hourly_wh=complete(slow_curve), per_plane=per_plane, cloud_class_by_hour=cloud,
        corrected_ac_hourly_wh=complete(result.ac_hourly_wh), computed_at=issue.isoformat(),
        bands=slot_bands or None)
    # These emitted hourly energy bounds sum the corresponding 15-minute
    # DC bounds. The replay pairs them with closed hourly recorder values.
    hourly_bands = {name: curve for name, curve in (
        ('p10', result.p10_hourly_wh), ('p50', result.p50_hourly_wh),
        ('p90', result.p90_hourly_wh),
    ) if curve}
    band_status = {
        hour: ('trained' if trained_by_hour[hour] == 4 else
               'mixed' if trained_by_hour[hour] else 'neutral')
        for hour in result.p10_hourly_wh
    }
    return snapshot, hourly_bands, band_status


def _labels(core, site, row, start, end):
    metered = [p.name for p in site.planes if p.actual_entity]
    hour = timedelta(hours=1)
    expected = {
        key for key in core.daylight.daylight_hour_keys(site.latitude, site.longitude, start, end)
        if timestamp(key) + hour <= end
    }
    needed = math.ceil(len(expected)*core.const.DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE)
    actuals = {}
    ratings = {p.name:p.wp for p in site.planes}
    for name in metered:
        raw = row.get('actuals_hourly', {}).get(name, {})
        if not isinstance(raw, dict):
            return None, 'invalid_labels'
        hours = {}
        for key, value in raw.items():
            at = timestamp(key)
            if not (start <= at and at + hour <= end):
                continue
            value = number(value)
            if value is not None and value > core.const.CHANNEL_PLAUSIBILITY_MAX_WP_FRAC*ratings[name]:
                return None, 'implausible_labels'
            if value is None or at.minute or at.second or at.microsecond:
                return None, 'invalid_labels'
            canonical = at.isoformat()
            if canonical in hours:
                return None, 'duplicate_labels'
            hours[canonical] = value
        if len(expected & set(hours)) < needed:
            return None, 'missing_daylight_labels'
        if core.measurement_quality.frozen_nonzero([hours[key] for key in sorted(hours)]):
            return None, 'frozen_labels'
        actuals[name] = hours
    if not metered:
        return None, 'no_measured_channels'
    if len(expected.intersection(*actuals.values())) < needed:
        return None, 'missing_common_daylight_labels'
    return actuals, 'valid'


def _train(core, site, snapshot, actuals, state, flags, iso):
    common = set.intersection(*(set(hours) for hours in actuals.values()))
    measured = {h: sum(hours[h] for hours in actuals.values()) for h in common}
    def day_part(h):
        return core.bias.day_part_for_solar(core.solpos.hours_from_solar_noon(timestamp(h), site.longitude))
    slow, bias, quantile = flags
    if bias:
        samples = core.learning_inputs.day_ahead_samples(site, snapshot.slow_only_hourly_wh,
            {p: sum(h.values()) for p, h in actuals.items()}, snapshot, measured, day_part=day_part)
        state['bias'] = core.bias.train_day_ahead_bias(state['bias'], samples)
    if slow:
        modeled = core.learning_inputs.metered_modeled_hourly(site, snapshot, snapshot.raw_hourly_wh)
        if modeled and sum(measured.values()) >= core.const.SHADEMAP_MEASURED_CLEAR_MIN_FRAC*sum(modeled.values()):
            for name, hours in actuals.items():
                if name in snapshot.per_plane:
                    state['shade'], _changed = core.learning_inputs.train_channel(site, state['shade'], name, snapshot.per_plane[name], hours)
    if quantile:
        modeled = core.learning_inputs.metered_modeled_hourly(site, snapshot, snapshot.corrected_hourly_wh, layer='corrected')
        samples = [core.quantiles.QuantileSample(cloud_class=snapshot.cloud_class_by_hour.get(h, 'clear'),
                    day_part=day_part(h), measured_wh=measured[h], corrected_wh=wh)
                   for h, wh in (modeled or {}).items() if h in measured]
        state['quantile'] = core.quantiles.train_quantiles(state['quantile'], samples, training_date=iso)


def _weather(core, row, start, end, issue):
    if row.get('weather_role') != 'issued_forecast' or timestamp(row['weather_available_at']) > issue:
        raise ValueError('Variant weather must be an originally available forecast')
    weather_rows = row['weather_slots']
    expected = [start+timedelta(minutes=15*i) for i in range(int((end-start).total_seconds()/900))]
    if [timestamp(w['start']) for w in weather_rows] != expected:
        raise ValueError('Weather must cover the full unique ordered local-day slot axis')
    slots = []
    for weather in weather_rows:
        values = {key: weather.get(key, default) for key, default in [('ghi',None),('dni',None),('dhi',None),('temp_c',None),
            ('cloud_low',0),('cloud_mid',0),('cloud_high',0),('visibility_m',None),('snow_depth_m',0)]}
        for key, value in values.items():
            if key == 'visibility_m' and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or key != 'temp_c' and value < 0:
                raise ValueError('Weather needs finite numeric irradiance/temperature')
        slots.append(core.types.WeatherSlot(timestamp(weather['start']), **values))
    return core.types.WeatherSeries(tuple(slots))


def replay_physical_days(bundle: dict) -> dict:
    """Compare independent persisted learners using frozen original forecast weather.

    Complete UTC slot axes cover local 23/24/25-hour days. Labels become visible
    only at their recorder availability time. An issue never borrows another
    variant's references. Partial or damaged days cannot become training evidence.
    """
    if bundle.get('schema_version') != 1 or bundle.get('basis') != 'DC' or bundle.get('actual_unit', 'Wh') != 'Wh':
        raise ValueError('Physical learning replay requires schema 1 and explicit DC basis')
    core = _core()
    tz, site = ZoneInfo(bundle['timezone']), core.validate_site(deepcopy(bundle['site']))
    requested = bundle.get('variants', list(PRESETS))
    if isinstance(requested, list):
        if not requested or len(set(requested)) != len(requested) or any(name not in PRESETS for name in requested):
            raise ValueError('Choose unique supported learner variants')
        definitions = {name: {'preset': name} for name in requested}
    elif isinstance(requested, dict) and requested:
        definitions = requested
    else:
        raise ValueError('Choose unique supported learner variants')
    presets = list(definitions)
    sites, flags, states, origins = {}, {}, {}, {}
    measured_identity = {(p.name, p.actual_entity) for p in site.planes if p.actual_entity}
    first_issue = min((timestamp(row['issued_at']) for row in bundle['days']), default=None)
    for name, definition in definitions.items():
        if not isinstance(definition, dict) or definition.get('preset') not in PRESETS:
            raise ValueError('Choose a supported preset for each variant')
        sites[name] = core.validate_site(deepcopy(definition.get('site', bundle['site'])))
        identity = {(p.name, p.actual_entity) for p in sites[name].planes if p.actual_entity}
        if identity != measured_identity or (sites[name].latitude, sites[name].longitude) != (site.latitude, site.longitude):
            raise ValueError('Variants must retain the same measured identities and weather location')
        flags[name] = PRESETS[definition['preset']]
        initial = deepcopy(bundle.get('initial_states', {}).get(name))
        if initial is not None:
            if first_issue is None or timestamp(initial['available_at']) > first_issue:
                raise ValueError('Initial state must be available before the first issue')
            states[name] = {'bias': core.types.BiasState.from_dict(initial.get('bias', {})),
                            'shade': core.types.ShademapState.from_dict(initial.get('shade', {})),
                            'quantile': core.types.QuantileState.from_dict(initial.get('quantile', {}))}
            origins[name] = {'source':'provided', 'available_at':initial['available_at']}
        else:
            states[name] = {'bias':core.types.BiasState(), 'shade':core.types.ShademapState(), 'quantile':core.types.QuantileState()}
            origins[name] = {'source':'cold'}
    pending, forecasts, seen, trained, skipped = [], [], set(), {name: [] for name in presets}, Counter()
    rows = sorted(deepcopy(bundle['days']), key=lambda r: timestamp(r['issued_at']))
    for row in rows:
        local_day = date.fromisoformat(row['local_day'])
        iso = local_day.isoformat()
        if iso in seen:
            raise ValueError('Duplicate local day identity')
        seen.add(iso)
        start, end = local_day_bounds(local_day, tz)
        if end <= start:
            raise ValueError('Local day has no forecastable intervals')
        issue = timestamp(row['issued_at'])
        if row.get('weather_role') != 'issued_forecast' or not timestamp(row['weather_available_at']) <= issue <= start:
            raise ValueError('Original forecast weather must be available before a fully future day')
        available = timestamp(row['actuals_available_at'])
        if available < end:
            raise ValueError('Actuals must describe a closed local day')
        weather = _weather(core, row, start, end, issue)
        overrides = row.get('variant_weather', {})
        if not isinstance(overrides, dict) or set(overrides)-set(presets):
            raise ValueError('Weather variants must refer to configured models')
        weather_by_variant = {name:_weather(core, overrides[name], start, end, issue) if name in overrides else weather
                              for name in presets}
        # Delayed labels retain the reference from the original issue, even if
        # other labels have changed the state before the recorder closes them.
        ready = sorted((p for p in pending if p['available'] <= issue), key=lambda p: (p['available'], p['iso']))
        for event in ready:
            actuals, reason = _labels(core, site, event['row'], event['start'], event['end'])
            if event['row'].get('quality', 'valid') != 'valid':
                actuals, reason = None, 'declared_bad_quality'
            if actuals is None:
                skipped[reason] += 1
            else:
                baseline = event['baseline']
                raw = core.learning_inputs.metered_modeled_hourly(site, baseline, baseline.raw_hourly_wh)
                if core.measurement_quality.production_collapsed(sum((raw or {}).values()), sum(sum(h.values()) for h in actuals.values())):
                    skipped['collapsed_labels'] += 1
                    pending.remove(event)
                    continue
                for name in presets:
                    _train(core, sites[name], event['snapshots'][name], actuals, states[name], flags[name], event['iso'])
                    trained[name].append(event['iso'])
            pending.remove(event)
        predictions = {
            name: _predict(core, sites[name], weather_by_variant[name], issue, tz,
                           deepcopy(states[name]), flags[name])
            for name in presets
        }
        snapshots = {name: output[0] for name, output in predictions.items()}
        forecasts.append({
            'local_day': iso, 'issued_at': issue.isoformat(),
            'forecasts': {name: snapshot.to_dict() for name, snapshot in snapshots.items()},
            'dc_hourly_bands': {name: hourly for name, (_, hourly, _) in predictions.items() if hourly},
            'dc_hourly_band_status': {name: status for name, (_, _, status) in predictions.items() if status},
            'weather_sha256': {name:hashlib.sha256(json.dumps(overrides.get(name,row)['weather_slots'], sort_keys=True).encode()).hexdigest() for name in presets},
        })
        baseline = next((snapshots[name] for name in presets if sites[name] == site and name not in overrides), None)
        if baseline is None:
            baseline, _, _ = _predict(core, site, weather, issue, tz, deepcopy(states[presets[0]]), (False,False,False))
        pending.append({'baseline':baseline, 'iso': iso, 'start': start, 'end': end, 'available': available, 'row': row, 'snapshots': snapshots})
    return {'kind': 'physical_persisted_learning_replay', 'basis':'DC', 'timezone':bundle['timezone'],
            'kernel_sha256':core.digest, 'initial_state':'cold' if all(origin['source']=='cold' for origin in origins.values()) else 'provided_or_cold',
            'state_origins':origins, 'state_load_contract':'production_validate_and_clamp',
            'variant_config_sha256':{name:hashlib.sha256(json.dumps(sites[name].to_dict(), sort_keys=True).encode()).hexdigest() for name in presets},
            'records': forecasts,
            'measurement_scope': {
                name: ('site' if all(plane.actual_entity for plane in sites[name].planes)
                       else 'metered_planes')
                for name in presets
            },
            'states': {name:{key:value.to_dict() for key,value in state.items()} for name,state in states.items()},
            'trained_days':trained, 'training_day_semantics':'eligible consumed labels; raw never updates', 'skipped_days':dict(skipped), 'pending_days':len(pending),
            'excluded_layers':['intraday','drift_auto_disable','collapse_next_day_freeze','eta_calibration','ensemble'], 'activation':'never_automatic'}


def score_physical_replay(bundle: dict, replay: dict) -> dict:
    """Score only paired full hourly DC targets on the configured metered subset."""
    core, tz = _core(), ZoneInfo(bundle['timezone'])
    site = core.validate_site(deepcopy(bundle['site']))
    models = list(replay['states'])
    definitions = bundle.get('variants')
    variants = definitions if isinstance(definitions, dict) else {}
    sites = {
        name: core.validate_site(deepcopy(variants[name]['site']))
        if name in variants and 'site' in variants[name] else site
        for name in models
    }
    scope = {
        name: ('site' if all(plane.actual_entity for plane in sites[name].planes)
               else 'metered_planes')
        for name in models
    }
    inputs = {date.fromisoformat(row['local_day']): row for row in bundle['days']}
    if len(inputs) != len(bundle['days']):
        raise ValueError('Duplicate local day identity')
    records = []
    invalid = Counter()
    for output in replay['records']:
        local_day = date.fromisoformat(output['local_day'])
        row = inputs[local_day]
        start, end = local_day_bounds(local_day, tz)
        actuals, reason = _labels(core, site, row, start, end)
        if actuals is None or row.get('quality', 'valid') != 'valid':
            invalid[reason if actuals is None else 'declared_bad_quality'] += 1
            actuals = {}
        common = set.intersection(*(set(hours) for hours in actuals.values())) if actuals else set()
        curves = {}
        for name in models:
            snapshot = core.types.IssuedSnapshot.from_dict(output['forecasts'][name])
            curves[name] = core.learning_inputs.metered_modeled_hourly(
                sites[name], snapshot, snapshot.corrected_hourly_wh, layer='corrected'
            )
        # The planned axis derives from the local calendar, independent of
        # recorder coverage. Partial hours at a local midnight have no label.
        first_hour = start.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
        if first_hour < start:
            first_hour += step
        for i in range((end - first_hour) // step):
            target = first_hour + i * step
            hour = target.isoformat()
            values, intervals, band_status = {}, {}, {}
            for name in models:
                curve = curves[name]
                snapshot = output['forecasts'][name]
                value = curve.get(hour) if curve is not None else None
                if value is None and curve is not None and snapshot['corrected_hourly_wh'].get(hour) == 0:
                    value = 0.0
                values[name] = value
                # The engine publishes site-level bands. A subset point
                # forecast can be rebuilt from planes, but not its interval.
                hourly_bands = output.get('dc_hourly_bands', {}).get(name, {})
                if scope[name] == 'site' and snapshot.get('issued_at') == output['issued_at']:
                    interval = [hourly_bands.get(p, {}).get(hour) for p in ('p10', 'p50', 'p90')]
                    if all(bound is not None for bound in interval):
                        intervals[name] = interval
                        status = output.get('dc_hourly_band_status', {}).get(name, {}).get(hour)
                        if status is not None:
                            band_status[name] = status
            records.append({'issued_at':output['issued_at'],'available_at':output['issued_at'],
                            'target_start':hour,'target_end':(target+timedelta(hours=1)).isoformat(),
                            'actual_available_at':row['actuals_available_at'],
                            'actual_wh':sum(hours[hour] for hours in actuals.values()) if hour in common else None,
                            'forecasts':values, 'intervals':intervals, 'band_status':band_status})
    scored = compare({'schema_version':1,'basis':'DC','timezone':bundle['timezone'], 'models':models,
                      'development_end':bundle['development_end'],'selection_end':bundle['selection_end'],'records':records})
    scored['ablation'] = 'independent_physical_learning'
    scored['measurement_scope'] = scope
    scored['interval_scope'] = {
        name: 'hourly_site_dc_sum_of_15min_bounds'
        if scope[name] == 'site' else 'unavailable_for_metered_subset'
        for name in models
    }
    scored['interval_evaluation'] = {
        'basis': 'DC',
        'target_minutes': 60,
        'source_minutes': 15,
        'bound_aggregation': 'sum_of_15min_bounds',
    }
    scored['label_day_exclusions'] = dict(invalid)
    scored['activation'] = 'never_automatic'
    return scored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        validate_output_paths(args.manifest, args.output)
    except ValueError as error:
        parser.error(str(error))
    bundle = load_verified(args.manifest)
    result = replay_physical_days(bundle)
    result['input_sha256'] = json.loads(args.manifest.read_text())['input']['sha256']
    if 'development_end' in bundle and 'selection_end' in bundle:
        result['comparison'] = score_physical_replay(bundle, result)
    args.output.write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
