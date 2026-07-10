"""Golden tests against offline pvlib reference vectors (SPEC §9 Phase 1).

Merge-blocker per the phase plan: our stdlib core must reproduce pvlib on the
two links pvlib actually covers — solar position and Hay-Davies plane-of-array
transposition — across all six operator planes, the four solstice/equinox
dates, and hourly 05:00..20:00 UTC, for a nominal and a low-sun synthetic
irradiance case.

Vectors are produced OUTSIDE this repo by scratchpad/gen_reference_vectors.py
in a throwaway pvlib venv (pvlib/pandas are NEVER runtime deps, SPEC §4) and
committed as tests/core/reference_vectors.json.

Scope / deliberate deviations:
  * pvlib comparison is transposition-only. Horizon transmittance, per-plane
    sky-view scaling, electrical (Ross/derate/AC-clamp) are OUT of scope here
    (pvlib does not model our horizon tables) and covered by their own suites.
  * Fixed synthetic (GHI/DNI/DHI) isolate the transposition math from the
    separation/clear-sky chain.
  * Azimuth convention matches: pvlib solar azimuth and surface_azimuth are
    both 0=N clockwise (pvlib >= 0.11), identical to our INTERNAL convention —
    no remap. A convention regression here is exactly the SPEC's azimuth-sign
    trap on the 25 deg planes.
  * Sun elevation <= LOW_SUN_CUTOFF_DEG (3 deg): equality is NOT asserted.
    Our Rb cap and circumsolar=0 gate intentionally diverge from pvlib there;
    we assert only that the POA does not explode (low-sun no-explosion trap).

Tolerances:
  * solar position: 0.5 deg (elevation and azimuth).
  * Hay-Davies total POA: max(2 W/m2, 0.5%) of the pvlib value. This was
    max(2 W/m2, 1.5%) until the anisotropy index gained the Earth-Sun
    eccentricity correction (``doy``): pvlib's ``get_extra_radiation`` carries
    that eccentricity, so our old fixed-solar-constant AI drifted up to ~1.9 %
    from pvlib and the 1.5 % pad absorbed exactly that gap. With ``doy`` passed
    (as the engine runs) the worst deviation drops to ~0.28 %, so the pad is
    tightened to a true 0.5 % guard (see ``_poa_total``).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

# Import the core modules directly from their files so the test stays strictly
# HA-free (SPEC §4), the same self-contained pattern used by test_solpos.py.
# Loading via the full package path ``custom_components.balcony_solar_forecast``
# would execute the integration-root ``__init__.py`` (imports ``homeassistant``)
# and fail under bare pytest. ``const``/``solpos``/``transpose`` depend only on
# stdlib math/datetime (transpose imports two names from ``const``), so a
# file-based load is fully self-contained.
_CORE_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "balcony_solar_forecast"
)


def _load(mod_name: str, rel_path: str) -> ModuleType:
    """Load a core module from file under a private name, HA-free."""
    if mod_name in __import__("sys").modules:
        return __import__("sys").modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _CORE_DIR / rel_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ``core/transpose.py`` does ``from ..const import LOW_SUN_CUTOFF_DEG, RB_CAP``.
# For that relative import to resolve, transpose must be loaded as
# ``<root>.core.transpose`` (so ``..`` climbs from ``<root>.core`` to ``<root>``
# where ``const`` lives). We register two package objects, ``<root>`` and
# ``<root>.core``, then load ``const`` and ``transpose`` as their submodules.
import sys as _sys  # noqa: E402


def _register_pkg(name: str, path: Path) -> None:
    if name not in _sys.modules:
        m = ModuleType(name)
        m.__path__ = [str(path)]
        m.__package__ = name
        _sys.modules[name] = m


_ROOT = "_bsf_golden_pkg"
_register_pkg(_ROOT, _CORE_DIR)
_register_pkg(f"{_ROOT}.core", _CORE_DIR / "core")

_const = _load(f"{_ROOT}.const", "const.py")
_solpos = _load(f"{_ROOT}.core.solpos", "core/solpos.py")
_transpose = _load(f"{_ROOT}.core.transpose", "core/transpose.py")

LOW_SUN_CUTOFF_DEG = _const.LOW_SUN_CUTOFF_DEG
sun_position = _solpos.sun_position
hay_davies_poa = _transpose.hay_davies_poa

# --- reference vectors ---------------------------------------------------

_VECTORS_PATH = Path(__file__).resolve().parent / "reference_vectors.json"

if not _VECTORS_PATH.exists():  # pragma: no cover - guards a missing artifact
    pytest.skip(
        "reference_vectors.json missing; regenerate with "
        "scratchpad/gen_reference_vectors.py in the pvlib venv",
        allow_module_level=True,
    )

_VECTORS = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
_META = _VECTORS["meta"]
_LAT = _META["lat"]
_LON = _META["lon"]

# Tolerances (SPEC / task).
_SOLPOS_TOL_DEG = 0.5
_POA_ABS_TOL = 2.0  # W/m2
# Tightened 1.5% -> 0.5% now that the anisotropy index carries the Earth-Sun
# eccentricity (doy), matching pvlib's get_extra_radiation: worst deviation
# fell from ~1.9% to ~0.28%, so 0.5% is a true guard, not eccentricity padding.
_POA_REL_TOL = 0.005  # 0.5%

# Above this elevation pvlib and our model must agree; at/below it we only
# require no-explosion (Rb cap / circumsolar gate intentionally deviate). Use
# a hair above the cutoff so borderline cases stay in the equality band.
_EQUALITY_MIN_EL = LOW_SUN_CUTOFF_DEG


def _poa_total(vec: dict) -> float:
    """Our Hay-Davies POA total (beam+circumsolar+isotropic+ground) in W/m2.

    Uses OUR solar position (not the vector's) so the test also exercises the
    solpos->transpose handoff end to end, matching how the engine runs. The
    az/el agree with pvlib to < 0.5 deg (asserted separately), so this does
    not smuggle in a second error source at the tolerances we use. The vector's
    ``doy`` is passed so the anisotropy index carries the Earth-Sun eccentricity
    (matching pvlib's ``get_extra_radiation`` and the live engine).
    """
    dt = datetime.fromisoformat(vec["timestamp"])
    sun_az, sun_el = sun_position(dt, _LAT, _LON)
    comps = hay_davies_poa(
        ghi=vec["ghi"],
        dni=vec["dni"],
        dhi=vec["dhi"],
        sun_az=sun_az,
        sun_el=sun_el,
        plane_az=vec["plane_az"],
        plane_tilt=vec["plane_tilt"],
        albedo=vec["albedo"],
        doy=dt.timetuple().tm_yday,
    )
    return comps["beam"] + comps["circumsolar"] + comps["isotropic"] + comps["ground"]


# --- solar position ------------------------------------------------------


@pytest.mark.parametrize(
    "vec",
    _VECTORS["solpos"],
    ids=[v["timestamp"] for v in _VECTORS["solpos"]],
)
def test_solar_position_matches_pvlib(vec: dict) -> None:
    """Our NOAA solpos matches pvlib SPA within 0.5 deg (el and az)."""
    dt = datetime.fromisoformat(vec["timestamp"])
    az, el = sun_position(dt, _LAT, _LON)

    assert abs(el - vec["apparent_elevation"]) <= _SOLPOS_TOL_DEG, (
        f"elevation {el:.4f} vs pvlib {vec['apparent_elevation']:.4f} "
        f"at {vec['timestamp']}"
    )

    # Azimuth is only meaningful when the sun is up; below the horizon pvlib's
    # refracted azimuth and ours can differ harmlessly and the value is unused
    # by the engine (no beam below horizon).
    if vec["apparent_elevation"] > 0.0:
        # Compare on the circle: wrap the signed difference into (-180, 180].
        d = (az - vec["azimuth"] + 180.0) % 360.0 - 180.0
        assert abs(d) <= _SOLPOS_TOL_DEG, (
            f"azimuth {az:.4f} vs pvlib {vec['azimuth']:.4f} "
            f"(delta {d:.4f}) at {vec['timestamp']}"
        )


# --- Hay-Davies transposition -------------------------------------------


@pytest.mark.parametrize(
    "vec",
    _VECTORS["poa"],
    ids=[
        f"{v['timestamp']}_az{v['plane_az']:g}_t{v['plane_tilt']:g}_{v['case']}"
        for v in _VECTORS["poa"]
    ],
)
def test_haydavies_poa_matches_pvlib(vec: dict) -> None:
    """Our Hay-Davies total POA matches pvlib within max(2 W/m2, 1.5%).

    Skips equality for sun elevation <= LOW_SUN_CUTOFF_DEG (our Rb cap and
    circumsolar gate intentionally deviate there — covered by the
    no-explosion test instead).
    """
    if vec["apparent_elevation"] <= _EQUALITY_MIN_EL:
        pytest.skip(
            f"sun el {vec['apparent_elevation']:.2f} <= {_EQUALITY_MIN_EL} deg: "
            "equality not asserted (Rb-cap deviation); see no-explosion test"
        )

    ours = _poa_total(vec)
    ref = vec["poa_global"]
    tol = max(_POA_ABS_TOL, _POA_REL_TOL * abs(ref))
    assert abs(ours - ref) <= tol, (
        f"POA {ours:.4f} vs pvlib {ref:.4f} (tol {tol:.4f}) at "
        f"{vec['timestamp']} plane az{vec['plane_az']:g}/t{vec['plane_tilt']:g} "
        f"case {vec['case']} (el {vec['apparent_elevation']:.2f})"
    )


@pytest.mark.parametrize(
    "vec",
    [v for v in _VECTORS["poa"] if v["apparent_elevation"] <= _EQUALITY_MIN_EL],
    ids=[
        f"{v['timestamp']}_az{v['plane_az']:g}_t{v['plane_tilt']:g}_{v['case']}"
        for v in _VECTORS["poa"]
        if v["apparent_elevation"] <= _EQUALITY_MIN_EL
    ],
)
def test_low_sun_poa_does_not_explode(vec: dict) -> None:
    """Low-sun (el <= 3 deg) POA stays finite and bounded (Rb-cap guard).

    We do NOT compare to pvlib here (intentional deviation). The guard: the
    total POA must be finite, non-negative, and not exceed a generous envelope
    of the horizontal inputs — a runaway Rb would blow the beam term far past
    this. Envelope: DNI + GHI + DHI is comfortably above any legitimate POA at
    these low-sun inputs.
    """
    ours = _poa_total(vec)
    assert ours == ours, "POA is NaN"  # NaN != NaN
    assert ours >= 0.0, f"negative POA {ours} at {vec['timestamp']}"
    envelope = vec["dni"] + vec["ghi"] + vec["dhi"]
    assert ours <= envelope, (
        f"low-sun POA {ours:.4f} exceeds envelope {envelope:.1f} (Rb explosion?) "
        f"at {vec['timestamp']} plane az{vec['plane_az']:g}/t{vec['plane_tilt']:g}"
    )


# --- vector-integrity smoke ---------------------------------------------


def test_vector_counts() -> None:
    """Vector file has the expected shape (4 dates x 16 h, 6 planes x 2 cases)."""
    n_slots = len(_META["dates"]) * len(_META["hours_utc"])
    assert len(_VECTORS["solpos"]) == n_slots
    assert len(_VECTORS["poa"]) == n_slots * len(_META["planes"]) * len(_META["cases"])
