# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Dashboard UX fixes.** The observability dashboard (both the generated one and
  the shipped copy-paste YAML) is tidied: the forecast graph drops the pointless
  today-vs-tomorrow juxtaposition and is retitled *Forecast power (time-accurate)*
  so it pairs top-to-bottom with the measured per-module card; the measured
  DC-power graph now labels its rows by plane (M1…M8) instead of the inverter
  ports' ambiguous own names. The generated dashboard additionally drops its
  redundant *Shade profile (per date & module)* controls card (those controls are
  embedded in the bundled diagram card) and its shademap note now points at
  *your* site's obstructions generically instead of hardcoding the reference
  install's east-hill/wall/tree sectors.

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
