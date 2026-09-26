/** Connection-scoped entity discovery and entry-scoped read services.
 * A hass snapshot changes on every state push; its connection is the lifetime
 * boundary. Failed lookups are evicted so reconnect/state updates can recover.
 */
const registryCache = new WeakMap();
export const connectionKey = (hass) => hass?.connection || hass;

export function entityRegistry(hass) {
  if (typeof hass?.callWS !== "function") return Promise.resolve(null);
  const connection = connectionKey(hass);
  let pending = registryCache.get(connection);
  if (!pending) {
    pending = Promise.resolve().then(() => hass.callWS({ type: "config/entity_registry/list" }))
      .then((list) => {
        if (!Array.isArray(list)) throw new Error("Invalid entity registry");
        return list;
      }).catch(() => {
        if (registryCache.get(connection) === pending) registryCache.delete(connection);
        return null;
      });
    registryCache.set(connection, pending);
  }
  return pending;
}

function entryOf(row, key) {
  return row.config_entry_id || row.unique_id.slice(0, -(key.length + 1));
}

/** All implicit entities come from one entry, inferred from explicit entities.
 * Ambiguous multi-site defaults deliberately show setup guidance instead of
 * choosing an arbitrary site. Explicit mismatched controls are omitted too.
 */
export function resolveEntities(config, list, hass, definitions) {
  const rows = (list || []).filter((row) => row?.platform === "balcony_solar_forecast"
    && !row.disabled_by && typeof row.unique_id === "string");
  const matches = (row, key) => row.unique_id.endsWith(`_${key}`);
  const entries = new Set();
  const explicitEntries = new Set();
  for (const [field, key] of definitions) {
    for (const row of rows.filter((r) => matches(r, key))) {
      entries.add(entryOf(row, key));
      if (config[field] === row.entity_id) explicitEntries.add(entryOf(row, key));
    }
  }
  const entry = config.entry_id || (explicitEntries.size === 1 ? [...explicitEntries][0]
    : explicitEntries.size === 0 && entries.size === 1 ? [...entries][0] : undefined);
  const ids = { entry_id: entry };
  for (const [field, key, regex] of definitions) {
    const candidates = rows.filter((r) => matches(r, key) && entryOf(r, key) === entry);
    if (config[field]) {
      const row = rows.find((r) => r.entity_id === config[field]);
      ids[field] = !list || !row || (entry && matches(row, key) && entryOf(row, key) === entry)
        ? config[field] : undefined;
    } else if (entry) {
      ids[field] = candidates[0]?.entity_id;
    } else if (!list && !config.entry_id) {
      const fallback = Object.keys(hass?.states || {}).filter((id) => regex.test(id));
      ids[field] = fallback.length === 1 ? fallback[0] : undefined;
    }
  }
  return ids;
}

/** Retain discovery across renders and detached cards, never across connections. */
export function ensureRegistry(card, hass) {
  const connection = connectionKey(hass);
  if (card._registryConnection !== connection) {
    card._registryConnection = connection;
    card._registryList = null;
    card._registryRequest = null;
  }
  if (card._registryList) return Promise.resolve(card._registryList);
  if (card._registryRequest) return card._registryRequest;
  const pending = entityRegistry(hass).then((list) => {
    if (card._registryConnection !== connection) return null;
    card._registryRequest = null;
    if (!list) return null; // next state push / reconnect retries
    const before = card._resolveIds(card._hass || hass);
    card._registryList = list;
    const after = card._resolveIds(card._hass || hass);
    if (JSON.stringify(before) !== JSON.stringify(after)) {
      card._liveDayKey = undefined;
      card._rendered = false;
      if (card.isConnected && card._hass) card.hass = card._hass;
    }
    return list;
  });
  card._registryRequest = pending;
  return pending;
}

export async function callEntryService(card, hass, service, data) {
  if (!card._config.entry_id) await card._ensureRegistry(hass);
  const entry = card._resolveIds(hass).entry_id;
  const response = await hass.callWS({
    type: "call_service", domain: "balcony_solar_forecast", service,
    service_data: { ...data, ...(entry ? { entry_id: entry } : {}) },
    return_response: true,
  });
  return response?.response?.result;
}
