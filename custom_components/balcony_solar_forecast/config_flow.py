"""Config and options flow for the Balcony Solar Forecast integration.

One config entry per named site (SPEC §2). Initial setup offers a guided
location/panel/inverter flow and the advanced ``site`` object editor. Both
produce the same structural data, with HA location and neutral open-sky
defaults. Every plane, horizon table and inverter group remains editable
and generic (SPEC §2, §7.8); nothing is persisted until final submission.

The submitted ``site`` object is validated by round-tripping it through
``SiteConfig.from_dict`` plus explicit range checks (azimuth 0..360, tilt
0..90, wp > 0, tau 0..1, horizon rows sorted by ascending azimuth). Any
violation is surfaced as a field error on the ``site`` key so the operator
sees it inline.

Structural setup — location, the fetch/recompute cadences and the full ``site``
object — lives in ``entry.data`` and is edited AFTER setup through the
reconfigure flow (``async_step_reconfigure``, the HA quality-scale pattern),
which writes it straight back into ``entry.data`` via
``hass.config_entries.async_update_entry`` (the entry's update listener then
fires the single reload — the flow never schedules one itself). Editing
structural data into ``entry.options`` (the legacy options behaviour)
permanently shadowed ``entry.data`` through the
``{**entry.data, **entry.options}`` merge every reader uses.

The options flow is therefore slimmed to RUNTIME TUNABLES only: the three
learner kill switches (fast learner / shademap learning / day-ahead bias —
SPEC §9 "Kill-Switches je Lernschicht im Options-Flow"; all default ON per the
2026-07-06 operator decision to build v0.3 early) and the v0.4 quantile kill
switch (SPEC §11.2). Modern
HA 2026 pattern: the framework supplies ``self.config_entry`` as a read-only
property — we never assign it. Turning a switch off writes ``False`` into the
entry options; the coordinator resolves every tunable via ``LearnerConfig`` from
``{**entry.data, **entry.options}`` on the next reload.

Azimuth here is the INTERNAL convention (0 = North, clockwise). The rany2 UI
uses the same 0=N numbers. This flow does no conversion from foreign azimuth
conventions (SPEC §20.1).
"""

from __future__ import annotations

from copy import deepcopy
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
    CONF_SITE_BEAM_GAIN,
    CONF_SLOW_LEARNER_ENABLED,
    DEFAULT_DAY_AHEAD_BIAS_ENABLED,
    DEFAULT_ENSEMBLE_ENABLED,
    DEFAULT_FAST_LEARNER_ENABLED,
    DEFAULT_INVERTER_EFFICIENCY,
    DEFAULT_QUANTILES_ENABLED,
    DEFAULT_SLOW_LEARNER_ENABLED,
    DOMAIN,
    FETCH_INTERVAL_SECONDS,
    INVERTER_EFFICIENCY_MAX,
    INVERTER_EFFICIENCY_MIN,
    RECOMPUTE_INTERVAL_SECONDS,
    SITE_ALBEDO_MAX,
    SITE_ALBEDO_MIN,
    SITE_BEAM_GAIN_MAX,
    SITE_BEAM_GAIN_MIN,
    SITE_MAX_PLANES,
)
from .core.types import SiteConfig

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
# the SAME atomic ``async_update_entry`` call.
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


def _guided_number(minimum: float, maximum: float, unit: str | None = None) -> selector.Selector:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step="any", mode=selector.NumberSelectorMode.BOX,
    )
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _guided_panel_schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required("name", default=values["name"]): str,
        vol.Required("azimuth_deg", default=values.get("azimuth_deg", 180)): _guided_number(0, 360, "°"),
        vol.Required("tilt_deg", default=values.get("tilt_deg", 30)): _guided_number(0, 90, "°"),
        vol.Required("wp", default=values.get("wp", 400)): _guided_number(.1, 100000, "Wp"),
        vol.Optional("actual_entity", description={"suggested_value": values.get("actual_entity")}):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Required("group_name", default=values.get("group_name", "Inverter 1")): str,
        vol.Required("ac_limit_w", default=values.get("ac_limit_w", 800)): _guided_number(.1, 100000, "W"),
        vol.Required("inverter_efficiency", default=values.get("inverter_efficiency", DEFAULT_INVERTER_EFFICIENCY)):
            _guided_number(INVERTER_EFFICIENCY_MIN, INVERTER_EFFICIENCY_MAX),
        vol.Required("add_panel", default=False): _bool_selector(),
    })


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
    beam_gain: float | None = None,
    include_name: bool = True,
) -> vol.Schema:
    """Schema for the user / reconfigure step: STRUCTURAL setup only.

    ``include_name`` is False for the reconfigure step, where the name (and its
    unique-id) is immutable after setup. The runtime tunables (learner kill
    switches, quantile bands, ensemble bands) are NOT first-setup fields and
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
            # Optional bifacial beam gain (forensik T6) — same suggested_value
            # pattern so a cleared field stays cleared (=> identity 1.0). Lifts
            # the honestly under-modeled direct beam on clear mornings.
            vol.Optional(
                CONF_SITE_BEAM_GAIN,
                description={"suggested_value": beam_gain},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=SITE_BEAM_GAIN_MIN, max=SITE_BEAM_GAIN_MAX, step="any",
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
) -> vol.Schema:
    """Schema for the options step: RUNTIME TUNABLES only.

    Structural setup lives in the reconfigure flow (see the module docstring).
    What remains here are the three per-layer learner kill switches (SPEC §9),
    the v0.4 quantile kill switch (SPEC §11.2) and the v0.16 ensemble-band kill
    switch (SPEC §11.3, default OFF).
    Every switch is a plain boolean toggle (no NumberSelector, so the HA-2026
    ``step >= 1e-3`` selector rule cannot bite here). The learner/quantile
    switches default ON; the ensemble switch defaults OFF (opt-in).
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
    # Optional bifacial beam gain (forensik T6): same authoritative-field
    # convention — a filled field is merged into the site dict, a cleared field
    # removes any stored value so the identity default (1.0) applies again.
    beam_gain_raw = user_input.get(CONF_SITE_BEAM_GAIN)
    beam_gain: float | None
    try:
        beam_gain = float(beam_gain_raw) if beam_gain_raw is not None else None
    except (TypeError, ValueError):
        beam_gain = None
    if beam_gain is not None:
        site_dict[CONF_SITE_BEAM_GAIN] = beam_gain
    else:
        site_dict.pop(CONF_SITE_BEAM_GAIN, None)
    return {
        CONF_LATITUDE: lat,
        CONF_LONGITUDE: lon,
        CONF_FETCH_INTERVAL: int(user_input[CONF_FETCH_INTERVAL]),
        CONF_RECOMPUTE_INTERVAL: int(user_input[CONF_RECOMPUTE_INTERVAL]),
        CONF_SITE: site_dict,
    }



def _validate_measurement_sources(hass, site, user_input):
    """Check configured power roles while keeping unavailable sources usable."""
    from collections.abc import Mapping

    from .core.measurement_quality import power_source_problem

    dc = {p.actual_entity for p in site.planes if p.actual_entity}
    ac = user_input.get(CONF_AC_ACTUAL_ENTITY) or site.ac_actual_entity
    if ac in dc:
        raise SiteValidationError("measurement_role_collision")
    states = getattr(hass, "states", None)
    if states is None:
        return
    for entity_id in dc | ({ac} if ac else set()):
        state = states.get(entity_id)
        attrs = getattr(state, "attributes", None)
        if isinstance(attrs, Mapping):
            problem = power_source_problem(dict(attrs))
            if problem is not None:
                raise SiteValidationError(problem)

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
        if user_input is None and not getattr(self, "_advanced_mode", False):
            return self.async_show_menu(step_id="user", menu_options=["guided", "advanced"])
        errors: dict[str, str] = {}

        if user_input is not None:
            name = str(user_input[CONF_NAME]).strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                # One entry per name (SPEC §2: one instance per name).
                await self.async_set_unique_id(name.casefold())
                self._abort_if_unique_id_configured()

            if not errors:
                try:
                    site = validate_site(user_input.get(CONF_SITE))
                    _validate_measurement_sources(self.hass, site, user_input)
                except SiteValidationError as err:
                    errors[CONF_SITE] = err.code
                else:
                    data = {CONF_NAME: name, **_structural_data(site, user_input)}
                    return self.async_create_entry(title=name, data=data)

        # First render (or re-render after an error): default location from
        # hass.config, default site from const, keep just-entered values.
        defaults = _current_values(user_input or getattr(self, "_setup_draft", None), hass_config=self.hass.config)
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
                beam_gain=defaults["beam_gain"],
            ),
            errors=errors,
        )

    async def async_step_advanced(self, user_input=None) -> ConfigFlowResult:
        self._advanced_mode = True
        return await self.async_step_user(user_input)

    async def async_step_guided(self, user_input=None) -> ConfigFlowResult:
        """Location first; panels are accumulated privately until final review."""
        errors = {}
        defaults = _current_values(user_input, hass_config=self.hass.config)
        if user_input is not None:
            if not str(user_input.get(CONF_NAME, "")).strip():
                errors[CONF_NAME] = "name_required"
            else:
                self._setup_draft = {**user_input, CONF_FETCH_INTERVAL: defaults["fetch_interval"],
                                     CONF_RECOMPUTE_INTERVAL: defaults["recompute_interval"]}
                self._setup_draft[CONF_SITE] = {
                    "latitude": float(user_input[CONF_LATITUDE]), "longitude": float(user_input[CONF_LONGITUDE]),
                    "planes": [], "groups": [],
                }
                return await self.async_step_guided_panel()
        return self.async_show_form(step_id="guided", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
            vol.Required(CONF_LATITUDE, default=defaults[CONF_LATITUDE]): _guided_number(-90, 90),
            vol.Required(CONF_LONGITUDE, default=defaults[CONF_LONGITUDE]): _guided_number(-180, 180),
        }))

    async def async_step_guided_panel(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        draft = getattr(self, "_setup_draft", None)
        if draft is None:
            return await self.async_step_guided()
        defaults = {**getattr(self, "_guided_panel_defaults", {}),
                    "name": f"Panel {len(draft[CONF_SITE]['planes'])+1}"}
        if user_input is not None:
            defaults.update(user_input)
            proposed = deepcopy(draft[CONF_SITE])
            plane = {key: user_input[key] for key in ("name", "azimuth_deg", "tilt_deg", "wp")}
            plane["name"] = str(plane["name"]).strip()
            plane["horizon"] = []
            if user_input.get("actual_entity"):
                plane["actual_entity"] = user_input["actual_entity"]
            proposed["planes"].append(plane)
            group_name = str(user_input.get("group_name", "")).strip()
            group = next((g for g in proposed["groups"] if g["name"] == group_name), None)
            if not group_name:
                errors["group_name"] = "name_required"
            elif group is not None and (group["ac_limit_w"] != user_input["ac_limit_w"]
                                       or group["inverter_efficiency"] != user_input["inverter_efficiency"]):
                errors["group_name"] = "guided_group_conflict"
            else:
                if group is None:
                    group = {"name": group_name, "plane_names": [], "ac_limit_w": user_input["ac_limit_w"],
                             "inverter_efficiency": user_input["inverter_efficiency"]}
                    proposed["groups"].append(group)
                group["plane_names"].append(plane["name"])
                try:
                    site = validate_site(proposed)
                    _validate_measurement_sources(self.hass, site, {})
                except SiteValidationError as err:
                    errors["base"] = err.code
                else:
                    draft[CONF_SITE] = site.to_dict()
                    self._guided_panel_defaults = {key: user_input[key] for key in
                        ("azimuth_deg", "tilt_deg", "wp", "group_name", "ac_limit_w", "inverter_efficiency")}
                    if user_input.get("add_panel") and len(site.planes) < SITE_MAX_PLANES:
                        return await self.async_step_guided_panel()
                    return await self.async_step_guided_review()
        return self.async_show_form(step_id="guided_panel", errors=errors,
                                    data_schema=_guided_panel_schema(defaults))

    async def async_step_guided_review(self, user_input=None) -> ConfigFlowResult:
        draft = getattr(self, "_setup_draft", None)
        if draft is None or not draft[CONF_SITE]["planes"]:
            return await self.async_step_guided()
        site = draft[CONF_SITE]
        summary = "; ".join(f"{p['name']}: {p['wp']} Wp, {p['azimuth_deg']}° / {p['tilt_deg']}°" for p in site['planes'])
        groups = "; ".join(f"{g['name']}: {', '.join(g['plane_names'])}, {g['ac_limit_w']} W" for g in site['groups'])
        if user_input is not None:
            if user_input.get("advanced"):
                return await self.async_step_advanced()
            return await self.async_step_user(draft)
        return self.async_show_form(step_id="guided_review", data_schema=vol.Schema({
            vol.Required("advanced", default=False): _bool_selector(),
        }), description_placeholders={"panels": summary, "groups": groups})

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
                _validate_measurement_sources(self.hass, site, user_input)
            except SiteValidationError as err:
                errors[CONF_SITE] = err.code
            else:
                proposed = {**entry.data, **_structural_data(site, user_input)}
                previous = {**entry.data, **entry.options}
                from .core.config_changes import site_changes
                from .core.types import SiteConfig

                old_site = dict(previous[CONF_SITE])
                for key in (CONF_LATITUDE, CONF_LONGITUDE):
                    if key in previous:
                        old_site[key] = previous[key]
                changes = site_changes(SiteConfig.from_dict(old_site), SiteConfig.from_dict(proposed[CONF_SITE]))
                self._pending_reconfigure = (proposed, dict(entry.data), dict(entry.options), changes)
                return await self.async_step_confirm_changes()

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
                beam_gain=defaults["beam_gain"],
                include_name=False,
            ),
            errors=errors,
        )


    async def async_step_confirm_changes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review concrete site/learning consequences before the atomic update."""
        pending = getattr(self, "_pending_reconfigure", None)
        if pending is None:
            return await self.async_step_reconfigure()
        proposed, old_data, old_options, changes = pending
        entry = self._get_reconfigure_entry()
        if entry.data != old_data or entry.options != old_options:
            self._pending_reconfigure = None
            return await self.async_step_reconfigure()
        if user_input is not None:
            stripped_options = {key: value for key, value in entry.options.items()
                                if key not in _STRUCTURAL_OPTION_KEYS}
            # The update listener owns reload. Scheduling another reload here
            # duplicates setup and is deprecated in HA's reconfigure helper.
            self.hass.config_entries.async_update_entry(entry, data=proposed, options=stripped_options)
            self._pending_reconfigure = None
            return self.async_abort(reason="reconfigure_successful")
        language = getattr(getattr(self.hass, "config", None), "language", "en")
        german = language.startswith("de")
        if changes["model_changed"]:
            effect = ("Die Tageskorrektur lernt beschleunigt neu. Quantil-Evidenz und Drift-Verlustfolgen beginnen neu."
                      if german else "The day correction reopens learning. Quantile evidence and drift loss streaks restart.")
        else:
            effect = "Der Lernzustand bleibt erhalten." if german else "Learning state is retained."
        # Use the same empty-plane fallback as SiteConfig.from_dict: legacy
        # structural options can contain an incomplete site awaiting repair.
        before = {p["name"]: p for p in {**old_data, **old_options}[CONF_SITE].get("planes", [])}
        after = {p["name"]: p for p in proposed[CONF_SITE]["planes"]}
        module_descriptions = []
        for module, changed_fields in changes["modules"].items():
            if changed_fields == ["display_name"]:
                old_label = before[module].get("display_name") or module
                new_label = after[module].get("display_name") or module
                module_descriptions.append(f"{old_label} → {new_label}")
            else:
                module_descriptions.append(module)
        modules = ", ".join(module_descriptions) or ("keine" if german else "none")
        field_names = set(changes["site_fields"]) | {key for keys in changes["modules"].values() for key in keys}
        fields = ", ".join(sorted(field_names)) or ("keine" if german else "none")
        return self.async_show_form(
            step_id="confirm_changes", data_schema=vol.Schema({}),
            description_placeholders={"modules": modules, "fields": fields, "learning_effect": effect},
        )


class BalconySolarForecastOptionsFlow(OptionsFlow):
    """Edit the RUNTIME TUNABLES of a live install (SPEC §9/§11/§15).

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
            # The learner kill switches (SPEC §9) round-trip as plain booleans;
            # a missing key falls back to the shipped ON default so an older
            # entry that predates a switch keeps that layer active.
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
                # v0.4 quantile kill switch (SPEC §11.2, default ON).
                CONF_QUANTILES_ENABLED: bool(
                    user_input.get(
                        CONF_QUANTILES_ENABLED, DEFAULT_QUANTILES_ENABLED
                    )
                ),
                # v0.16 ensemble-band kill switch (SPEC §11.3, default OFF/opt-in).
                CONF_ENSEMBLE_ENABLED: bool(
                    user_input.get(
                        CONF_ENSEMBLE_ENABLED, DEFAULT_ENSEMBLE_ENABLED
                    )
                ),
            }
            # Legacy cleanup: entries configured before the external-comparison
            # machinery was removed may still carry a "comparison_sensors" list
            # in options (spread in above). The key has no reader anymore, so
            # drop it on this save instead of letting the dead list ride along
            # in the entry forever.
            data.pop("comparison_sensors", None)
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
    neutral defaults. The new-site template is freshly allocated on each call. Returns both the
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
        else {
            "latitude": default_lat, "longitude": default_lon,
            "planes": [{"name": "Panel 1", "azimuth_deg": 180.0,
                        "tilt_deg": 30.0, "wp": 400.0}],
            "groups": [{"name": "Inverter 1", "plane_names": ["Panel 1"], "ac_limit_w": 800.0}],
        }
    )

    # AC-meter picker defaults (Phase 4): the meter lives INSIDE the site dict
    # (merged there by _structural_data), so pull the pre-fill from the resolved
    # site — unless a just-submitted top-level value is present (error re-render
    # keeps the operator's in-progress edit).
    site_ac_entity = ""
    site_ac_invert = False
    site_albedo: float | None = None
    site_beam_gain: float | None = None
    if isinstance(default_site, dict):
        raw_ac = default_site.get(CONF_AC_ACTUAL_ENTITY)
        site_ac_entity = raw_ac if isinstance(raw_ac, str) else ""
        site_ac_invert = bool(default_site.get(CONF_AC_ACTUAL_INVERT, False))
        raw_albedo = default_site.get(CONF_SITE_ALBEDO)
        try:
            site_albedo = float(raw_albedo) if raw_albedo is not None else None
        except (TypeError, ValueError):
            site_albedo = None
        raw_beam_gain = default_site.get(CONF_SITE_BEAM_GAIN)
        try:
            site_beam_gain = (
                float(raw_beam_gain) if raw_beam_gain is not None else None
            )
        except (TypeError, ValueError):
            site_beam_gain = None

    def _bool_default(key: str, fallback: bool) -> bool:
        # Precedence mirrors the other fields: just-submitted edit > existing
        # option > shipped default. ``existing`` already merges data+options.
        if key in src:
            return bool(src[key])
        if existing is not None and key in existing:
            return bool(existing[key])
        return fallback

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
        "beam_gain": src.get(CONF_SITE_BEAM_GAIN, site_beam_gain),
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
    }
