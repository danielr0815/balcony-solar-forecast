"""Integration-wide services: ``get_forecast``, ``import_bootstrap``,
``dump_shademap``, ``rollback_learners``, ``install_dashboard``,
``suggest_shade_groups``, ``get_shade_profile`` and ``get_issued_forecast``.

  * ``get_issued_forecast`` (SPEC §15.4, ``SupportsResponse.ONLY``): return the
    forecast AS IT WAS ISSUED for one past LOCAL date, read straight from the
    store's 90-day issued ring — the read-only source behind the power-history
    card's dashed forecast line on PAST days. It never recomputes from today's
    learned state (no hindsight); a date with no archived snapshot yields
    ``available: False`` (the card draws no line) rather than an error.

  * ``get_shade_profile`` (SPEC §15, ``SupportsResponse.ONLY``): compute the
    sun-path + learned-shade profile for a given module/date WITHOUT changing the
    coordinator's live diagram selection — the read-only source behind the
    shade-profile card's comparison-date overlay. Defaults module/date to the
    coordinator's current selection; the compute (``build_shade_profile_for``)
    reuses the same pure geometry + read-time pool as the live diagram but bypasses
    the single-slot memo so an ad-hoc query never evicts the primary entry.

  * ``suggest_shade_groups`` (SPEC §5, ``SupportsResponse.ONLY``): compare every
    plane's individually-learned shademap channel bin-wise and return a
    similarity matrix plus a data-driven grouping suggestion (complete-linkage
    agglomeration over the n-weighted mean tau difference), so the operator no
    longer eyeballs the polar tables to decide shade groups. The similarity math
    is pure + HA-free (:func:`shademap.suggest_shade_groups`); this layer only
    resolves the target coordinator, hands it the site's plane names + the live
    ``ShademapState``, and adds the CURRENT grouping for comparison.

  * ``install_dashboard`` (SPEC §14.3, ``SupportsResponse.OPTIONAL``): write the
    observability dashboard config — with THIS install's real entity ids — into
    a UI-created (empty) storage-mode dashboard, so the operator no longer
    copy-pastes the reference YAML and hand-edits object_ids. Idempotent: a
    re-run refreshes the config (e.g. after an integration update). The config
    shaping is pure and lives in :mod:`._dashboard` (unit-tested bare); this
    layer resolves the entity registry + the live lovelace collection and guards
    against clobbering a dashboard the operator authored. It NEVER creates a
    dashboard registry entry nor a second ``DashboardsCollection`` (which could
    wipe entries on later UI edits) — it only writes via the existing
    ``LovelaceStorage.async_save``.

Owner: platforms. ALL services are registered once from ``async_setup``
(quality-scale ``action-setup``) and stay registered for the lifetime of HA —
independent of config-entry load state, so callers get a clear
ServiceValidationError instead of "Service not found" while no entry is
loaded. The ``get_forecast`` response builder itself stays in ``sensor.py``
(imported lazily by its handler).

  * ``import_bootstrap`` (SPEC §6): ingest the offline backfill JSON to pre-seed
    the day-ahead bias + shademap learner states. The heavy lifting (schema
    validation, clamping, n-credit capping, persistence) belongs to the store /
    coordinator; this layer only resolves the target entry, loads the payload
    from an inline object/string or an allowed file path, and forwards it to
    ``coordinator.async_import_bootstrap(dict)``.
  * ``dump_shademap`` (SPEC §5, ``SupportsResponse.ONLY``): return the learned
    shademap as a polar table (sun azimuth x elevation) per channel so the
    operator can eyeball it against known obstructions. The polar-table shaping
    is a pure, HA-free presentation concern implemented + tested here
    (:func:`build_polar_table`); the coordinator only hands us the raw
    ``ShademapState`` via ``get_shademap_state()``.

Both handlers degrade gracefully: a coordinator that does not (yet) implement
the backing method raises a clear ``ServiceValidationError`` (import) or yields
an empty dump, never an unhandled traceback.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from ._dashboard import (
    build_dashboard_config,
    collect_entity_map,
    config_has_cards,
    is_managed,
    missing_entity_keys,
)
from .const import (
    DOMAIN,
    INTEGRATION_VERSION,
    LEARNER_SNAPSHOT_RING,
    SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX,
    SERVICE_DUMP_SHADEMAP,
    SERVICE_GET_FORECAST,
    SERVICE_GET_ISSUED_FORECAST,
    SERVICE_GET_SHADE_PROFILE,
    SERVICE_IMPORT_BOOTSTRAP,
    SERVICE_INSTALL_DASHBOARD,
    SERVICE_ROLLBACK_LEARNERS,
    SERVICE_SUGGEST_SHADE_GROUPS,
    SHADE_SIM_MAX_MEAN_DIFF,
    SHADE_SIM_MIN_COMMON_BINS,
    SHADEMAP_AZ_BIN_DEG,
    SHADEMAP_EL_BIN_DEG,
)
from .core.shademap import suggest_shade_groups

# Service field names.
ATTR_ENTRY_ID = "entry_id"
ATTR_PAYLOAD = "payload"
ATTR_PATH = "path"
ATTR_SNAPSHOTS_BACK = "snapshots_back"
ATTR_DASHBOARD = "dashboard"
ATTR_OVERWRITE = "overwrite"
ATTR_MAX_DIFF = "max_diff"
ATTR_MIN_COMMON_BINS = "min_common_bins"
ATTR_MODULE = "module"
ATTR_DATE = "date"

# The default UI dashboard URL the operator is told to create (must contain a
# hyphen — Home Assistant rejects single-word storage-dashboard url_paths).
DEFAULT_DASHBOARD_URL = "balcony-solar"

IMPORT_BOOTSTRAP_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        # payload may be a mapping (object selector) or a JSON string.
        vol.Optional(ATTR_PAYLOAD): vol.Any(dict, str),
        vol.Optional(ATTR_PATH): str,
    }
)

DUMP_SHADEMAP_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): str})

SUGGEST_SHADE_GROUPS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        # Similarity threshold in tau units, bounded (0, 0.5]; default from const.
        vol.Optional(ATTR_MAX_DIFF, default=SHADE_SIM_MAX_MEAN_DIFF): vol.All(
            vol.Coerce(float),
            vol.Range(min=0, max=0.5, min_included=False, max_included=True),
        ),
        # Minimum shared bins before a pair is treated as evidence, >= 1.
        vol.Optional(
            ATTR_MIN_COMMON_BINS, default=SHADE_SIM_MIN_COMMON_BINS
        ): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

GET_FORECAST_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): str})

GET_SHADE_PROFILE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        # Module/plane to profile; defaults to the coordinator's current pick.
        vol.Optional(ATTR_MODULE): str,
        # Local date (ISO YYYY-MM-DD); defaults to the coordinator's current pick.
        vol.Optional(ATTR_DATE): str,
    }
)

GET_ISSUED_FORECAST_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        # Local date (ISO YYYY-MM-DD) whose ISSUED day-ahead curve to return;
        # required (there is no meaningful default — the card always names a day).
        vol.Required(ATTR_DATE): str,
    }
)

ROLLBACK_LEARNERS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Optional(ATTR_SNAPSHOTS_BACK, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=LEARNER_SNAPSHOT_RING)
        ),
    }
)

INSTALL_DASHBOARD_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Optional(ATTR_DASHBOARD, default=DEFAULT_DASHBOARD_URL): str,
        vol.Optional(ATTR_OVERWRITE, default=False): bool,
    }
)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register ALL integration services once, from ``async_setup``.

    Quality-scale ``action-setup``: services are registered when the component
    loads — independent of any config entry — and stay registered, so an
    automation calling e.g. ``get_forecast`` during a startup Open-Meteo outage
    gets a clear ServiceValidationError ("no entry set up / loaded") instead of
    "Service not found". Every handler resolves its target coordinator(s)
    dynamically from ``hass.data[DOMAIN]``, so registration needs no live
    coordinator. Kept idempotent as a belt-and-braces guard.
    """
    if not hass.services.has_service(DOMAIN, SERVICE_GET_FORECAST):

        async def _get_forecast(call: ServiceCall) -> ServiceResponse:
            # Lazy import: the response builder lives in sensor.py (its tests
            # exercise it there); importing it at module load would pull the
            # whole sensor platform in before HA needs it.
            from .sensor import _build_forecast_response

            return _build_forecast_response(hass, call.data.get(ATTR_ENTRY_ID))

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_FORECAST,
            _get_forecast,
            schema=GET_FORECAST_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_BOOTSTRAP):

        async def _import_bootstrap(call: ServiceCall) -> ServiceResponse:
            return await _handle_import_bootstrap(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_BOOTSTRAP,
            _import_bootstrap,
            schema=IMPORT_BOOTSTRAP_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DUMP_SHADEMAP):

        async def _dump_shademap(call: ServiceCall) -> ServiceResponse:
            return _handle_dump_shademap(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_DUMP_SHADEMAP,
            _dump_shademap,
            schema=DUMP_SHADEMAP_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_ROLLBACK_LEARNERS):

        async def _rollback_learners(call: ServiceCall) -> ServiceResponse:
            return await _handle_rollback_learners(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_ROLLBACK_LEARNERS,
            _rollback_learners,
            schema=ROLLBACK_LEARNERS_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_INSTALL_DASHBOARD):

        async def _install_dashboard(call: ServiceCall) -> ServiceResponse:
            return await _handle_install_dashboard(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_INSTALL_DASHBOARD,
            _install_dashboard,
            schema=INSTALL_DASHBOARD_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SUGGEST_SHADE_GROUPS):

        async def _suggest_shade_groups(call: ServiceCall) -> ServiceResponse:
            return _handle_suggest_shade_groups(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SUGGEST_SHADE_GROUPS,
            _suggest_shade_groups,
            schema=SUGGEST_SHADE_GROUPS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GET_SHADE_PROFILE):

        async def _get_shade_profile(call: ServiceCall) -> ServiceResponse:
            return _handle_get_shade_profile(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_SHADE_PROFILE,
            _get_shade_profile,
            schema=GET_SHADE_PROFILE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GET_ISSUED_FORECAST):

        async def _get_issued_forecast(call: ServiceCall) -> ServiceResponse:
            return _handle_get_issued_forecast(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_ISSUED_FORECAST,
            _get_issued_forecast,
            schema=GET_ISSUED_FORECAST_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )


def _resolve_single_coordinator(hass: HomeAssistant, entry_id: str | None) -> Any:
    """Return the one coordinator to import into, or raise a clear error.

    ``import_bootstrap`` writes state, so it must target exactly one entry: the
    named ``entry_id`` if given, else the sole configured entry. Zero or (with
    no id) multiple entries is a user error surfaced as ServiceValidationError.
    """
    store = hass.data.get(DOMAIN, {})
    if not store:
        raise ServiceValidationError("No Balcony Solar Forecast entry is set up.")
    if entry_id is not None:
        coordinator = store.get(entry_id)
        if coordinator is None:
            raise ServiceValidationError(f"Unknown config entry: {entry_id}")
        return coordinator
    if len(store) != 1:
        raise ServiceValidationError(
            "Multiple sites are configured; specify 'entry_id'."
        )
    return next(iter(store.values()))


# ---------------------------------------------------------------------------
# import_bootstrap
# ---------------------------------------------------------------------------


async def _handle_import_bootstrap(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Load the bootstrap payload and forward it to the target coordinator."""
    payload = call.data.get(ATTR_PAYLOAD)
    path = call.data.get(ATTR_PATH)

    if (payload is None) == (path is None):
        # Neither or both supplied.
        raise ServiceValidationError(
            "Provide exactly one of 'payload' or 'path'."
        )

    data = await _load_bootstrap_data(hass, payload, path)

    coordinator = _resolve_single_coordinator(hass, call.data.get(ATTR_ENTRY_ID))
    importer = getattr(coordinator, "async_import_bootstrap", None)
    if not callable(importer):
        raise ServiceValidationError(
            "This installation does not support bootstrap import."
        )
    try:
        result = await importer(data)
    except ValueError as err:
        # The store rejects a schema / site-signature mismatch with ValueError;
        # surface it as a user error, not an unhandled traceback.
        raise ServiceValidationError(f"Bootstrap rejected: {err}") from err
    # The coordinator returns a small summary dict (counts of ingested cells /
    # bins, rejected schema, etc.). Surface it as the service response.
    return {"result": result} if isinstance(result, dict) else {"result": {}}


async def _load_bootstrap_data(
    hass: HomeAssistant, payload: Any, path: str | None
) -> dict[str, Any]:
    """Resolve the bootstrap payload to a dict from inline data or a file."""
    if path is not None:
        if not hass.config.is_allowed_path(path):
            raise ServiceValidationError(
                f"Path is not inside an allowed directory: {path}"
            )
        try:
            text = await hass.async_add_executor_job(_read_text, path)
        except OSError as err:
            raise ServiceValidationError(
                f"Could not read bootstrap file: {err}"
            ) from err
        return _parse_json_object(text)

    # Inline payload: a mapping passes through; a string is parsed as JSON.
    if isinstance(payload, str):
        return _parse_json_object(payload)
    if isinstance(payload, dict):
        return payload
    raise ServiceValidationError(
        "The bootstrap payload must be a JSON object or a JSON string."
    )


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as err:
        raise ServiceValidationError(
            f"The bootstrap payload is not valid JSON: {err}"
        ) from err
    if not isinstance(parsed, dict):
        raise ServiceValidationError(
            "The bootstrap payload must decode to a JSON object."
        )
    return parsed


# ---------------------------------------------------------------------------
# rollback_learners
# ---------------------------------------------------------------------------


async def _handle_rollback_learners(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Restore both learner states from the target entry's rollback ring."""
    coordinator = _resolve_single_coordinator(hass, call.data.get(ATTR_ENTRY_ID))
    restorer = getattr(coordinator, "async_rollback_learners", None)
    if not callable(restorer):
        raise ServiceValidationError(
            "This installation does not support learner rollback."
        )
    try:
        result = await restorer(call.data.get(ATTR_SNAPSHOTS_BACK, 1))
    except ValueError as err:
        # Empty ring etc. — a user-visible condition, not a traceback.
        raise ServiceValidationError(f"Rollback rejected: {err}") from err
    return {"result": result} if isinstance(result, dict) else {"result": {}}


# ---------------------------------------------------------------------------
# install_dashboard
# ---------------------------------------------------------------------------


async def _handle_install_dashboard(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Write the observability dashboard into a UI-created empty dashboard.

    The operator creates an EMPTY dashboard once (Settings → Dashboards → Add,
    URL ``balcony-solar``); this action fills it with the full observability
    config wired to THIS install's real entity ids, and refreshes it on every
    re-run. It only ever writes through the existing ``LovelaceStorage`` for that
    url_path (``hass.data[LOVELACE_DATA].dashboards``); it never touches the
    dashboard registry / collection.
    """
    from homeassistant.components.lovelace.const import (
        LOVELACE_DATA,
        ConfigNotFound,
    )
    from homeassistant.helpers import entity_registry as er

    coordinator = _resolve_single_coordinator(hass, call.data.get(ATTR_ENTRY_ID))
    entry = getattr(coordinator, "entry", None)
    if entry is None:
        raise ServiceValidationError(
            "The selected site has no config entry; cannot resolve its entities."
        )
    entry_id = entry.entry_id

    url_path = call.data.get(ATTR_DASHBOARD, DEFAULT_DASHBOARD_URL)
    overwrite = bool(call.data.get(ATTR_OVERWRITE, False))

    dash = _resolve_storage_dashboard(hass, LOVELACE_DATA, url_path)

    # SAFETY GATE: never clobber a dashboard the operator authored. A config we
    # generated (carries the marker) or an empty one is overwritten freely — the
    # idempotent refresh; anything else needs an explicit ``overwrite: true``.
    try:
        existing = await dash.async_load(False)
    except ConfigNotFound:
        existing = None
    if (
        config_has_cards(existing)
        and not is_managed(existing)
        and not overwrite
    ):
        raise ServiceValidationError(
            f"Dashboard '{url_path}' already contains cards this integration did "
            "not create; refusing to overwrite it. Pass 'overwrite: true' to "
            "replace it, or target a freshly created empty dashboard."
        )

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry_id)
    entity_map = collect_entity_map(entries, entry_id)
    comparison_slugs = _comparison_slugs(coordinator, entity_map)
    measured_entities = _measured_entities(coordinator)

    config = build_dashboard_config(
        entity_map=entity_map,
        comparison_slugs=comparison_slugs,
        measured_entities=measured_entities,
        version=INTEGRATION_VERSION,
    )
    try:
        await dash.async_save(config)
    except HomeAssistantError as err:
        # Recovery mode (async_save raises) — surface as a user error.
        raise ServiceValidationError(
            f"Could not write dashboard '{url_path}': {err}"
        ) from err

    views = config.get("views", [])
    return {
        "result": {
            "dashboard": url_path,
            "views": len(views),
            "cards": sum(len(v.get("cards", [])) for v in views),
            "missing_entities": missing_entity_keys(entity_map),
        }
    }


def _resolve_storage_dashboard(
    hass: HomeAssistant, lovelace_data_key: Any, url_path: str
) -> Any:
    """Return the storage-mode ``LovelaceConfig`` for ``url_path`` or raise.

    Raises a clear ServiceValidationError when lovelace is not set up, the
    url_path does not exist (listing the available storage dashboards + the
    creation hint), or the dashboard is YAML-managed (not writable).
    """
    lovelace = hass.data.get(lovelace_data_key)
    if lovelace is None:
        raise ServiceValidationError(
            "Lovelace is not set up yet; open the dashboards UI once and retry."
        )
    dashboards = getattr(lovelace, "dashboards", None) or {}
    dash = dashboards.get(url_path)
    if dash is None:
        available = sorted(k for k in dashboards if isinstance(k, str))
        raise ServiceValidationError(
            f"No dashboard with URL '{url_path}' exists. First create an empty "
            "one: Settings → Dashboards → Add dashboard (the URL field must "
            f"contain a hyphen, e.g. '{DEFAULT_DASHBOARD_URL}'), then run this "
            f"action again. Available storage dashboards: {available or '(none)'}."
        )
    if getattr(dash, "mode", None) != "storage":
        raise ServiceValidationError(
            f"Dashboard '{url_path}' is YAML-managed and cannot be written by "
            "this action; create a new UI (storage-mode) dashboard and target it."
        )
    return dash


def _comparison_slugs(
    coordinator: Any, entity_map: dict[str, str]
) -> list[tuple[str, str]]:
    """``[(name, entity_id)]`` for the configured comparison MAE sensors.

    Reuses ``sensor._configured_comparisons`` (lazy import — it lives on the
    HA-importing sensor platform) and resolves each comparison's real entity_id
    from ``entity_map`` via its slug-keyed unique_id suffix. A comparison with no
    registered MAE sensor (not yet materialised) is skipped.
    """
    from .sensor import _configured_comparisons

    out: list[tuple[str, str]] = []
    for comparison in _configured_comparisons(coordinator):
        key = f"{SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX}_{comparison.slug}"
        entity_id = entity_map.get(key)
        if entity_id:
            out.append((comparison.name, entity_id))
    return out


def _measured_entities(coordinator: Any) -> list[tuple[str, str]]:
    """The planes' ``(name, measured DC-power entity id)`` pairs.

    Order-preserving and deduped on entity id; the plane NAME (M1…M8) labels
    the measured-power graph rows so they are not shown under the per-port
    sensors' own ambiguous friendly names. Planes without an ``actual_entity``
    are skipped.
    """
    site = getattr(coordinator, "_site", None)
    if site is None:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for plane in getattr(site, "planes", ()):
        entity_id = getattr(plane, "actual_entity", None)
        if isinstance(entity_id, str) and entity_id and entity_id not in seen:
            seen.add(entity_id)
            name = getattr(plane, "name", None)
            out.append((str(name) if name else entity_id, entity_id))
    return out


# ---------------------------------------------------------------------------
# dump_shademap
# ---------------------------------------------------------------------------


def _handle_dump_shademap(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Return a per-entry polar-table dump of every channel's shademap."""
    entry_id = call.data.get(ATTR_ENTRY_ID)
    entries: dict[str, Any] = {}
    store = hass.data.get(DOMAIN, {})
    for eid, coordinator in store.items():
        if entry_id is not None and eid != entry_id:
            continue
        entries[eid] = _dump_one(coordinator)
    return {"entries": entries}


def _dump_one(coordinator: Any) -> dict[str, Any]:
    """Polar-table dump for a single coordinator (empty if unsupported)."""
    getter = getattr(coordinator, "get_shademap_state", None)
    if not callable(getter):
        return {"channels": {}, "available": False}
    try:
        state = getter()
    except Exception as err:  # noqa: BLE001 -- a dump must never raise
        return {"channels": {}, "error": repr(err)}
    return build_polar_table(state)


# ---------------------------------------------------------------------------
# suggest_shade_groups
# ---------------------------------------------------------------------------


def _handle_suggest_shade_groups(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Compare the per-plane shademaps and propose a data-driven grouping.

    Resolves the single target coordinator, reads the site's plane names (config
    order) and the live shademap state via ``get_shademap_state``, runs the pure
    :func:`suggest_shade_groups`, and returns the suggestion alongside the
    CURRENT grouping (each plane's ``shade_channel``) so the operator can compare
    the data-driven proposal against what is configured today.
    """
    coordinator = _resolve_single_coordinator(hass, call.data.get(ATTR_ENTRY_ID))
    getter = getattr(coordinator, "get_shademap_state", None)
    if not callable(getter):
        raise ServiceValidationError(
            "This installation does not support shade-group suggestions."
        )
    site = getattr(coordinator, "_site", None)
    planes = tuple(getattr(site, "planes", ()) or ())
    if not planes:
        raise ServiceValidationError(
            "The selected site has no planes to compare."
        )

    state = getter()
    result = suggest_shade_groups(
        state,
        [plane.name for plane in planes],
        max_diff=call.data.get(ATTR_MAX_DIFF, SHADE_SIM_MAX_MEAN_DIFF),
        min_common_bins=call.data.get(
            ATTR_MIN_COMMON_BINS, SHADE_SIM_MIN_COMMON_BINS
        ),
    )
    # Also surface the CURRENT grouping (plane -> its shademap channel) so the
    # response is directly comparable against the suggestion.
    result["current_groups"] = {plane.name: plane.shade_channel for plane in planes}
    return {"result": result}


# ---------------------------------------------------------------------------
# get_shade_profile
# ---------------------------------------------------------------------------


def _handle_get_shade_profile(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Return the sun-path + learned-shade profile for a module/date, read-only.

    The on-demand analysis behind the shade-profile card's comparison-date
    overlay (SPEC §15): resolve the single target coordinator, default ``module``
    to its current shade-profile selection and ``date`` to its current selected
    date, validate the module against the site's plane names, and compute the
    profile via ``build_shade_profile_for`` WITHOUT mutating the coordinator's
    live selection (an ad-hoc query must not evict the primary diagram memo). The
    response is the same parallel-array + summary dict the sensor exposes (incl.
    ``sample_n``), under ``result``.
    """
    coordinator = _resolve_single_coordinator(hass, call.data.get(ATTR_ENTRY_ID))
    builder = getattr(coordinator, "build_shade_profile_for", None)
    names_getter = getattr(coordinator, "shade_profile_plane_names", None)
    if not callable(builder) or not callable(names_getter):
        raise ServiceValidationError(
            "This installation does not support the shade-profile diagram."
        )

    names = list(names_getter())
    # module: default to the coordinator's current selection (always valid).
    module = call.data.get(ATTR_MODULE)
    if module is None:
        module = coordinator.shade_profile_module
    if module not in names:
        valid = ", ".join(names) or "(none)"
        raise ServiceValidationError(
            f"Unknown module '{module}'. Valid modules: {valid}."
        )

    # date: default to the coordinator's current selection; ISO-parse otherwise,
    # surfacing garbage as a clear ServiceValidationError (never a traceback).
    raw_date = call.data.get(ATTR_DATE)
    if raw_date is None:
        day = coordinator.shade_profile_date
    else:
        try:
            day = date.fromisoformat(str(raw_date))
        except (ValueError, TypeError) as err:
            raise ServiceValidationError(
                f"Invalid date '{raw_date}'; expected ISO format YYYY-MM-DD."
            ) from err

    return {"result": builder(module, day)}


# ---------------------------------------------------------------------------
# get_issued_forecast
# ---------------------------------------------------------------------------


def _handle_get_issued_forecast(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Return the ISSUED day-ahead forecast curve for one past LOCAL date.

    The read-only source behind the power-history card's dashed forecast line for
    PAST days (part 2b): the card can only reconstruct today's line from the live
    ``wh_period`` attribute, so a historical day reads back the forecast AS IT WAS
    ISSUED from the store's 90-day issued ring — the frozen ~01:30 day-ahead stand,
    with NO hindsight (this is the whole point: it must NOT be recomputed from
    today's learned state).

    Resolve the single target coordinator, ISO-parse ``date`` (garbage → a clear
    ServiceValidationError, never a traceback), and look the snapshot up in the
    ring by its LOCAL-date key (exactly how ``snapshot_issued`` records it). A date
    with no snapshot is NOT an error — the card treats ``available: False`` as "no
    archived forecast, draw no line". A hit slices BOTH curves to the requested
    local day with :func:`_filter_hourly_to_local_day` — the SAME helper the drift
    monitor / nightly scorer use — so the returned ``hourly_wh`` (the served, i.e.
    corrected, curve; the raw physics curve rides along under ``raw_hourly_wh``) is
    exactly the day the nightly job scored against measured actuals.

    BOTH branches additionally carry ``oldest_available``: the OLDEST archived
    date in the ring (``store.issued_dates()`` is sorted ascending, so its first
    entry), or ``None`` on an empty ring. The card turns a miss into an honest
    "archive since <date>" hint instead of a bare emptiness the operator would
    misread as "the forecast stopped updating".
    """
    # Lazy imports: ``_glue_util`` pulls in ``.core.types`` (DriftState /
    # ForecastResult), which would create an import cycle if done at module load —
    # ``_services`` is imported from the package ``__init__`` before the coordinator
    # concern-modules finish initialising. ``IssuedSnapshot`` is read straight from
    # the ``.core.types`` submodule (not the ``.core`` package facade).
    from ._glue_util import _filter_hourly_to_local_day, _round3
    from .core.types import IssuedSnapshot

    coordinator = _resolve_single_coordinator(hass, call.data.get(ATTR_ENTRY_ID))
    store = getattr(coordinator, "_store", None)
    getter = getattr(store, "get_issued", None)
    if not callable(getter):
        raise ServiceValidationError(
            "This installation does not support issued-forecast lookup."
        )

    raw_date = call.data[ATTR_DATE]
    try:
        day = date.fromisoformat(str(raw_date))
    except (ValueError, TypeError) as err:
        raise ServiceValidationError(
            f"Invalid date '{raw_date}'; expected ISO format YYYY-MM-DD."
        ) from err
    iso = day.isoformat()

    # Oldest archived day (ascending ring keys → first entry), None when the
    # ring is empty or a legacy store lacks the accessor.
    dates_getter = getattr(store, "issued_dates", None)
    issued = dates_getter() if callable(dates_getter) else []
    oldest = issued[0] if issued else None

    stored = getter(iso)
    if stored is None:
        # Missing day: not an error — the card draws no forecast line.
        return {
            "result": {
                "date": iso,
                "available": False,
                "oldest_available": oldest,
            }
        }

    snap = IssuedSnapshot.from_dict(stored)
    # corrected == the ISSUED (served) curve; fall back to raw exactly like the
    # nightly scorer (``snap.corrected_hourly_wh or snap.raw_hourly_wh``). Both are
    # re-sliced to the local day so a legacy full-horizon v1 entry is trimmed too.
    corrected = _filter_hourly_to_local_day(
        snap.corrected_hourly_wh or snap.raw_hourly_wh, iso
    )
    raw = _filter_hourly_to_local_day(
        snap.raw_hourly_wh or snap.corrected_hourly_wh, iso
    )
    return {
        "result": {
            "date": iso,
            "available": True,
            "issued_at": snap.issued_at,
            "oldest_available": oldest,
            "hourly_wh": {k: _round3(v) for k, v in corrected.items()},
            "raw_hourly_wh": {k: _round3(v) for k, v in raw.items()},
        }
    }


# ---------------------------------------------------------------------------
# Pure polar-table builder (HA-free; unit-tested without Home Assistant).
# ---------------------------------------------------------------------------


def build_polar_table(state: Any) -> dict[str, Any]:
    """Shape a :class:`ShademapState` into a per-channel polar table.

    Accepts a ``ShademapState``, its ``to_dict()`` mapping, or any object with a
    ``channels`` mapping of ``{channel: {bin_key: bin}}`` where ``bin_key`` is
    ``"az_idx:el_idx:half"`` (SPEC §5). Returns::

        {"channels": {channel: {"bins": [
            {"sun_az": <bin-centre deg 0=N>,
             "sun_el": <bin-centre deg>,
             "half": 0|1,           # 0 = before summer solstice, 1 = after
             "tau": <learned transmittance>,
             "n": <sample count>},
            ... sorted by (half, sun_az, sun_el)
        ]}}}

    Bin centres are reconstructed from the indices and the const bin widths, so
    the operator (or a card) can plot azimuth/elevation directly. Malformed bin
    keys or bins are skipped rather than raising — a diagnostic dump must be
    total.
    """
    channels_in = _channels_of(state)
    out: dict[str, dict[str, Any]] = {}
    for channel, bins in channels_in.items():
        if not isinstance(channel, str) or not isinstance(bins, dict):
            continue
        rows: list[dict[str, Any]] = []
        for bin_key, bin_val in bins.items():
            parsed = _parse_bin_key(bin_key)
            if parsed is None:
                continue
            az_idx, el_idx, half = parsed
            tau, n = _tau_n_of(bin_val)
            if tau is None:
                continue
            rows.append(
                {
                    "sun_az": round((az_idx + 0.5) * SHADEMAP_AZ_BIN_DEG, 3),
                    "sun_el": round((el_idx + 0.5) * SHADEMAP_EL_BIN_DEG, 3),
                    "half": half,
                    "tau": round(float(tau), 4),
                    "n": int(n),
                }
            )
        rows.sort(key=lambda r: (r["half"], r["sun_az"], r["sun_el"]))
        out[channel] = {"bins": rows}
    return {"channels": out}


def _channels_of(state: Any) -> dict[str, Any]:
    """Extract the ``{channel: {bin_key: bin}}`` mapping from any input shape.

    Duck-typed: accepts a ``ShademapState`` (``.channels`` attribute), its
    ``to_dict()`` mapping (``["channels"]``), or any object exposing a
    ``channels`` mapping. Kept free of a concrete ``ShademapState`` import so
    the pure builder stays importable without the full HA core package.
    """
    if isinstance(state, dict):
        chans = state.get("channels", {})
        return chans if isinstance(chans, dict) else {}
    chans = getattr(state, "channels", None)
    return chans if isinstance(chans, dict) else {}


def _parse_bin_key(bin_key: Any) -> tuple[int, int, int] | None:
    """Parse ``"az_idx:el_idx:half"`` into ints; None if malformed."""
    if not isinstance(bin_key, str):
        return None
    parts = bin_key.split(":")
    if len(parts) != 3:
        return None
    try:
        az_idx = int(parts[0])
        el_idx = int(parts[1])
        half = int(parts[2])
    except (ValueError, TypeError):
        return None
    if half not in (0, 1):
        return None
    return az_idx, el_idx, half


def _tau_n_of(bin_val: Any) -> tuple[float | None, int]:
    """Extract (tau, n) from a ShademapBin, its dict, or a bare number."""
    tau = getattr(bin_val, "tau", None)
    n = getattr(bin_val, "n", None)
    if tau is not None:
        try:
            return float(tau), int(n or 0)
        except (ValueError, TypeError):
            return None, 0
    if isinstance(bin_val, dict):
        raw_tau = bin_val.get("tau")
        if raw_tau is None:
            return None, 0
        try:
            return float(raw_tau), int(bin_val.get("n", 0) or 0)
        except (ValueError, TypeError):
            return None, 0
    return None, 0
