# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
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

### Added
- Tests for the previously uncovered SPEC §7 degradation ladder (status rungs,
  fetch failure/success/coverage-refusal, end-to-end cached/unavailable paths,
  learner-hook composition) and for the initial config-flow submit path
  (including the lat/lon-into-site merge that prevents forecasting for the
  wrong location), plus the channel-dropout gates.

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
