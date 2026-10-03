"""Compare two checked-out physics contracts on a public synthetic matrix.

Loads only the HA-free kernels in separate namespace packages. This measures
model-change magnitude, not predictive accuracy; it never reads a live site.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import uuid4


def kernel(root: Path):
    directory = root/'custom_components'/'balcony_solar_forecast'
    namespace = '_bsf_impact_'+uuid4().hex
    package = ModuleType(namespace)
    package.__path__ = [str(directory)]
    sys.modules[namespace] = package
    core = ModuleType(namespace+'.core')
    core.__path__ = [str(directory/'core')]
    sys.modules[namespace+'.core'] = core
    modules = [importlib.import_module(namespace+'.core.'+name) for name in ('types', 'engine', 'horizon')]
    files = [directory/'const.py', *sorted((directory/'core').glob('*.py'))]
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(directory).as_posix().encode()+b'\0'+file.read_bytes()+b'\0')
    return (*modules, digest.hexdigest())


def matrix(root: Path) -> dict:
    types, engine, horizon, code_hash = kernel(root)
    at = datetime(2026, 6, 21, tzinfo=UTC)
    records = []
    for tilt in (0, 30, 70, 90):
        for obstruction in (0, 30, 60):
            plane = types.PlaneConfig('Panel', 180, tilt, 400,
                                      horizon=(types.HorizonRow(0, obstruction, 0),))
            site = types.SiteConfig(51, 10, (plane,), ())
            weather = types.WeatherSeries(tuple(types.WeatherSlot(at+timedelta(minutes=15*i),
                                                                  150, 0, 150, 20) for i in range(96)))
            result = engine.compute_forecast(site, weather, at)
            records.append({'case': f'diffuse_tilt_{tilt}_horizon_{obstruction}',
                            'sky_view_factor': horizon.sky_view_factor(plane),
                            'dc_wh': sum(result.hourly_wh.values()),
                            'ac_wh': sum(result.ac_hourly_wh.values())})
    for clipped, wp, limit in ((False, 400, 800), (True, 4000, 90)):
        for factor in (.5, 1, 1.5):
            plane = types.PlaneConfig('Panel', 180, 0, wp)
            site = types.SiteConfig(0, 0, (plane,), (types.InverterGroup('G', ('Panel',), limit, .9),))
            slot = at.replace(hour=10)
            weather = types.WeatherSeries((types.WeatherSlot(slot, 900, 900, 100, 20),))
            result = engine.compute_forecast(site, weather, slot,
                                             hooks=engine.LearnerHooks(slot_factor=lambda start, value=factor: value))
            records.append({'case': f'electrical_clipped_{clipped}_factor_{factor}',
                            'dc_wh': sum(result.hourly_wh.values()), 'ac_wh': sum(result.ac_hourly_wh.values())})
    return {'kernel_sha256': code_hash, 'records': records}


def impact(baseline: dict, current: dict) -> dict:
    old = {row['case']: row for row in baseline['records']}
    if set(old) != {row['case'] for row in current['records']}:
        raise ValueError('Both contracts require the same matrix')
    rows = []
    for row in current['records']:
        previous = old[row['case']]
        rows.append({'case': row['case'], 'baseline': previous, 'current': row,
                     'delta': {key: row[key]-previous[key] for key in row if key != 'case'}})
    return {'kind': 'synthetic_contract_impact', 'basis': ['DC', 'AC'],
            'baseline_kernel_sha256': baseline['kernel_sha256'],
            'current_kernel_sha256': current['kernel_sha256'],
            'predictive_improvement': 'not_measured', 'records': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--current-root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if any(args.output.resolve().is_relative_to((root/'custom_components').resolve())
           for root in (args.baseline_root, args.current_root)):
        parser.error('Report must not overwrite a calculation kernel')
    report = impact(matrix(args.baseline_root), matrix(args.current_root))
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
