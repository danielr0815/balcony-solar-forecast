/** Synthetic real-browser smoke: own Chromium, no HA connection or shared profile.
 * Prerequisite: scripts/playwright-mcp.sh install (or BSF_PLAYWRIGHT_MODULE).
 * Usage: node scripts/validation/browser_cards.mjs /tmp/bsf-card-browser
 */
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const root = fileURLToPath(new URL("../../", import.meta.url));
const require = createRequire(import.meta.url);
process.env.PLAYWRIGHT_BROWSERS_PATH ||= path.join(root, ".ha-dev/playwright-browsers");
const { chromium } = require(process.env.BSF_PLAYWRIGHT_MODULE || path.join(root, ".ha-dev/playwright-runtime/node_modules/playwright"));
const output = path.resolve(process.argv[2] || "/tmp/bsf-card-browser");
await fs.mkdir(output, { recursive: true });
const frontend = path.join(root, "custom_components/balcony_solar_forecast/frontend");
const server = http.createServer(async (req, res) => {
  const name = new URL(req.url, "http://localhost").pathname;
  if (name === "/") { res.setHeader("content-type", "text/html"); res.end('<!doctype html><meta name="viewport" content="width=device-width"><style>body{margin:0;font:14px sans-serif}ha-card{display:block}button,input,select{font:inherit}</style>'); return; }
  const file = path.join(frontend, name);
  if (!file.startsWith(frontend+path.sep) || !name.endsWith(".js")) { res.writeHead(404).end(); return; }
  try { res.setHeader("content-type", "text/javascript"); res.end(await fs.readFile(file)); }
  catch { res.writeHead(404).end(); }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await chromium.launch({ headless: true, executablePath: chromium.executablePath(), args: ["--no-sandbox"] });
try {
  const page = await browser.newPage({ viewport: { width: 375, height: 812 } });
  const errors = []; page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(async () => {
    await import("/power_history_card.js"); await import("/shade_profile_card.js");
    const day = new Date().toISOString().slice(0, 10), hour = day+"T10:00:00Z";
    const states = {
      "sensor.total": { state: "100", attributes: { sources: ["sensor.port"], source_names: ["Panel 1"] } },
      "sensor.forecast": { state: "1", attributes: { wh_period: { [hour]: 100 }, quantile_readiness: {
        enabled: true, weather_status: "fresh", dated_days: 3, by_day: { [day]: { positive_slots: 40, trained_slots: 10, state: "partial" } } } } },
      "sensor.profile": { state: "ready", attributes: { module: "Panel 1", azimuth: [90, 180, 270],
        date: day, sun_elevation: [10, 20, 10], time: ["09:00", "10:00", "11:00"], horizon_azimuth: [90, 180, 270], transmittance: [.5, .8, 1], transmittance_individual: [.6, .9, 1],
        sample_n: [10, 20, 30], shade_horizon: [15, 15, 15], static_horizon: [10, 10, 10],
        sun_path: { azimuth: [90, 180, 270], elevation: [10, 40, 10], time: [hour, hour, hour] } } },
      "select.module": { state: "Panel 1", attributes: { options: ["Panel 1", "Panel 2"] } },
      "date.profile": { state: day, attributes: {} },
    };
    const roles = { measured_dc_power_total: "sensor.total", energy_production_today: "sensor.forecast",
      shade_profile: "sensor.profile", shade_profile_module: "select.module", shade_profile_date: "date.profile" };
    const hass = { language: "de", config: { time_zone: "UTC" }, states,
      connection: { subscribeEvents: async () => () => {} },
      callService: async () => {}, callWS: async (message) => {
        if (message.type === "config/entity_registry/list") return Object.entries(roles).map(([key, entity_id]) =>
          ({ entity_id, config_entry_id: "synthetic", unique_id: "synthetic_"+key, platform: "balcony_solar_forecast" }));
        if (message.type === "recorder/statistics_during_period") return { "sensor.port": [{ start: hour, mean: 100 }] };
        return { response: { result: { available: false } } };
      } };
    const power = document.createElement("balcony-power-history-card");
    power.setConfig({ entry_id: "synthetic", total_sensor: "sensor.total", forecast_sensor: "sensor.forecast" });
    document.body.append(power); power.hass = hass;
    const shade = document.createElement("balcony-shade-profile-card");
    shade.setConfig({ entry_id: "synthetic", sensor: "sensor.profile", module_select: "select.module", date_entity: "date.profile" });
    document.body.append(shade); shade.hass = hass;
    window.testHass = hass;
  });
  await page.locator("balcony-power-history-card table").waitFor({ state: "attached" });
  await page.locator("balcony-shade-profile-card table").waitFor({ state: "attached" });
  assert.equal(await page.locator("balcony-shade-profile-card svg").count(), 1);
  for (const tag of ["balcony-power-history-card", "balcony-shade-profile-card"]) {
    const card = page.locator(tag);
    await card.locator("details summary").click();
    const control = card.locator("input,select,button").first(); await control.focus();
    const identity = await control.evaluate((el) => ({ tag: el.tagName, id: el.id }));
    await card.evaluate((el) => {
      const hass = window.testHass;
      el.hass = { ...hass, states: Object.fromEntries(Object.entries(hass.states).map(([id, state]) => [id, { ...state }])) };
    });
    assert.deepEqual(await card.evaluate((el) => ({ tag: el.shadowRoot.activeElement?.tagName, id: el.shadowRoot.activeElement?.id })), identity);
    assert.equal(await card.locator("details").evaluate((el) => el.open), true);
    await page.keyboard.press("Tab");
  }
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
    "mobile viewport must not require page-level horizontal scrolling");
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(output, "cards-mobile.png"), fullPage: true });
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.screenshot({ path: path.join(output, "cards-desktop.png"), fullPage: true });
  console.log("PASS: real Chromium; keyboard focus, open tables, 375px viewport, no page errors");
} finally {
  await browser.close(); await new Promise((resolve) => server.close(resolve));
}
