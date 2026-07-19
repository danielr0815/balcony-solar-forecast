"""Tests for the ``install_dashboard`` service (SPEC §14.3).

Two halves:

  * PURE builder tests (``_dashboard.build_dashboard_config`` /
    ``collect_entity_map``): card inventory vs. the shipped YAML, the bundled
    shade-profile card wired to the three real ids, missing-entity omission, and
    the entity-map prefix/disabled/foreign-entry filtering.
  * HANDLER tests driven against fake hass / lovelace / coordinator doubles
    (mirrors ``test_services_learning.py`` + ``test_frontend_resource.py``):
    the safety gate in each case, the storage/YAML/absent-lovelace guards, and
    the response counts.

Needs Home Assistant (the handler imports ServiceValidationError / the lovelace
+ entity-registry helpers); skipped on the plain-core path.
"""

from __future__ import annotations

import json

import homeassistant.helpers.entity_registry as er_mod
import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from balcony_solar_forecast import _dashboard as d  # noqa: E402
from balcony_solar_forecast import _services as svc  # noqa: E402
from balcony_solar_forecast.const import (  # noqa: E402
    CONF_COMPARISON_SENSORS,
    DOMAIN,
    INTEGRATION_VERSION,
    SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX,
    SENSOR_ENERGY_TOMORROW,
)
from homeassistant.components.lovelace.const import (  # noqa: E402
    LOVELACE_DATA,
    ConfigNotFound,
)
from homeassistant.exceptions import (  # noqa: E402
    HomeAssistantError,
    ServiceValidationError,
)

# --------------------------------------------------------------------------
# Pure builder: build_dashboard_config.
# --------------------------------------------------------------------------


def _full_entity_map() -> dict[str, str]:
    """A real entity_id for every integration key the dashboard references."""
    return {key: f"sensor.real_{key}" for key in d.DASHBOARD_ENTITY_KEYS}


def _card_types(config: dict) -> list[str]:
    return [c["type"] for c in config["views"][0]["cards"]]


def test_build_full_inventory_matches_shipped_yaml():
    entity_map = _full_entity_map()
    config = d.build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=[
            ("8-Entry Baseline", "sensor.cmp_a"),
            ("Alt 1600W", "sensor.cmp_b"),
        ],
        measured_entities=[("M1", "sensor.m1"), ("M2", "sensor.m2")],
        version=INTEGRATION_VERSION,
    )
    # Marker present, carries the version.
    assert config[d.MANAGED_MARKER] == INTEGRATION_VERSION
    assert d.is_managed(config)
    # One view mirroring the shipped YAML's single "Forecast" view.
    views = config["views"]
    assert len(views) == 1
    assert views[0]["path"] == "forecast"
    # 12 cards: the shipped YAML's built-in-card inventory MINUS the redundant
    # "Shade profile (per date & module)" entities card (its module/date/fraction
    # controls are embedded in the bundled diagram card) — apexcharts markdown ->
    # bundled shade card, and the measured-DC-power history-graph -> bundled
    # power-history card (one card either way) — MINUS the per-module LTS
    # statistics-graph (the bundled power-history card charts the same daily
    # means of the same sensors, stacked and with the forecast overlay) — PLUS
    # the Phase-4 "DC model & inverter calibration (diagnostic)" entities card.
    cards = views[0]["cards"]
    assert len(cards) == 12
    types = _card_types(config)
    for required in ("markdown", "gauge", "entities", "history-graph"):
        assert required in types
    # The redundant per-module LTS card is not generated (still in the shipped
    # built-ins-only YAML, where the bundled card does not exist).
    assert "statistics-graph" not in types
    # The redundant shade-profile controls entities card is gone from the builder.
    assert not any(
        c.get("title") == "Shade profile (per date & module)" for c in cards
    )
    # The bundled shade-profile card replaces the opt-in apexcharts snippet,
    # wired to the three real ids.
    custom = [c for c in cards if c["type"] == "custom:balcony-shade-profile-card"]
    assert len(custom) == 1
    cc = custom[0]
    assert cc["sensor"] == "sensor.real_shade_profile"
    assert cc["module_select"] == "sensor.real_shade_profile_module"
    assert cc["date_entity"] == "sensor.real_shade_profile_date"
    assert cc["title"]
    # No custom apexcharts card leaks in.
    assert not any(str(t).startswith("custom:apexcharts") for t in types)
    # The gauge binds the vs-best-baseline entity.
    gauge = next(c for c in cards if c["type"] == "gauge")
    assert gauge["entity"] == "sensor.real_vs_best_baseline_pct"
    # The measured-DC-power history-graph is replaced by the bundled
    # power-history card, wired to the measured-total + forecast-today ids.
    power_hist = next(
        c for c in cards if c["type"] == "custom:balcony-power-history-card"
    )
    assert power_hist["total_sensor"] == "sensor.real_measured_dc_power_total"
    assert power_hist["forecast_sensor"] == "sensor.real_energy_production_today"
    # Title spells out the mixed units: measured DC bars vs AC forecast line.
    assert power_hist["title"] == "Production per module (measured DC · forecast AC)"
    # No leftover per-module measured history-graph (its data is in the card).
    assert not any(
        c.get("title", "").startswith("Measured DC power") for c in cards
    )
    # The pointless today-vs-tomorrow juxtaposition is gone: tomorrow's kWh is
    # referenced nowhere in the built config, and the card is retitled.
    assert SENSOR_ENERGY_TOMORROW not in json.dumps(config)
    assert not any(
        c.get("title") == "Forecast (today kWh) vs recent horizon" for c in cards
    )
    assert not any(
        c.get("title") == "Forecast power (time-accurate)" for c in cards
    )
    # The forecast card is now a pure W-vs-W comparison: Forecast AC (power_now)
    # vs Measured. The full map carries the AC meter (measured_ac_power), so the
    # card pairs AC-vs-AC and is retitled "(AC power)".
    fvm = next(
        c for c in cards if c.get("title") == "Forecast vs. measured (AC power)"
    )
    assert [(r["entity"], r["name"]) for r in fvm["entities"]] == [
        ("sensor.real_power_production_now", "Forecast"),
        ("sensor.real_measured_ac_power", "Measured"),
    ]
    # The Phase-4 DC-model diagnostic card: the two DC diagnostic rows + the
    # learned-eta attribute row on the AC power sensor.
    dc = next(
        c
        for c in cards
        if c.get("title") == "DC model & inverter calibration (diagnostic)"
    )
    assert dc["type"] == "entities"
    dc_rows = dc["entities"]
    assert (dc_rows[0]["entity"], dc_rows[0]["name"]) == (
        "sensor.real_power_production_now_dc",
        "Forecast power now (DC model)",
    )
    assert (dc_rows[1]["entity"], dc_rows[1]["name"]) == (
        "sensor.real_energy_production_today_dc",
        "Forecast energy today (DC model)",
    )
    eta_row = dc_rows[2]
    assert eta_row["type"] == "attribute"
    assert eta_row["entity"] == "sensor.real_power_production_now"
    assert eta_row["attribute"] == "inverter_efficiency_learned"
    # The shademap markdown no longer hardcodes the reference site's obstructions.
    shademap = next(
        c
        for c in cards
        if c["type"] == "markdown" and c.get("title", "").startswith("Shademap")
    )
    assert "East hill" not in shademap["content"]
    assert "dump_shademap" in shademap["content"]  # the how-to is kept
    # Comparison rows carried through into the scoreboard card.
    scoreboard = next(c for c in cards if c.get("title") == "Skill scoreboard")
    labels = [r.get("label") for r in scoreboard["entities"] if "label" in r]
    assert "Comparison baselines (daily-kWh MAE)" in labels
    cmp_ids = [r["entity"] for r in scoreboard["entities"] if "entity" in r]
    assert "sensor.cmp_a" in cmp_ids and "sensor.cmp_b" in cmp_ids
    # Full map -> nothing missing.
    assert d.missing_entity_keys(entity_map) == []


def test_build_omits_missing_entities():
    entity_map = _full_entity_map()
    # Drop the two entities that gate whole cards + one entities-card row.
    for gone in (
        "vs_best_baseline_pct",  # gauge + a scoreboard row + kill-gate markdown
        "kill_gate_passed",  # kill-gate markdown + a scoreboard row
        "drift_mae_corrected",  # the drift-trend history-graph + a learner row
        "shade_profile_date",  # the bundled custom card (needs all three ids)
    ):
        entity_map.pop(gone)
    config = d.build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=[],
        measured_entities=[],  # no measured cards either
        version="1.2.3",
    )
    types = _card_types(config)
    # Whole cards dropped.
    assert "gauge" not in types
    assert "custom:balcony-shade-profile-card" not in types
    assert "statistics-graph" not in types  # never generated at all
    assert types.count("history-graph") == 1  # forecast graph only; drift + measured gone
    # The kill-gate markdown is gone but the two static markdown cards remain.
    markdowns = [
        c for c in config["views"][0]["cards"] if c["type"] == "markdown"
    ]
    assert all(c["title"] != "Kill-gate verdict" for c in markdowns)
    assert len(markdowns) == 2
    # Scoreboard survived with only its still-present rows (no comparison section).
    scoreboard = next(
        c for c in config["views"][0]["cards"] if c.get("title") == "Skill scoreboard"
    )
    names = [r.get("name") for r in scoreboard["entities"]]
    assert "Forecast daily-kWh MAE" in names
    assert "Forecast vs best baseline" not in names
    assert not any(r.get("type") == "section" for r in scoreboard["entities"])
    # Still a valid, marker-bearing shape.
    assert d.is_managed(config)
    assert d.config_has_cards(config)
    assert set(d.missing_entity_keys(entity_map)) == {
        "vs_best_baseline_pct",
        "kill_gate_passed",
        "drift_mae_corrected",
        "shade_profile_date",
    }


def test_build_no_comparisons_drops_section():
    config = d.build_dashboard_config(
        entity_map=_full_entity_map(),
        comparison_slugs=[],
        measured_entities=[],
        version="0.0.0",
    )
    scoreboard = next(
        c for c in config["views"][0]["cards"] if c.get("title") == "Skill scoreboard"
    )
    assert not any(r.get("type") == "section" for r in scoreboard["entities"])


def test_build_measured_cards_use_measured_entities():
    config = d.build_dashboard_config(
        entity_map=_full_entity_map(),
        comparison_slugs=[],
        measured_entities=[("M1", "sensor.a"), ("M2", "sensor.b"), ("M3", "sensor.c")],
        version="0.0.0",
    )
    cards = config["views"][0]["cards"]
    # With the measured-total sensor present, the per-module view is the bundled
    # power-history card (it reads its own module list + hourly LTS), not a
    # per-module history-graph.
    power_hist = next(
        c for c in cards if c["type"] == "custom:balcony-power-history-card"
    )
    assert power_hist["total_sensor"] == "sensor.real_measured_dc_power_total"
    assert power_hist["forecast_sensor"] == "sensor.real_energy_production_today"
    assert not any(
        c.get("title", "").startswith("Measured DC power") for c in cards
    )
    # No LTS statistics-graph alongside it: the bundled card supersedes it.
    assert not any(c["type"] == "statistics-graph" for c in cards)


def test_build_measured_power_falls_back_to_history_graph():
    """No measured-total sensor in the map (registered-but-disabled) but the site
    still has actual_entity planes -> keep the OLD per-module history-graph so a
    partial install still renders a measured view."""
    entity_map = _full_entity_map()
    entity_map.pop("measured_dc_power_total")
    config = d.build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=[],
        measured_entities=[("M1", "sensor.a"), ("M2", "sensor.b")],
        version="0.0.0",
    )
    cards = config["views"][0]["cards"]
    # The bundled power-history card is gated on the measured-total id: absent.
    assert not any(
        c["type"] == "custom:balcony-power-history-card" for c in cards
    )
    # The fallback per-module history-graph carries plane-name labels.
    hist = next(
        c for c in cards if c.get("title", "").startswith("Measured DC power")
    )
    assert [(r["entity"], r["name"]) for r in hist["entities"]] == [
        ("sensor.a", "M1"),
        ("sensor.b", "M2"),
    ]
    # Still no LTS statistics-graph on the fallback path either.
    assert not any(c["type"] == "statistics-graph" for c in cards)


def test_forecast_card_survives_without_measured_row():
    """No measured sensor at all (no AC meter, no actual_entity) -> the forecast
    card keeps its single Forecast row rather than being dropped, retitled to the
    DC-fallback title."""
    entity_map = _full_entity_map()
    entity_map.pop("measured_ac_power")
    entity_map.pop("measured_dc_power_total")
    config = d.build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=[],
        measured_entities=[],
        version="0.0.0",
    )
    card = next(
        c
        for c in config["views"][0]["cards"]
        if c.get("title") == "Forecast vs. measured (DC power)"
    )
    assert [(r["entity"], r["name"]) for r in card["entities"]] == [
        ("sensor.real_power_production_now", "Forecast"),
    ]


def test_forecast_card_dc_fallback_without_ac_meter():
    """No AC meter but a DC-total sensor -> the card pairs the AC forecast with
    the DC total and is retitled "(DC power)"."""
    entity_map = _full_entity_map()
    entity_map.pop("measured_ac_power")
    config = d.build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=[],
        measured_entities=[],
        version="0.0.0",
    )
    card = next(
        c
        for c in config["views"][0]["cards"]
        if c.get("title") == "Forecast vs. measured (DC power)"
    )
    assert [(r["entity"], r["name"]) for r in card["entities"]] == [
        ("sensor.real_power_production_now", "Forecast"),
        ("sensor.real_measured_dc_power_total", "Measured"),
    ]


def test_dc_diagnostics_card_gated_on_dc_sensors():
    """The DC-model diagnostic card is dropped when its two DC diagnostic sensors
    are absent, present (with the learned-eta attribute row) when they exist."""
    # Present in the full map.
    full = d.build_dashboard_config(
        entity_map=_full_entity_map(),
        comparison_slugs=[],
        measured_entities=[],
        version="0.0.0",
    )
    assert any(
        c.get("title") == "DC model & inverter calibration (diagnostic)"
        for c in full["views"][0]["cards"]
    )
    # Drop both DC diagnostic sensors -> the whole card is omitted.
    entity_map = _full_entity_map()
    entity_map.pop("power_production_now_dc")
    entity_map.pop("energy_production_today_dc")
    gone = d.build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=[],
        measured_entities=[],
        version="0.0.0",
    )
    assert not any(
        c.get("title") == "DC model & inverter calibration (diagnostic)"
        for c in gone["views"][0]["cards"]
    )


def test_forecast_card_omitted_without_power_now():
    """The forecast-vs-measured card is gated on power_production_now: absent
    that row, the whole card is omitted (even if the measured sensor exists)."""
    entity_map = _full_entity_map()
    entity_map.pop("power_production_now")
    config = d.build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=[],
        measured_entities=[],
        version="0.0.0",
    )
    assert not any(
        str(c.get("title", "")).startswith("Forecast vs. measured")
        for c in config["views"][0]["cards"]
    )


# --------------------------------------------------------------------------
# Pure builder: collect_entity_map.
# --------------------------------------------------------------------------


class _RegEntry:
    def __init__(self, unique_id, entity_id, disabled_by=None):
        self.unique_id = unique_id
        self.entity_id = entity_id
        self.disabled_by = disabled_by


def test_collect_entity_map_prefix_disabled_foreign():
    entries = [
        _RegEntry("e1_energy_production_today", "sensor.today"),
        _RegEntry("e1_degraded", "binary_sensor.degraded"),
        # Disabled -> skipped (no live entity to bind).
        _RegEntry("e1_hourly_mae", "sensor.hourly", disabled_by="user"),
        # Foreign entry_id prefix -> skipped.
        _RegEntry("e2_energy_production_today", "sensor.other_today"),
        # Missing/blank entity_id -> skipped.
        _RegEntry("e1_source_status", ""),
        # Non-string unique_id -> skipped.
        _RegEntry(None, "sensor.junk"),
    ]
    out = d.collect_entity_map(entries, "e1")
    assert out == {
        "energy_production_today": "sensor.today",
        "degraded": "binary_sensor.degraded",
    }
    assert "hourly_mae" not in out
    assert "energy_production_today" in out and out["energy_production_today"] != "sensor.other_today"


# --------------------------------------------------------------------------
# Handler fakes.
# --------------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, entry_id="e1", data=None, options=None):
        self.entry_id = entry_id
        self.data = data or {}
        self.options = options or {}


class _FakePlane:
    def __init__(self, actual_entity):
        self.actual_entity = actual_entity


class _FakeSite:
    def __init__(self, planes):
        self.planes = tuple(planes)


class _FakeCoordinator:
    def __init__(self, *, entry=None, planes=()):
        self.entry = entry if entry is not None else _FakeEntry()
        self._site = _FakeSite([_FakePlane(p) for p in planes])


class _FakeDash:
    """Minimal LovelaceStorage stand-in recording the saved config."""

    def __init__(self, *, mode="storage", existing=None, load_missing=False,
                 save_error=None):
        self.mode = mode
        self._existing = existing
        self._load_missing = load_missing
        self._save_error = save_error
        self.saved = None

    async def async_load(self, force):
        if self._load_missing:
            raise ConfigNotFound
        return self._existing

    async def async_save(self, config):
        if self._save_error is not None:
            raise self._save_error
        self.saved = config


class _FakeLovelace:
    def __init__(self, dashboards):
        self.dashboards = dashboards


class _FakeHass:
    def __init__(self, store, lovelace):
        self.data = {DOMAIN: store}
        if lovelace is not None:
            self.data[LOVELACE_DATA] = lovelace


class _Call:
    def __init__(self, data):
        self.data = data


def _patch_registry(monkeypatch, entity_map_entries):
    """Make the real entity_registry helpers return controlled entries."""
    monkeypatch.setattr(er_mod, "async_get", lambda hass: object())
    monkeypatch.setattr(
        er_mod,
        "async_entries_for_config_entry",
        lambda registry, entry_id: list(entity_map_entries),
    )


def _registry_for_all_keys(entry_id="e1", comparisons=()):
    """Registry entries covering every dashboard key + given comparison slugs."""
    entries = [
        _RegEntry(f"{entry_id}_{key}", f"sensor.{key}")
        for key in d.DASHBOARD_ENTITY_KEYS
    ]
    for slug in comparisons:
        entries.append(
            _RegEntry(
                f"{entry_id}_{SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX}_{slug}",
                f"sensor.cmp_{slug}",
            )
        )
    return entries


# --------------------------------------------------------------------------
# Handler: guards.
# --------------------------------------------------------------------------


async def test_lovelace_absent_errors():
    hass = _FakeHass({"e1": _FakeCoordinator()}, lovelace=None)
    with pytest.raises(ServiceValidationError, match="Lovelace is not set up"):
        await svc._handle_install_dashboard(hass, _Call({}))


async def test_unknown_dashboard_lists_available_and_hint():
    lovelace = _FakeLovelace({"other-dash": _FakeDash()})
    hass = _FakeHass({"e1": _FakeCoordinator()}, lovelace=lovelace)
    with pytest.raises(ServiceValidationError) as exc:
        await svc._handle_install_dashboard(hass, _Call({"dashboard": "balcony-solar"}))
    msg = str(exc.value)
    assert "balcony-solar" in msg  # the missing url + the creation hint
    assert "Settings → Dashboards → Add dashboard" in msg
    assert "other-dash" in msg  # available storage dashboards listed
    assert "hyphen" in msg


async def test_yaml_mode_dashboard_errors():
    lovelace = _FakeLovelace({"balcony-solar": _FakeDash(mode="yaml")})
    hass = _FakeHass({"e1": _FakeCoordinator()}, lovelace=lovelace)
    with pytest.raises(ServiceValidationError, match="YAML-managed"):
        await svc._handle_install_dashboard(hass, _Call({"dashboard": "balcony-solar"}))


# --------------------------------------------------------------------------
# Handler: the safety gate.
# --------------------------------------------------------------------------


async def test_foreign_nonempty_without_overwrite_refused(monkeypatch):
    foreign = {"views": [{"cards": [{"type": "markdown", "content": "mine"}]}]}
    dash = _FakeDash(existing=foreign)
    hass = _FakeHass(
        {"e1": _FakeCoordinator()}, lovelace=_FakeLovelace({"balcony-solar": dash})
    )
    _patch_registry(monkeypatch, _registry_for_all_keys())
    with pytest.raises(ServiceValidationError, match="did not create"):
        await svc._handle_install_dashboard(hass, _Call({"dashboard": "balcony-solar"}))
    assert dash.saved is None  # nothing written


async def test_foreign_nonempty_with_overwrite_saved(monkeypatch):
    foreign = {"views": [{"cards": [{"type": "markdown", "content": "mine"}]}]}
    dash = _FakeDash(existing=foreign)
    hass = _FakeHass(
        {"e1": _FakeCoordinator()}, lovelace=_FakeLovelace({"balcony-solar": dash})
    )
    _patch_registry(monkeypatch, _registry_for_all_keys())
    resp = await svc._handle_install_dashboard(
        hass, _Call({"dashboard": "balcony-solar", "overwrite": True})
    )
    assert dash.saved is not None
    assert d.is_managed(dash.saved)
    assert resp["result"]["dashboard"] == "balcony-solar"


async def test_marker_bearing_refreshed_without_overwrite(monkeypatch):
    managed = {
        d.MANAGED_MARKER: "0.6.0",
        "views": [{"cards": [{"type": "markdown", "content": "old"}]}],
    }
    dash = _FakeDash(existing=managed)
    hass = _FakeHass(
        {"e1": _FakeCoordinator()}, lovelace=_FakeLovelace({"balcony-solar": dash})
    )
    _patch_registry(monkeypatch, _registry_for_all_keys())
    resp = await svc._handle_install_dashboard(hass, _Call({"dashboard": "balcony-solar"}))
    # Idempotent refresh: overwritten freely, marker re-stamped with our version.
    assert dash.saved is not None
    assert dash.saved[d.MANAGED_MARKER] == INTEGRATION_VERSION
    assert resp["result"]["views"] == 1


async def test_empty_dashboard_saved_with_counts(monkeypatch):
    dash = _FakeDash(load_missing=True)  # ConfigNotFound -> empty
    coord = _FakeCoordinator(
        entry=_FakeEntry(
            options={
                CONF_COMPARISON_SENSORS: [
                    {"name": "8-Entry Baseline", "daily_entity": "sensor.ext_a"},
                ]
            }
        ),
        planes=["sensor.inv_1", "sensor.inv_2"],
    )
    hass = _FakeHass(
        {"e1": coord}, lovelace=_FakeLovelace({"balcony-solar": dash})
    )
    _patch_registry(
        monkeypatch, _registry_for_all_keys(comparisons=("8_entry_baseline",))
    )
    resp = await svc._handle_install_dashboard(hass, _Call({"dashboard": "balcony-solar"}))
    assert dash.saved is not None
    result = resp["result"]
    assert result["dashboard"] == "balcony-solar"
    assert result["views"] == 1
    assert result["cards"] > 0
    assert result["missing_entities"] == []  # all keys registered
    # The configured comparison made it into the config.
    cards = dash.saved["views"][0]["cards"]
    all_ids = _all_entity_ids(cards)
    assert "sensor.cmp_8_entry_baseline" in all_ids
    # The per-plane ids are NOT enumerated anywhere on this path (0.20.6): the
    # bundled power-history card resolves its module list at runtime from the
    # measured-total sensor's `sources`, and the LTS statistics-graph that used
    # to list them is no longer generated. They are still spelled out on the
    # history-graph fallback path — see
    # test_build_measured_power_falls_back_to_history_graph.
    assert "sensor.inv_1" not in all_ids and "sensor.inv_2" not in all_ids
    power_hist = next(
        c for c in cards if c["type"] == "custom:balcony-power-history-card"
    )
    assert power_hist["total_sensor"] == "sensor.measured_dc_power_total"


async def test_empty_dashboard_reports_missing_keys(monkeypatch):
    dash = _FakeDash(load_missing=True)
    hass = _FakeHass(
        {"e1": _FakeCoordinator()}, lovelace=_FakeLovelace({"balcony-solar": dash})
    )
    # Registry has only a couple of keys -> the rest are reported missing.
    partial = [
        _RegEntry("e1_energy_production_today", "sensor.today"),
        _RegEntry("e1_degraded", "binary_sensor.degraded"),
    ]
    _patch_registry(monkeypatch, partial)
    resp = await svc._handle_install_dashboard(hass, _Call({"dashboard": "balcony-solar"}))
    missing = resp["result"]["missing_entities"]
    assert "vs_best_baseline_pct" in missing
    assert "energy_production_today" not in missing
    assert dash.saved is not None


async def test_save_error_surfaced_as_validation_error(monkeypatch):
    dash = _FakeDash(load_missing=True, save_error=HomeAssistantError("recovery mode"))
    hass = _FakeHass(
        {"e1": _FakeCoordinator()}, lovelace=_FakeLovelace({"balcony-solar": dash})
    )
    _patch_registry(monkeypatch, _registry_for_all_keys())
    with pytest.raises(ServiceValidationError, match="Could not write dashboard"):
        await svc._handle_install_dashboard(hass, _Call({"dashboard": "balcony-solar"}))


def _all_entity_ids(cards):
    ids = set()
    for card in cards:
        ent = card.get("entity")
        if isinstance(ent, str):
            ids.add(ent)
        for row in card.get("entities", []) or []:
            if isinstance(row, str):
                ids.add(row)
            elif isinstance(row, dict) and isinstance(row.get("entity"), str):
                ids.add(row["entity"])
        for key in ("sensor", "module_select", "date_entity"):
            val = card.get(key)
            if isinstance(val, str):
                ids.add(val)
    return ids
