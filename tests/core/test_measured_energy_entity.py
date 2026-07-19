"""The measured-per-module LTS statistics-graph card (SPEC §14.3).

Pure test: ``_dashboard`` imports only ``const``, so the card builder is
reachable without Home Assistant.

Regression origin: the card asked for ``stat_types: [sum]`` on the planes'
``actual_entity`` POWER sensors. The recorder keeps mean/min/max for a
``state_class: measurement`` sensor and reports ``has_sum: false``, so the card
had no series to draw and rendered as an empty plot area — the measured
production looked lost while 14 days of daily statistics were present.
"""

from __future__ import annotations

from balcony_solar_forecast import _dashboard as d

_POWER = [("M1", "sensor.m1_power"), ("M2", "sensor.m2_power")]


def _lts(power):
    cards: list[dict] = []
    d._add_measured_lts(cards, power)
    return cards


class TestLtsCard:
    def test_never_asks_for_sum_on_power_sensors(self):
        """THE regression: power sensors have no sum, so `sum` draws nothing."""
        card = _lts(_POWER)[0]
        assert card["stat_types"] == ["mean"]
        assert "sum" not in card["stat_types"]

    def test_keeps_bare_entity_ids_and_says_what_it_charts(self):
        card = _lts(_POWER)[0]
        assert card["entities"] == ["sensor.m1_power", "sensor.m2_power"]
        assert card["title"] == "Measured mean DC power per module (LTS)"

    def test_no_measured_entities_emits_no_card(self):
        assert _lts([]) == []

    def test_card_stays_a_builtin_daily_bar_chart(self):
        card = _lts(_POWER)[0]
        assert card["type"] == "statistics-graph"
        assert card["chart_type"] == "bar"
        assert card["period"] == "day"
        assert card["days_to_show"] == 14
