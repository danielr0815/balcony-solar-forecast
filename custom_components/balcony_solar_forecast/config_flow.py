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

Structural setup — location, the fetch/recompute cadences and the full ``site``
object — lives in ``entry.data`` and is edited AFTER setup through the
reconfigure flow (``async_step_reconfigure``, the HA quality-scale pattern),
which writes it straight back into ``entry.data`` via
``async_update_reload_and_abort``. Editing structural data into
``entry.options`` (the legacy options behaviour) permanently shadowed
``entry.data`` through the ``{**entry.data, **entry.options}`` merge every
reader uses.

The options flow is therefore slimmed to RUNTIME TUNABLES only: the three
learner kill switches (fast learner / shademap learning / day-ahead bias —
SPEC §5 "Kill-Switches je Lernschicht im Options-Flow"; all default ON per the
2026-07-06 operator decision to build v0.3 early), the v0.4 quantile kill
switch (SPEC §6) and the editable comparison-sensors list (SPEC §9/§10). Modern
HA 2026 pattern: the framework supplies ``self.config_entry`` as a read-only
property — we never assign it. Turning a switch off writes ``False`` into the
entry options; the coordinator resolves every tunable via ``LearnerConfig`` from
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
    CONF_AC_ACTUAL_ENTITY,
    CONF_AC_ACTUAL_INVERT,
    CONF_COMPARISON_DAILY_ENTITY,
    CONF_COMPARISON_NAME,
    CONF_COMPARISON_SENSORS,
    CONF_DAY_AHEAD_BIAS_ENABLED,
    CONF_ENSEMBLE_ENABLED,
    CONF_FAST_LEARNER_ENABLED,
    CONF_FETCH_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_QUANTILES_ENABLED,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    CONF_SITE_ALBEDO,
    CONF_SLOW_LEARNER_ENABLED,
    DEFAULT_COMPARISON_SENSORS,
    DEFAULT_DAY_AHEAD_BIAS_ENABLED,
    DEFAULT_ENSEMBLE_ENABLED,
    DEFAULT_FAST_LEARNER_ENABLED,
    DEFAULT_QUANTILES_ENABLED,
    DEFAULT_SITE,
    DEFAULT_SLOW_LEARNER_ENABLED,
    DOMAIN,
    FETCH_INTERVAL_SECONDS,
    RECOMPUTE_INTERVAL_SECONDS,
    SITE_ALBEDO_MAX,
    SITE_ALBEDO_MIN,
)
from .core.types import ComparisonConfig, SiteConfig

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

# The five STRUCTURAL keys that belong in ``entry.data`` (edited via the
# reconfigure flow), never in ``entry.options``. Stale copies left in options —
# e.g. by the legacy options flow that used to edit the site there — would
# silently shadow the just-reconfigured data through the ``{**data, **options}``
# merge every reader uses, so the reconfigure step strips them out of options in
# the SAME atomic ``async_update_reload_and_abort`` call.
_STRUCTURAL_OPTION_KEYS = frozenset(
    {
        CONF_LATITUDE,
        CONF_LONGITUDE,
        CONF_FETCH_INTERVAL,
        CONF_RECOMPUTE_INTERVAL,
        CONF_SITE,
    }
)


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

    Each row is a structured object with a required operator label and a
    required daily-kWh forecast sensor (domain ``sensor``). The field keys are
    EXACTLY ``CONF_COMPARISON_NAME`` / ``CONF_COMPARISON_DAILY_ENTITY`` so a
    persisted row round-trips straight back into the editor. ``label_field``
    shows the name as each row's header; ``multiple`` yields a list-of-dicts.
    Ships EMPTY; the operator's two comparisons are documented
    (docs/DASHBOARD.md), never hardcoded in the runtime defaults.
    ``ComparisonConfig.list_from_options`` stays the lenient backstop on save —
    half-filled / malformed rows are dropped rather than persisted.
    """
    return selector.ObjectSelector(
        selector.ObjectSelectorConfig(
            multiple=True,
            label_field=CONF_COMPARISON_NAME,
            fields={
                CONF_COMPARISON_NAME: {
                    "required": True,
                    "selector": selector.TextSelector(
                        selector.TextSelectorConfig()
                    ),
                },
                CONF_COMPARISON_DAILY_ENTITY: {
                    "required": True,
                    "selector": selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                },
            },
        )
    )


def _user_schema(
    *,
    name: str,
    latitude: float,
    longitude: float,
    fetch_interval: int,
    recompute_interval: int,
    site: dict[str, Any],
    ac_actual_entity: str = "",
    ac_actual_invert: bool = False,
    albedo: float | None = None,
    include_name: bool = True,
) -> vol.Schema:
    """Schema for the user / reconfigure step: STRUCTURAL setup only.

    ``include_name`` is False for the reconfigure step, where the name (and its
    unique-id) is immutable after setup. The runtime tunables (learner kill
    switches, quantile bands, comparison sensors) are NOT first-setup fields and
    live in the options flow — see ``_options_schema``.

    ``ac_actual_entity`` / ``ac_actual_invert`` are the site-level TOTAL-AC meter
    picker (AC-side Phase 4): both OPTIONAL, shown just above the site object.
    They are NOT part of the object selector — they are separate first-class
    fields so the operator sets the AC calibration target without editing raw
    JSON — and get merged INTO the site dict in ``_structural_data`` so they
    round-trip through ``SiteConfig`` exactly like lat/lon. The entity uses a
    ``suggested_value`` (not a ``default``) so a cleared field stays cleared
    (an EntitySelector default would silently re-apply on clear).
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
            # Optional site-level AC meter (Phase 4) — above the site object.
            vol.Optional(
                CONF_AC_ACTUAL_ENTITY,
                description={"suggested_value": ac_actual_entity or None},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_AC_ACTUAL_INVERT, default=bool(ac_actual_invert)
            ): _bool_selector(),
            # Optional site ground albedo (v0.20) — same suggested_value pattern
            # as the AC meter so a cleared field stays cleared (=> shipped
            # default applies). Matters most on steep balcony tilts, where the
            # ground-reflected diffuse is a large share of the floor.
            vol.Optional(
                CONF_SITE_ALBEDO,
                description={"suggested_value": albedo},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=SITE_ALBEDO_MIN, max=SITE_ALBEDO_MAX, step="any",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_SITE, default=site): _site_selector(),
        }
    )
    return vol.Schema(fields)


def _options_schema(
    *,
    fast_learner_enabled: bool = DEFAULT_FAST_LEARNER_ENABLED,
    slow_learner_enabled: bool = DEFAULT_SLOW_LEARNER_ENABLED,
    day_ahead_bias_enabled: bool = DEFAULT_DAY_AHEAD_BIAS_ENABLED,
    quantiles_enabled: bool = DEFAULT_QUANTILES_ENABLED,
    ensemble_enabled: bool = DEFAULT_ENSEMBLE_ENABLED,
    comparison_sensors: list[dict] | None = None,
) -> vol.Schema:
    """Schema for the options step: RUNTIME TUNABLES only.

    Structural setup lives in the reconfigure flow (see the module docstring).
    What remains here are the three per-layer learner kill switches (SPEC §5),
    the v0.4 quantile kill switch (SPEC §6), the v0.16 ensemble-band kill switch
    (SPEC §6, default OFF) and the editable comparison-sensors list (SPEC §9/§10).
    Every switch is a plain boolean toggle (no NumberSelector, so the HA-2026
    ``step >= 1e-3`` selector rule cannot bite here). The learner/quantile
    switches default ON; the ensemble switch defaults OFF (opt-in). The comparison
    list is Optional so an empty list means "no comparisons" without a
    required-field error; the default is the current value.
    """
    return vol.Schema(
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
            vol.Required(
                CONF_QUANTILES_ENABLED, default=quantiles_enabled
            ): _bool_selector(),
            vol.Required(
                CONF_ENSEMBLE_ENABLED, default=ensemble_enabled
            ): _bool_selector(),
            vol.Optional(
                CONF_COMPARISON_SENSORS,
                default=list(comparison_sensors or []),
            ): _comparison_sensors_selector(),
        }
    )


def _structural_data(site: SiteConfig, user_input: dict[str, Any]) -> dict[str, Any]:
    """Build the structural (``entry.data``) dict from validated input.

    Shared by the user AND reconfigure steps so the load-bearing lat/lon→site
    merge can never diverge between the two entry points: the coordinator reads
    ONLY the site-embedded coordinates (fetch + sun position), so the visible
    lat/lon fields MUST be merged into the site dict — otherwise they are stored
    but silently ignored and every off-reference user forecasts for the shipped
    reference-site default. Returns the five structural keys; the user step
    layers ``CONF_NAME`` on top.
    """
    lat = float(user_input[CONF_LATITUDE])
    lon = float(user_input[CONF_LONGITUDE])
    # Store the normalised (round-tripped) site so downstream readers get a
    # canonical dict regardless of the input shape.
    site_dict = site.to_dict()
    site_dict[CONF_LATITUDE] = lat
    site_dict[CONF_LONGITUDE] = lon
    # Merge the site-level AC-meter picker (Phase 4) INTO the site dict, exactly
    # like lat/lon, so it round-trips through SiteConfig (the coordinator reads
    # the meter only from the site-embedded config). The two visible form fields
    # are AUTHORITATIVE: an empty/absent entity clears any value the site object
    # carried (stored as absent → None), and the invert flag is written only when
    # True (mirrors SiteConfig.to_dict's only-when-set convention).
    ac_entity_raw = user_input.get(CONF_AC_ACTUAL_ENTITY)
    ac_entity = (
        ac_entity_raw.strip() if isinstance(ac_entity_raw, str) else ""
    )
    if ac_entity:
        site_dict[CONF_AC_ACTUAL_ENTITY] = ac_entity
    else:
        site_dict.pop(CONF_AC_ACTUAL_ENTITY, None)
    if bool(user_input.get(CONF_AC_ACTUAL_INVERT, False)):
        site_dict[CONF_AC_ACTUAL_INVERT] = True
    else:
        site_dict.pop(CONF_AC_ACTUAL_INVERT, None)
    # Optional site albedo (v0.20): same authoritative-field convention — a
    # filled field is merged into the site dict, a cleared field removes any
    # stored value so the shipped default applies again.
    albedo_raw = user_input.get(CONF_SITE_ALBEDO)
    albedo: float | None
    try:
        albedo = float(albedo_raw) if albedo_raw is not None else None
    except (TypeError, ValueError):
        albedo = None
    if albedo is not None:
        site_dict[CONF_SITE_ALBEDO] = albedo
    else:
        site_dict.pop(CONF_SITE_ALBEDO, None)
    return {
        CONF_LATITUDE: lat,
        CONF_LONGITUDE: lon,
        CONF_FETCH_INTERVAL: int(user_input[CONF_FETCH_INTERVAL]),
        CONF_RECOMPUTE_INTERVAL: int(user_input[CONF_RECOMPUTE_INTERVAL]),
        CONF_SITE: site_dict,
    }


class BalconySolarForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup and later reconfiguration of the structural site data."""

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
                    data = {CONF_NAME: name, **_structural_data(site, user_input)}
                    return self.async_create_entry(title=name, data=data)

        # First render (or re-render after an error): default location from
        # hass.config, default site from const, keep just-entered values.
        defaults = _current_values(user_input, hass_config=self.hass.config)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(
                name=defaults["name"],
                latitude=defaults["latitude"],
                longitude=defaults["longitude"],
                fetch_interval=defaults["fetch_interval"],
                recompute_interval=defaults["recompute_interval"],
                site=defaults["site"],
                ac_actual_entity=defaults["ac_actual_entity"],
                ac_actual_invert=defaults["ac_actual_invert"],
                albedo=defaults["albedo"],
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the structural setup of an existing entry into ``entry.data``.

        HA quality-scale pattern: structural data (location, cadences, the full
        site object) belongs in ``entry.data``, not ``entry.options`` — editing
        it into options permanently shadows ``entry.data`` through the
        ``{**data, **options}`` merge. Uses the SAME site validation and the SAME
        lat/lon→site merge as the user step (shared ``_structural_data``); the
        name is immutable so there is no name field and no learner switches here.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                site = validate_site(user_input.get(CONF_SITE))
            except SiteValidationError as err:
                errors[CONF_SITE] = err.code
            else:
                # Strip any stale structural keys from options in the SAME
                # atomic update: left behind, they would shadow the just-
                # reconfigured data through the {**data, **options} merge and
                # silently revert the live site to the pre-edit values.
                stripped_options = {
                    k: v
                    for k, v in entry.options.items()
                    if k not in _STRUCTURAL_OPTION_KEYS
                }
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=_structural_data(site, user_input),
                    options=stripped_options,
                )

        merged = {**entry.data, **entry.options}
        defaults = _current_values(user_input, existing=merged)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(
                name=defaults["name"],
                latitude=defaults["latitude"],
                longitude=defaults["longitude"],
                fetch_interval=defaults["fetch_interval"],
                recompute_interval=defaults["recompute_interval"],
                site=defaults["site"],
                ac_actual_entity=defaults["ac_actual_entity"],
                ac_actual_invert=defaults["ac_actual_invert"],
                albedo=defaults["albedo"],
                include_name=False,
            ),
            errors=errors,
        )


class BalconySolarForecastOptionsFlow(OptionsFlow):
    """Edit the RUNTIME TUNABLES of a live install (SPEC §5/§6/§9/§10).

    Structural setup (location, cadences, the site object) is edited through the
    reconfigure flow into ``entry.data``, NOT here — see the module docstring.
    Modern HA 2026 pattern: ``self.config_entry`` is provided by the framework
    as a read-only property — we do NOT assign it in ``__init__`` (the setter
    was removed).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Spread the EXISTING options FIRST: an old entry that edited its
            # site via the legacy options flow still carries structural keys in
            # options, and dropping them here would silently revert the live
            # site to the stale ``entry.data`` version. They are cleaned up by
            # the next reconfigure, not by an options save. Only the five runtime
            # tunables below are (re)written on top.
            #
            # The learner kill switches (SPEC §5) round-trip as plain booleans;
            # a missing key falls back to the shipped ON default so an older
            # entry that predates a switch keeps that layer active. The
            # comparison list (SPEC §9/§10) is normalised through
            # ComparisonConfig so half-filled / malformed rows are dropped and
            # only clean {name, daily_entity} objects are persisted.
            data = {
                **self.config_entry.options,
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
                # v0.16 ensemble-band kill switch (SPEC §6, default OFF/opt-in).
                CONF_ENSEMBLE_ENABLED: bool(
                    user_input.get(
                        CONF_ENSEMBLE_ENABLED, DEFAULT_ENSEMBLE_ENABLED
                    )
                ),
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
        # Only runtime tunables here; the name and structural fields belong to
        # the reconfigure flow.
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                fast_learner_enabled=defaults["fast_learner_enabled"],
                slow_learner_enabled=defaults["slow_learner_enabled"],
                day_ahead_bias_enabled=defaults["day_ahead_bias_enabled"],
                quantiles_enabled=defaults["quantiles_enabled"],
                ensemble_enabled=defaults["ensemble_enabled"],
                comparison_sensors=defaults["comparison_sensors"],
            ),
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
    ``DEFAULT_SITE`` is never mutated by later editing. Returns both the
    structural values (user/reconfigure steps) and the runtime tunables (options
    step); each caller reads only the subset its schema renders.
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

    # AC-meter picker defaults (Phase 4): the meter lives INSIDE the site dict
    # (merged there by _structural_data), so pull the pre-fill from the resolved
    # site — unless a just-submitted top-level value is present (error re-render
    # keeps the operator's in-progress edit).
    site_ac_entity = ""
    site_ac_invert = False
    site_albedo: float | None = None
    if isinstance(default_site, dict):
        raw_ac = default_site.get(CONF_AC_ACTUAL_ENTITY)
        site_ac_entity = raw_ac if isinstance(raw_ac, str) else ""
        site_ac_invert = bool(default_site.get(CONF_AC_ACTUAL_INVERT, False))
        raw_albedo = default_site.get(CONF_SITE_ALBEDO)
        try:
            site_albedo = float(raw_albedo) if raw_albedo is not None else None
        except (TypeError, ValueError):
            site_albedo = None

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
        "ac_actual_entity": src.get(CONF_AC_ACTUAL_ENTITY, site_ac_entity),
        "ac_actual_invert": bool(
            src.get(CONF_AC_ACTUAL_INVERT, site_ac_invert)
        ),
        "albedo": src.get(CONF_SITE_ALBEDO, site_albedo),
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
        "ensemble_enabled": _bool_default(
            CONF_ENSEMBLE_ENABLED, DEFAULT_ENSEMBLE_ENABLED
        ),
        "comparison_sensors": _comparison_default(),
    }
