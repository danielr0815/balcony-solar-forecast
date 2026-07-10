"""Pure builder for the generated observability Lovelace dashboard (SPEC §14.3).

The ``install_dashboard`` service (see :mod:`._services`) writes a full Lovelace
config into a UI-created (empty) dashboard, wiring every card to the INSTALL's
REAL entity ids instead of the reference install's hardcoded object_ids. This
module holds the two PURE, Home-Assistant-free functions that do the shaping so
they unit-test bare (the service glue that resolves the entity registry / the
lovelace collection lives in ``_services.py``):

  * :func:`collect_entity_map` — turn a config entry's registry entries into a
    ``{key: entity_id}`` map (``key`` = the unique_id suffix, i.e. the stable
    ``f"{entry_id}_{key}"`` contract every entity uses — NOT the name-derived
    object_id, which diverges, e.g. ``learner_status_fast`` →
    ``sensor.…_fast_learner_status``).
  * :func:`build_dashboard_config` — mirror ``dashboards/balcony_solar_forecast``
    ``.yaml`` (its single view + card inventory), substituting the real ids. A
    card whose entity is absent from the map is OMITTED (a partial install still
    renders); an entities-card drops only the missing row. The opt-in HACS
    apexcharts snippet is replaced by the bundled ``custom:balcony-shade-profile``
    ``-card`` shipped with the integration.

Both functions stay import-light: only :mod:`.const` (itself HA-free) is
imported, never ``sensor.py`` (which pulls in Home Assistant). The handful of
diagnostic keys that live in ``sensor.py`` are mirrored here as literals.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .const import (
    BINARY_SENSOR_DEGRADED,
    BINARY_SENSOR_KILL_GATE_PASSED,
    DATE_SHADE_PROFILE_DATE,
    SELECT_SHADE_PROFILE_MODULE,
    SENSOR_DRIFT_MAE_CORRECTED,
    SENSOR_ENERGY_TODAY,
    SENSOR_ENERGY_TODAY_P10,
    SENSOR_ENERGY_TODAY_P90,
    SENSOR_FORECAST_DAILY_KWH_MAE,
    SENSOR_FORECAST_HOURLY_MAE,
    SENSOR_FORECAST_VS_BEST_BASELINE_PCT,
    SENSOR_INTRADAY_SCALAR,
    SENSOR_MEASURED_DC_TOTAL,
    SENSOR_POWER_NOW,
    SENSOR_SHADE_PROFILE,
)

# Diagnostic sensor keys OWNED by sensor.py (unique_id suffix == translation_key).
# Mirrored here as literals because the builder must not import sensor.py (which
# imports Home Assistant); kept in sync by the dashboard tests.
SENSOR_SOURCE_STATUS = "source_status"
SENSOR_LAST_FETCH_AGE = "last_fetch_age_min"
SENSOR_LEARNER_STATUS_FAST = "learner_status_fast"
SENSOR_LEARNER_STATUS_SLOW = "learner_status_slow"
SENSOR_LEARNER_STATUS_DAY_AHEAD = "learner_status_day_ahead"

# The marker key stamped at the top of every config this service writes. Its
# PRESENCE is the idempotent-refresh / safe-overwrite signal (the safety gate
# refuses to clobber a non-empty dashboard that LACKS it); its VALUE is the
# integration version that generated the config.
MANAGED_MARKER = "bsf_managed"

# The bundled shade-profile card (served + auto-registered by _frontend.py). It
# REPLACES the opt-in HACS apexcharts snippet in the generated dashboard.
_SHADE_PROFILE_CARD = "custom:balcony-shade-profile-card"
# The bundled power-history card (served + auto-registered by _frontend.py). It
# REPLACES the messy per-module "Measured DC power" history-graph with stacked
# hourly production bars per module + a dashed forecast line.
_POWER_HISTORY_CARD = "custom:balcony-power-history-card"

# Every INTEGRATION-OWNED entity key the dashboard can reference (the comparison
# MAE rows + the measured-power ids are dynamic and handled separately). A key
# absent from the entity_map is reported in the response's ``missing_entities``
# and its card/row is omitted.
DASHBOARD_ENTITY_KEYS: tuple[str, ...] = (
    BINARY_SENSOR_KILL_GATE_PASSED,
    SENSOR_FORECAST_VS_BEST_BASELINE_PCT,
    SENSOR_FORECAST_DAILY_KWH_MAE,
    SENSOR_FORECAST_HOURLY_MAE,
    SENSOR_ENERGY_TODAY,
    SENSOR_POWER_NOW,
    SENSOR_MEASURED_DC_TOTAL,
    SENSOR_ENERGY_TODAY_P10,
    SENSOR_ENERGY_TODAY_P90,
    SENSOR_SOURCE_STATUS,
    BINARY_SENSOR_DEGRADED,
    SENSOR_LAST_FETCH_AGE,
    SENSOR_LEARNER_STATUS_FAST,
    SENSOR_LEARNER_STATUS_SLOW,
    SENSOR_LEARNER_STATUS_DAY_AHEAD,
    SENSOR_INTRADAY_SCALAR,
    SENSOR_DRIFT_MAE_CORRECTED,
    SENSOR_SHADE_PROFILE,
    SELECT_SHADE_PROFILE_MODULE,
    DATE_SHADE_PROFILE_DATE,
)


# ---------------------------------------------------------------------------
# Entity-map collection (pure).
# ---------------------------------------------------------------------------


def collect_entity_map(
    registry_entries: Iterable[Any], entry_id: str
) -> dict[str, str]:
    """Build ``{key: entity_id}`` for one config entry's registry entries.

    ``key`` is the unique_id with the ``f"{entry_id}_"`` prefix stripped — the
    stable contract every entity sets (``BalconyForecastEntity.__init__``), which
    is what :func:`build_dashboard_config` looks entities up by. Entries whose
    unique_id does not carry the prefix (foreign entry), or that are disabled,
    are skipped; a disabled entity has no live entity to bind a card to.
    """
    prefix = f"{entry_id}_"
    out: dict[str, str] = {}
    for entry in registry_entries:
        uid = getattr(entry, "unique_id", None)
        if not isinstance(uid, str) or not uid.startswith(prefix):
            continue
        if getattr(entry, "disabled_by", None) is not None:
            continue
        entity_id = getattr(entry, "entity_id", None)
        if not isinstance(entity_id, str) or not entity_id:
            continue
        out[uid[len(prefix):]] = entity_id
    return out


def missing_entity_keys(entity_map: dict[str, str]) -> list[str]:
    """The dashboard entity keys absent from ``entity_map`` (sorted, for the
    service response's ``missing_entities``)."""
    return sorted(k for k in DASHBOARD_ENTITY_KEYS if k not in entity_map)


# ---------------------------------------------------------------------------
# Config-shape helpers (pure).
# ---------------------------------------------------------------------------


def config_has_cards(config: Any) -> bool:
    """True if ``config`` is a Lovelace config with at least one card.

    The safety gate uses this to distinguish an EMPTY dashboard (freely
    overwritten) from one already carrying content.
    """
    if not isinstance(config, dict):
        return False
    views = config.get("views")
    if not isinstance(views, list):
        return False
    return any(isinstance(view, dict) and view.get("cards") for view in views)


def is_managed(config: Any) -> bool:
    """True if ``config`` carries our :data:`MANAGED_MARKER` (we wrote it)."""
    return isinstance(config, dict) and MANAGED_MARKER in config


# ---------------------------------------------------------------------------
# The builder (pure).
# ---------------------------------------------------------------------------


def build_dashboard_config(
    *,
    entity_map: dict[str, str],
    comparison_slugs: list[tuple[str, str]],
    measured_entities: list[tuple[str, str]],
    version: str,
) -> dict[str, Any]:
    """Assemble the full Lovelace config, mirroring the shipped YAML.

    ``entity_map`` maps entity KEY (unique_id suffix) → real entity_id;
    ``comparison_slugs`` is ``[(name, entity_id), ...]`` for the configured
    comparison MAE sensors; ``measured_entities`` is ``[(plane_name,
    entity_id), ...]`` for the planes' measured DC-power sensors — the
    ``plane_name`` becomes each row's label so the graph reads M1…M8 instead
    of the sensors' ambiguous own friendly names. ``version`` stamps the
    :data:`MANAGED_MARKER`.

    A card referencing an entity missing from ``entity_map`` is omitted; an
    entities-card drops just that row. Returns
    ``{MANAGED_MARKER: version, "title": ..., "views": [one view]}``.
    """
    cards: list[dict[str, Any]] = []

    _add_kill_gate_verdict(cards, entity_map)
    _add_vs_best_gauge(cards, entity_map)
    _add_scoreboard(cards, entity_map, comparison_slugs)
    _add_forecast_history(cards, entity_map)
    _add_measured_power(cards, entity_map, measured_entities)
    _add_measured_lts(cards, measured_entities)
    _add_forecast_band(cards, entity_map)
    _add_learners(cards, entity_map)
    _add_drift_trend(cards, entity_map)
    _add_shademap_markdown(cards)
    _add_shade_profile_card(cards, entity_map)
    _add_comparison_reminder(cards)

    view = {
        "title": "Forecast",
        "path": "forecast",
        "icon": "mdi:solar-power",
        "cards": cards,
    }
    return {MANAGED_MARKER: version, "title": "Balcony Solar Forecast", "views": [view]}


# ---------------------------------------------------------------------------
# Per-card builders.
# ---------------------------------------------------------------------------


def _row(entity_map: dict[str, str], key: str, name: str) -> dict[str, Any] | None:
    """An entities-card row for ``key``, or None if that entity is absent."""
    entity_id = entity_map.get(key)
    if entity_id is None:
        return None
    return {"entity": entity_id, "name": name}


def _has_entity_row(rows: list[dict[str, Any]]) -> bool:
    return any("entity" in row for row in rows)


def _add_kill_gate_verdict(
    cards: list[dict[str, Any]], entity_map: dict[str, str]
) -> None:
    """Kill-gate verdict markdown (SPEC §9/§10) — templated on the real ids."""
    kill = entity_map.get(BINARY_SENSOR_KILL_GATE_PASSED)
    pct = entity_map.get(SENSOR_FORECAST_VS_BEST_BASELINE_PCT)
    if kill is None or pct is None:
        return
    # Placeholder tokens avoid escaping the Jinja braces in this file.
    content = _KILL_GATE_TEMPLATE.replace("__KILL__", kill).replace("__PCT__", pct)
    cards.append({"type": "markdown", "title": "Kill-gate verdict", "content": content})


def _add_vs_best_gauge(
    cards: list[dict[str, Any]], entity_map: dict[str, str]
) -> None:
    """Forecast-vs-best-baseline gauge (SPEC §9); positive = forecast better."""
    entity_id = entity_map.get(SENSOR_FORECAST_VS_BEST_BASELINE_PCT)
    if entity_id is None:
        return
    cards.append(
        {
            "type": "gauge",
            "name": "Forecast vs best baseline",
            "entity": entity_id,
            "unit": "%",
            "min": -50,
            "max": 50,
            "needle": True,
            "segments": [
                {"from": -50, "color": "#c0392b"},
                {"from": -10, "color": "#e67e22"},
                {"from": 0, "color": "#f1c40f"},
                {"from": 10, "color": "#2ecc71"},
            ],
        }
    )


def _add_scoreboard(
    cards: list[dict[str, Any]],
    entity_map: dict[str, str],
    comparison_slugs: list[tuple[str, str]],
) -> None:
    """Skill scoreboard (SPEC §9/§10): forecast MAE + per-comparison MAE."""
    rows: list[dict[str, Any]] = []
    for key, name in (
        (BINARY_SENSOR_KILL_GATE_PASSED, "Kill-gate passed"),
        (SENSOR_FORECAST_DAILY_KWH_MAE, "Forecast daily-kWh MAE"),
        (SENSOR_FORECAST_HOURLY_MAE, "Forecast hourly MAE"),
        (SENSOR_FORECAST_VS_BEST_BASELINE_PCT, "Forecast vs best baseline"),
    ):
        row = _row(entity_map, key, name)
        if row is not None:
            rows.append(row)
    if comparison_slugs:
        rows.append(
            {"type": "section", "label": "Comparison baselines (daily-kWh MAE)"}
        )
        for name, entity_id in comparison_slugs:
            rows.append({"entity": entity_id, "name": name})
    if _has_entity_row(rows):
        cards.append(
            {
                "type": "entities",
                "title": "Skill scoreboard",
                "show_header_toggle": False,
                "entities": rows,
            }
        )


def _add_forecast_history(
    cards: list[dict[str, Any]], entity_map: dict[str, str]
) -> None:
    """Forecast-vs-measured SITE POWER comparison history-graph (SPEC §14.3).

    A pure power comparison on ONE y-scale: the instantaneous forecast power
    (``power_production_now``) against the measured site-total DC power
    (``measured_dc_power_total``, the live sum of the per-module sensors). The
    today-kWh row that used to share this card is gone — mixing kWh and W on one
    axis is unreadable; the daily-kWh story lives in the band card + scoreboard.

    Gated on the forecast-power row: the card is omitted entirely only when
    ``power_production_now`` is absent. When just the measured-total sensor is
    missing (no plane has an ``actual_entity``, so the summing sensor was never
    created) the forecast-only row survives.
    """
    forecast_row = _row(entity_map, SENSOR_POWER_NOW, "Forecast")
    if forecast_row is None:
        return
    rows = [forecast_row]
    measured_row = _row(entity_map, SENSOR_MEASURED_DC_TOTAL, "Measured")
    if measured_row is not None:
        rows.append(measured_row)
    cards.append(
        {
            "type": "history-graph",
            "title": "Forecast vs. measured (site power)",
            "hours_to_show": 72,
            "entities": rows,
        }
    )


def _add_measured_power(
    cards: list[dict[str, Any]],
    entity_map: dict[str, str],
    measured_entities: list[tuple[str, str]],
) -> None:
    """Measured production per module (ground truth) — the bundled power card.

    When the measured-total sensor exists (at least one plane has an
    ``actual_entity``), use the bundled ``balcony-power-history-card``: stacked
    hourly production bars per module (M1…M8) + a dashed forecast line, reading
    the module list + hourly LTS itself. It REPLACES the messy 8-line
    per-module history-graph.

    Fallback: when the measured-total sensor is ABSENT from the map (e.g. the
    entity is registered-but-disabled while the site still has ``actual_entity``
    planes), keep the OLD per-module ``history-graph`` (labelled M1…M8) so a
    partial install still renders a measured view.
    """
    total = entity_map.get(SENSOR_MEASURED_DC_TOTAL)
    if total is not None:
        card: dict[str, Any] = {
            "type": _POWER_HISTORY_CARD,
            "total_sensor": total,
            "title": "Hourly production per module",
        }
        forecast = entity_map.get(SENSOR_ENERGY_TODAY)
        if forecast is not None:
            card["forecast_sensor"] = forecast
        cards.append(card)
        return

    if not measured_entities:
        return
    cards.append(
        {
            "type": "history-graph",
            "title": "Measured DC power per module (ground truth)",
            "hours_to_show": 48,
            "entities": [
                {"entity": eid, "name": name} for name, eid in measured_entities
            ],
        }
    )


def _add_measured_lts(
    cards: list[dict[str, Any]], measured_entities: list[tuple[str, str]]
) -> None:
    """Measured daily energy per module (LTS) statistics-graph (SPEC §14.3)."""
    if not measured_entities:
        return
    cards.append(
        {
            "type": "statistics-graph",
            "title": "Measured daily energy per module (LTS)",
            "period": "day",
            "stat_types": ["sum"],
            "chart_type": "bar",
            "days_to_show": 14,
            "entities": [eid for _name, eid in measured_entities],
        }
    )


def _add_forecast_band(
    cards: list[dict[str, Any]], entity_map: dict[str, str]
) -> None:
    """Today's P10 / P50 / P90 band (SPEC §6/§14.2)."""
    rows: list[dict[str, Any]] = []
    for key, name in (
        (SENSOR_ENERGY_TODAY_P10, "P10 (conservative)"),
        (SENSOR_ENERGY_TODAY, "P50 (planning)"),
        (SENSOR_ENERGY_TODAY_P90, "P90 (optimistic)"),
    ):
        row = _row(entity_map, key, name)
        if row is not None:
            rows.append(row)
    if rows:
        cards.append(
            {
                "type": "entities",
                "title": "Today's forecast band (P10 / P50 / P90)",
                "show_header_toggle": False,
                "entities": rows,
            }
        )


def _add_learners(
    cards: list[dict[str, Any]], entity_map: dict[str, str]
) -> None:
    """Learner status + drift MAE + degradation source (SPEC §5/§7)."""
    head: list[dict[str, Any]] = []
    for key, name in (
        (SENSOR_SOURCE_STATUS, "Source status (degradation ladder)"),
        (BINARY_SENSOR_DEGRADED, "Degraded"),
        (SENSOR_LAST_FETCH_AGE, "Weather image age"),
    ):
        row = _row(entity_map, key, name)
        if row is not None:
            head.append(row)
    learners: list[dict[str, Any]] = []
    for key, name in (
        (SENSOR_LEARNER_STATUS_FAST, "Fast (intraday) learner"),
        (SENSOR_LEARNER_STATUS_SLOW, "Shademap (slow) learner"),
        (SENSOR_LEARNER_STATUS_DAY_AHEAD, "Day-ahead bias"),
        (SENSOR_INTRADAY_SCALAR, "Intraday scalar (applied)"),
        (SENSOR_DRIFT_MAE_CORRECTED, "Drift MAE (corrected vs physics)"),
    ):
        row = _row(entity_map, key, name)
        if row is not None:
            learners.append(row)
    rows = list(head)
    if learners:
        rows.append({"type": "section", "label": "Learners"})
        rows.extend(learners)
    if _has_entity_row(rows):
        cards.append(
            {
                "type": "entities",
                "title": "Learners, drift & degradation",
                "show_header_toggle": False,
                "entities": rows,
            }
        )


def _add_drift_trend(
    cards: list[dict[str, Any]], entity_map: dict[str, str]
) -> None:
    """Drift MAE (corrected) trend history-graph (SPEC §5)."""
    entity_id = entity_map.get(SENSOR_DRIFT_MAE_CORRECTED)
    if entity_id is None:
        return
    cards.append(
        {
            "type": "history-graph",
            "title": "Drift MAE (corrected) trend",
            "hours_to_show": 168,
            "entities": [{"entity": entity_id, "name": "Corrected MAE (Wh)"}],
        }
    )


def _add_shademap_markdown(cards: list[dict[str, Any]]) -> None:
    """Shademap documentation markdown — how to pull the dump_shademap table."""
    cards.append(
        {
            "type": "markdown",
            "title": "Shademap (learned shade transmittance)",
            "content": _SHADEMAP_MARKDOWN,
        }
    )


def _add_shade_profile_card(
    cards: list[dict[str, Any]], entity_map: dict[str, str]
) -> None:
    """The bundled shade-profile diagram card (SPEC §15) — replaces the opt-in
    HACS apexcharts snippet, wired to the three real entity ids."""
    sensor = entity_map.get(SENSOR_SHADE_PROFILE)
    module_select = entity_map.get(SELECT_SHADE_PROFILE_MODULE)
    date_entity = entity_map.get(DATE_SHADE_PROFILE_DATE)
    if sensor is None or module_select is None or date_entity is None:
        return
    cards.append(
        {
            "type": _SHADE_PROFILE_CARD,
            "sensor": sensor,
            "module_select": module_select,
            "date_entity": date_entity,
            "title": "Shade profile diagram",
        }
    )


def _add_comparison_reminder(cards: list[dict[str, Any]]) -> None:
    """Comparison-sensor configuration reminder markdown (D-P9: ships EMPTY)."""
    cards.append(
        {
            "type": "markdown",
            "title": "Scoreboard comparison sensors",
            "content": _COMPARISON_MARKDOWN,
        }
    )


# ---------------------------------------------------------------------------
# Static card content (mirrors the shipped YAML 1:1).
# ---------------------------------------------------------------------------

_KILL_GATE_TEMPLATE = (
    "{% set passed = states('__KILL__') %} "
    "{% set pct = states('__PCT__') %} "
    "{% if passed == 'on' %} ## ✅ Kill-gate PASSED\n\n"
    "The forecast is beating the best configured baseline on daily-kWh MAE by "
    "the required margin over a full rolling window. It is safe to consider "
    "re-pointing consumers (e.g. battery_manager) to the forecast sensors. "
    "{% elif passed == 'off' %} ## ❌ Kill-gate NOT passed\n\n"
    "Over the current full window the forecast is **not** yet the required "
    "margin better than the best baseline. Keep the frozen baseline in place "
    "(SPEC §9). {% else %} ## ⏳ Kill-gate: window not full yet\n\n"
    "Not enough scored days in the rolling window to assert the gate "
    "(`unknown` until a full window of nightly scores exists). {% endif %}\n\n"
    "{% if pct not in ('unknown', 'unavailable') %} Forecast vs best baseline: "
    "**{{ pct }} %** (positive = forecast better). {% endif %}"
)

_SHADEMAP_MARKDOWN = (
    "The slow learner holds a per-channel polar map of beam transmittance τ "
    "over (sun-azimuth × elevation × half-year). It is **not** a sensor "
    "attribute — pull the full polar table with the built-in service:\n\n\n"
    "**Developer Tools → Actions →** `balcony_solar_forecast.dump_shademap` "
    "(enable *\"Return response\"*). Each channel returns bins of "
    "`{az_deg, el_deg, tau, n}`; a richer polar plot can be rendered offline "
    "from that JSON.\n\n\n"
    "Eyeball the learned τ against your site's known obstructions — persistent "
    "low-τ wedges should line up with real objects (buildings, trees, terrain) "
    "as seen from the modules, at the sun-azimuth and elevation where each one "
    "cuts the beam.\n\n\n"
    "Shademap status and bin count are in the *Learners, drift & degradation* "
    "card above."
)

_COMPARISON_MARKDOWN = (
    "The scoreboard ships with **no** comparison baselines configured "
    "(generic, not hardcoded — D-P9). Add them in **Settings → Devices & "
    "Services → Balcony Solar Forecast → Configure → Comparison sensors**. The "
    "operator's live site uses:\n\n\n"
    "| Name | Daily-kWh entity |\n"
    "|---|---|\n"
    "| `8-Entry Baseline` | `sensor.pv_prognose_heute_alle_module` |\n"
    "| `Alt 1600W` | `sensor.energy_production_today_4` |\n\n\n"
    "Each added comparison creates a `…_comparison_daily_kwh_mae_<slug>` "
    "sensor. See docs/DASHBOARD.md."
)
