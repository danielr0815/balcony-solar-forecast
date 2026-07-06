"""Integration-wide services: ``import_bootstrap`` and ``dump_shademap``.

Owner: platforms. Registered once (from ``async_setup_entry`` on the first
entry) and removed when the last entry unloads, mirroring the ``get_forecast``
service in ``sensor.py``.

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
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError

from .const import (
    DOMAIN,
    LEARNER_SNAPSHOT_RING,
    SERVICE_DUMP_SHADEMAP,
    SERVICE_IMPORT_BOOTSTRAP,
    SERVICE_ROLLBACK_LEARNERS,
    SHADEMAP_AZ_BIN_DEG,
    SHADEMAP_EL_BIN_DEG,
)

# Service field names.
ATTR_ENTRY_ID = "entry_id"
ATTR_PAYLOAD = "payload"
ATTR_PATH = "path"
ATTR_SNAPSHOTS_BACK = "snapshots_back"

IMPORT_BOOTSTRAP_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        # payload may be a mapping (object selector) or a JSON string.
        vol.Optional(ATTR_PAYLOAD): vol.Any(dict, str),
        vol.Optional(ATTR_PATH): str,
    }
)

DUMP_SHADEMAP_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): str})

ROLLBACK_LEARNERS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Optional(ATTR_SNAPSHOTS_BACK, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=LEARNER_SNAPSHOT_RING)
        ),
    }
)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the learner services once for the whole integration.

    Idempotent: safe to call from every entry's setup; only the first call
    actually registers. Removal happens in ``__init__.async_unload_entry`` when
    the last entry unloads.
    """
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


@callback
def async_remove_services(hass: HomeAssistant) -> None:
    """Remove the learner services (called when the last entry unloads)."""
    for name in (
        SERVICE_IMPORT_BOOTSTRAP,
        SERVICE_DUMP_SHADEMAP,
        SERVICE_ROLLBACK_LEARNERS,
    ):
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)


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
