# Observability dashboard

A ready-to-paste Lovelace dashboard for Balcony Solar Forecast, using **only
built-in Home Assistant cards** — it needs zero custom cards and zero HACS
frontend resources. It surfaces the v0.4 skill scoreboard (the kill-gate), the
P10/P50/P90 uncertainty band, the learner/drift/degradation status, and a
best-effort shademap view (SPEC §9/§10/§14.3).

The dashboard file is [`dashboards/balcony_solar_forecast.yaml`](../dashboards/balcony_solar_forecast.yaml).

---

## 1. Install

### 1a. One-click: the `install_dashboard` action (recommended)

The integration can build the whole dashboard for you, wired to **your**
install's real entity ids — no copy-paste, no hand-editing object-ids. You only
create the (empty) dashboard shell once:

1. In Home Assistant go to **Settings → Dashboards → ＋ Add dashboard → New
   dashboard from scratch**. Give it a title (e.g. *Balcony Solar*), an icon
   (`mdi:solar-power`), and — important — set the **URL** to `balcony-solar`
   (the URL field must contain a hyphen). **Create**, then leave it empty.
2. Go to **Developer Tools → Actions**, pick
   `balcony_solar_forecast.install_dashboard`, and **Perform action**. That's
   it — open the dashboard and it is fully populated.

**Re-run it any time** (e.g. after an integration update) to refresh the layout:
the action stamps a `bsf_managed` marker on the config it writes, so a re-run
overwrites its own previous output silently. It **will not** clobber a dashboard
you authored yourself — if the target already has content without that marker it
refuses unless you pass `overwrite: true`.

Optional fields:

- **dashboard** — the target dashboard's URL path (default `balcony-solar`); set
  it if you created the shell under a different URL.
- **entry_id** — only needed if you run multiple sites; omit for a single one.
- **overwrite** — set `true` to replace a hand-authored (non-managed) dashboard.

The response reports the target `dashboard`, the number of `views` and `cards`
written, and `missing_entities` — the keys of any entities not present yet (e.g.
comparison sensors you have not configured), whose cards/rows were omitted so a
partial install still renders. The generated dashboard already embeds the
bundled shade-profile card (§4b) — no extra step.

> Needs storage-mode Lovelace (the default). A YAML-mode dashboard cannot be
> written by the action; use the manual copy-paste below instead.

### 1b. Manual alternative: raw-configuration copy-paste

If you prefer to paste the YAML yourself (or run YAML-mode Lovelace):

1. In Home Assistant go to **Settings → Dashboards**.
2. Click **＋ Add dashboard → New dashboard from scratch**. Give it a title
   (e.g. *Balcony Solar*) and an icon (`mdi:solar-power`), then **Create**.
3. Open the new dashboard, click the **pencil / Edit dashboard**, accept the
   "take control" prompt if shown.
4. Open the **three-dot menu (top right) → Raw configuration editor**.
5. **Delete** the placeholder content and **paste the entire contents** of
   [`dashboards/balcony_solar_forecast.yaml`](../dashboards/balcony_solar_forecast.yaml).
6. Click **Save**, then **✕ / Done**.

> The YAML defines a single `views:` entry. If you prefer to add it as one view
> inside an existing dashboard, paste only the item under `views:` into that
> dashboard's `views:` list instead of the whole file.

#### Entity-id assumptions (manual paste only)

The pasted YAML references the reference install's entity ids (device **Balcony
Solar Forecast**), e.g.:

| Purpose | Entity id |
|---|---|
| Forecast today kWh | `sensor.balcony_solar_forecast_energy_production_today` |
| Forecast daily-kWh MAE | `sensor.balcony_solar_forecast_daily_kwh_mae` |
| Forecast hourly MAE | `sensor.balcony_solar_forecast_hourly_mae` |
| Forecast vs best baseline (%) | `sensor.balcony_solar_forecast_vs_best_baseline_pct` |
| Kill-gate | `binary_sensor.balcony_solar_forecast_kill_gate_passed` |
| Today P10 / P90 | `sensor.balcony_solar_forecast_energy_production_today_p10` / `_p90` |
| Per-comparison MAE | `sensor.balcony_solar_forecast_comparison_daily_kwh_mae_<slug>` |
| Measured site power (total) | `sensor.balcony_solar_forecast_measured_dc_power_total` |
| Measured module power | `sensor.inverter_port_{1,2}_dc_power[_2.._4]` |

If your entity ids differ (multiple installs, renamed entities), fix the
`entity:` lines in the raw editor after pasting — or just use the one-click
action above, which resolves them for you. Any entity that does not exist yet
simply renders as *unknown* — the dashboard never errors on a missing one.

The scoreboard sensors (`engine_daily_kwh_mae`, `engine_vs_best_baseline_pct`,
the per-comparison MAE sensors, `kill_gate_passed`) only appear after v0.4 is
installed **and** at least one nightly scoreboard run has completed. Until then
they read *unknown*, and the kill-gate card shows the "window not full yet"
state.

---

## 2. Configure the comparison baselines (the scoreboard's opponents)

The scoreboard ships with **no comparison baselines** configured — they are
generic and configurable, never hardcoded (D-P9). Add them so the kill-gate has
something to beat:

1. **Settings → Devices & Services → Balcony Solar Forecast → Configure**.
2. In **Comparison sensors**, add one row per external forecast, each with:
   - **name** — a label (becomes the MAE sensor's object-id suffix, slugified);
   - **daily_entity** — an HA sensor whose *state* is that forecast's
     **daily-kWh for today** (same shape as our own
     `energy_production_today`).

The scoreboard reads each comparison entity's **recorder history for
yesterday** (the value *as it stood* during yesterday — no leakage), never its
live value, and compares it against the measured site energy for yesterday.

### The operator's live site (reference config)

| name | daily_entity |
|---|---|
| `8-Entry Baseline` | `sensor.pv_prognose_heute_alle_module` |
| `Alt 1600W` | `sensor.energy_production_today_4` |

- **8-Entry Baseline** — the frozen 8-single-module rany2 ensemble sum template
  (Phase 0), the primary baseline the Phase-1 kill-gate is measured against.
- **Alt 1600W** — the old rany2 "Home-LA" single-1600 Wp today sensor
  (`sensor.energy_production_today_4`), the pre-project baseline.

With those two configured, the per-comparison MAE sensors materialise as:

- `sensor.balcony_solar_forecast_comparison_daily_kwh_mae_8_entry_baseline`
- `sensor.balcony_solar_forecast_comparison_daily_kwh_mae_alt_1600w`

(the `<slug>` is the lowercased alphanumeric form of the name: `8-Entry
Baseline` → `8_entry_baseline`, `Alt 1600W` → `alt_1600w`). These are the
entity ids the shipped dashboard's *Skill scoreboard* card references — if you
choose different comparison names, update those two `entity:` lines.

### As YAML (options snippet)

If you configure the entry via YAML/import rather than the UI, the option looks
like:

```yaml
comparison_sensors:
  - name: "8-Entry Baseline"
    daily_entity: sensor.pv_prognose_heute_alle_module
  - name: "Alt 1600W"
    daily_entity: sensor.energy_production_today_4
```

---

## 3. What the dashboard shows

- **Kill-gate verdict** (markdown) — PASSED / not passed / window-not-full,
  derived from `binary_sensor.…_kill_gate_passed` and the engine-vs-baseline
  percent. This is the gate the whole plan hangs on (SPEC §9/§10): once it is
  green, it is safe to consider re-pointing consumers (e.g. battery_manager) at
  the engine sensors. **The battery_manager cutover stays deferred until then**
  (D-P11).
- **Engine vs best baseline** (gauge) — bound to
  `engine_vs_best_baseline_pct`; positive = engine better on daily-kWh MAE.
- **Skill scoreboard** (entities) — engine daily-kWh MAE, engine hourly MAE,
  engine-vs-best percent, plus each configured comparison's daily-kWh MAE.
- **Forecast vs. measured (site power)** (history-graph) — the instantaneous
  forecast power overlaid with the measured site-total DC power
  (`sensor.…_measured_dc_power_total`, the live sum of the per-module sensors),
  a like-for-like W-vs-W comparison on one y-scale. The today-kWh row is gone:
  mixing kWh and W on one axis is unreadable (the daily-kWh story lives in the
  band card + scoreboard). The measured-total sensor exists only when at least
  one plane has an `actual_entity`; with none configured the graph shows the
  forecast row alone.
- **Hourly production per module** (bundled card — no HACS install) — an
  energy-dashboard-style chart: one **stacked hourly production bar per module**
  (M1…M8, coloured segments) with a **dashed forecast line**; hovering (or
  touching) shows a crosshair with every module's Wh **and the total** for that
  hour (§4c). It replaces the old messy 8-line measured-power history-graph. On a
  partial install where the measured-total sensor is absent it falls back to that
  per-module `history-graph` so a measured view still renders.
- **Measured daily energy per module** (statistics-graph) — daily LTS sums per
  representative module (bar), a best-effort per-plane view.
- **Today's forecast band** (entities) — P10 / P50 / P90 for today (SPEC §6).
- **Learners, drift & degradation** (entities) — source status, degraded flag,
  weather-image age, each learner's status, the applied intraday scalar, and
  the corrected-vs-physics drift MAE.
- **Drift MAE trend** (history-graph).
- **Shademap** (markdown) — how to pull the learned transmittance table via
  `dump_shademap` and eyeball it against your site's known obstructions; see
  below.

---

## 4. Shademap (learned shade transmittance)

The slow learner's per-channel polar map of beam transmittance τ over
(sun-azimuth × elevation × half-year) is **not** exposed as a sensor
attribute — it lives in the `dump_shademap` service response. The dashboard
therefore documents it rather than plotting it, and shows the shademap
**learner status** (active/frozen, bin count) in the *Learners* card.

To pull the full polar table:

1. **Developer Tools → Actions** (Services).
2. Action `balcony_solar_forecast.dump_shademap`, enable **Return response**,
   **Perform action**.
3. Each channel returns a list of bins `{az_deg, el_deg, tau, n}` (sun-azimuth
   0 = North internal). A richer polar/heatmap plot can be rendered offline
   from that JSON (e.g. a small script or a notebook), or fed to a custom card
   if you later install one.

Eyeball the learned τ against the known obstructions (SPEC §13):

- **East hill** — reduced τ at low elevation, sun-azimuth ~60–100° (morning),
  all channels.
- **Building wall** — τ → 0 above the edge for the **south** modules M4/M8 at
  sun-azimuth ~205–218° (early-afternoon beam collapse).
- **Trees** (seasonal) — reduced τ on M4 (strong, ~−15–17 % leafed) and M8
  (weak, ~−4 %) at sun-azimuth ~135–175°, elevation ~30–45°.

---

## 4b. Shade profile diagram (sun path vs. learned shading)

A per-date, per-module picture of the shading: the **sun path** for a chosen
date (elevation over azimuth) with the **currently-learned** beam transmittance
τ coloured along it, plus the learned and static **horizon lines**. It answers
"on *this* date, where along the sun's path is *this* module shaded, and how
much?" — the interactive companion to the `dump_shademap` polar table.

Three integration-owned entities drive it (device **Balcony Solar Forecast**):

| Entity | Purpose |
|---|---|
| `select.balcony_solar_forecast_shade_profile_module` | pick the module/plane |
| `date.balcony_solar_forecast_shade_profile_date` | pick the date to visualise |
| `sensor.balcony_solar_forecast_shade_profile` | state = shaded fraction of daylight (%); the curve arrays are its attributes |

The **date** always defaults to **today** (it is not remembered across
restarts, so the diagram re-opens on the current day), and the **module**
defaults to a **front-facing plane** (the orientation the most modules share,
e.g. the reference site's 115° module `M2`); a manual module pick is remembered.

The **module select** and the **date picker** appear on the built-in dashboard
(the *Shade profile* cards) together with the shaded-fraction state — no custom
card needed for those. The sensor's attributes carry the plottable arrays,
excluded from the recorder like the energy-curve dicts:

- `azimuth`, `sun_elevation`, `transmittance`, `time` — one entry per daylight
  sun-path sample (transmittance is the *effective* beam τ the forecast applies
  there: the static config horizon blended with the learned shademap);
- `horizon_azimuth`, `static_horizon`, `shade_horizon` — the config horizon and
  the learned shade horizon (elevation below which the beam is mostly blocked)
  on an azimuth grid over the day's daylight span.

### The chart (bundled card — no HACS install)

The diagram now ships **with the integration** as a self-contained custom card
— no HACS frontend install and no YAML snippet. If you installed the dashboard
via the one-click `install_dashboard` action (§1a), this card is **already
embedded** (wired to your three shade-profile entity ids) — nothing more to do.
To add it to another dashboard by hand: the integration serves the card's
JavaScript and, in storage-mode Lovelace, auto-registers it as a dashboard
resource, so it appears directly in the card picker:

1. Open your dashboard → **Edit dashboard** → **＋ Add card**.
2. Pick **"Balcony Shade Profile"** from the card list (type *shade* to filter),
   then add it. A live preview renders straight away.
3. Pick a module + date in the card's own header controls; the chart redraws.
   Reading it: the yellow line is the sun's elevation, the dots recolour green →
   amber → red as the learned τ falls (free → partial → shaded), the grey area
   is the learned shade horizon and the thin dashed grey line the static
   configured horizon. The x-axis is the sun azimuth (90° = East, 180° = South,
   270° = West).

The x-axis is now fixed to the site's whole-year daylight azimuth span (both
solstices), so the sun path stays comparable as you step through dates instead
of rescaling with the season, and hovering (or touching) the chart snaps a
crosshair to the nearest sun-path point and shows a readout line with its time,
azimuth + compass direction, shading % (τ) and elevation.

The card auto-discovers the three shade-profile entities above, so the default
YAML is simply `type: custom:balcony-shade-profile-card`. It has four optional
keys — `sensor`, `module_select`, `date_entity`, `title` — set them only to pin
a specific device's entities (e.g. multiple installs). It changes no state
except through the module/date controls.

> **YAML-mode Lovelace only.** Auto-registration needs storage-mode Lovelace
> (the default). If your dashboards are configured in YAML, the integration logs
> the resource URL on start; add it once yourself:
>
> ```yaml
> lovelace:
>   resources:
>     - url: /balcony_solar_forecast/frontend/shade_profile_card.js?v=0.7.0
>       type: module
> ```

### Alternative: the ApexCharts card (HACS)

The same picture can also be drawn by the HACS `apexcharts-card` from an opt-in
snippet, if you prefer it over the bundled card:

1. Install **`apexcharts-card`** via HACS → Frontend.
2. Add a **Manual** card and paste
   [`dashboards/shade_profile_apexcharts.yaml`](../dashboards/shade_profile_apexcharts.yaml).
3. Pick a module + date; the chart redraws. Reading it: the yellow line is the
   sun's elevation; the dots recolour green → amber → red as the learned τ falls
   (free → partial → shaded); the grey area is the learned shade horizon and the
   thin grey line the static configured horizon. The x-axis is the sun azimuth
   (90° = East, 180° = South, 270° = West).

The snippet reads only the three entity ids above; adjust them if your entity
ids differ. It changes no state.

---

## 4c. Power history (hourly production per module)

The messy 8-line *Measured DC power per module* history-graph is replaced by a
second bundled card, modelled on the Home Assistant **Energy dashboard** chart:
one **stacked hourly production bar per local hour**, split into a coloured
segment per module (M1…M8, in config order), overlaid with a **dashed forecast
line** (today's forecast Wh per hour). Hovering (or touching) the chart snaps a
crosshair to the hovered hour column and shows a floating readout panel listing
each module's Wh for that hour, the **Total** (bold), and the **Forecast** Wh —
so you can read the exact per-module contribution and the site total at a glance.

It reads two integration-owned entities (device **Balcony Solar Forecast**),
auto-discovered from `hass.states` when not configured:

| Entity | Purpose |
|---|---|
| `sensor.balcony_solar_forecast_measured_dc_power_total` | the module list (its `sources` + `source_names` attributes) and the statistic ids to chart |
| `sensor.balcony_solar_forecast_energy_production_today` | the 15-min forecast `wh_period` attribute, aggregated to local hours for the dashed line |

The bars come from the recorder's **hourly long-term statistics** (the mean DC
power of each module sensor over the hour × 1 h = Wh), pulled directly via the
`recorder/statistics_during_period` websocket command from local midnight to now
— refetched on load, every 5 minutes, and when the local day rolls over. Modules
therefore need `state_class` for LTS to exist (they do — LTS since 2024-07);
until the recorder has written hourly statistics the card shows a *No hourly
statistics yet* hint. If the forecast sensor is missing its `wh_period` attribute
the bars still render, just without the dashed line.

If you installed the dashboard via the one-click `install_dashboard` action
(§1a), this card is **already embedded** (wired to your two entity ids). To add
it to another dashboard by hand, the integration serves the card's JavaScript
and (storage-mode Lovelace) auto-registers it, so it appears in the card picker:

1. Open your dashboard → **Edit dashboard** → **＋ Add card**.
2. Pick **"Balcony Power History"** from the card list (type *power* to filter),
   then add it. A live preview renders straight away.

The default YAML is simply `type: custom:balcony-power-history-card` (both
entities auto-discovered). Optional keys — `total_sensor`, `forecast_sensor`,
`title`, and `hours_forecast` (set `false` to hide the dashed forecast line) —
set them only to pin a specific device's entities (e.g. multiple installs) or
tweak the look. It changes no state.

> **YAML-mode Lovelace only.** Auto-registration needs storage-mode Lovelace
> (the default). If your dashboards are configured in YAML, the integration logs
> the resource URL on start; add it once yourself:
>
> ```yaml
> lovelace:
>   resources:
>     - url: /balcony_solar_forecast/frontend/power_history_card.js?v=0.10.0
>       type: module
> ```

---

## 5. Notes

- Every card here is a **built-in** Lovelace card (`markdown`, `entities`,
  `history-graph`, `statistics-graph`, `gauge`). No HACS frontend resources are
  required. A pure test
  ([`tests/core/test_dashboard_yaml.py`](../tests/core/test_dashboard_yaml.py))
  asserts the YAML parses, uses only built-in card types, and references the
  load-bearing entities.
- `history-graph` shows recorder history; `statistics-graph` shows long-term
  statistics (LTS). Modules must have `state_class` (they do — LTS since
  2024-07) for the statistics graph to have data.
- The dashboard is read-only observability; it changes no state and touches no
  consumer (battery_manager is untouched — D-P11).
