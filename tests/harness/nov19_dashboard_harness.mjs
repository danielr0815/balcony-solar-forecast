// Real cards with a previous-release installer configuration and cold browser
// connection. The fake only supplies the HA WebSocket and state boundaries.
import assert from "node:assert/strict";
import { installDOM, elements, settle } from "./card_dom.mjs";

const classes = installDOM();
await import(new URL("../../custom_components/balcony_solar_forecast/frontend/power_history_card.js", import.meta.url));
await import(new URL("../../custom_components/balcony_solar_forecast/frontend/shade_profile_card.js", import.meta.url));
const Power = classes.get("balcony-power-history-card");
const Shade = classes.get("balcony-shade-profile-card");

const date = "2026-11-19";
const RealDate = Date;
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [`${date}T12:00:00Z`])); }
  static now() { return RealDate.parse(`${date}T12:00:00Z`); }
};

const legacyPower = {
  type: "custom:balcony-power-history-card", entry_id: "second",
  total_sensor: "sensor.old_total", forecast_sensor: "sensor.second_forecast",
  title: "Production per module (measured DC · forecast DC)",
};
const legacyShade = {
  type: "custom:balcony-shade-profile-card", entry_id: "second",
  sensor: "sensor.old_shade", module_select: "select.second_module", date_entity: "date.second_date",
  title: "Shade profile diagram",
};
const entity = (entry, key, id) => ({
  platform: "balcony_solar_forecast", config_entry_id: entry,
  unique_id: `${entry}_${key}`, entity_id: id,
});

function registry(total = "sensor.new_total", shade = "sensor.new_shade") {
  return [
    entity("first", "measured_dc_power_total", "sensor.other_total"),
    entity("first", "shade_profile", "sensor.other_shade"),
    entity("second", "measured_dc_power_total", total),
    entity("second", "energy_production_today", "sensor.second_forecast"),
    entity("second", "shade_profile", shade),
    entity("second", "shade_profile_module", "select.second_module"),
    entity("second", "shade_profile_date", "date.second_date"),
  ];
}

let failures = 0;
async function check(name, run) {
  try { await run(); console.log(`PASS ${name}`); }
  catch (error) { failures++; console.error(`FAIL ${name}: ${error.stack}`); }
}

await check("old dashboard cold browser resolves renamed cards and reads Recorder", async () => {
  const messages = [];
  const power = new Power(), shade = new Shade();
  shade._render = () => {};
  power.setConfig(legacyPower); shade.setConfig(legacyShade);
  const h = {
    config: { time_zone: "UTC" }, connection: {},
    states: {
      "sensor.new_total": { state: "unavailable", attributes: {} },
      "sensor.old_total": { state: "100", attributes: { sources: ["sensor.reassigned"] } },
      "sensor.other_total": { state: "100", attributes: { sources: ["sensor.other_module"] } },
    },
    callWS: async (msg) => {
      messages.push(msg);
      if (msg.type === "config/entity_registry/list") return [
        ...registry(),
        { ...entity("foreign", "measured_dc_power_total", "sensor.old_total"), platform: "template" },
        entity("third", "shade_profile", "sensor.old_shade"),
      ];
      if (msg.type === "balcony_solar_forecast/measurement_history") {
        assert.equal(msg.entity_id, "sensor.new_total");
        assert.equal(msg.entry_id, "second");
        assert.equal(Object.hasOwn(msg, "statistic_ids"), false);
        return {
          sources: ["sensor.m2"], source_names: ["East"], source_limits_w: [500],
          statistics: { "sensor.m2": [{ start: `${date}T10:00:00Z`, mean: 120 }] },
        };
      }
      if (msg.type === "call_service") {
        assert.equal(msg.service_data.entry_id, "second");
        return { response: { result: { available: false } } };
      }
      throw new Error(`Unexpected ${msg.type}`);
    },
  };
  power.hass = h; shade.hass = h; await settle();
  assert.equal(shade._resolveIds(h).sensor, "sensor.new_shade");
  assert.equal(power._resolveIds(h).total_sensor, "sensor.new_total");
  assert.ok(messages.some((m) => m.type === "balcony_solar_forecast/measurement_history"));
  assert.equal(power._dayBars["sensor.m2"][10], 120);
  assert.match(power.shadowRoot.textContent, /East/);
  const previous = elements(power.shadowRoot, (node) => node.classList.contains("nav-btn"))[0];
  assert.ok(previous, "historical navigation is available on cold load");
  previous.dispatch("click"); await settle();
  assert.ok(messages.some((m) => m.type === "balcony_solar_forecast/measurement_history"
    && m.start_time.startsWith("2026-11-18")));
  power.disconnectedCallback(); shade.disconnectedCallback();
});

await check("both cards reuse registry resolution and follow registry updates", async () => {
  let scanned = 0, event;
  let rows = registry("sensor.old_total", "sensor.old_shade");
  for (const row of rows) {
    const uniqueId = row.unique_id;
    Object.defineProperty(row, "unique_id", { get: () => { scanned++; return uniqueId; } });
  }
  const connection = { subscribeEvents: async (cb) => { event = cb; return () => {}; } };
  const h = { config: { time_zone: "UTC" }, connection, states: {}, callWS: async () => rows };
  const power = new Power(), shade = new Shade();
  power._render = shade._render = () => {};
  power.setConfig(legacyPower); shade.setConfig(legacyShade);
  power.hass = h; shade.hass = h; await settle();
  assert.ok(scanned > 0);
  scanned = 0;
  const update = { ...h, states: { "sensor.unrelated": { state: "1" } } };
  power.hass = update; shade.hass = update;
  assert.equal(scanned, 0, "unrelated state changes do not rescan entity registry rows");

  rows = registry();
  event({}); await settle();
  assert.equal(power._resolveIds(h).total_sensor, "sensor.new_total");
  assert.equal(shade._resolveIds(h).sensor, "sensor.new_shade");
  power.setConfig({ ...legacyPower, title: "User title", total_sensor: "sensor.my_total" });
  assert.equal(power._resolveIds(h).total_sensor, "sensor.my_total", "manual override stays fixed");
  power.setConfig({ ...legacyPower, title: "User title", total_sensor: "sensor.other_total" });
  assert.equal(power._resolveIds(h).total_sensor, undefined, "foreign entry override may not cross scope");
  power.disconnectedCallback(); shade.disconnectedCallback();
});

await check("live overlay keeps valid zeros and skips a partial hour on an unknown day", () => {
  const power = new Power();
  power.setConfig({});
  power._hass = { config: { time_zone: "UTC" }, states: {} };
  const curve = Object.fromEntries([
    ...["00", "15", "30", "45"].map((min) => [`${date}T10:${min}:00Z`, 0]),
    ...["00", "15", "30"].map((min) => [`${date}T11:${min}:00Z`, 50]),
  ]);
  power._recomputeForecast({ state: "unknown", attributes: { wh_period: curve } });
  assert.equal(power._dayForecast?.[10], 0);
  assert.equal(power._dayForecast[11], null);
});

process.exitCode = failures ? 1 : 0;
