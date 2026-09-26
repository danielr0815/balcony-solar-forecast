// Behavioral regressions run against actual constructors, generated DOM and
// deferred websocket replies. This file also runs unchanged on the parent tree.
import assert from "node:assert/strict";
import { installDOM, elements, settle } from "./card_dom.mjs";
const classes = installDOM();
await import(new URL("../../custom_components/balcony_solar_forecast/frontend/shade_profile_card.js", import.meta.url));
await import(new URL("../../custom_components/balcony_solar_forecast/frontend/power_history_card.js", import.meta.url));
const Shade = classes.get("balcony-shade-profile-card"), Power = classes.get("balcony-power-history-card");
let failures = 0;
async function check(name, run) {
  try { await run(); console.log(`PASS ${name}`); }
  catch (e) { failures++; console.error(`FAIL ${name}: ${e.stack}`); }
}
const make = (Class, dom = false) => { const card = new Class(); if (!dom) card._render = () => {}; return card; };
const keys = ["shade_profile", "shade_profile_module", "shade_profile_date", "measured_dc_power_total", "energy_production_today"];
const domains = ["sensor", "select", "date", "sensor", "sensor"];
const registry = ["first", "second"].flatMap((entry) => keys.map((key, i) => ({
  platform: "balcony_solar_forecast", unique_id: `${entry}_${key}`, config_entry_id: entry,
  entity_id: `${domains[i]}.${entry}_deutsch_${i}`,
})));
const hass = (callWS, extra = {}) => ({ language: "de", config: { time_zone: "Europe/Berlin" },
  connection: {}, states: {}, callWS, ...extra });
const RealDate = Date;
async function at(iso, run) {
  globalThis.Date = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : [iso])); }
    static now() { return RealDate.parse(iso); }
  };
  try { await run(); } finally { globalThis.Date = RealDate; }
}
for (const Class of [Shade, Power]) {
  await check(`${Class.name}: state push during registry request`, async () => {
    let resolve; let requests = 0;
    const h = hass(() => { requests++; return new Promise((r) => { resolve = r; }); });
    const card = make(Class); card.hass = h; await settle();
    card.hass = { ...h, states: { changed: {} } };
    resolve(registry.filter((r) => r.config_entry_id === "second")); await settle();
    assert.equal(card._resolveIds(card._hass)[Class === Shade ? "sensor" : "total_sensor"],
      `sensor.second_deutsch_${Class === Shade ? 0 : 3}`);
    assert.equal(requests, 1, "same connection shares one registry request");
  });
  await check(`${Class.name}: failed lookup recovers and detached result survives`, async () => {
    let calls = 0, resolve;
    const h = hass(() => ++calls === 1 ? Promise.reject(new Error("offline")) : new Promise((r) => { resolve = r; }));
    const card = make(Class); card.hass = h; await settle();
    card.hass = { ...h }; await settle();
    assert.equal(calls, 2, "transient discovery error must retry");
    card.isConnected = false; card.disconnectedCallback();
    resolve(registry.filter((r) => r.config_entry_id === "second")); await settle();
    card.isConnected = true; card.connectedCallback(); card.disconnectedCallback();
    assert.equal(card._resolveIds(card._hass)[Class === Shade ? "sensor" : "total_sensor"],
      `sensor.second_deutsch_${Class === Shade ? 0 : 3}`);
  });
}
await check("multi-site service and entity scope", async () => {
  const messages = [];
  const h = hass(async (msg) => { messages.push(msg); return msg.type === "config/entity_registry/list"
    ? registry : { response: { result: { module: "M1", available: false } } }; });
  const shade = make(Shade); shade._hass = h; shade.setConfig({ entry_id: "second", sensor: "sensor.second_deutsch_0" });
  shade._registryList = registry;
  assert.equal(shade._resolveIds(h).module_select, "select.second_deutsch_1");
  await shade._fetchCompare(h, "M1", "2026-09-01");
  const power = make(Power); power._hass = h; power.setConfig({ entry_id: "second" });
  await power._fetchIssued(h, "2026-09-01", power._fetchSeq);
  await power._issuedTotal(h, "2026-09-01");
  assert.equal(messages.filter((m) => m.type === "call_service").length, 3);
  for (const msg of messages.filter((m) => m.type === "call_service")) assert.equal(msg.service_data.entry_id, "second");
  shade.setConfig({ sensor: "sensor.second_deutsch_0" });
  assert.equal(shade._resolveIds(h).module_select, "select.second_deutsch_1", "explicit sensor determines entry");
  shade.setConfig({});
  assert.equal(shade._resolveIds(h).sensor, undefined, "ambiguous sites require a choice");
});
await check("weekly service outage recovers without recreating card", async () => {
  let failing = true, calls = 0;
  const h = hass(async () => { calls++; if (failing) throw new Error("offline");
    return { response: { result: { available: true, hourly_wh: { "2026-09-01T12:00Z": 123 } } } }; });
  const card = make(Power); card.setConfig({ entry_id: "second" }); card._hass = h;
  const days = Array.from({ length: 7 }, (_, i) => new Date(`2026-09-0${i + 1}T12:00Z`));
  await card._fetchWeekForecast(h, days, card._fetchSeq);
  failing = false;
  await card._fetchWeekForecast(h, days, card._fetchSeq);
  assert.equal(calls, 14, "failed days must be retried");
  assert.deepEqual(card._weekForecast, Array(7).fill(123));
  await card._fetchWeekForecast(h, days, card._fetchSeq);
  assert.equal(calls, 14, "confirmed archive results stay cached");
});
const dataHass = (callWS) => hass(callWS, { states: {
  "sensor.total": { attributes: { sources: ["sensor.m1"], source_names: ["M1"] } },
  "sensor.forecast": { attributes: { wh_period: {} } },
} });
await check("HA day boundary differs from browser timezone", async () => at("2026-09-26T22:30Z", async () => {
  const messages = [];
  const h = dataHass(async (msg) => { messages.push(msg); return {}; });
  const card = make(Power); card.setConfig({ total_sensor: "sensor.total", forecast_sensor: "sensor.forecast", entry_id: "second" }); card._hass = h;
  await card._fetch();
  assert.equal(messages[0].start_time, "2026-09-26T22:00:00.000Z");
}));
for (const [instant, length, values] of [
  ["2026-10-25T12:00Z", 25, [100, 200]], ["2026-03-29T12:00Z", 23, [100, 200]],
]) {
  await check(`DST calendar ${length} hours preserves distinct buckets`, async () => at(instant, async () => {
    const iso = instant.slice(0, 10);
    const h = dataHass(async () => ({ "sensor.m1": [
      { start: `${iso}T00:00Z`, mean: values[0] }, { start: `${iso}T01:00Z`, mean: values[1] },
    ] }));
    h.states["sensor.forecast"].attributes.wh_period = { [`${iso}T00:00Z`]: 50, [`${iso}T01:00Z`]: 60 };
    const card = make(Power); card.setConfig({ entry_id: "second", total_sensor: "sensor.total", forecast_sensor: "sensor.forecast" }); card._hass = h;
    await card._fetch();
    assert.equal(card._dayBars["sensor.m1"].length, length);
    assert.equal(card._dayBars["sensor.m1"].reduce((a, b) => a + b, 0), 300);
    assert.equal(card._dayBars["sensor.m1"].filter((v) => v > 0).length, 2, "repeated clock hours must not merge");
    assert.equal(card._dayForecast.filter((v) => v > 0).length, 2);
  }));
}
await check("statistics success, error, recovery and empty data remain distinguishable", async () => at("2026-09-26T12:00Z", async () => {
  let state = "success";
  const h = dataHass(async () => {
    if (state === "error") throw new Error("recorder offline");
    return { "sensor.m1": state === "empty" ? [] : [{ start: "2026-09-26T10:00Z", mean: state === "recovered" ? 200 : 100 }] };
  });
  const card = make(Power, true); card.setConfig({ entry_id: "second", total_sensor: "sensor.total", forecast_sensor: "sensor.forecast" }); card._hass = h;
  card._rebuildModules(h.states["sensor.total"]);
  await card._fetchDay(h, ["sensor.m1"], card._fetchSeq);
  assert.equal(card._dayBars["sensor.m1"].reduce((a, b) => a + b, 0), 100);
  state = "error";
  await card._fetchDay(h, ["sensor.m1"], card._fetchSeq);
  assert.match(card.shadowRoot.textContent, /Statistik-Abruf fehlgeschlagen/);
  assert.match(card.shadowRoot.textContent, /Letzte erfolgreiche Aktualisierung/);
  assert.equal(card._dayBars["sensor.m1"].reduce((a, b) => a + b, 0), 100, "retain the marked stale bars");
  assert.ok(elements(card.shadowRoot, (el) => el.tagName === "table").length);
  state = "recovered";
  await card._fetchDay(h, ["sensor.m1"], card._fetchSeq);
  assert.doesNotMatch(card.shadowRoot.textContent, /Statistik-Abruf fehlgeschlagen/);
  assert.equal(card._dayBars["sensor.m1"].reduce((a, b) => a + b, 0), 200);
  state = "empty";
  await card._fetchDay(h, ["sensor.m1"], card._fetchSeq);
  assert.equal(card._loadState, "empty");
  assert.doesNotMatch(card.shadowRoot.textContent, /Statistik-Abruf fehlgeschlagen/);
}));
await check("shade controls, accessible labels/table and real compare events", async () => {
  const calls = [], writes = [];
  const attributes = { module: "M1", date: "2026-09-26", azimuth: [100, 110], sun_elevation: [20, 25],
    transmittance: [0.4, 0.9], transmittance_individual: [0.3, 0.8], sample_n: [0, 12],
    time: ["10:00", "11:00"], horizon_azimuth: [90, 120], shade_horizon: [10, 20], static_horizon: [12, 22] };
  const h = hass(async (msg) => { calls.push(msg); return { response: { result: { ...attributes, date: msg.service_data.date } } }; }, {
    states: { "sensor.profile": { state: "50", attributes }, "select.module": { state: "M1", attributes: { options: ["M1", "M2"] } },
      "date.profile": { state: "2026-09-26" } }, callService: (...args) => { writes.push(args); },
  });
  const card = make(Shade, true); card._hass = h;
  card.setConfig({ entry_id: "second", sensor: "sensor.profile", module_select: "select.module", date_entity: "date.profile" });
  card._render(h, card._resolveIds(h), h.states["sensor.profile"], h.states["select.module"], h.states["date.profile"]);
  for (const label of elements(card.shadowRoot, (el) => el.tagName === "label").slice(0, 3)) {
    assert.ok(label.htmlFor, "visible label must have an input association");
    assert.equal(elements(card.shadowRoot, (el) => el.id === label.htmlFor).length, 1);
  }
  assert.ok(elements(card.shadowRoot, (el) => el.tagName === "svg")[0].getAttribute("aria-label"));
  assert.equal(elements(card.shadowRoot, (el) => el.tagName === "table").length, 1);
  const select = elements(card.shadowRoot, (el) => el.tagName === "select")[0];
  select.value = "M2"; select.dispatch("change");
  assert.deepEqual(writes[0], ["select", "select_option", { entity_id: "select.module", option: "M2" }]);
  const date = elements(card.shadowRoot, (el) => el.id === "shade-date")[0];
  date.value = "2026-09-25"; date.dispatch("change");
  assert.deepEqual(writes[1], ["date", "set_value", { entity_id: "date.profile", date: "2026-09-25" }]);
  const single = elements(card.shadowRoot, (el) => el.tagName === "button" && el.textContent === "Einzeln")[0];
  single.dispatch("click");
  assert.equal(elements(card.shadowRoot, (el) => el.tagName === "button" && el.textContent === "Einzeln")[0].getAttribute("aria-pressed"), "true");
  const compare = elements(card.shadowRoot, (el) => el.className === "compare-input")[0];
  compare.value = "2026-06-21"; compare.dispatch("change"); await settle();
  assert.equal(calls[0].service, "get_shade_profile"); assert.equal(calls[0].service_data.entry_id, "second");
  assert.equal(writes.length, 2, "comparison must not change the shared date entity");
  assert.match(card.shadowRoot.textContent, /2026-06-21/);
  const overlay = elements(card.shadowRoot, (el) => el.tagName === "rect" && el.listeners.pointermove?.length)[0];
  assert.ok(overlay, "must register an actual pointer listener"); overlay.dispatch("pointermove", { clientX: 400 });
  assert.match(elements(card.shadowRoot, (el) => el.className.includes("readout"))[0].textContent, /Verschattung/);
});
await check("shade colour thresholds agree with Python and confidence is observable", async () => {
  const threshold = Number(process.env.BSF_SHADE_THRESHOLD || "0.5");
  const card = make(Shade, true); card._hass = hass(async () => ({}));
  const attrs = { date: "2026-09-26", azimuth: [100, 110, 120, 130],
    sun_elevation: [20, 21, 22, 23], time: ["10:00", "10:15", "10:30", "10:45"],
    transmittance: [threshold - 0.000001, threshold, 0.849999, 0.85], sample_n: [0, 12, 12, 12] };
  card._readoutEl = document.createElement("div");
  const plot = card._plot({ state: "25", attributes: attrs }, card._t());
  const dots = elements(plot, (el) => el.tagName === "circle" && el.children.some((c) => c.tagName === "title"));
  assert.deepEqual(dots.map((dot) => dot.getAttribute("stroke") || dot.getAttribute("fill")),
    ["#c0392b", "#e67e22", "#e67e22", "#2ecc71"]);
  assert.equal(dots[0].getAttribute("fill"), "transparent");
  assert.ok(Number(dots[0].getAttribute("r")) < Number(dots[1].getAttribute("r")));
});
if (failures) process.exitCode = 1;
else console.log("ALL OK: card lifecycle, site, calendar, recovery and accessible DOM regressions");
