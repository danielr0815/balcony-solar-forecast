"""Numeric boundary regressions through the real site/store validators."""

from __future__ import annotations

from copy import deepcopy

import pytest
from balcony_solar_forecast._site_validation import SiteValidationError, validate_site
from balcony_solar_forecast.const import (
    DEFAULT_SITE,
    STORE_KEY_BIAS_STATE,
    STORE_KEY_QUANTILE_STATE,
    STORE_KEY_SHADEMAP_STATE,
)
from balcony_solar_forecast.core.types import (
    BiasState,
    QuantileState,
    ShademapBin,
    ShademapState,
)
from balcony_solar_forecast.store import validate_state


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_nonfinite_count_cannot_abort_other_store_sections(value):
    shade = ShademapState(channels={"P": {"1:1:0": ShademapBin(0.6, 20)}}).to_dict()
    quantile = QuantileState(bins={"clear|midday": [["2026-06-21", 0.9]]}).to_dict()
    blob = {
        "schema_version": 3,
        STORE_KEY_BIAS_STATE: {"version": 1, "cells": {"clear|midday": {"theta": 1.1, "n": value}}},
        STORE_KEY_SHADEMAP_STATE: shade,
        STORE_KEY_QUANTILE_STATE: quantile,
    }

    loaded = validate_state(blob)

    assert BiasState.from_dict(loaded[STORE_KEY_BIAS_STATE]).get_bias("clear", "midday") == 1.0
    assert loaded[STORE_KEY_SHADEMAP_STATE] == shade
    assert loaded[STORE_KEY_QUANTILE_STATE] == quantile


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), "inf", "-inf", "nan"])
def test_site_rejects_nonfinite_module_power(value):
    raw = deepcopy(DEFAULT_SITE)
    raw["planes"][0]["wp"] = value
    with pytest.raises(SiteValidationError, match="bad_wp"):
        validate_site(raw)


def test_bare_horizon_profile_cannot_hide_nan_elevation():
    raw = deepcopy(DEFAULT_SITE)
    raw["planes"][0]["horizon"] = [{
        "azimuth_deg": 0, "elevation_deg": 30, "tau": 0.3,
        "seasonal": True, "tau_leafed": 0.3, "tau_bare": 0.6,
        "tau_points": [[5.0, 0.3]], "tau_points_bare": [[float("nan"), 0.6]],
    }]
    with pytest.raises(SiteValidationError, match="seasonal_points_mismatch"):
        validate_site(raw)
