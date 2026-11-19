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

const registryIndexes = new WeakMap();
function registryIndex(list) {
  let index = registryIndexes.get(list);
  if (!index) {
    const byId = new Map(list.map((row) => [row?.entity_id, row]));
    const rows = list.filter((row) => row?.platform === "balcony_solar_forecast"
      && !row.disabled_by && typeof row.unique_id === "string");
    index = { byId, rows };
    registryIndexes.set(list, index);
  }
  return index;
}

const resolutions = new WeakMap();

/** Legacy installed cards used a stable title/shape with transient entity IDs. */
function legacyInstallerConfig(config, title, definitions) {
  const fields = new Set(["entry_id", "type", "title", ...definitions.map(([field]) => field)]);
  return config.entry_id && config.type?.startsWith("custom:balcony-")
    && config.title === title && config[definitions[0][0]]
    && Object.keys(config).every((field) => fields.has(field));
}

/** All implicit entities come from one entry, inferred from explicit entities.
 * Ambiguous multi-site defaults deliberately show setup guidance.
 */
export function resolveEntities(config, list, hass, definitions, legacyTitle) {
  // A WS state push only changes hass, so it need not scan the registry again.
  // The state-name fallback (no registry snapshot yet) is intentionally live.
  const cached = resolutions.get(config);
  if (list && cached?.list === list && cached.definitions === definitions) return cached.ids;
  const { byId, rows } = list ? registryIndex(list) : { byId: new Map(), rows: [] };
  const matches = (row, key) => typeof row?.unique_id === "string"
    && row.unique_id === `${entryOf(row, key)}_${key}`;
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
  const legacy = legacyInstallerConfig(config, legacyTitle, definitions);
  for (const [field, key, regex] of definitions) {
    const candidate = rows.find((r) => matches(r, key) && entryOf(r, key) === entry);
    const explicit = config[field];
    if (legacy) {
      // This was an automatically selected ID. The old name may already be
      // registered to a different entity, so use only this entry's binding.
      ids[field] = candidate?.entity_id;
    } else if (explicit) {
      const row = byId.get(explicit);
      if (row?.platform === "balcony_solar_forecast") {
        ids[field] = entry && !row.disabled_by && matches(row, key) && entryOf(row, key) === entry
          ? explicit : undefined;
      } else {
        ids[field] = explicit;
      }
    } else if (entry) {
      ids[field] = candidate?.entity_id;
    } else if (!list && !config.entry_id) {
      const fallback = Object.keys(hass?.states || {}).filter((id) => regex.test(id));
      ids[field] = fallback.length === 1 ? fallback[0] : undefined;
    }
  }
  if (list) resolutions.set(config, { list, definitions, ids });
  return ids;
}

// One event subscription per connection, released after the last card detaches.
// subscribeEvents is the HA websocket connection API; it returns an async
// unsubscribe function. A pending subscription must also be released on detach.
const registryWatchers = new WeakMap();
function watchRegistry(card, hass, connection) {
  if (!card.isConnected || typeof connection?.subscribeEvents !== "function") return;
  if (card._registryWatchConnection !== connection) {
    stopRegistry(card);
    card._registryWatchConnection = connection;
    card._registryList = null;
    card._registryRequest = null;
  }
  let watch = registryWatchers.get(connection);
  if (!watch) {
    watch = { cards: new Set(), unsubscribe: null };
    registryWatchers.set(connection, watch);
    Promise.resolve().then(() => connection.subscribeEvents(() => {
      registryCache.delete(connection);
      for (const current of watch.cards) {
        current._registryList = null;
        current._registryRequest = null;
        ensureRegistry(current, current._hass || hass);
      }
    }, "entity_registry_updated")).then((unsubscribe) => {
      if (!watch.cards.size) unsubscribe();
      else watch.unsubscribe = unsubscribe;
    }).catch(() => {
      if (registryWatchers.get(connection) === watch) registryWatchers.delete(connection);
    });
  }
  watch.cards.add(card);
}

export function stopRegistry(card) {
  const connection = card._registryWatchConnection;
  const watch = connection && registryWatchers.get(connection);
  if (watch) {
    watch.cards.delete(card);
    if (!watch.cards.size) {
      if (watch.unsubscribe) watch.unsubscribe();
      registryWatchers.delete(connection);
      registryCache.delete(connection);
    }
  }
  card._registryWatchConnection = null;
}

/** Retain discovery across renders, while tracking renames/removals. */
export function ensureRegistry(card, hass) {
  const connection = connectionKey(hass);
  watchRegistry(card, hass, connection);
  if (card._registryConnection !== connection) {
    card._registryConnection = connection;
    card._registryList = null;
    card._registryRequest = null;
  }
  if (card._registryList) return Promise.resolve(card._registryList);
  if (card._registryRequest) return card._registryRequest;
  const pending = entityRegistry(hass).then((list) => {
    if (card._registryConnection !== connection || card._registryRequest !== pending) return null;
    card._registryRequest = null;
    if (!list) return null;
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
