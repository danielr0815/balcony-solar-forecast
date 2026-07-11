# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
