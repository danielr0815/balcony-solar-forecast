import assert from "node:assert/strict";
import { installDOM } from "./card_dom.mjs";

const classes = installDOM();
await import(new URL("../../custom_components/balcony_solar_forecast/frontend/power_history_card.js", import.meta.url));

const NativeDate = Date;
let now = "2026-11-18T22:59:59Z"; // Berlin is about to end the 18th.
globalThis.Date = class extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return NativeDate.parse(now); }
};

const card = new (classes.get("balcony-power-history-card"))();
card.setConfig({ entry_id: "test", total_sensor: "sensor.total", forecast_sensor: "sensor.forecast" });
const queried = [];
const stamp = "2026-11-18T10:00:00Z";
card._hass = {
  config: { time_zone: "Europe/Berlin" },
  states: { "sensor.total": { attributes: { sources: ["sensor.module"] } } },
  callWS: async (msg) => {
    if (msg.type === "recorder/statistics_during_period") return {};
    assert.equal(msg.type, "call_service");
    queried.push(msg.service_data.date);
    return { response: { result: { available: true, curve_source: "legacy", hourly_wh: { [stamp]: 123 } } } };
  },
};
card._render = () => {};
card._navigate(-1); // The 17th remains selected at midnight.
await new Promise((resolve) => setImmediate(resolve));
assert.deepEqual(queried, ["2026-11-17"]);
now = "2026-11-18T23:00:01Z";
card._navigate(1); // The 18th is now yesterday and still a historical view.
await new Promise((resolve) => setImmediate(resolve));
assert.deepEqual(queried, ["2026-11-17", "2026-11-18"]);
assert.equal(card._dayForecast[11], 123);
assert.equal(card._provenanceCaption(card._t()).textContent, "Forecast (legacy archive)");
card._navigate(1);
assert.equal(card._isLive(), true);
