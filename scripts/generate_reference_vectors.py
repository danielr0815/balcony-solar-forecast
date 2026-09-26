# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["pvlib==0.15.2"]
# ///
"""Rebuild the independent pvlib oracle in its own locked uv script environment.

    uv run --script --locked scripts/generate_reference_vectors.py --check

Inputs are the original reference metadata. No integration physics is imported.
The old generator was not retained; this reconstruction fixes the SPA inputs
explicitly (sea level, 101325 Pa, 12 C, delta_t=67 s) and checks all rounded
reference values before anyone deliberately rewrites the artifact.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

VECTOR_PATH = Path(__file__).resolve().parents[1] / "tests/core/reference_vectors.json"
ROUND_DIGITS = 4


def generate(meta: dict) -> dict:
    import pandas as pd
    import pvlib

    if pvlib.__version__ != meta["pvlib_version"]:
        raise ValueError(f"Expected pvlib {meta['pvlib_version']}, got {pvlib.__version__}")
    times = pd.DatetimeIndex([
        f"{day}T{hour:02}:00:00+00:00" for day in meta["dates"] for hour in meta["hours_utc"]
    ])
    positions = pvlib.solarposition.get_solarposition(
        times, latitude=meta["lat"], longitude=meta["lon"], altitude=0.0,
        pressure=101325.0, temperature=12.0, method="nrel_numpy", delta_t=67.0,
    )
    extra = pvlib.irradiance.get_extra_radiation(times, method="spencer")
    solpos, poa = [], []
    for stamp, row in positions.iterrows():
        solpos.append({
            "timestamp": stamp.isoformat(),
            "apparent_elevation": round(float(row["apparent_elevation"]), ROUND_DIGITS),
            "azimuth": round(float(row["azimuth"]), ROUND_DIGITS),
        })
        for plane in meta["planes"]:
            for case, radiation in meta["cases"].items():
                irradiance = pvlib.irradiance.get_total_irradiance(
                    surface_tilt=plane["tilt"], surface_azimuth=plane["az"],
                    solar_zenith=row["apparent_zenith"], solar_azimuth=row["azimuth"],
                    **radiation, dni_extra=float(extra.loc[stamp]),
                    albedo=meta["albedo"], model=meta["transposition_model"],
                )
                poa.append({
                    "timestamp": stamp.isoformat(), "plane_az": plane["az"],
                    "plane_tilt": plane["tilt"], "case": case, **radiation,
                    "albedo": meta["albedo"],
                    "solar_zenith": round(float(row["apparent_zenith"]), ROUND_DIGITS),
                    "solar_azimuth": round(float(row["azimuth"]), ROUND_DIGITS),
                    "apparent_elevation": round(float(row["apparent_elevation"]), ROUND_DIGITS),
                    "dni_extra": round(float(extra.loc[stamp]), ROUND_DIGITS),
                    **{key: round(float(value), ROUND_DIGITS) for key, value in irradiance.items()},
                })
    return {"meta": meta, "solpos": solpos, "poa": poa}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    args = parser.parse_args()
    meta = json.loads(Path(__file__).with_name("reference_inputs.json").read_text())
    rebuilt = generate(meta)
    if args.write:
        VECTOR_PATH.write_text(json.dumps(rebuilt, indent=2) + "\n")
        return 0
    original = json.loads(VECTOR_PATH.read_text())
    if rebuilt != original:
        for section in ("solpos", "poa"):
            for expected, actual in zip(original[section], rebuilt[section], strict=True):
                if expected != actual:
                    print(f"Reference mismatch in {section}: {expected} != {actual}")
                    return 1
        return 1
    print(f"Independent pvlib oracle reproduced: {len(rebuilt['solpos'])} solar + {len(rebuilt['poa'])} POA vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
