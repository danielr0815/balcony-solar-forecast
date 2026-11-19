import assert from "node:assert/strict";
import { installDOM, settle } from "./card_dom.mjs";

const classes = installDOM();
await import(new URL("../../custom_components/balcony_solar_forecast/frontend/power_history_card.js", import.meta.url));
const Power = classes.get("balcony-power-history-card");
const config = { entry_id: "site", total_sensor: "sensor.total" };

const reply = (allowed) => ({
  sources: allowed ? ["sensor.ok"] : [],
  source_names: allowed ? ["Visible"] : [],
  source_limits_w: allowed ? [500] : [],
  statistics: allowed ? { "sensor.ok": [{ start: Date.now(), mean: 123 }] } : {},
});

const makeConnection = () => {
  const queries = [];
  let allowed = true, pending;
  let defer = false;
  const connection = {};
  const states = {
    // Old or malicious entity attributes may claim another source. They are
    // not authorization, even when the total is currently available.
    "sensor.total": { state: "200", attributes: { sources: ["sensor.hidden"], source_names: ["Private"] } },
    "sensor.ok": { state: "123", attributes: {} },
  };
  const hass = {
    config: { time_zone: "UTC" }, connection, states,
    callWS: async (msg) => {
      if (msg.type === "config/entity_registry/list") return [];
      assert.equal(msg.type, "balcony_solar_forecast/measurement_history");
      assert.equal(msg.entity_id, "sensor.total");
      assert.equal(msg.entry_id, "site");
      assert.equal(Object.hasOwn(msg, "statistic_ids"), false);
      queries.push(msg);
      if (defer) return new Promise((resolve) => { pending = resolve; });
      return reply(allowed);
    },
  };
  return {
    hass, queries,
    revoke: () => { allowed = false; },
    defer: () => { defer = true; },
    finish: () => pending(reply(true)),
  };
};

const card = new Power();
card._render = () => {};
card.setConfig(config);
const one = makeConnection();
card.hass = one.hass;
await settle();
assert.ok(one.queries.length > 0, "even an available total uses the permission-checked history route");
assert.deepEqual(card._modules.map((module) => module.id), ["sensor.ok"]);

one.revoke();
const hidden = { ...one.hass, states: { "sensor.total": one.hass.states["sensor.total"] } };
card.hass = hidden;
assert.deepEqual(card._modules, [], "a source disappearing from the same connection clears old values immediately");
await settle();
assert.deepEqual(card._dayBars, {});

const two = makeConnection();
card.hass = two.hass;
await settle();
assert.deepEqual(card._modules.map((module) => module.id), ["sensor.ok"]);
two.defer();
const oldFetch = card._fetch();

const three = makeConnection();
three.revoke();
card.hass = three.hass;
assert.deepEqual(card._modules, [], "a different websocket connection must never display the old user's values");
two.finish();
await oldFetch;
await settle();
assert.deepEqual(card._modules, [], "late authorized result from old connection must be discarded");
assert.deepEqual(card._dayBars, {});
card.disconnectedCallback();
console.log("PASS authorized history, live rights removal and stale connection reply");
