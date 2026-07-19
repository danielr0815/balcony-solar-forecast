# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.20.6] - 2026-07-19

### Removed

- **Withdrawn: the per-plane `actual_energy_entity` field from 0.20.5.** It
  rested on a wrong premise. Measured on the live install, the inverter's
  `*_dc_total_energy` counters do **not** report DC energy despite their name —
  they track the **AC** output. Per-inverter over a full day: AC 1235 / 1586 /
  1432 / 1679 Wh against counters 1229 / 1585 / 1430 / 1673 Wh, i.e. a ratio of
  1.000–1.005. A 100 %-efficient inverter does not exist. Against the DC power
  sensors the same day gives η = 0.9472 on all four units (identical to four
  decimals), which is the plausible partial-load figure for these microinverters.
  Charting those counters as "measured daily energy per module" therefore
  labelled AC yield as DC energy, next to a DC dashboard, in a project whose
  whole point is per-module attribution. The config field, its validation, the
  translations and the generator wiring are reverted; existing entries that
  carry the key are unaffected (unknown keys are ignored on load).

- **The per-module LTS `statistics-graph` is no longer generated.** The bundled
  power-history card charts daily Wh per module from the SAME daily `mean`
  statistics of the SAME power sensors — stacked, with the forecast overlay and
  a day/week toggle. A second grouped-bar view of identical data added nothing.
  The shipped built-ins-only `dashboards/balcony_solar_forecast.yaml` **keeps**
  it (there the bundled card does not exist, so it is the only per-module view),
  with the 0.20.4 `sum` → `mean` fix intact and now guarded by a YAML test.

### Note

The 0.20.4 fix stands and is unaffected: charting `sum` on a power sensor yields
an empty card, and `mean × 24 h` is exact. That was verified three independent
ways — time-weighted integration of 1420 raw states, the sum of hourly means,
and daily-mean × 24 all give 858 Wh for the same module-day. The ~6 % gap to the
counters is the inverter's conversion loss, not an error.

## [0.20.4] - 2026-07-19

### Fixed

- **"Measured daily energy per module (LTS)" rendered as an empty card.** The
  statistics-graph asked for `stat_types: [sum]`, but the entities it charts are
  the configured per-plane `actual_entity` POWER sensors (W, `state_class:
  measurement`). The recorder keeps mean/min/max for those and reports
  `has_sum: false`, so the card had no series to draw and showed an empty plot
  area — the measured production looked "gone" even though 14 days of daily LTS
  rows were present the whole time. The card now charts `mean` (the statistic
  that actually exists) and is retitled "Measured mean DC power per module
  (LTS)"; daily mean W × 24 h is the day's energy, so the bar shape is
  unchanged. Fixed in both the shipped `dashboards/balcony_solar_forecast.yaml`
  and the `install_dashboard` generator.

## [0.20.3] - 2026-07-17

### Fixed

- **Shade-profile card: the status readout no longer gets cut off.** With the
  shade-edge and live "Jetzt" additions the readout line grew long enough to
  overflow one line on a narrow card, where `text-overflow: ellipsis` clipped
  the tail (e.g. "… Schattenkante 43° · …"). The line now wraps instead of
  clipping, so every value stays visible; the block grows a line rather than
  truncating.

## [0.20.2] - 2026-07-17

### Added

- **Shade-profile card (`shade_profile_card.js`): the cursor readout now shows
  the shading-edge elevation.** The bundled custom card — the one the setup
  guide ships and most installs actually use — gains the same feature that
  0.20.1 added to the optional ApexCharts snippet: next to the sun's elevation
  at the hovered azimuth, the hover line now appends the horizon (obstruction)
  elevation there ("Schattenkante") — the angle below which the beam would be
  blocked ("unter welchem Winkel der Schatten zuschlägt"). It is interpolated
  live from the card's own horizon arrays (learned `shade_horizon`, falling back
  to `static_horizon`), so no sensor/back-end change is required. Because the
  card is cache-busted by `?v=<INTEGRATION_VERSION>`, a browser hard-reload
  after the update picks up the new readout automatically.
- **Shade-profile card: the current sun position is shown when idle.** Whenever
  the pointer is NOT over the plot (so no hover crosshair is drawn), the card now
  marks where the sun is *right now* on the plotted path — an accent halo on the
  sun path plus a faint vertical guide — and the status line shows its live
  readout ("Jetzt · HH:MM · az° · Verschattung … · Elevation … · Schattenkante
  …") instead of the "hover for details" hint. It appears only when the plotted
  date is today and the sun is currently up (between sunrise and sunset);
  otherwise the idle hint is unchanged. The marker refreshes on a ~1-minute
  timer (started/stopped with the element's lifecycle) so it tracks the sun and
  clears itself at sunset without needing a page reload — the forecast sensor is
  time-of-day-invariant for a fixed date and would not otherwise trigger an
  update. Pointer Events drive the hover so a tap on a touchscreen cleanly hands
  over to the crosshair and restores the live marker on release. "Today" and
  "now" are resolved in the site's timezone (`hass.config.time_zone`) so the
  marker lines up with the sensor's local-time samples even if the browser is
  elsewhere.
- **Power-history card (`power_history_card.js`): the hover tooltip is larger and
  more legible.** The floating per-hour readout panel (time, per-module values,
  total, forecast) drew its text at a fixed size in viewBox units, so on a narrow
  card it shrank to roughly the axis-tick size and read as tiny next to the HTML
  title and legend. The panel — font, row height, padding, colour swatches and
  width — is now derived from one font-size constant, bumped ~45 %, so the whole
  tooltip scales up as a unit while still flipping sides at the mid-line and
  fitting within the plot.

## [0.20.1] - 2026-07-17

### Added

- **Shade-profile diagram: the cursor tooltip now shows the shading-edge
  elevation.** Alongside the sun's elevation at the hovered azimuth, the tooltip
  reports the learned and configured horizon (obstruction) elevation there — the
  angle below which the beam would be blocked ("unter welchem Winkel der Schatten
  zuschlägt") — plus a free/shaded verdict. Interpolated live from the plotted
  horizon series in `dashboards/shade_profile_apexcharts.yaml`, so it is a
  card-only change (no integration/sensor update required).

## [0.20.0] - 2026-07-16

### Added

- **Configurable site ground albedo.** New optional "Ground albedo" field in
  the setup/reconfigure flow (`site.albedo`, blank = the shipped 0.2). The
  reflected-diffuse term matters disproportionately on steep balcony tilts
  (70–90°), where the ground-view factor reaches 0.4–0.5 — a dark courtyard or
  lawn (~0.1) vs the textbook 0.2 shifts the diffuse floor by 10–20 %. Snow
  days still override with the snow albedo. Values are clamped to [0.05, 0.9];
  pre-0.20 configs are untouched (absent key = default, bit-identical curve).
  The offline backfill honours the same value.
- **AC-calibration raw-ratio diagnostic.** The nightly inverter calibration now
  records the measured AC/DC ratio summary BEFORE the plausibility band
  (`raw: {date, median_ratio, n, in_band_n}` inside the
  `inverter_efficiency_learned` attribute) — including when every sample is
  out-of-band and the EMA folds nothing. A median far outside [0.90, 0.99]
  with `in_band_n: 0` is the smoking gun for a mis-scaled DC sensor (or a
  mis-wired AC meter): previously the calibration silently refused and the
  operator never saw why.

## [0.19.2] - 2026-07-16

### Fixed

- **CRITICAL: nightly actuals reader parsed statistics timestamps as
  milliseconds — every day was discarded, silently starving ALL nightly
  learning since the completeness gate landed.** The in-process recorder API
  (`statistics_during_period`) returns row `start` as float epoch **seconds**;
  `_stat_row_hour_key` assumed epoch **milliseconds** (the WebSocket wire
  format), so all 24 hourly rows of a day collapsed onto one 1970 hour key,
  the per-module day-completeness gate saw `covers only 1 of ~16 daylight
  hours` and discarded every day. Consequence in the field: day-ahead bias,
  shademap training, scoreboard/kill-gate, quantile bands and drift monitoring
  never received a single live training day (bootstrap-seeded state was the
  only learned state). Numeric `start` values are now disambiguated by
  magnitude (> 1e11 ⇒ ms, else seconds); regression tests feed the real
  float-seconds format. The same guard was added to `scripts/backfill.py`.
  After updating, the nightly catch-up refills the last
  `NIGHTLY_CATCHUP_MAX_DAYS` days from long-term statistics automatically.

### Changed

- **Status honesty (operator-facing signals now say what is really
  happening):**
  - New learner status `cold_start`: the day-ahead bias reports it while it
    has NO learned cells (fresh install / right after `reset_day_ahead_bias`)
    instead of claiming `active` while applying nothing.
  - The day-ahead status sensor keeps its `bias_cells` attribute present as
    `{}` with `cells_n: 0` when empty — a deliberate reset is now
    distinguishable from a broken attribute pipeline.
  - `inverter_efficiency` on the power sensor carries an
    `inverter_efficiency_source: config | learned` label — without an AC-meter
    calibration the per-group eta is a verbatim config echo and now says so.
  - The P10/P90 band sensors only expose `band_source` while a band actually
    exists; a non-existent band is no longer labelled "learned".

## [0.19.1] - 2026-07-12

### Fixed

- **Offline backfill now bins the day-ahead bias by solar time too.** The
  bootstrap generator (`scripts/backfill.py`) still binned morning / midday /
  afternoon by the clock hour while the live coordinator moved to apparent solar
  time in 0.19.0 — so a bootstrapped cell and a live-trained cell for the same
  `(cloud_class, day_part)` could mean slightly different sun positions near the
  boundaries. Backfill now uses `solpos.hours_from_solar_noon` +
  `bias.day_part_for_solar`, matching the live binning exactly.

## [0.19.0] - 2026-07-12

### Added

- **Day-ahead bias cells are now visible in the UI.** The learned per-(cloud
  class × day part) multipliers ride along as a `bias_cells` attribute on
  `sensor.balcony_solar_forecast_day_ahead_bias_status` — each cell's raw
  `theta`, trained-day count `n`, and the `applied` factor actually served — so a
  mis-trained cell can be spotted directly in the UI instead of only in a
  diagnostics download.
- **New action `reset_day_ahead_bias`.** Clears all learned day-ahead bias cells
  so the served forecast falls back to pure physics + shademap at once and
  re-learns each cell from scratch over the following nights. Use it after a
  binning change or when a cell is distorting the curve. Leaves the shademap, the
  per-layer enable switches and the rollback ring untouched; returns the number
  of cells cleared.

### Changed

- **Day-ahead bias is now binned by apparent SOLAR time, not the wall clock.**
  The morning / midday / afternoon boundaries were fixed local hours (10:00 /
  14:00): they drift against the sun across the DST changeover and the seasons,
  and pin the correction's transition to a clock time rather than the sun. They
  now bracket solar noon symmetrically (± 2 h) via the sun's hour angle
  (`solpos.hours_from_solar_noon`), so a boundary tracks the sun instead of the
  clock and a cell learned in summer applies at the same solar position in
  winter. The quantile bands share the same solar day-part binning. Cell keys are
  unchanged, so the upgrade resets no learner state — run the new
  `reset_day_ahead_bias` action to retrain cleanly under the new binning if a
  pre-existing cell is distorting the forecast.

## [0.18.1] - 2026-07-12

### Fixed

- **Day-ahead bias no longer steps at the day-part boundaries.** The learned
  day-ahead correction is bucketed per (cloud class × day part), and it was
  *applied* as a hard per-part step — producing an unphysical cliff in the
  forecast exactly at 10:00 (morning→midday) and 14:00 (midday→afternoon), e.g.
  a ~35 % drop from the 09:00 hour to the 10:00 hour on an otherwise smooth
  morning ramp. The forecast shape comes from weather × physics × shading, which
  is smooth, so the correction on top must be smooth too: the learned cells are
  now the anchors and the applied factor is **linearly blended** between the two
  adjacent parts within ±`DAY_PART_BLEND_HALFWIDTH_MIN` (45 min) of each
  boundary (`bias.day_ahead_factor`). Away from the boundaries nothing changes;
  the nightly training is unchanged.

## [0.18.0] - 2026-07-12

### Added

- **AC-side forecast (Phases 1–4).** The forecast now models the served **AC**
  power behind the micro-inverters, not only the DC array:
  - **DC→AC chain** — per inverter group `AC = min(η_inv · Σ_ports DC ·
    slot-factor, ac_limit_w)`, with the DC clip point at `ac_limit_w / η_inv`
    (where the ports really clip, because the micro-inverter caps AC and
    back-drives the MPP).
  - **`measured_ac_power` sensor** — the live reading of an optional whole-site AC
    meter, the AC ground-truth partner of `measured_dc_power_total`; created only
    when a meter is configured, with an optional sign-invert for meters that
    report the fed-in balcony-solar power as a negative value.
  - **Learned inverter efficiency η** — a single site-level scalar calibrated
    against the AC meter over unclipped, above-min-load hours, clamped to
    [0.90, 0.99] and trusted only after ≥ 20 eligible hours. **Never
    load-bearing**: no meter / too few samples / an out-of-band ratio all fall
    back to the configured/default η, and the DC learning + scoreboard are
    untouched. It rides as the `inverter_efficiency_learned` attribute of
    `power_production_now`.
  - **Config-flow AC-meter picker** — the setup and reconfigure steps gain
    optional **Total-AC meter (behind the inverters)** and **Invert the AC meter
    sign** fields, merged into the site config so they round-trip through
    `SiteConfig` exactly like the coordinates.
  - **Dashboard** — the *Forecast vs. measured* card pairs the AC forecast with
    the AC meter (an honest **AC-vs-AC** comparison) when one is configured,
    falling back to the DC total otherwise; a new *DC model & inverter calibration
    (diagnostic)* card surfaces the DC forecast plus the learned η; the
    power-history card's title and provenance caption now mark the bars as measured
    DC and the dashed line as the AC forecast.

### Changed

- **The existing main sensors now report AC (behind the inverters), not DC** — a
  deliberate, operator-visible history step. `energy_production_today / _tomorrow
  / _d2`, `power_production_now` and the P10/P50/P90 bands are the **AC** curve
  (the operator-facing standard); the model-internal **DC** view moved to the new
  `power_production_now_dc` and `energy_production_{today,tomorrow,d2}_dc`
  diagnostic sensors. DC stays the self-learning / scoreboard ground truth, so the
  learning behaviour and skill scores are unchanged — only the headline unit is now
  AC.

## [0.17.1] - 2026-07-11

### Fixed

- **Power-history week view massively overstated today's production.** The week
  bars use the `period: "day"` mean statistic × 24 h, which recovers a day's
  energy exactly — but only for a COMPLETE day. For the still-running current
  day, Home Assistant builds the daily mean over just the hours elapsed so far
  (the sunlit ones), so × 24 extrapolated a full day from them and overstated
  today by up to ~24/elapsed-hours (e.g. ~17.6 kWh at ~16:00 for a ~12 kWh day).
  Today's column is now summed from HOURLY statistics (× 1 h) exactly like the
  day view, so it shows production up to now; complete past days are unchanged.

## [0.17.0] - 2026-07-11

### Added

- **Power-history card: forecast overlay in the week view.** Each day column now
  carries a dashed forecast segment at that day's forecast total — past days
  from the archived ISSUED snapshots (one `get_issued_forecast` lookup per day,
  fired concurrently and cached per window), today from the live `wh_period`
  sum. A day with no archived snapshot keeps an honest gap (no segment), and
  the hover panel gains a **Forecast** row ("—" on gap days). The
  `get_issued_forecast` response additionally reports `oldest_available` (the
  oldest archived date in the 90-day ring, or `null` while the ring is empty).

### Fixed

- **Power-history card: an empty past day was misread as "the forecast is not
  updating".** Navigating the day view to a date without an archived snapshot
  silently dropped the line behind a tiny hint. Now the previous day's line is
  cleared the moment navigation starts (no stale line while the new day loads),
  a FAILED service lookup is reported distinctly (*Forecast lookup failed*)
  from a genuinely missing snapshot (*No archived forecast for \<date\> — the
  archive fills with each nightly run.*, plus *archive since \<date\>* when the
  ring is non-empty), and a drawn line carries a provenance caption —
  *Forecast (live)* today vs *Forecast (as issued 01:30)* on past days.

## [0.16.1] - 2026-07-11

## [0.16.0] - 2026-07-11

### Added

- **Ensemble-weather uncertainty bands** (opt-in, default OFF; SPEC §6.1). When
  enabled, today's Open-Meteo ensemble spread (`ensemble-api`, `icon_seamless`,
  40 members) is folded into the learned P10/P90 bands by **envelope-max** — the
  wider band wins per slot, never multiplied, so the climatological weather share
  already inside the learned residuals is not double counted. Per-slot factors are
  the 0.1/0.9 percentiles of `member_GHI / deterministic_GHI` (a documented
  GHI-proportionality approximation — no per-member engine pass). The ensemble is
  **never load-bearing**: P50, the headline, the scoreboard and the kill-gate are
  untouched, and any fetch failure/absence degrades seamlessly to the learned
  bands (its absence is not a degradation rung). Fetched on its own ~3 h cadence,
  cached in memory only (no store-schema change). A new `band_source` attribute on
  the P10/P90 sensors reports whether today's band came from `learned`, `envelope`
  or the cold-start `ensemble` win; diagnostics gain an ensemble block.

## [0.15.0] - 2026-07-11

### Added
- **Power-history card: day/week navigation + an archived forecast line for past
  days.** The bundled power-history card gains a header `◀ [label] ▶` to step the
  selected day (Today / Yesterday / the date; ▶ disabled at today) and a
  **Day | Week** toggle. The **week view** charts seven stacked day-bars of daily
  production per module from `period: "day"` mean statistics (mean W × 24 h). For
  **past days** the dashed line is no longer the live curve but the forecast **as
  it was issued** that day, read from the store's 90-day issued ring via a new
  read-only action, `balcony_solar_forecast.get_issued_forecast`
  (`SupportsResponse.ONLY`) — the frozen ~01:30 day-ahead stand with no hindsight,
  so "issued vs. measured" stays an honest comparison; a day with no archived
  snapshot returns `available: false` and the card draws no line (with a small
  hint). The 5-min auto-refresh and day-roll handling apply only while viewing the
  live window (today / current week); a past view is static. The selection is
  card-local and never persisted. See SPEC §15.4 and docs/DASHBOARD.md §4c.
- **Shade-profile card: confidence visualisation + a card-local comparison
  date.** Each sun-path dot is now *sized* by the learned evidence behind its τ:
  the sensor gains a per-sample `sample_n` attribute (the pooled shademap-bin
  sample count, summed over the read pool via a new shared
  `shademap.pooled_bin_n` helper so it can never diverge from the applied τ), and
  the bundled card renders `n=0` as a small hollow ring and `n>0` as a filled dot
  that grows to full size at `N_SAT` (12) samples (the hover readout adds
  `· n=<count>`). The card also gains a header **"Compare"** date picker that
  overlays a second date's sun path as a dashed line with hollow τ rings (its
  shade horizon omitted for readability), a legend naming both dates, and a
  crosshair readout that appends the comparison's shading at the same azimuth.
  The overlay is fed by a new read-only action,
  `balcony_solar_forecast.get_shade_profile` (`SupportsResponse.ONLY`), which
  returns the diagram's curve arrays for any module/date (defaulting to the
  current selection) without mutating the live selection or evicting the diagram
  memo. `sample_n` is excluded from the recorder like the other curve arrays.
  See SPEC §15 and docs/DASHBOARD.md §4b.

## [0.14.1] - 2026-07-11

### Fixed
- **`energy_production_today` headline no longer understated on AC-clamped,
  up-corrected slots.** The day-ahead headline strips the transient intraday
  scalar by dividing it back out of each current-day slot. On a slot where the
  up-corrected grouped power hit the inverter AC ceiling, the second re-clamp had
  already discarded the scalar, so dividing it out again removed a correction
  that was never applied — understating the headline by up to the full factor
  (2.5). The engine now exposes the per-slot pre-re-clamp corrected total
  (`ForecastResult.corrected_unclamped_watts`); the coordinator uses the served
  ceiling unchanged on a clamped slot and divides only where the scalar actually
  reached the served value. Sites with no inverter groups (clamp never bites) and
  older cached results (empty field → divide-always) are bit-identical to before.

## [0.14.0] - 2026-07-10

### Added
- **`suggest_shade_groups` service — data-driven shade grouping.** Since v0.13.0
  every module's shading is learned individually; this read-only action compares
  the per-plane shademap channels bin-wise (n-weighted mean transmittance
  difference over the bins both planes learned) and returns a similarity matrix
  plus a grouping suggestion built by complete-linkage agglomeration, so the
  operator no longer eyeballs the polar tables (or the card's Group/Single
  toggle) to decide which planes share shade. Two thresholds are configurable per
  call (`max_diff`, `min_common_bins`); the response also echoes the CURRENT
  grouping for comparison. Planes with no learned evidence are flagged
  `insufficient_data`. Pure similarity math in `core/shademap.py`
  (`channel_similarity` / `suggest_shade_groups`). See SPEC §5.

### Changed
- **Recompute-path performance (BIT-IDENTICAL outputs).** Three hot-path
  optimisations, each proven equal to the prior implementation by test (no
  forecast number moves): (1) the engine no longer runs the Hay-Davies
  transposition + horizon interpolation TWICE per plane per slot when a learner
  is active — the tau-independent POA decomposition is computed once and the RAW
  (static-tau) and CORRECTED (learned-tau) curves are derived by re-gating the
  shared beam; (2) the sky-view-factor quadrature is memoised at module level,
  keyed on the plane geometry + day-of-year, so it survives across the 15-min
  recompute cycles instead of being redone once per `compute_forecast` call; and
  (3) the cached Open-Meteo payload is parsed into a `WeatherSeries` once per
  fetch and reused across recomputes rather than re-parsed every tick.
- **Three physics refinements (forecast numbers shift slightly).** (1) The
  Hay-Davies anisotropy index now divides DNI by the *eccentricity-corrected*
  extraterrestrial normal irradiance `E0n = 1361·(1 + 0.033·cos(2π·doy/365))`
  (Spencer/Duffie-Beckman) instead of the fixed solar constant, so the
  circumsolar weight tracks the ±3.3 % Earth-Sun distance over the year — this
  moves our transposition toward pvlib (worst golden-vector deviation ~1.9 % →
  ~0.28 %, tolerance tightened 1.5 % → 0.5 %). (2) The Ross cell-temperature
  coefficient is now overridable per plane (`ross_coeff`, ~0.02 free-standing …
  ~0.056 facade-parallel; validated to a finite `[0.005, 0.12]`), defaulting to
  the global `ROSS_COEFF`. (3) The sky-view factor treats the horizon as
  *semi-transparent to the diffuse*: sky below the horizon line contributes the
  row's (seasonally-resolved) transmittance of its value instead of being fully
  blocked, so a tree line no longer darkens the diffuse like a wall — the SVF is
  now day-of-year-dependent (foliage ramp), memoized per (plane, doy). The beam
  path is unchanged except for the anisotropy weighting, so no shademap
  re-convergence is needed. Live and backfill share the identical refined
  physics.
- **Quality-scale housekeeping (no behaviour change).** Entity icons moved out
  of hardcoded `_attr_icon` into the central `icons.json`, keyed by
  translation_key (plus icons for the five services); the one dynamic
  per-comparison MAE sensor keeps its icon inline because it has no stable
  translation_key. Every entity platform module (`sensor`, `binary_sensor`,
  `select`, `date`) also declares `PARALLEL_UPDATES = 0`, since all I/O is
  centralised in the coordinator and entity updates are local.

## [0.13.0] - 2026-07-10

### Added
- **Brand icon (local, no upstream submission).** The integration now ships its
  own brand PNGs under `custom_components/balcony_solar_forecast/brand/`
  (`icon`/`logo`, plus `@2x`), served by Home Assistant ≥ 2026.3's **local brands
  proxy** — so the custom integration shows its icon with no PR to the
  `home-assistant/brands` repository (a deliberate no-upstream-submission choice).

### Changed
- **Shade groups now pool at READ time, not by merging (supersedes the v0.12.0
  merge design).** Every module's learned shading is stored INDIVIDUALLY under
  its own channel forever; grouped planes are pooled only when the forecast /
  diagram reads the map — the n-weighted mean of each pool channel's matching
  bin (`tau_pool = Σ nᵢ·τᵢ / Σ nᵢ`) blended once against the static prior with
  the shared shrinkage weight (`w = n_pool/(n_pool+K)`). Grouping and dissolving
  a group are therefore **fully reversible and lossless**: a dissolved group
  instantly reads each plane's own channel again, with no data lost. The
  nightly trainer and `scripts/backfill.py` write per-plane again; the
  coordinator's `beam_tau` hook and the shade-profile diagram do the pooling via
  the new pure `shademap.effective_tau_pooled`. Group channels left behind by
  the earlier v0.12.0 merge migration are read as a **legacy evidence source**
  (folded into their members' pool until diluted by live per-plane data), so
  already-merged installs keep their learning. The one-way
  `shademap.merge_channels` migration and its setup call are removed. The
  shade-profile sensor now exposes a second `transmittance_individual` curve
  (the module's own channel) and the bundled card gains a **Group/Single
  toggle** so the operator can compare each module's individual shading against
  the pooled view and decide groupings. See SPEC §5.

## [0.12.0] - 2026-07-10

### Added
- **Configurable shade groups (shared shademap learning).** An optional
  `shade_group` per plane lets modules that see the same sky occlusion (a
  building edge, a tree line — a property of the *site*, not one module) pool
  their slow-learner shade map into ONE channel instead of one per measurement
  channel, so a bin the south module proves also informs the north module (only
  the per-plane beam-share impact still differs). Default (no group) is
  per-plane, exactly as before. The measurement and all quasi-clear gates stay
  per plane; only the storage/read channel is shared (`PlaneConfig.shade_channel
  = shade_group or name` is the single source of truth, applied in the
  coordinator's `beam_tau` hook, the nightly trainer and `scripts/backfill.py`).
  Grouping existing planes migrates their persisted per-plane channels into the
  group channel once via the new pure `shademap.merge_channels` (n-weighted bin
  merge); dissolving a group is a documented one-way step (planes restart from
  the static prior, the group channel lingers as a harmless orphan, recoverable
  via `rollback_learners`). Validation guards against a group name aliasing a
  non-member plane's own channel. See SPEC §5.

## [0.11.0] - 2026-07-10

### Added
- **Bundled power-history card (energy-dashboard style).** A second self-contained
  Lovelace card, `custom:balcony-power-history-card` ("Balcony Power History"),
  served and auto-registered by the integration (no HACS install). It replaces
  the messy 8-line *Measured DC power per module* history-graph with a
  Home-Assistant-Energy-dashboard-style chart: **stacked hourly production bars
  per module** (M1…M8, one coloured segment each) overlaid with a **dashed
  forecast line**, and a hover crosshair whose floating readout lists every
  module's Wh **and the total** for the hovered hour. The bars come from the
  recorder's hourly long-term statistics (pulled via
  `recorder/statistics_during_period`, refreshed every 5 minutes and on day
  roll-over); the forecast line aggregates the forecast sensor's 15-min
  `wh_period` to local hours. To support it, the measured-total sensor now also
  exposes a `source_names` attribute (plane names M1…M8 aligned with `sources`),
  and the generated dashboard embeds the new card (falling back to the old
  per-module history-graph when the measured-total sensor is absent). See
  docs/DASHBOARD.md §4c.

## [0.10.0] - 2026-07-10

### Added
- **Measured site-total DC-power sensor.** A new
  `sensor.…_measured_dc_power_total` sums the configured per-module measured
  DC-power entities (each plane's `actual_entity`) into one site-total power
  reading (W, `state_class: measurement`, so Home Assistant keeps long-term
  statistics). It tracks its source sensors directly and stays available while at
  least one still reports — independent of the forecast coordinator — so its
  history is the real measured envelope even when the forecast is degraded. It is
  created only when at least one plane has an `actual_entity`.

### Changed
- **Dashboard UX fixes.** The observability dashboard (both the generated one and
  the shipped copy-paste YAML) is tidied: the forecast graph drops the pointless
  today-vs-tomorrow juxtaposition and becomes a like-for-like power comparison —
  retitled *Forecast vs. measured (site power)* — pairing the forecast power with
  the new measured-total sensor and dropping its today-kWh row (kWh and W do not
  share a y-axis); the measured per-module DC-power graph now labels its rows by
  plane (M1…M8) instead of the inverter ports' ambiguous own names. The generated
  dashboard additionally drops its redundant *Shade profile (per date & module)*
  controls card (those controls are embedded in the bundled diagram card) and its
  shademap note now points at *your* site's obstructions generically instead of
  hardcoding the reference install's east-hill/wall/tree sectors.

### Fixed
- **Learner corrections + quantile bands re-clamped to the inverter AC limits.**
  The fast-learner slot factor is applied to the already-clamped per-plane watts
  and the groups are then clamped a SECOND time, so an up-correction (factor > 1)
  or a P90 band factor > 1 can no longer lift the served curve above what the
  inverters can physically deliver (live-observed 3382 W on a 3200 W site).
  Down-corrections (factor ≤ 1) and ungrouped, ceiling-free planes are unchanged.

## [0.9.0] - 2026-07-10

### Added
- **Shade-profile card hover readout.** Moving the mouse (or touching) over the
  bundled shade-profile card now snaps a crosshair to the nearest sun-path sample
  and shows a fixed status line with its time, azimuth + compass direction,
  shading % (τ) and elevation — the exact shading value is surfaced here rather
  than as a second curve. The card keeps its single elevation y-axis.

### Changed
- **Year-stable shade-profile x-axis.** The card's azimuth axis is fixed to the
  site's widest whole-year daylight span (both solstices, exposed by the sensor
  as `axis_azimuth_min` / `axis_azimuth_max` and defensively unioned with the
  per-date data span) so the sun path stays comparable across dates instead of
  rescaling with the season.

## [0.8.0] - 2026-07-10

### Added
- **One-click dashboard install.** New action `balcony_solar_forecast.install_dashboard`
  writes the full observability dashboard — wired to *this* install's real entity
  ids (resolved from the entity registry) and embedding the bundled shade-profile
  card — into a dashboard you created empty in the UI (URL `balcony-solar`). It is
  idempotent (re-run to refresh after an update, via a `bsf_managed` marker) and
  refuses to overwrite a dashboard it did not create unless `overwrite: true` is
  passed. The raw-YAML copy-paste remains as the manual alternative.
- **Bundled shade-profile card.** The sun-path-vs-learned-shading diagram
  (SPEC §15) now ships as a self-contained, dependency-free custom Lovelace card
  (`custom:balcony-shade-profile-card`): the integration serves the JavaScript
  under `/balcony_solar_forecast/frontend/shade_profile_card.js` and, in
  storage-mode Lovelace, auto-registers it as a version-busted dashboard
  resource, so it appears in the card picker with zero HACS installs and zero
  YAML. The HACS `apexcharts-card` snippet remains as an alternative.

## [0.7.0] - 2026-07-10

### Changed
- **Shademap warm-up:** a fresh bin's first sample no longer dominates the EMA
  for weeks — young bins use an adaptive alpha (`max(α, 1/(n+1))`), i.e. the
  exact arithmetic mean of their first ~6 samples, then the standard EMA. The
  offline backfill mirrors the formula sample-for-sample.
- **Cloud classification uses cumulative (random-overlap) total cover** instead
  of the arithmetic layer mean: a single opaque deck now correctly classifies
  *overcast* instead of *mixed*, cleaning the taxonomy shared by the day-ahead
  bias, the quantile bins and the scoreboard strata.
- **Quantile ring is date-windowed** (`QUANTILE_RING_DAYS` relative to the
  trained day; samples stored as dated pairs, legacy bare floats grandfathered)
  and bands additionally require evidence from `QUANTILE_MIN_DAYS` (5)
  **distinct days** — a burst of correlated hours on a single day can no longer
  un-collapse a band.

### Fixed
- **Drift monitor blames the guilty layer only.** The nightly snapshot
  additionally records the shademap-only curve; a losing day is attributed per
  layer (slow: shademap-only vs physics; day-ahead: corrected vs shademap-only)
  with independent streaks, so a drifting layer no longer drags the innocent
  one into auto-disable + rollback. Legacy snapshots keep the old shared
  signal.

### Added
- 34 tests closing the last audit gaps: the shade-profile UI entities
  (select/date/sensor platform behaviour) and the nightly orchestration
  (catch-up date math incl. month/year boundaries, failure isolation,
  idempotent re-runs). CI now prints a report-only coverage summary (no gate).

## [0.6.0] - 2026-07-10

### Added
- **Reconfigure flow.** Structural setup (location, update cadences, the full
  site object) is now edited via the integration's "Reconfigure" action
  straight into `entry.data` (HA quality-scale pattern); stale structural keys
  left in `entry.options` by the legacy options flow are stripped atomically on
  the first reconfigure. The options dialog is slimmed to runtime tunables
  (learner switches, quantile bands, comparison sensors) and preserves existing
  option keys so legacy entries keep their live site until reconfigured.
- **Structured comparison-sensor editor.** The scoreboard comparison list is a
  proper per-row form (name + entity picker filtered to `sensor`) instead of a
  raw object editor.
- **CONTRIBUTING.md** (hand-formatting policy, SPEC-is-contract rule, dev env,
  test architecture, release process) and a real HACS store page: the README
  gains installation + configuration sections and links the previously
  orphaned `docs/BACKFILL.md`.
- **ASHRAE incidence-angle modifier on beam + circumsolar** (`IAM_B0` = 0.05,
  SPEC §4). Glass reflection costs 5–15 % of the direct share at AOI > 60° —
  a large part of the day on 70–80° facade planes — and without the modifier
  the shademap absorbed the optics deficit as AOI-shaped **phantom shading**
  (visible in the shade-profile diagram as learned shade no obstacle explains).
  Applied at the engine stage (pvlib-style, after the pure transposition, so
  the pvlib golden vectors stay comparable) and before the ungated trainer
  reference; the backfill applies it byte-identically. Expect slightly lower
  raw forecasts at high AOI and cleaner learned bins over time.
- **SPEC §15** documents the v0.5.0 shade-profile diagram (entities, defaults,
  slow-active gating, tunables); the code's stale "§5" citations now point at
  it, and §4 records the IAM.

- Tests for the previously uncovered SPEC §7 degradation ladder (status rungs,
  fetch failure/success/coverage-refusal, end-to-end cached/unavailable paths,
  learner-hook composition) and for the initial config-flow submit path
  (including the lat/lon-into-site merge that prevents forecasting for the
  wrong location), plus the channel-dropout gates.

### Changed
- **Coordinator split into concern-group modules** (pure code motion, no
  behaviour change): the 2900-line `coordinator.py` now delegates to
  `_actuals.py` (LTS reader + dropout gates), `_nightly.py` (training/guard
  sweep), `_scoreboard_glue.py` (leak-free scorer) and `_glue_util.py` (shared
  helpers).

- **One shared hourly-kc reduction for both training paths**
  (`clearsky.hourly_kc`, the clear-sky-energy-weighted mean). The live nightly
  trainer previously collapsed each hour to its final slot's kc — the highest-
  elevation slot of a morning hour but the lowest of an evening hour, an
  azimuth-asymmetric quasi-clear gate — while the backfill used the hour-mean
  GHI. A bootstrapped shademap now gates identically to live training.
- **Backfill gained the live trainer's day-level hygiene gates**: the
  measured-clear day gate (a snow-covered or overcast day passes every
  per-hour check and would seed τ≈0 into every traversed winter bin), a
  per-hour snow gate, and the frozen-channel module-day drop.
- **Store trims:** night hours (all-zero) are dropped from the issued ring's
  per-plane curves, values are rounded (0.01 Wh / 6-decimal kc), and the
  never-populated `ghi` dict is no longer serialized — old blobs round-trip
  unchanged.
- **Services are registered in `async_setup`** (quality-scale `action-setup`):
  all four services exist independent of config-entry load state, so
  automations get a clear error instead of "Service not found" during startup
  outages.

### Fixed
- **HTTP 429 from Open-Meteo is now retried and Retry-After honoured.** 429 was
  misclassified as a permanent client error; the fetcher now treats it as
  transient, honours a parseable delta-seconds Retry-After exactly (instead of
  jittered backoff), and never stalls the recompute tick longer than 30 s — a
  longer server wait defers to the coordinator's own cadence with the last-good
  cache serving (SPEC §7).
- **Comparison-MAE sensor object-id pinning actually works now.** The formerly
  used `_attr_suggested_object_id` does not exist in HA 2026 and was silently
  ignored; the id is now pinned via a pre-set `entity_id` (the supported
  integration-suggested path), and `ComparisonConfig.slug` is strictly ASCII so
  a non-ASCII label ("Süd") can no longer produce an invalid unique_id/entity
  id that diverges from the documented dashboard id.
- **Drift monitor no longer auto-disables a learner on rounding-scale noise.**
  A "losing" day now requires the corrected daily-kWh MAE to exceed physics by
  both the relative margin AND an absolute floor (`DRIFT_LOSS_MIN_ABS_WH`, 50
  Wh). Previously, on a well-trained/clear day where corrected and raw totals
  differ by only a few Wh, the >2%-relative test was a coin flip on rounding
  noise; seven such flips would auto-disable the layer and roll its state back
  seven snapshots, destroying weeks of legitimate learning over meaningless
  deltas.
- **Channel dropout now discards the whole training day (SPEC §5).** A
  configured module with no usable LTS rows (dead/unavailable DTU port), or one
  covering too little of the daylight span (died mid-day), previously slipped
  through: the day trained every nightly consumer (day-ahead RLS, quantile
  ring, drift monitor, scoreboard kill-gate) with FULL-site modeled vs
  PARTIAL-site measured energy — a persistent phantom production deficit in
  write-once rings. The per-module completeness gate now applies to every
  configured module (previously the best-covered module masked a partial
  sibling), matching the SPEC's "Messkanal-Dropout ⇒ ganzen Tag verwerfen".
- **The keep-richer fetch branch no longer stamps stale weather as fresh
  (SPEC §7).** When a new Open-Meteo payload had less radiation coverage than
  the stored one, the coordinator kept the old payload but reset its age — a
  sustained partial degradation would serve arbitrarily old weather at status
  "fresh"/age ~0 forever, and the cached/physics_fallback/unavailable ladder
  could never trigger. Fetch scheduling and payload age now use separate
  anchors; the served payload ages honestly through the ladder.
- **Release workflow can no longer ship the wrong version.** The post-publish
  version-bump job (whose commit never landed in the released tag that HACS
  installs) is replaced by a guard that fails the release when the tag does not
  match the tagged commit's manifest/pyproject/const version strings. Also
  removes the unpinned third-party push action.

## [0.5.0] - 2026-07-09

### Added
- **Shade-profile diagram — the currently-known shading for any date & module.**
  For a selectable module and a selectable local date the integration exposes
  the sun path (elevation over azimuth) with the *effective* beam transmittance
  τ the forecast actually applies at each sun position — the static config
  horizon blended with the learned shademap — plus a static and a learned shade
  horizon line. Three device-owned entities drive it: a `select`
  (`shade_profile_module`, defaults to a front-facing plane), a `date`
  (`shade_profile_date`, always defaults to today), and a `sensor`
  (`shade_profile`; state = shaded fraction of daylight, curve arrays as
  recorder-excluded attributes). The full diagram renders via an optional HACS
  `apexcharts-card` (`dashboards/shade_profile_apexcharts.yaml`); the built-in
  dashboard gains module/date controls + the shaded-fraction headline with no
  custom card. Pure, HA-free maths in `core/shadeprofile.py` (SPEC §15). The
  learned blend is shown ONLY when the slow learner is active (kill switch on,
  not drift-disabled, not collapse-frozen), matching the served forecast.
- **Reproducible developer environment + CI.** `make install` (or
  `scripts/setup-env.sh` / `scripts/setup-env.ps1`, both wrapping the pure-stdlib
  `scripts/setup_env.py`) creates a local `.venv` and installs the dev tooling
  from the new `[dependency-groups] dev` in `pyproject.toml` (Home Assistant,
  pytest, pytest-homeassistant-custom-component, ruff) — the same setup as
  battery-manager-ha. GitHub Actions (`validate.yml`) run HACS + hassfest
  validation, ruff, a manifest/pyproject/const version-consistency check, and
  the full pytest suite on Linux (the HA test layer cannot load on Windows).

### Changed
- **`energy_production_today` is now a stable day-ahead expectation.** The
  transient intraday clear-sky-index scalar is divided back out of the headline
  daily-kWh value (it stays in the served 15-min `watts` / `wh_period` curve), so
  the number no longer balloons in the morning and settles by afternoon while the
  underlying forecast is unchanged. On the current day
  `energy_production_today != sum(today's wh_period)` by design; tomorrow / d2 are
  unaffected.
- Repo-wide `ruff` cleanup (import ordering, `datetime.UTC`, `raise ... from`,
  explicit `zip(strict=...)`, dead-code removal); `ruff check` is clean across
  `custom_components`, `tests` and `scripts`.

## [0.4.0]

### Added
- Skill scoreboard (kill-gate: engine vs. baselines vs. measured, stratified,
  leak-free "as issued"), P10/P50/P90 quantile bands, and a built-in-card
  observability dashboard (SPEC §9/§10).

## [0.3.0]

### Added
- Slow shademap learner (per-channel beam transmittance by sun position) fully
  wired into the engine, with drift monitor, collapse detector and rollback ring.

## [0.2.0]

### Added
- Fast intraday / day-ahead-bias learner.

## [0.1.0]

### Added
- Initial pure-physics multi-plane forecast engine (raw-irradiance transposition,
  per-plane horizon, degradation ladder) — live deployed in a 14-day parallel run.
