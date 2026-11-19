// Consume the public response from a real coordinator + archive snapshot.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [work, day, expectedJson] = process.argv.slice(2);
const { installDOM } = await import(pathToFileURL(join(work, "tests/harness/card_dom.mjs")));
const classes = installDOM();
await import(pathToFileURL(join(work, "custom_components/balcony_solar_forecast/frontend/power_history_card.js")));
const card = new (classes.get("balcony-power-history-card"))();
card.setConfig({ entry_id: "e1" });
const response = JSON.parse(readFileSync(0, "utf8"));
const hass = {
  connection: {},
  config: { time_zone: "UTC" },
  states: {},
  callWS: async (message) => {
    assert.equal(message.service, "get_issued_forecast");
    assert.equal(message.service_data.entry_id, "e1");
    const result = message.service_data.date === day ? response : { available: false };
    return { response: { result } };
  },
};
card._hass = hass;
const days = Array.from({ length: 7 }, (_, i) => {
  const last = new Date(`${day}T12:00:00Z`);
  last.setUTCDate(last.getUTCDate() + i - 6);
  return last;
});
await card._fetchWeekForecast(hass, days, card._fetchSeq);
assert.equal(card._weekForecast[6], JSON.parse(expectedJson));
