"""Plain-pytest tests for the config-flow site validation (HA-free).

These test ``_site_validation.validate_site`` directly — it imports only the
pure core + const, so no Home Assistant install is needed. They also assert
that every error code the validator can raise has a matching translation key
in both ``de.json`` and ``en.json`` (the operator-facing surface).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from balcony_solar_forecast._site_validation import (
    SiteValidationError,
    validate_site,
)
from balcony_solar_forecast.const import (
    CONF_AZIMUTH,
    CONF_EFFICIENCY,
    CONF_GROUP_AC_LIMIT,
    CONF_GROUP_PLANES,
    CONF_GROUPS,
    CONF_HORIZON,
    CONF_HZ_AZIMUTH,
    CONF_HZ_ELEVATION,
    CONF_HZ_SEASONAL,
    CONF_HZ_TAU,
    CONF_HZ_TAU_BARE,
    CONF_HZ_TAU_LEAFED,
    CONF_PLANE_NAME,
    CONF_PLANES,
    CONF_TILT,
    CONF_WP,
    DEFAULT_SITE,
)

_COMPONENT_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "balcony_solar_forecast"
)


def _site() -> dict:
    """A deep copy of the shipped operator site (a known-good baseline)."""
    return copy.deepcopy(DEFAULT_SITE)


# --------------------------------------------------------------------------
# Happy path: the shipped default site must validate and round-trip cleanly.
# --------------------------------------------------------------------------


def test_default_site_valid() -> None:
    site = validate_site(_site())
    assert len(site.planes) == 8
    assert len(site.groups) == 4
    # Round-trips losslessly (config-flow object selector contract).
    assert site.to_dict() == validate_site(site.to_dict()).to_dict()


def test_default_site_returns_parsed_siteconfig() -> None:
    site = validate_site(_site())
    m4 = site.plane_by_name("M4")
    assert m4 is not None
    # M4 carries the seasonal tree rows (SPEC §13) — foliage encoded.
    assert any(r.seasonal for r in m4.horizon)


# --------------------------------------------------------------------------
# Structural errors.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, [], "site", 42, 3.14])
def test_non_object_rejected(bad) -> None:
    with pytest.raises(SiteValidationError) as exc:
        validate_site(bad)
    assert exc.value.code == "site_not_object"


def test_missing_lat_lon_malformed() -> None:
    site = _site()
    del site["latitude"]
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "site_malformed"


def test_no_planes_rejected() -> None:
    site = _site()
    site[CONF_PLANES] = []
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "no_planes"


# --------------------------------------------------------------------------
# Plane field range checks — the classic silent-error traps.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("azimuth", [-1.0, 360.1, 400.0, -155.0])
def test_azimuth_out_of_range(azimuth) -> None:
    """Guards the 25-deg-plane sign-error trap (SPEC Anhang A).

    An operator who accidentally enters the Open-Meteo 0=S signed value
    (e.g. -155 for the 25-deg plane) instead of the internal 0=N value must
    be rejected here, not silently mis-modelled.
    """
    site = _site()
    site[CONF_PLANES][0][CONF_AZIMUTH] = azimuth
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_azimuth"


@pytest.mark.parametrize("azimuth", [0.0, 25.0, 115.0, 205.0, 360.0])
def test_azimuth_in_range_ok(azimuth) -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_AZIMUTH] = azimuth
    assert validate_site(site) is not None


@pytest.mark.parametrize("tilt", [-0.1, 90.1, 120.0])
def test_tilt_out_of_range(tilt) -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_TILT] = tilt
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_tilt"


@pytest.mark.parametrize("wp", [0.0, -100.0])
def test_wp_must_be_positive(wp) -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_WP] = wp
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_wp"


@pytest.mark.parametrize("eff", [-0.1, 1.5])
def test_efficiency_out_of_range(eff) -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_EFFICIENCY] = eff
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_efficiency"


def test_plane_no_name() -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_PLANE_NAME] = ""
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "plane_no_name"


def test_plane_dup_name() -> None:
    site = _site()
    site[CONF_PLANES][1][CONF_PLANE_NAME] = site[CONF_PLANES][0][CONF_PLANE_NAME]
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "plane_dup_name"


# --------------------------------------------------------------------------
# Horizon-row checks: order, elevation, tau, seasonal completeness.
# --------------------------------------------------------------------------


def test_horizon_unsorted_is_normalised_not_rejected() -> None:
    """An out-of-order horizon table is stable-sorted, not rejected.

    Downstream interpolation needs ascending azimuth; the flow persists the
    canonical (sorted) form so a merely mis-ordered operator entry still
    works. This also covers the shipped M4/M8 default whose seasonal tree
    rows (135/175) follow the far-field 150 breakpoint in source order.
    """
    site = _site()
    site[CONF_PLANES][0][CONF_HORIZON] = list(
        reversed(site[CONF_PLANES][0][CONF_HORIZON])
    )
    result = validate_site(site)
    azimuths = [r.azimuth_deg for r in result.planes[0].horizon]
    assert azimuths == sorted(azimuths)


def test_default_south_horizon_normalised_ascending() -> None:
    """The shipped M4/M8 south horizons come out ascending after validation."""
    result = validate_site(_site())
    for name in ("M4", "M8"):
        plane = result.plane_by_name(name)
        az = [r.azimuth_deg for r in plane.horizon]
        assert az == sorted(az), f"{name} horizon not sorted: {az}"


def test_horizon_sorted_ok() -> None:
    # Default site horizons validate and normalise cleanly.
    assert validate_site(_site()) is not None


@pytest.mark.parametrize("tau", [-0.01, 1.01, 2.0])
def test_horizon_tau_out_of_range(tau) -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_HORIZON][0][CONF_HZ_TAU] = tau
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_tau"


@pytest.mark.parametrize("elev", [-1.0, 91.0])
def test_horizon_elevation_out_of_range(elev) -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_HORIZON][0][CONF_HZ_ELEVATION] = elev
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_horizon_elevation"


@pytest.mark.parametrize("az", [-1.0, 361.0])
def test_horizon_azimuth_out_of_range(az) -> None:
    site = _site()
    # Single-row horizon so the bad azimuth is what trips (not order).
    site[CONF_PLANES][0][CONF_HORIZON] = [
        {CONF_HZ_AZIMUTH: az, CONF_HZ_ELEVATION: 10.0, CONF_HZ_TAU: 0.0}
    ]
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_horizon_azimuth"


def test_seasonal_row_needs_both_taus() -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_HORIZON] = [
        {
            CONF_HZ_AZIMUTH: 140.0,
            CONF_HZ_ELEVATION: 40.0,
            CONF_HZ_TAU: 0.45,
            CONF_HZ_SEASONAL: True,
            CONF_HZ_TAU_LEAFED: 0.45,
            # tau_bare missing
        }
    ]
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "seasonal_missing_tau"


def test_seasonal_row_out_of_range_tau() -> None:
    site = _site()
    site[CONF_PLANES][0][CONF_HORIZON] = [
        {
            CONF_HZ_AZIMUTH: 140.0,
            CONF_HZ_ELEVATION: 40.0,
            CONF_HZ_TAU: 0.45,
            CONF_HZ_SEASONAL: True,
            CONF_HZ_TAU_LEAFED: 0.45,
            CONF_HZ_TAU_BARE: 1.9,
        }
    ]
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_tau"


# --------------------------------------------------------------------------
# Inverter-group checks.
# --------------------------------------------------------------------------


def test_group_unknown_plane() -> None:
    site = _site()
    site[CONF_GROUPS][0][CONF_GROUP_PLANES] = ["DoesNotExist"]
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "group_unknown_plane"


def test_group_no_planes() -> None:
    site = _site()
    site[CONF_GROUPS][0][CONF_GROUP_PLANES] = []
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "group_no_planes"


@pytest.mark.parametrize("limit", [0.0, -1.0])
def test_group_bad_ac_limit(limit) -> None:
    site = _site()
    site[CONF_GROUPS][0][CONF_GROUP_AC_LIMIT] = limit
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "bad_ac_limit"


def test_group_dup_name() -> None:
    site = _site()
    site[CONF_GROUPS][1]["name"] = site[CONF_GROUPS][0]["name"]
    with pytest.raises(SiteValidationError) as exc:
        validate_site(site)
    assert exc.value.code == "group_dup_name"


def test_site_with_no_groups_ok() -> None:
    """Groups are optional at validation time (empty AC-clamp set allowed)."""
    site = _site()
    site[CONF_GROUPS] = []
    assert validate_site(site) is not None


# --------------------------------------------------------------------------
# Translation coverage: every raisable code must exist in both locales.
# --------------------------------------------------------------------------

_ALL_ERROR_CODES = {
    "site_not_object",
    "site_malformed",
    "no_planes",
    "plane_no_name",
    "plane_dup_name",
    "bad_azimuth",
    "bad_tilt",
    "bad_wp",
    "bad_efficiency",
    "bad_horizon_azimuth",
    "bad_horizon_elevation",
    "bad_tau",
    "seasonal_missing_tau",
    "group_no_name",
    "group_dup_name",
    "group_no_planes",
    "group_unknown_plane",
    "bad_ac_limit",
}


@pytest.mark.parametrize("locale", ["de", "en"])
def test_translation_covers_all_error_codes(locale) -> None:
    path = _COMPONENT_DIR / "translations" / f"{locale}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    config_errors = set(data["config"]["error"])
    options_errors = set(data["options"]["error"])
    # Config-step codes include name_required; the options step never raises it.
    missing_config = _ALL_ERROR_CODES - config_errors
    assert not missing_config, f"{locale} config missing: {missing_config}"
    # Options errors are the site codes only (no name_required).
    missing_options = (_ALL_ERROR_CODES - {"site_not_object"}) - options_errors
    # site_not_object is still raisable in options too, so require it there.
    assert "site_not_object" in options_errors
    assert not missing_options, f"{locale} options missing: {missing_options}"


@pytest.mark.parametrize("locale", ["de", "en"])
def test_translation_has_user_reconfigure_and_init_steps(locale) -> None:
    path = _COMPONENT_DIR / "translations" / f"{locale}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "user" in data["config"]["step"]
    # Structural setup now edits into entry.data via the reconfigure flow.
    assert "reconfigure" in data["config"]["step"]
    assert "init" in data["options"]["step"]
    # The structural fields carry a label in the user AND the reconfigure step;
    # the name is set once in the user step and immutable thereafter.
    for field in ("latitude", "longitude", "site"):
        assert field in data["config"]["step"]["user"]["data"]
        assert field in data["config"]["step"]["reconfigure"]["data"]
    assert "name" in data["config"]["step"]["user"]["data"]
    assert "name" not in data["config"]["step"]["reconfigure"]["data"]
    # The slim options step labels only the runtime tunables — no structural
    # fields (they moved to reconfigure so options can't shadow entry.data).
    init_data = data["options"]["step"]["init"]["data"]
    for tunable in (
        "fast_learner_enabled",
        "slow_learner_enabled",
        "day_ahead_bias_enabled",
        "quantiles_enabled",
        "comparison_sensors",
    ):
        assert tunable in init_data
    for structural in (
        "latitude",
        "longitude",
        "site",
        "fetch_interval_seconds",
        "recompute_interval_seconds",
    ):
        assert structural not in init_data


def test_de_and_en_have_same_error_keys() -> None:
    de = json.loads((_COMPONENT_DIR / "translations" / "de.json").read_text("utf-8"))
    en = json.loads((_COMPONENT_DIR / "translations" / "en.json").read_text("utf-8"))
    assert set(de["config"]["error"]) == set(en["config"]["error"])
    assert set(de["options"]["error"]) == set(en["options"]["error"])


# --------------------------------------------------------------------------
# Entity + service name coverage: every translation_key set on an entity and
# the get_forecast service must resolve to a name in both locales, otherwise
# the seven entities collapse onto the bare device name and become
# indistinguishable in battery_manager's entity picker (SPEC §8).
# --------------------------------------------------------------------------

# translation_key -> platform, mirroring sensor.py / binary_sensor.py.
_ENTITY_TRANSLATION_KEYS = {
    "sensor": {
        "energy_production_today",
        "energy_production_tomorrow",
        "energy_production_d2",
        "power_production_now",
        "last_fetch_age_min",
        "source_status",
    },
    "binary_sensor": {"degraded"},
}

_SOURCE_STATUS_STATES = {"fresh", "cached", "physics_fallback", "unavailable"}


@pytest.mark.parametrize("locale", ["de", "en"])
def test_translation_covers_all_entity_names(locale) -> None:
    path = _COMPONENT_DIR / "translations" / f"{locale}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entity = data.get("entity", {})
    for platform, keys in _ENTITY_TRANSLATION_KEYS.items():
        section = entity.get(platform, {})
        for key in keys:
            assert key in section, f"{locale}: entity.{platform}.{key} missing"
            assert section[key].get("name"), (
                f"{locale}: entity.{platform}.{key}.name empty"
            )
    # The source_status ENUM sensor needs a state map for each ladder rung.
    states = entity["sensor"]["source_status"].get("state", {})
    assert set(states) >= _SOURCE_STATUS_STATES, (
        f"{locale}: source_status states missing {_SOURCE_STATUS_STATES - set(states)}"
    )


@pytest.mark.parametrize("locale", ["de", "en"])
def test_translation_has_get_forecast_service(locale) -> None:
    path = _COMPONENT_DIR / "translations" / f"{locale}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    svc = data.get("services", {}).get("get_forecast", {})
    assert svc.get("name"), f"{locale}: get_forecast service name missing"
    assert svc.get("description"), f"{locale}: get_forecast service description missing"
    assert "entry_id" in svc.get("fields", {}), f"{locale}: entry_id field missing"


def test_de_and_en_entity_and_service_keys_match() -> None:
    de = json.loads((_COMPONENT_DIR / "translations" / "de.json").read_text("utf-8"))
    en = json.loads((_COMPONENT_DIR / "translations" / "en.json").read_text("utf-8"))
    assert set(de["entity"]["sensor"]) == set(en["entity"]["sensor"])
    assert set(de["entity"]["binary_sensor"]) == set(en["entity"]["binary_sensor"])
    assert set(de["services"]) == set(en["services"])
