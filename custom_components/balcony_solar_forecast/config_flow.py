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
assign it).

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
    CONF_FETCH_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    DEFAULT_SITE,
    DOMAIN,
    FETCH_INTERVAL_SECONDS,
    RECOMPUTE_INTERVAL_SECONDS,
)

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


def _user_schema(
    *,
    name: str,
    latitude: float,
    longitude: float,
    fetch_interval: int,
    recompute_interval: int,
    site: dict[str, Any],
    include_name: bool = True,
) -> vol.Schema:
    """Schema for the user step / options step, pre-filled with defaults.

    ``include_name`` is False for the options step, where the name (and its
    unique-id) is immutable after setup.
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
                data = {
                    CONF_LATITUDE: lat,
                    CONF_LONGITUDE: lon,
                    CONF_FETCH_INTERVAL: int(user_input[CONF_FETCH_INTERVAL]),
                    CONF_RECOMPUTE_INTERVAL: int(
                        user_input[CONF_RECOMPUTE_INTERVAL]
                    ),
                    CONF_SITE: site_dict,
                }
                return self.async_create_entry(title="", data=data)

        merged = {**self.config_entry.data, **self.config_entry.options}
        defaults = _current_values(user_input, existing=merged)
        # Name is immutable in options; omit it from the schema.
        return self.async_show_form(
            step_id="init",
            data_schema=_user_schema(**defaults, include_name=False),
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
    }
