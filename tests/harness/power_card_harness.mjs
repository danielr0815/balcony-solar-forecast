// Runtime harness for the SHIPPED power-history card JS (no build, no DOM).
//
// Loads the real custom_components/balcony_solar_forecast/frontend/
// power_history_card.js under minimal customElements/HTMLElement stubs,
// instantiates the card class via Object.create(prototype) (the constructor
// needs attachShadow, which the stubs deliberately omit — prototype methods
// are what we exercise), and drives the ACTUAL fetch paths against a stubbed
// hass.callWS. This is the runtime companion to the static greps in
// tests/test_frontend_resource.py: node --check and greps cannot catch
// async/state-machine breakage (the v0.15 property-shadowing bug was exactly
// such a runtime-only failure).
//
// Scenarios (each throws/exits 1 on failure; prints one OK line on success):
//   1. WEEK: one hourly-statistics query + CONCURRENT issued lookups for the
//      non-today days; per-day totals built with an honest GAP (null) for a
//      day whose snapshot is unavailable; today's slot uses the LIVE
//      wh_period sum; a repeat fetch is served from the per-window cache.
//   2. DAY nav to a date with available:false → the stale line is cleared the
//      moment navigation starts, and the final state is "missing" (NOT
//      "error"), with oldest_available captured for the archive-since note.
//   3. DAY nav where the service call THROWS → state "error" (+ message).
//   4. WEEK bars: today's column is the summed HOURLY energy, not the running
//      daily-mean × 24 h (which overstates a still-running day).
//   5. i18n-proof discovery: German (translated) object slugs defeat the
//      entity_id regex fallback; the entity-registry unique_id SUFFIX match
//      must still resolve both entities (and never claim a foreign platform's
//      look-alike unique_id).
//
// Run:  node tests/harness/power_card_harness.mjs
// CI:   tests/test_frontend_harness.py wraps this via subprocess (skips when
//       node is not on PATH).

import { readFile } from "node:fs/promises";

// --- browser stubs ---------------------------------------------------------
globalThis.window = { customCards: [] };
let CardClass = null;
globalThis.customElements = {
  define: (name, cls) => {
    if (name === "balcony-power-history-card") CardClass = cls;
  },
  get: () => undefined,
};
globalThis.HTMLElement = class {};

const src = await readFile(
  new URL(
    "../../custom_components/balcony_solar_forecast/frontend/power_history_card.js",
    import.meta.url,
  ),
  "utf8",
);
new Function(src)(); // the card is a plain script: no imports/exports
if (!CardClass) throw new Error("card class was not registered");

// --- helpers ----------------------------------------------------------------
function pad2(n) {
  return n < 10 ? `0${n}` : `${n}`;
}
function isoAt(offset) {
  const now = new Date();
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}
function settle(ms = 25) {
  return new Promise((r) => setTimeout(r, ms));
}
function fail(msg) {
  console.error(`FAIL: ${msg}`);
  process.exit(1);
}
function assert(cond, msg) {
  if (!cond) fail(msg);
}

/** A bare card instance: constructor state minus DOM, render stubbed out. */
function makeCard(hass) {
  const card = Object.create(CardClass.prototype);
  card._config = { total_sensor: "sensor.t", forecast_sensor: "sensor.f" };
  card._view = "day";
  card._offset = 0;
  card._fetchSeq = 0;
  card._liveDayKey = undefined;
  card._modules = [];
  card._dayBars = {};
  card._weekBars = {};
  card._dayForecast = null;
  card._weekForecast = null;
  card._weekForecastCache = {};
  card._forecastState = "none";
  card._forecastError = "";
  card._oldestIssued = null;
  card._loadState = "loading";
  card.isConnected = true;
  card._render = () => {}; // no DOM in the harness
  card._hass = hass;
  return card;
}

const STATES = {
  "sensor.t": {
    attributes: {
      sources: ["sensor.m1"],
      source_names: ["M1"],
      source_limits_w: [500],
    },
  },
  "sensor.f": {
    // Live TODAY forecast: two 15-min slots, daily total 5000 Wh.
    attributes: {
      wh_period: {
        [`${isoAt(0)}T10:00:00+00:00`]: 2000,
        [`${isoAt(0)}T10:15:00+00:00`]: 3000,
      },
    },
  },
};

// ============================================================================
// Scenario 1 — WEEK: 1 stats call + concurrent issued lookups, gap, cache.
// ============================================================================
{
  const statCalls = [];
  const issuedDates = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const gapIso = isoAt(-3); // this day has no archived snapshot → gap
  const hass = {
    language: "de",
    states: STATES,
    callWS: async (msg) => {
      if (msg.type === "recorder/statistics_during_period") {
        statCalls.push(msg);
        return { "sensor.m1": [{ start: Date.now(), mean: 100 }] };
      }
      if (msg.type === "call_service") {
        const iso = msg.service_data.date;
        issuedDates.push(iso);
        inFlight += 1;
        if (inFlight > maxInFlight) maxInFlight = inFlight;
        await settle(10); // hold every lookup open to observe concurrency
        inFlight -= 1;
        if (iso === gapIso) {
          return {
            response: {
              result: { date: iso, available: false, oldest_available: null },
            },
          };
        }
        // Distinct total per day: day-of-month × 1000 Wh at 12:00 UTC.
        const day = Number(iso.slice(-2));
        return {
          response: {
            result: {
              date: iso,
              available: true,
              hourly_wh: { [`${iso}T12:00:00+00:00`]: day * 1000 },
            },
          },
        };
      }
      throw new Error(`unexpected WS ${msg.type}`);
    },
  };
  const card = makeCard(hass);
  card._view = "week";
  card._offset = 0; // live window: today + 6 past days

  await card._fetch();

  assert(
    statCalls.length === 1,
    `expected 1 hourly statistics call, got ${statCalls.length}`,
  );
  assert(
    statCalls[0].period === "hour",
    `week statistics period is ${statCalls[0].period}, want hour`,
  );
  assert(
    issuedDates.length === 6,
    `expected 6 issued lookups (non-today days), got ${issuedDates.length}`,
  );
  assert(
    !issuedDates.includes(isoAt(0)),
    "today must use the LIVE wh_period sum, never the service",
  );
  assert(
    maxInFlight === 6,
    `issued lookups not concurrent: max in-flight was ${maxInFlight}, want 6`,
  );
  const wf = card._weekForecast;
  assert(Array.isArray(wf) && wf.length === 7, "no 7-slot week forecast built");
  assert(wf[3] === null, `gap day (index 3, ${gapIso}) is ${wf[3]}, want null`);
  assert(wf[6] === 5000, `today's slot is ${wf[6]}, want live sum 5000`);
  for (const i of [0, 1, 2, 4, 5]) {
    const iso = isoAt(i - 6);
    const want = Number(iso.slice(-2)) * 1000;
    assert(wf[i] === want, `day ${iso} total is ${wf[i]}, want ${want}`);
  }

  // Cache: a refetch of the SAME window must not refire any issued lookup.
  const before = issuedDates.length;
  await card._fetch();
  assert(
    issuedDates.length === before,
    `cached week refetch refired ${issuedDates.length - before} issued lookups`,
  );
  console.log("OK scenario 1: week = 1 hourly stats call + 6 concurrent issued lookups, gap + live today, cache hit");
}

// ============================================================================
// Scenario 6 — an impossible hourly mean is omitted from measured bars.
// ============================================================================
{
  const card = makeCard({ language: "de", states: STATES });
  card._sources(card._hass); // also reads the aligned plausibility limits
  card._ingestDay(
    {
      "sensor.m1": [
        { start: `${isoAt(0)}T08:00:00+00:00`, mean: 180 },
        { start: `${isoAt(0)}T09:00:00+00:00`, mean: 264482.587 },
      ],
    },
    ["sensor.m1"],
  );
  const energy = card._dayBars["sensor.m1"].reduce((a, b) => a + b, 0);
  assert(energy === 180, `implausible hourly mean leaked into bars: ${energy} Wh`);
  console.log("OK scenario 6: impossible hourly module mean is omitted");
}

// ============================================================================
// Scenario 2 — DAY nav to available:false → cleared line, state "missing".
// ============================================================================
{
  const hass = {
    language: "de",
    states: STATES,
    callWS: async (msg) => {
      if (msg.type === "recorder/statistics_during_period") {
        return { "sensor.m1": [{ start: Date.now(), mean: 100 }] };
      }
      if (msg.type === "call_service") {
        return {
          response: {
            result: {
              date: msg.service_data.date,
              available: false,
              oldest_available: isoAt(-2),
            },
          },
        };
      }
      throw new Error(`unexpected WS ${msg.type}`);
    },
  };
  const card = makeCard(hass);
  card._offset = -1;
  // Pretend yesterday's issued line is on screen…
  card._dayForecast = new Array(24).fill(100);
  card._forecastState = "issued";

  card._navigate(-1); // → the day before yesterday (fetch runs async)
  // _reload must clear the stale line SYNCHRONOUSLY, before any await lands.
  assert(card._dayForecast === null, "stale line not cleared on navigation");
  assert(
    card._forecastState === "none",
    `state after nav is "${card._forecastState}", want loading-neutral "none"`,
  );

  await settle();
  assert(
    card._forecastState === "missing",
    `state is "${card._forecastState}", want "missing" (available:false is NOT an error)`,
  );
  assert(card._dayForecast === null, "missing day must draw no line");
  assert(
    card._oldestIssued === isoAt(-2),
    `oldest_available not captured: ${card._oldestIssued}`,
  );
  console.log("OK scenario 2: nav clears stale line; available:false → \"missing\" + oldest_available");
}

// ============================================================================
// Scenario 3 — DAY: the service call THROWS → state "error".
// ============================================================================
{
  const hass = {
    language: "de",
    states: STATES,
    callWS: async (msg) => {
      if (msg.type === "recorder/statistics_during_period") {
        return { "sensor.m1": [{ start: Date.now(), mean: 100 }] };
      }
      if (msg.type === "call_service") {
        throw new Error("boom: service unavailable");
      }
      throw new Error(`unexpected WS ${msg.type}`);
    },
  };
  const card = makeCard(hass);
  card._offset = 0;

  card._navigate(-1); // → yesterday; the issued lookup will throw
  await settle();
  assert(
    card._forecastState === "error",
    `state is "${card._forecastState}", want "error" (a failed lookup is not "missing")`,
  );
  assert(card._dayForecast === null, "error state must draw no line");
  assert(
    card._forecastError.includes("boom"),
    `error message not remembered: "${card._forecastError}"`,
  );
  console.log('OK scenario 3: service exception → "error" + remembered message');
}

// ============================================================================
// Scenario 4 — WEEK bars: today's column = summed HOURLY energy, NOT the
// running daily-mean × 24 h (the v0.17.0 overstatement bug: a still-running
// day's daily mean covers only the sunlit hours seen so far).
// ============================================================================
{
  const dayMs = (offset) => {
    const now = new Date();
    return new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() + offset,
    ).getTime();
  };
  const hass = {
    language: "de",
    states: STATES,
    callWS: async (msg) => {
      if (msg.type === "recorder/statistics_during_period" && msg.period === "hour") {
        // Six complete days at 24 × 400 Wh plus today's eight × 300 Wh.
        return {
          "sensor.m1": [
            ...[-6, -5, -4, -3, -2, -1].flatMap((o) =>
              Array.from({ length: 24 }, () => ({ start: dayMs(o), mean: 400 })),
            ),
            ...Array.from({ length: 8 }, () => ({ start: dayMs(0), mean: 300 })),
          ],
        };
      }
      if (msg.type === "call_service") {
        return {
          response: { result: { available: false, oldest_available: null } },
        };
      }
      throw new Error(`unexpected WS ${msg.type}`);
    },
  };
  const card = makeCard(hass);
  card._view = "week";
  card._offset = 0;

  await card._fetch();

  const bars = card._weekBars["sensor.m1"];
  assert(Array.isArray(bars) && bars.length === 7, "no 7-slot week bars built");
  assert(
    bars[6] === 2400,
    `today's bar is ${bars[6]} Wh, want summed hourly 2400 (not daily-mean × 24)`,
  );
  assert(
    bars[6] !== 300 * 24,
    `today used daily-mean × 24 (${300 * 24}) — the overstatement bug`,
  );
  for (const i of [0, 1, 2, 3, 4, 5]) {
    assert(bars[i] === 9600, `complete-day slot ${i} is ${bars[i]}, want 9600`);
  }
  console.log(
    "OK scenario 4: every week column is summed from plausibility-checked hours",
  );
}

// ============================================================================
// Scenario 5 — i18n-proof discovery: German object slugs defeat the entity_id
// regex; the entity-registry unique_id suffix match must still resolve both
// entities, platform-gated so a foreign look-alike unique_id is never claimed.
// ============================================================================
{
  const germanStates = {
    // HA generates the entity_id object slug from the TRANSLATED entity name,
    // so a German install never matches the English discovery regex.
    "sensor.balkon_gemessene_dc_leistung_gesamt": {
      attributes: { sources: ["sensor.m1"], source_names: ["M1"] },
    },
    "sensor.balkon_energieproduktion_heute": {
      attributes: {
        wh_period: { [`${isoAt(0)}T10:00:00+00:00`]: 2000 },
      },
    },
  };
  const hass = {
    language: "de",
    states: germanStates,
    callWS: async (msg) => {
      if (msg.type === "config/entity_registry/list") {
        return [
          // A FOREIGN platform entry whose unique_id happens to end with one of
          // the keys must never be claimed by the card.
          {
            entity_id: "sensor.foreign_measured_dc_power_total",
            unique_id: "zzz_measured_dc_power_total",
            platform: "other_integration",
          },
          {
            entity_id: "sensor.balkon_gemessene_dc_leistung_gesamt",
            unique_id: "entry42_measured_dc_power_total",
            platform: "balcony_solar_forecast",
          },
          {
            entity_id: "sensor.balkon_energieproduktion_heute",
            unique_id: "entry42_energy_production_today",
            platform: "balcony_solar_forecast",
          },
        ];
      }
      if (msg.type === "recorder/statistics_during_period") {
        return { "sensor.m1": [{ start: Date.now(), mean: 100 }] };
      }
      throw new Error(`unexpected WS ${msg.type}`);
    },
  };
  const card = makeCard(hass);
  card._config = {}; // no manual ids: pure discovery

  // Sync path: the regex fallback must find NOTHING under German slugs.
  const before = card._resolveIds(hass);
  assert(
    before.total_sensor === undefined,
    `regex unexpectedly matched a German slug: ${before.total_sensor}`,
  );

  // The async registry pass resolves via the language-stable unique_id suffix.
  card._ensureRegistry(hass);
  await settle();
  const after = card._resolveIds(hass);
  assert(
    after.total_sensor === "sensor.balkon_gemessene_dc_leistung_gesamt",
    `registry unique_id match failed for total: ${after.total_sensor}`,
  );
  assert(
    after.forecast_sensor === "sensor.balkon_energieproduktion_heute",
    `registry unique_id match failed for forecast: ${after.forecast_sensor}`,
  );
  console.log(
    "OK scenario 5: unique_id registry discovery beats German object slugs",
  );
}

console.log("ALL OK: power-card runtime harness passed");
