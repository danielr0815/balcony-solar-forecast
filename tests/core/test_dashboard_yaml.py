"""Validate the observability dashboard YAML (SPEC §14.3).

Pure test (no Home Assistant import): it only needs PyYAML, which ships with
HA and is available in the plain-core environment too. Guards the two things
that make the dashboard a *zero-custom-card* deliverable:

  1. every ``type:`` used is a documented BUILT-IN Lovelace card — a stray
     ``custom:...`` card (or a typo'd built-in) would silently break the
     dashboard on a stock install;
  2. the load-bearing v0.4 scoreboard / quantile / learner / degradation
     entities the SPEC calls out are actually referenced, so a rename in
     sensor.py that forgets the dashboard is caught here.

The YAML is read as UTF-8 (Home Assistant always loads Lovelace YAML as UTF-8;
the file contains ✅/❌ verdict glyphs in a markdown card).
"""

from __future__ import annotations

import json
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

# Repo-root-relative path to the dashboard under test.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DASHBOARD = _REPO_ROOT / "dashboards" / "balcony_solar_forecast.yaml"
_EN_JSON = (
    _REPO_ROOT
    / "custom_components"
    / "balcony_solar_forecast"
    / "translations"
    / "en.json"
)
# The device name the object_ids are prefixed with (has_entity_name=True).
_DEVICE_SLUG = "balcony_solar_forecast"


def _ha_slugify(text: str) -> str:
    """Mirror Home Assistant's object_id slugify (lowercase, alnum runs -> _)."""
    out: list[str] = []
    for ch in text.strip().lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


def _object_id_from_translation(platform: str, translation_key: str) -> str:
    """Expected entity_id for a name-derived (has_entity_name) entity.

    Reads the entity's translation NAME from en.json and slugifies it exactly as
    HA does, prefixed by the device slug — the same derivation HA uses at
    registration. This catches key/name divergence (the bug where the
    translation_key was ``engine_vs_best_baseline_pct`` but the NAME was
    "Engine vs best baseline", yielding the wrong object_id).
    """
    data = json.loads(_EN_JSON.read_text(encoding="utf-8"))
    name = data["entity"][platform][translation_key]["name"]
    return f"{platform}.{_DEVICE_SLUG}_{_ha_slugify(name)}"

# Built-in Lovelace card types this dashboard is allowed to use. Deliberately a
# CLOSED allow-list (not "anything without a custom: prefix") so a typo'd
# built-in like "history_graph" is rejected too. Only the cards this dashboard
# actually needs are listed; extend consciously.
_ALLOWED_CARD_TYPES = frozenset(
    {
        "markdown",
        "entities",
        "history-graph",
        "statistics-graph",
        "gauge",
        "grid",
        "vertical-stack",
        "horizontal-stack",
    }
)

# Section rows inside an entities card are NOT entity references.
_NON_ENTITY_ROW_TYPES = frozenset({"section", "divider"})


@pytest.fixture(scope="module")
def dashboard() -> dict:
    with _DASHBOARD.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    assert isinstance(doc, dict), "dashboard must be a mapping"
    return doc


def _iter_cards(node):
    """Yield every card mapping (recursing into stack/grid ``cards`` lists)."""
    if isinstance(node, dict):
        if "type" in node and (
            "entity" in node
            or "entities" in node
            or "content" in node
            or "cards" in node
            or node.get("type") in ("gauge", "history-graph", "statistics-graph")
        ):
            yield node
        for value in node.values():
            yield from _iter_cards(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_cards(item)


def _iter_entity_ids(node):
    """Yield every ``entity``/``entities`` id referenced anywhere in the tree."""
    if isinstance(node, dict):
        ent = node.get("entity")
        if isinstance(ent, str):
            yield ent
        for value in node.values():
            yield from _iter_entity_ids(value)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, str) and "." in item and " " not in item:
                # bare entity string in an ``entities:`` list
                yield item
            else:
                yield from _iter_entity_ids(item)


def test_dashboard_parses_and_has_one_view(dashboard):
    views = dashboard.get("views")
    assert isinstance(views, list) and views, "expected at least one view"
    cards = views[0].get("cards")
    assert isinstance(cards, list) and cards, "the view must define cards"


def test_only_builtin_card_types(dashboard):
    types = {card["type"] for card in _iter_cards(dashboard)}
    assert types, "no cards discovered — the walker or the YAML is broken"
    unknown = {t for t in types if t not in _ALLOWED_CARD_TYPES}
    assert not unknown, f"non-built-in / unknown card types: {sorted(unknown)}"
    # And explicitly: no custom cards anywhere (belt and braces).
    assert not any(str(t).startswith("custom:") for t in types)


def test_uses_the_five_documented_builtin_cards(dashboard):
    """SPEC §14.3 names history-graph, entities, gauge, markdown (+ statistics)."""
    types = {card["type"] for card in _iter_cards(dashboard)}
    for required in ("markdown", "entities", "history-graph", "gauge"):
        assert required in types, f"missing required built-in card: {required}"


def test_gauge_binds_engine_vs_best_baseline(dashboard):
    """SPEC §14.3: a gauge bound to engine_vs_best_baseline_pct."""
    gauges = [c for c in _iter_cards(dashboard) if c["type"] == "gauge"]
    assert gauges, "expected a gauge card"
    assert any(
        g.get("entity")
        == "sensor.balcony_solar_forecast_engine_vs_best_baseline_pct"
        for g in gauges
    ), "the gauge must bind to engine_vs_best_baseline_pct"


def test_required_scoreboard_and_learner_entities_referenced(dashboard):
    referenced = set(_iter_entity_ids(dashboard))
    required = {
        # scoreboard / kill-gate (SPEC §9/§10/§14.1)
        "binary_sensor.balcony_solar_forecast_kill_gate_passed",
        "sensor.balcony_solar_forecast_engine_daily_kwh_mae",
        "sensor.balcony_solar_forecast_engine_vs_best_baseline_pct",
        # the two documented operator comparisons (SPEC §14.1)
        "sensor.balcony_solar_forecast_comparison_daily_kwh_mae_8_entry_baseline",
        "sensor.balcony_solar_forecast_comparison_daily_kwh_mae_alt_1600w",
        # quantile band sensors (SPEC §6/§14.2)
        "sensor.balcony_solar_forecast_energy_production_today_p10",
        "sensor.balcony_solar_forecast_energy_production_today_p90",
        # forecast total vs measured (SPEC §14.3)
        "sensor.balcony_solar_forecast_energy_production_today",
        # learners / drift / degradation (SPEC §5/§7/§14.3)
        "sensor.balcony_solar_forecast_source_status",
        "binary_sensor.balcony_solar_forecast_degraded",
        "sensor.balcony_solar_forecast_fast_learner_status",
        "sensor.balcony_solar_forecast_shademap_learner_status",
        "sensor.balcony_solar_forecast_drift_mae_corrected",
    }
    missing = required - referenced
    assert not missing, f"dashboard is missing required entities: {sorted(missing)}"


def test_per_module_actual_sensors_referenced(dashboard):
    """SPEC §14.3: measured ground truth = the 8 per-module DC power sensors."""
    referenced = set(_iter_entity_ids(dashboard))
    modules = {
        "sensor.inverter_port_1_dc_power",
        "sensor.inverter_port_2_dc_power",
        "sensor.inverter_port_1_dc_power_2",
        "sensor.inverter_port_2_dc_power_2",
        "sensor.inverter_port_1_dc_power_3",
        "sensor.inverter_port_2_dc_power_3",
        "sensor.inverter_port_1_dc_power_4",
        "sensor.inverter_port_2_dc_power_4",
    }
    # At least the four plane-representative modules must appear (M1/M2/M4/M8);
    # the measured-power graph references all eight.
    present = modules & referenced
    assert len(present) >= 4, f"too few per-module actuals referenced: {present}"


def test_derived_object_ids_match_dashboard(dashboard):
    """The dashboard ids equal the ids HA derives from en.json (no drift).

    Derives the expected entity_ids from the translation NAMES (not hardcoded
    literals), so a future en.json rename that changes the object_id but forgets
    the dashboard is caught here.
    """
    referenced = set(_iter_entity_ids(dashboard))
    for platform, key in (
        ("sensor", "engine_vs_best_baseline_pct"),
        ("sensor", "engine_daily_kwh_mae"),
        ("sensor", "engine_hourly_mae"),
        ("sensor", "energy_production_today_p10"),
        ("sensor", "energy_production_today_p90"),
        ("binary_sensor", "kill_gate_passed"),
    ):
        expected = _object_id_from_translation(platform, key)
        assert expected in referenced, (
            f"dashboard missing derived id {expected} for {platform}.{key}"
        )


def test_comparison_ids_follow_slug_pattern(dashboard):
    """The per-comparison MAE ids follow ``…_comparison_daily_kwh_mae_<slug>``.

    Derived from the comparison-name -> slug pattern (ComparisonConfig.slug and
    the sensor's suggested_object_id), matching the documented operator pair, so
    the dashboard cannot drift from the sensor's actual object_id.
    """
    referenced = set(_iter_entity_ids(dashboard))
    for name in ("8-Entry Baseline", "Alt 1600W"):
        slug = _ha_slugify(name)
        expected = (
            f"sensor.{_DEVICE_SLUG}_comparison_daily_kwh_mae_{slug}"
        )
        assert expected in referenced, (
            f"dashboard missing comparison id {expected} for {name!r}"
        )


def test_shademap_documented_via_dump_service(dashboard):
    """SPEC §14.3: note that dump_shademap yields the full polar data."""
    blobs = [
        c.get("content", "")
        for c in _iter_cards(dashboard)
        if c["type"] == "markdown"
    ]
    joined = "\n".join(blobs)
    assert "dump_shademap" in joined, "must document the dump_shademap service"


def test_comparison_config_example_documented(dashboard):
    """D-P9: the operator's two comparison entities are documented in-dash."""
    joined = "\n".join(
        c.get("content", "")
        for c in _iter_cards(dashboard)
        if c["type"] == "markdown"
    )
    assert "sensor.pv_prognose_heute_alle_module" in joined
    assert "sensor.energy_production_today_4" in joined
