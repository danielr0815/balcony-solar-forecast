"""Config and options flow for the Balcony Solar Forecast integration.

One config entry per named site (SPEC §4). The user step collects the site
name, latitude/longitude (defaulting to ``hass.config``), the fetch/recompute
cadences, and a single ``site`` object (config-flow object selector) whose
default is the full operator reference site from ``const.DEFAULT_SITE`` — so
the operator sets it up in one click, but every plane, horizon table and
inverter group stays fully editable and generic (SPEC D-P9).

The submitted ``site`` object is validated by round-tripping it through
``SiteConfig.from_dict`` plus explicit range checks (azimuth 0..360, tilt
0..90, wp > 0, tau 0..1, horizon rows sorted by ascending azimuth). Any
violation is surfaced as a field error on the ``site`` key so the operator
sees it inline.

The options flow edits the very same object (modern HA 2026 pattern: the
framework supplies ``self.config_entry`` as a read-only property — we never
assign it) and additionally exposes the three learner kill switches (fast
learner / shademap learning / day-ahead bias — SPEC §5 "Kill-Switches je
Lernschicht im Options-Flow"; all default ON per the 2026-07-06 operator
decision to build v0.3 early). Turning a switch off writes ``False`` into the
entry options; the coordinator resolves them via ``LearnerConfig`` from
``{**entry.data, **entry.options}`` on the next reload.

Azimuth here is the INTERNAL convention (0 = North, clockwise). The rany2 UI
uses the same 0=N numbers; conversions to Open-Meteo / PVGIS conventions live
in the fetcher, not here (SPEC Anhang A).
"""

from __future__ import annotations

import copy
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from ._site_validation import SiteValidationError, validate_site
from .const import (
    CONF_COMPARISON_SENSORS,
    CONF_DAY_AHEAD_BIAS_ENABLED,
    CONF_FAST_LEARNER_ENABLED,
    CONF_FETCH_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_QUANTILES_ENABLED,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    CONF_SLOW_LEARNER_ENABLED,
    DEFAULT_COMPARISON_SENSORS,
    DEFAULT_DAY_AHEAD_BIAS_ENABLED,
    DEFAULT_FAST_LEARNER_ENABLED,
    DEFAULT_QUANTILES_ENABLED,
    DEFAULT_SITE,
    DEFAULT_SLOW_LEARNER_ENABLED,
    DOMAIN,
    FETCH_INTERVAL_SECONDS,
    RECOMPUTE_INTERVAL_SECONDS,
)
from .core.types import ComparisonConfig

# Re-export so consumers/tests can import the validation surface from here too.
__all__ = [
    "BalconySolarForecastConfigFlow",
    "BalconySolarForecastOptionsFlow",
    "SiteValidationError",
    "validate_site",
]

# Sensible bounds for the update-interval selectors (seconds).
_MIN_FETCH_SECONDS = 300  # 5 min — stay well under Open-Meteo's daily budget
_MAX_FETCH_SECONDS = 21600  # 6 h
_MIN_RECOMPUTE_SECONDS = 60  # 1 min
_MAX_RECOMPUTE_SECONDS = 3600  # 1 h


def _interval_selector(minimum: int, maximum: int) -> selector.Selector:
    """A seconds number-box for an update interval."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _site_selector() -> selector.Selector:
    """Object selector for the full editable site config."""
    return selector.ObjectSelector(selector.ObjectSelectorConfig())


def _bool_selector() -> selector.Selector:
    """A plain on/off toggle for a learner / feature kill switch."""
    return selector.BooleanSelector()


def _comparison_sensors_selector() -> selector.Selector:
    """Editable list of comparison-forecast entries (SPEC §9/§10, D-P9).

    Each row is an object ``{name, daily_entity}`` naming an external daily-kWh
    forecast sensor the scoreboard compares the engine against. Ships EMPTY;
    the operator's two comparisons are documented (docs/DASHBOARD.md), never
    hardcoded in the runtime defaults. ObjectSelector with ``multiple`` yields a
    plain list-of-dicts, parsed leniently by ``ComparisonConfig.list_from_options``
    (malformed / half-filled rows are dropped rather than raising).
    """
    return selector.ObjectSelector(selector.ObjectSelectorConfig(multiple=True))


def _user_schema(
    *,
    name: str,
    latitude: float,
    longitude: float,
    fetch_interval: int,
    recompute_interval: int,
    site: dict[str, Any],
    include_name: bool = True,
    include_learner_switches: bool = False,
    fast_learner_enabled: bool = DEFAULT_FAST_LEARNER_ENABLED,
    slow_learner_enabled: bool = DEFAULT_SLOW_LEARNER_ENABLED,
    day_ahead_bias_enabled: bool = DEFAULT_DAY_AHEAD_BIAS_ENABLED,
    quantiles_enabled: bool = DEFAULT_QUANTILES_ENABLED,
    comparison_sensors: list[dict] | None = None,
) -> vol.Schema:
    """Schema for the user step / options step, pre-filled with defaults.

    ``include_name`` is False for the options step, where the name (and its
    unique-id) is immutable after setup. ``include_learner_switches`` is True
    for the options step only: the per-layer learner kill switches (SPEC §5),
    the v0.4 quantile kill switch (SPEC §6) and the editable comparison-sensors
    list (SPEC §9/§10) are runtime tunables, not first-setup fields, so the
    wizard stays lean and they appear where the operator manages a live install.
    Every switch is a plain boolean toggle (no NumberSelector, so the HA-2026
    ``step >= 1e-3`` selector rule cannot bite here) and defaults ON.
    """
    fields: dict[Any, Any] = {}
    if include_name:
        fields[vol.Required(CONF_NAME, default=name)] = selector.TextSelector(
            selector.TextSelectorConfig()
        )
    fields.update(
        {
            # step="any": HA's NumberSelector schema rejects numeric steps
            # below 1e-3 (vol.Range(min=1e-3)); coordinates need ~1e-6
            # precision, so free-form input is the only valid choice here.
            vol.Required(CONF_LATITUDE, default=latitude): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-90, max=90, step="any", mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_LONGITUDE, default=longitude
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-180, max=180, step="any", mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_FETCH_INTERVAL, default=fetch_interval
            ): _interval_selector(_MIN_FETCH_SECONDS, _MAX_FETCH_SECONDS),
            vol.Required(
                CONF_RECOMPUTE_INTERVAL, default=recompute_interval
            ): _interval_selector(_MIN_RECOMPUTE_SECONDS, _MAX_RECOMPUTE_SECONDS),
            vol.Required(CONF_SITE, default=site): _site_selector(),
        }
    )
    if include_learner_switches:
        fields.update(
            {
                vol.Required(
                    CONF_FAST_LEARNER_ENABLED, default=fast_learner_enabled
                ): _bool_selector(),
                vol.Required(
                    CONF_SLOW_LEARNER_ENABLED, default=slow_learner_enabled
                ): _bool_selector(),
                vol.Required(
                    CONF_DAY_AHEAD_BIAS_ENABLED, default=day_ahead_bias_enabled
                ): _bool_selector(),
                # v0.4 quantile bands kill switch (SPEC §6, default ON).
                vol.Required(
                    CONF_QUANTILES_ENABLED, default=quantiles_enabled
                ): _bool_selector(),
                # v0.4 comparison-forecast list (SPEC §9/§10). Optional so an
                # empty list means "no external comparisons" without forcing a
                # required-field error; the default is the current value.
                vol.Optional(
                    CONF_COMPARISON_SENSORS,
                    default=list(comparison_sensors or []),
                ): _comparison_sensors_selector(),
            }
        )
    return vol.Schema(fields)


class BalconySolarForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup: name, location, cadences and the site object."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> BalconySolarForecastOptionsFlow:
        return BalconySolarForecastOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = str(user_input[CONF_NAME]).strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                # One entry per name (SPEC §4: one instance per name).
                await self.async_set_unique_id(name.casefold())
                self._abort_if_unique_id_configured()

            if not errors:
                try:
                    site = validate_site(user_input.get(CONF_SITE))
                except SiteValidationError as err:
                    errors[CONF_SITE] = err.code
                else:
                    lat = float(user_input[CONF_LATITUDE])
                    lon = float(user_input[CONF_LONGITUDE])
                    # The coordinator reads the site-embedded coordinates only
                    # (fetch + sun position), so the visible lat/lon fields MUST
                    # be merged into the site dict — otherwise they are stored
                    # but silently ignored and every off-reference user forecasts
                    # for the shipped Landshut default.
                    site_dict = site.to_dict()
                    site_dict[CONF_LATITUDE] = lat
                    site_dict[CONF_LONGITUDE] = lon
                    # Store the normalised (round-tripped) site so downstream
                    # readers get a canonical dict regardless of input shape.
                    data = {
                        CONF_NAME: name,
                        CONF_LATITUDE: lat,
                        CONF_LONGITUDE: lon,
                        CONF_FETCH_INTERVAL: int(user_input[CONF_FETCH_INTERVAL]),
                        CONF_RECOMPUTE_INTERVAL: int(
                            user_input[CONF_RECOMPUTE_INTERVAL]
                        ),
                        CONF_SITE: site_dict,
                    }
                    return self.async_create_entry(title=name, data=data)

        # First render (or re-render after an error): default location from
        # hass.config, default site from const, keep just-entered values.
        defaults = _current_values(user_input, hass_config=self.hass.config)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(**defaults),
            errors=errors,
        )


class BalconySolarForecastOptionsFlow(OptionsFlow):
    """Edit the same site object (and cadences/location) after setup.

    Modern HA 2026 pattern: ``self.config_entry`` is provided by the
    framework as a read-only property — we do NOT assign it in ``__init__``
    (the setter was removed).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                site = validate_site(user_input.get(CONF_SITE))
            except SiteValidationError as err:
                errors[CONF_SITE] = err.code
            else:
                lat = float(user_input[CONF_LATITUDE])
                lon = float(user_input[CONF_LONGITUDE])
                # Merge the visible lat/lon into the site dict too: the
                # coordinator reads only the site-embedded coordinates.
                site_dict = site.to_dict()
                site_dict[CONF_LATITUDE] = lat
                site_dict[CONF_LONGITUDE] = lon
                # Name/unique-id are fixed after setup; only editable fields
                # are written to options (merged over entry.data downstream).
                # The three learner kill switches (SPEC §5) round-trip as plain
                # booleans; missing keys fall back to the shipped ON defaults so
                # an older entry that predates them keeps both learners active.
                data = {
                    CONF_LATITUDE: lat,
                    CONF_LONGITUDE: lon,
                    CONF_FETCH_INTERVAL: int(user_input[CONF_FETCH_INTERVAL]),
                    CONF_RECOMPUTE_INTERVAL: int(
                        user_input[CONF_RECOMPUTE_INTERVAL]
                    ),
                    CONF_SITE: site_dict,
                    CONF_FAST_LEARNER_ENABLED: bool(
                        user_input.get(
                            CONF_FAST_LEARNER_ENABLED, DEFAULT_FAST_LEARNER_ENABLED
                        )
                    ),
                    CONF_SLOW_LEARNER_ENABLED: bool(
                        user_input.get(
                            CONF_SLOW_LEARNER_ENABLED, DEFAULT_SLOW_LEARNER_ENABLED
                        )
                    ),
                    CONF_DAY_AHEAD_BIAS_ENABLED: bool(
                        user_input.get(
                            CONF_DAY_AHEAD_BIAS_ENABLED,
                            DEFAULT_DAY_AHEAD_BIAS_ENABLED,
                        )
                    ),
                    # v0.4 quantile kill switch (SPEC §6, default ON).
                    CONF_QUANTILES_ENABLED: bool(
                        user_input.get(
                            CONF_QUANTILES_ENABLED, DEFAULT_QUANTILES_ENABLED
                        )
                    ),
                    # v0.4 comparison-forecast list (SPEC §9/§10): normalise
                    # through ComparisonConfig so half-filled / malformed rows
                    # are dropped and only clean {name, daily_entity} objects are
                    # persisted. Stored as a list of plain dicts.
                    CONF_COMPARISON_SENSORS: [
                        c.to_dict()
                        for c in ComparisonConfig.list_from_options(
                            user_input.get(CONF_COMPARISON_SENSORS)
                        )
                    ],
                }
                return self.async_create_entry(title="", data=data)

        merged = {**self.config_entry.data, **self.config_entry.options}
        defaults = _current_values(user_input, existing=merged)
        # Name is immutable in options; omit it from the schema. The learner
        # kill switches only appear here (not in first setup).
        return self.async_show_form(
            step_id="init",
            data_schema=_user_schema(
                **defaults,
                include_name=False,
                include_learner_switches=True,
            ),
            errors=errors,
        )


def _current_values(
    user_input: dict[str, Any] | None,
    *,
    existing: dict[str, Any] | None = None,
    hass_config: Any = None,
) -> dict[str, Any]:
    """Resolve the schema pre-fill values.

    Precedence: just-submitted ``user_input`` (so an error re-render keeps
    the operator's edits) > ``existing`` entry data/options > hass.config /
    shipped constants. The site default is a deep copy so the shared
    ``DEFAULT_SITE`` is never mutated by later editing.
    """
    src = user_input or existing or {}

    if existing is not None:
        default_lat = existing.get(CONF_LATITUDE, 0.0)
        default_lon = existing.get(CONF_LONGITUDE, 0.0)
    elif hass_config is not None:
        default_lat = hass_config.latitude
        default_lon = hass_config.longitude
    else:  # pragma: no cover - defensive
        default_lat = 0.0
        default_lon = 0.0

    default_site = (
        existing.get(CONF_SITE)
        if existing is not None and existing.get(CONF_SITE)
        else copy.deepcopy(DEFAULT_SITE)
    )

    def _bool_default(key: str, fallback: bool) -> bool:
        # Precedence mirrors the other fields: just-submitted edit > existing
        # option > shipped default. ``existing`` already merges data+options.
        if key in src:
            return bool(src[key])
        if existing is not None and key in existing:
            return bool(existing[key])
        return fallback

    def _comparison_default() -> list[dict]:
        # Same precedence as the bool switches. The value is a list of
        # {name, daily_entity} objects; a just-submitted raw list (possibly with
        # half-filled rows from the object editor) is passed through verbatim so
        # an error re-render keeps the operator's in-progress edits, while a
        # value pulled from the persisted entry is already normalised.
        if CONF_COMPARISON_SENSORS in src:
            raw = src[CONF_COMPARISON_SENSORS]
            return list(raw) if isinstance(raw, list) else []
        if existing is not None and CONF_COMPARISON_SENSORS in existing:
            raw = existing[CONF_COMPARISON_SENSORS]
            return list(raw) if isinstance(raw, list) else []
        return list(DEFAULT_COMPARISON_SENSORS)

    return {
        "name": src.get(CONF_NAME, existing.get(CONF_NAME, "") if existing else ""),
        "latitude": src.get(CONF_LATITUDE, default_lat),
        "longitude": src.get(CONF_LONGITUDE, default_lon),
        "fetch_interval": src.get(
            CONF_FETCH_INTERVAL,
            existing.get(CONF_FETCH_INTERVAL, FETCH_INTERVAL_SECONDS)
            if existing
            else FETCH_INTERVAL_SECONDS,
        ),
        "recompute_interval": src.get(
            CONF_RECOMPUTE_INTERVAL,
            existing.get(CONF_RECOMPUTE_INTERVAL, RECOMPUTE_INTERVAL_SECONDS)
            if existing
            else RECOMPUTE_INTERVAL_SECONDS,
        ),
        "site": src.get(CONF_SITE, default_site),
        "fast_learner_enabled": _bool_default(
            CONF_FAST_LEARNER_ENABLED, DEFAULT_FAST_LEARNER_ENABLED
        ),
        "slow_learner_enabled": _bool_default(
            CONF_SLOW_LEARNER_ENABLED, DEFAULT_SLOW_LEARNER_ENABLED
        ),
        "day_ahead_bias_enabled": _bool_default(
            CONF_DAY_AHEAD_BIAS_ENABLED, DEFAULT_DAY_AHEAD_BIAS_ENABLED
        ),
        "quantiles_enabled": _bool_default(
            CONF_QUANTILES_ENABLED, DEFAULT_QUANTILES_ENABLED
        ),
        "comparison_sensors": _comparison_default(),
    }
