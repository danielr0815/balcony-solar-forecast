"""The optional per-plane ``actual_energy_entity`` and the LTS card (SPEC §14.3).

Pure test: ``_dashboard`` and ``_site_validation`` both import only ``const`` +
the core types, so the whole feature is reachable without Home Assistant.

Regression origin: the LTS statistics-graph asked for ``stat_types: [sum]`` on
the planes' ``actual_entity`` POWER sensors. The recorder keeps mean/min/max for
a ``state_class: measurement`` sensor and reports ``has_sum: false``, so the card
had no series to draw and rendered as an empty plot area — the measured
production looked lost while 14 days of daily statistics were present.
"""

from __future__ import annotations

import pytest
from balcony_solar_forecast import _dashboard as d
from balcony_solar_forecast._site_validation import (
    SiteValidationError,
    validate_site,
)
from balcony_solar_forecast.const import (
    CONF_ACTUAL_ENERGY_ENTITY,
    CONF_ACTUAL_ENTITY,
    CONF_PLANES,
    DEFAULT_SITE,
)
from balcony_solar_forecast.core.types import PlaneConfig

_POWER = [("M1", "sensor.m1_power"), ("M2", "sensor.m2_power")]
_ENERGY = [("M1", "sensor.m1_energy"), ("M2", "sensor.m2_energy")]


def _lts(power, energy):
    cards: list[dict] = []
    d._add_measured_lts(cards, power, energy)
    return cards


class TestLtsCard:
    def test_never_asks_for_sum_on_power_sensors(self):
        """THE regression: power sensors have no sum, so `sum` draws nothing."""
        card = _lts(_POWER, [])[0]
        assert card["stat_types"] == ["mean"]
        assert "sum" not in card["stat_types"]

    def test_power_fallback_keeps_bare_entity_ids(self):
        card = _lts(_POWER, [])[0]
        assert card["entities"] == ["sensor.m1_power", "sensor.m2_power"]
        assert card["title"] == "Measured mean DC power per module (LTS)"

    def test_energy_counters_win_and_chart_change(self):
        """With energy counters the card shows TRUE daily energy, and the
        running day's bar is the energy so far rather than a mean-so-far."""
        card = _lts(_POWER, _ENERGY)[0]
        assert card["stat_types"] == ["change"]
        assert card["title"] == "Measured daily energy per module (LTS)"
        assert card["entities"] == [
            {"entity": "sensor.m1_energy", "name": "M1"},
            {"entity": "sensor.m2_energy", "name": "M2"},
        ]

    def test_energy_rows_carry_plane_names(self):
        """The per-port counters' own friendly names are ambiguous (every
        inverter calls them "Port 1/2"), so the rows must read M1..M8."""
        names = [r["name"] for r in _lts(_POWER, _ENERGY)[0]["entities"]]
        assert names == ["M1", "M2"]

    def test_no_measured_entities_at_all_emits_no_card(self):
        assert _lts([], []) == []

    def test_energy_only_still_emits_the_change_card(self):
        """A site that declares counters but no power sensor still gets a card."""
        card = _lts([], _ENERGY)[0]
        assert card["stat_types"] == ["change"]

    def test_card_stays_a_builtin_bar_chart_in_both_branches(self):
        for card in (_lts(_POWER, [])[0], _lts(_POWER, _ENERGY)[0]):
            assert card["type"] == "statistics-graph"
            assert card["chart_type"] == "bar"
            assert card["period"] == "day"
            assert card["days_to_show"] == 14


class TestPlaneRoundTrip:
    def test_energy_entity_survives_a_round_trip(self):
        plane = PlaneConfig.from_dict(
            {
                "name": "M1",
                "azimuth_deg": 25.0,
                "tilt_deg": 70.0,
                "wp": 370.0,
                CONF_ACTUAL_ENTITY: "sensor.m1_power",
                CONF_ACTUAL_ENERGY_ENTITY: "sensor.m1_energy",
            }
        )
        assert plane.actual_energy_entity == "sensor.m1_energy"
        assert plane.to_dict()[CONF_ACTUAL_ENERGY_ENTITY] == "sensor.m1_energy"

    def test_plane_without_the_field_round_trips_without_the_key(self):
        """Backward compatibility: an existing entry's planes must serialise to
        the exact pre-0.20.5 dict, with no new key appearing."""
        plane = PlaneConfig.from_dict(
            {
                "name": "M1",
                "azimuth_deg": 25.0,
                "tilt_deg": 70.0,
                "wp": 370.0,
                CONF_ACTUAL_ENTITY: "sensor.m1_power",
            }
        )
        assert plane.actual_energy_entity is None
        assert CONF_ACTUAL_ENERGY_ENTITY not in plane.to_dict()


class TestValidation:
    def _site_with(self, value):
        """DEFAULT_SITE with only M1's counter overridden. All eight planes stay
        — the inverter groups reference M2..M8, so dropping them would fail on
        ``group_unknown_plane`` instead of the check under test."""
        planes = list(DEFAULT_SITE[CONF_PLANES])
        planes[0] = {**planes[0], CONF_ACTUAL_ENERGY_ENTITY: value}
        return {**DEFAULT_SITE, CONF_PLANES: planes}

    @pytest.mark.parametrize("bad", ["", "   ", 42, []])
    def test_present_but_blank_is_rejected(self, bad):
        """Silently dropping a fat-fingered value would leave the card on the
        power-mean fallback with no hint why."""
        with pytest.raises(SiteValidationError) as err:
            validate_site(self._site_with(bad))
        assert err.value.code == "actual_energy_entity_empty"

    def test_explicit_null_is_accepted_as_not_set(self):
        site = validate_site(self._site_with(None))
        assert site.planes[0].actual_energy_entity is None

    def test_default_site_validates_and_keeps_its_counters(self):
        site = validate_site(DEFAULT_SITE)
        assert [p.actual_energy_entity for p in site.planes] == [
            "sensor.inverter_port_1_dc_total_energy",
            "sensor.inverter_port_2_dc_total_energy",
            "sensor.inverter_port_1_dc_total_energy_2",
            "sensor.inverter_port_2_dc_total_energy_2",
            "sensor.inverter_port_1_dc_total_energy_3",
            "sensor.inverter_port_2_dc_total_energy_3",
            "sensor.inverter_port_1_dc_total_energy_4",
            "sensor.inverter_port_2_dc_total_energy_4",
        ]

    def test_every_default_plane_pairs_power_with_an_energy_counter(self):
        """Verified against the live install: each counter sits on the SAME
        device as its power sibling, and its daily `change` matches that
        sensor's daily mean x 24 h. Nothing in the entity registry encodes this
        pairing (both ports of an inverter share a device AND a
        translation_key), which is why it is stated explicitly here."""
        for plane in DEFAULT_SITE[CONF_PLANES]:
            power = plane[CONF_ACTUAL_ENTITY]
            energy = plane[CONF_ACTUAL_ENERGY_ENTITY]
            assert power.endswith("_dc_power") or "_dc_power_" in power
            # Same port, same inverter suffix -> only the measurand differs.
            assert energy == power.replace("_dc_power", "_dc_total_energy")
