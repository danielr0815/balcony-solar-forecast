# Observability dashboard

A ready-to-paste Lovelace dashboard for Balcony Solar Forecast, using **only
built-in Home Assistant cards** — it needs zero custom cards and zero HACS
frontend resources. It surfaces the v0.4 skill scoreboard (the kill-gate), the
P10/P50/P90 uncertainty band, the learner/drift/degradation status, and a
best-effort shademap view (SPEC §9/§10/§14.3).

The dashboard file is [`dashboards/balcony_solar_forecast.yaml`](../dashboards/balcony_solar_forecast.yaml).

---

## 1. Install (copy-paste, ~1 minute)

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

### Entity-id assumptions

The cards reference the reference install's entity ids (device **Balcony Solar
Forecast**), e.g.:

| Purpose | Entity id |
|---|---|
| Forecast today kWh | `sensor.balcony_solar_forecast_energy_production_today` |
| Forecast daily-kWh MAE | `sensor.balcony_solar_forecast_daily_kwh_mae` |
| Forecast hourly MAE | `sensor.balcony_solar_forecast_hourly_mae` |
| Forecast vs best baseline (%) | `sensor.balcony_solar_forecast_vs_best_baseline_pct` |
| Kill-gate | `binary_sensor.balcony_solar_forecast_kill_gate_passed` |
| Today P10 / P90 | `sensor.balcony_solar_forecast_energy_production_today_p10` / `_p90` |
| Per-comparison MAE | `sensor.balcony_solar_forecast_comparison_daily_kwh_mae_<slug>` |
| Measured module power | `sensor.inverter_port_{1,2}_dc_power[_2.._4]` |

If your entity ids differ (multiple installs, renamed entities), fix the
`entity:` lines in the raw editor after pasting. Any entity that does not exist
yet simply renders as *unknown* — the dashboard never errors on a missing one.

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
- **Forecast vs measured** (history-graphs) — engine today/tomorrow kWh and
  power-now vs the 8 per-module measured DC-power sensors (ground truth).
- **Measured daily energy per module** (statistics-graph) — daily LTS sums per
  representative module (bar), a best-effort per-plane view.
- **Today's forecast band** (entities) — P10 / P50 / P90 for today (SPEC §6).
- **Learners, drift & degradation** (entities) — source status, degraded flag,
  weather-image age, each learner's status, the applied intraday scalar, and
  the corrected-vs-physics drift MAE.
- **Drift MAE trend** (history-graph).
- **Shademap** (markdown) — see below.

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
