# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **SPEC section numbers in entries below 0.23.2 refer to the OLD SPEC
> structure.** `docs/SPEC.md` was rewritten as a current-state-only
> specification and renumbered once; the old→new mapping is the transition
> table in [docs/HISTORIE.md](docs/HISTORIE.md) §H13. Historical entries are
> deliberately left untouched.

## [0.25.0] - 2026-07-31

The Phase-1 acceptance gate has served its purpose — the engine beat the
best baseline by more than the 10 % margin over a full window for good —
and with the external comparison forecasts decommissioned there is nothing
left to compare against. This release removes the kill-gate and the whole
external-comparison machinery. The engine scoreboard (daily-kWh MAE,
hourly MAE, weather-strata breakdown) keeps running unchanged.

### Removed

- **`binary_sensor.kill_gate_passed`** and the gate logic behind it
  (margin, full-window, paired-days and staleness rules in
  `core/scoreboard.py`), plus the dashboard verdict card. The verdict's
  job — prove the engine beats the old baseline before consumers switch —
  is done. SPEC section 15.4 is gone with it.
- **External comparison forecasts end to end.** The `comparison_sensors`
  options-flow list and its selector, `ComparisonConfig`, the
  per-comparison `…_comparison_daily_kwh_mae_<slug>` sensors (including
  the registry ghost pruning), the `vs_best_baseline_pct` sensor and its
  dashboard gauge, and the nightly recorder read of comparison entities
  at the matched day-ahead horizon. SPEC section 15.3 is gone too.
- **Dashboard cards** for the verdict, the vs-best-baseline gauge and the
  comparison-sensors reminder, from both the shipped YAML and the
  generated observability dashboard (re-run the `install_dashboard`
  action after upgrading to refresh a managed dashboard).

### Changed

- **Options flow.** A leftover `comparison_sensors` key in an existing
  config entry's options is dropped on the next save, so upgraded
  installs clean themselves up.
- **Storage tolerance.** Stores carrying a legacy `comparison_ring` or
  comparison fields in scored days still load fine — the data is simply
  no longer written or read (SPEC §16.1 migration invariant untouched).
- **Scoreboard summary** (diagnostics) now reports the engine-only key
  set: MAE figures, window/scored days, newest scored date and the strata
  breakdown — no comparison or gate fields.

## [0.24.0] - 2026-07-31

A full review pass over the integration: four behaviour tranches (core
learner data bugs, HA-layer fixes, service/CI hardening, a 95 % coverage
gate) plus one documentation tranche, and a follow-up fix extending the
metered-plane subset to live quantile training.

### Fixed

- **Partially metered sites no longer train against the full model.** Every
  modeled comparison side — the shademap clear-gate, the day-ahead bias
  training curve, the quantile seeding — is now summed over only the metered
  planes (those with `actual_entity`), in the nightly path and in the
  bootstrap core alike. An unmetered module in the modeled sum read as a
  permanent production outage: the clear-gate discarded every clear day and θ
  learned the metering fraction instead of the forecast error. A site with no
  measurement channel at all does not learn (SPEC §9.1/§9.5/§11.1).
- **Live quantile training uses the metered-plane subset too.** The nightly
  `relerr` ring compared the issued corrected site curve over ALL planes
  against the measured (metered-only) sum, so on partially metered sites
  every hourly relative error — and with it P50 — sank by the metering
  fraction. The corrected side is now restricted to the planes with
  `actual_entity`; a partially metered legacy snapshot without the per-plane
  breakdown skips the day, same as the day-ahead bias (SPEC §11.1/§9.1).
- **"Unknown" is no longer "fog".** A missing visibility is now `None`
  (unknown), not `0.0`: the fog rule fires only on a *measured* visibility
  below `FOG_VISIBILITY_M` — provider gaps used to classify as fog and poison
  the fog cell. Same for temperature: a gap makes the slot unusable instead
  of fabricating 0 °C (SPEC §8).
- **`score_day` no longer fabricates scored days.** A non-finite or negative
  engine or measured value makes the day unscored (no ring entry, no
  persistence) — the old 0.0 clamp injected the worst possible engine day
  into the kill-gate window (SPEC §15.2).
- **Bootstrap RLS guards.** Non-finite or negative samples are discarded in
  the bootstrap's RLS step (mirroring the live trainer) instead of clamping θ
  to 0.5; and day-section aggregates now gate on
  `RLS_MIN_DAY_SECTION_MODELED_WH` (25 Wh) — the 15-min slot threshold (5 Wh)
  was meaningless for aggregates, so a dark winter section carried no bias
  information yet trained anyway.
- **Section version guards made true.** `BiasState` / `ShademapState` /
  `ScoreboardState` `from_dict` now discard an unknown or future `version`
  with a warning and a neutral state — the behaviour SPEC §16.1 and their
  docstrings always claimed.
- **`ensemble_band_factors` is total.** A non-iterable `members` value skips
  the hour instead of raising `TypeError`.
- **Reconfigure reloads exactly once.** `async_step_reconfigure` now uses
  `async_update_entry` + abort and lets the update listener drive the single
  reload — the previous `async_update_reload_and_abort` path reloaded twice
  and runs into HA's 2026.12 deprecation.
- **Bundled cards find their sensors on any UI language.** The power-history
  and shade-profile cards auto-discover via the entity registry's stable
  `unique_id` suffix (`{entry_id}_{key}`) instead of matching localized
  `entity_id` slugs (regex kept as fallback), so a German install no longer
  breaks the discovery.
- **AC/DC labelling.** The power-history card's dashed line is labelled
  "Prognose (live)" / "Prognose (Stand 01:30)" (with EN counterparts), and
  the generated dashboard's measured-power title no longer claims a DC curve
  were AC (SPEC §18.4).
- **Entry removal cleans up after itself.** Removing a config entry deletes
  its store file *and* every repair issue it raised (issues are entry-scoped
  via the `_{entry_id}` suffix) — a reinstalled entry no longer inherits
  stale, unactionable warnings.
- **Failed fetches back off.** After a failed fetch the coordinator retries
  at the earliest after `min(fetch_interval_seconds,
  FAILED_FETCH_MIN_INTERVAL_SECONDS)` (15 min) instead of every recompute
  tick; the served payload's age anchor keeps aging untouched (SPEC §3).
- **Engine passes run off the event loop.** `compute_forecast` (recompute
  tick and nightly snapshots) runs via `hass.async_add_executor_job`.

### Changed

- **`bifacial_beam_gain` is capped at 1.3** (was 1.6), enforced by the load
  clamp in `SiteConfig.from_dict` and the form limit: real bifacial gain is
  typically 5–25 % and the reference site validated 1.23–1.25 — beyond ~1.3
  the factor is no longer a physics correction but the very overstatement it
  replaces (SPEC §4.5).
- **Kill-gate requires 3 paired days.** `SCOREBOARD_MIN_PAIRED_DAYS` is now 3
  (was 1): a single lucky paired day no longer flips the verdict — a baseline
  with fewer paired days is not eligible and the gate returns `None` (no
  statement). The 10 % margin's reference is now spelled out: relative MAE
  reduction vs. the best eligible baseline (SPEC §15.4).
- **`DEFAULT_SITE` moved to generic central Germany** (51.1 N / 10.4 E, near
  the geographic centre) — deliberately *not* a real operator site, so a
  copied default config no longer borrows the reference plant's (Landshut)
  geometry (SPEC §7.8).
- **Config fingerprint includes latitude/longitude** (rounded to 4
  decimals): a location change via reconfigure re-seeds the day-ahead bias
  cells instead of silently training against the old geometry (SPEC §7.7).
- **Duplicate entry bookkeeping removed.** `entry.runtime_data` is gone; all
  readers use `hass.data`, which unload already cleans up.
- **HA floor raised to 2026.3.** `hacs.json` pins `"homeassistant":
  "2026.3.0"`, and a new `tests-ha-min` CI job runs the full suite against
  exactly that floor (`uv pip install "homeassistant==2026.3.*"` over the
  lockfile's HA, `uv run --no-sync pytest`) — a floor that is not tested is
  not a floor.
- **Test coverage raised to ≥ 95 % and gated.** The suite grew from ~92 % to
  over 95 % of the integration's statements — new tests cover config-entry
  diagnostics end-to-end (incl. coordinate redaction), real coordinator
  constructor/setup paths, the `__init__` lifecycle (flush on HA stop,
  unload, update listener), `core/openmeteo_backfill.py` fetch/parse with a
  mocked aiohttp session, service error paths, and behaviour assertions for
  previously assertion-less smoke tests. The `tests` job now enforces
  `--cov-fail-under=95`.

### Added

- **Plausibility gate for mis-scaled measurement channels.** A full hour
  above `CHANNEL_PLAUSIBILITY_MAX_WP_FRAC` (1.25) × the channel's configured
  Wp is physically impossible (cloud-edge enhancement is sub-hourly) and
  proves a mis-scaled *measurement* — the classic being kW instead of W. The
  day is discarded for learning *and* scoring (`implausible_channel`), in the
  nightly actuals path and the bootstrap core alike, with a WARNING naming
  plane and entity (SPEC §9.8/§10).
- **New repair issue `eta_out_of_band`.** When the nightly median of the raw
  AC/DC ratios stays outside [0.90, 0.99] for
  `INVERTER_CAL_OUT_OF_BAND_STREAK_DAYS` days in a row, a persistent issue
  names the mis-scaled measurement (streak, median, band, last day); the
  first in-band day clears it. Same counting contract as the discard streak
  (issued-day guard, day-idempotent, persisted in `learning_health`), purely
  observational (SPEC §10).

### Security

- **`run_bootstrap` span cap.** An explicit range wider than
  `BOOTSTRAP_MAX_RANGE_DAYS` (1826 days = 5 calendar years incl. a leap day)
  is rejected with `ServiceValidationError`, and a future `end_date` is
  clamped to yesterday — a single action call has no timeout, and an explicit
  multi-year range does not cheaply self-correct like the capped default
  range (SPEC §12.2, `services.yaml` documents both).
- **Site quantity limits.** `SITE_MAX_PLANES` (8), `SITE_MAX_SHADE_GROUPS`
  (8) and `SITE_MAX_HORIZON_POINTS` (64) are enforced by `validate_site`,
  bounding what a site object pasted as free JSON via the object selector can
  allocate per recompute tick (SPEC §7.2–§7.4).
- **CI hardened.** All workflow actions are pinned by commit SHA (version
  comment kept; Dependabot keeps updating them), top-level permissions are
  `contents: read` with job-local elevation for the release job, and
  `actions/setup-node` (Node 22) guarantees the JS card harness runs instead
  of silently skipping.
- **Token hygiene in the dev scripts.** `scripts/backfill.py` and
  `scripts/validation/` default the HA token from the
  `HA_LONG_LIVED_TOKEN` environment variable (`--token` remains an override);
  the docs warn against `http://` (plaintext) and process-list exposure.

### Docs

- **SPEC accuracy corrections (code is the reality):** the issued ring never
  stores `ghi`; the week view's running-today bar is summed from *hourly*
  statistics (not day-mean × 24 h); the write budget is ≤ 4 bundled
  writes/day (6 h gate); the degradation ladder's age limits are named as
  constants (`MAX_PAYLOAD_AGE_HOURS` 24 h, `MAX_PHYSICS_FALLBACK_AGE_HOURS`
  72 h) rather than "configurable"; the scoreboard window is documented as a
  constant, not an option; the 15-min curve attributes are explicitly DC
  against the AC sensor state; the bootstrap "attempt, not a blocker" rule
  names its subject (the single run) and its fallback chain; the shademap
  1.1 clamp is justified (reflection gains); the P10 day-aggregate formula is
  spelled out; and the "recompute under 50 ms" claim became a qualitative
  no-event-loop-impact requirement.
- **New SPEC requirements** for previously undocumented behaviour:
  deinstallation cleanup (store file and entry-scoped repair issues are
  deleted; the globally registered Lovelace resources stay — documented
  known limitation), entry migration (no `async_migrate_entry` is needed
  today), DST/leap-year as a documented approximation, several entries
  sharing one `actual_entity`, an outage beyond `NIGHTLY_CATCHUP_MAX_DAYS`
  staying permanently unlearned, and the Oct–Feb fog-month boundary as a
  deliberate trade-off.
- **project-knowledge and ADR fixes:** `--site` is required (no
  `DEFAULT_SITE` fallback) in 01, the snapshot ring is deliberately *larger*
  than the drift streak in 05, the open items O8/O9 are marked overdue with
  outcome open in 06 (no invented results), and ADR-0022 now points at the
  reconfigure flow and the current SPEC section numbers.
- **README brought up to 0.23.3** (status line, `bifacial_beam_gain`,
  `tau_points`/`diffuse_tau`, the `run_bootstrap` action, the
  learning-visibility repair issues), and **AGENTS.md** documents the 95 %
  coverage gate, the HA 2026.3 floor, Node in CI and the SHA-pinned actions.

## [0.23.3] - 2026-07-30

A tooling release: no runtime behaviour changes.

### Changed

- **Dev environment rebuilt on uv (dev-setup-2026), no runtime behaviour
  changes.** The dev toolchain now comes from a committed `uv.lock` (single
  source of truth, also used by CI) via `uv sync --group dev`; the Makefile is
  a thin wrapper around uv, and `scripts/setup-env.{sh,ps1}` /
  `scripts/setup_env.py` only install uv when it is missing. Python moves to
  **3.14** (`.python-version`, `requires-python >= 3.14.2`) with the dev floor
  `homeassistant>=2026.7.4`; `pytest-homeassistant-custom-component` stays the
  only fully pinned package because it carries the HA coupling. Added: a
  mypy baseline over `core/` (eight legacy modules with known errors are
  named and excluded in `[tool.mypy]`, the rest must stay clean), report-only
  coverage config, a devcontainer (Python 3.14 image + Node feature for the
  JS card harness), a pre-commit config with `ruff-check --fix` only
  (`ruff format` remains forbidden), `.editorconfig`, and `.gitattributes`
  (LF enforcement, brand PNGs binary). CI moves to uv (`setup-uv` with cache),
  `actions/checkout@v7`, pinned HACS/hassfest actions with a private-repo
  visibility guard, and a new devcontainer CI job that runs the full suite
  inside the container. Test invocation is unchanged everywhere:
  `pytest tests -p no:homeassistant`.

## [0.23.2] - 2026-07-25

A documentation release: no runtime behaviour changes. `docs/SPEC.md` stopped
being the project's founding document and became what the operator asked for —
a specification of the **requirements of the shipped version**, nothing else,
carrying the version it was last reviewed against. Everything it shed is kept
verbatim in the new, explicitly non-normative `docs/HISTORIE.md`, including the
old→new section table that keeps every pre-rewrite `SPEC §…` reference in this
changelog, in `docs/orders/` and in old PRs resolvable.

Two findings from the 0.23.1 review are fixed along the way.

### Changed

- **`docs/SPEC.md` is now a current-state specification.** On the operator's
  instruction the contract describes only the requirements of the shipped
  version — no founding context, no delivery plan, no decision log, no
  measurement-analysis snapshots, no "since v0.x" provenance. The document was
  rewritten thematically (§1 contract and change rules · §2–§8 system, weather,
  physics, horizon, electrics, configuration, weather classes · §9–§12 learning,
  learning visibility, uncertainty, bootstrap · §13–§16 degradation, consumer
  interfaces, metrics/kill-gate, persistence · §17–§21 shade diagram, dashboard,
  actions, conventions, QA) and carries a **version stamp** in its header
  ("Gilt für Version: 0.23.2") that a new guard checks against
  `const.INTEGRATION_VERSION`. This release is the stamp's first live exercise:
  bumping the version to 0.23.2 turned the suite red until the SPEC header was
  pulled along, which is exactly the drift it exists to stop.
- **New `docs/HISTORIE.md`** (explicitly NOT normative) holds everything the SPEC
  shed — founding context, the 2026-07-05 findings, the strategy decision, the
  phase plan, D-P1…D-P11, B1…B12, the LTS measurement analysis, the dated
  snapshots and the superseded structural rules — plus, in §H13, the **old→new
  section transition table**. Entries below in this changelog cite the OLD
  numbering; §H13 translates them.
- **All 626 `SPEC §…` citation runs were remapped in one pass** across
  `custom_components/`, `tests/`, `scripts/`, `dashboards/`, `docs/`, `CLAUDE.md`,
  `CONTRIBUTING.md` and the PR template. Citations whose old target was
  historical were moved to the section that owns the behaviour today (e.g. the
  scoreboard gate criteria from the phase plan now cite §15.1/§15.4, the horizon
  field semantics from the measurement chapter now cite §5.1). `CHANGELOG.md`
  and `docs/orders/` were deliberately left untouched and carry a pointer to
  §H13 instead.
- **`tests/test_spec_integrity.py` gained two guards and a wider scan.** (h) the
  header version stamp must equal `INTEGRATION_VERSION`; (i) the SPEC must carry
  no historical flags or provenance chapters, and `docs/HISTORIE.md` must keep
  its transition table. The citation scan now also covers `scripts/`,
  `dashboards/` and `docs/` (excluding `docs/orders/`), so the citations nobody
  used to test can no longer rot.
- **SPEC §10 now describes the card precedence in BOTH directions.** "One card
  per root cause" was only written down for the order presence-gap → streak. The
  shipped code also resolves the reverse — a `learning_stalled_*` card that is
  already standing is removed on the next counted discard night once a channel
  goes missing (mistyped entity id, renamed or deleted inverter entity, reload),
  leaving only the more specific presence card. That is intended, the streak
  itself is not reset, and the specification says so now instead of leaving a
  reader to read it as a regression.

### Fixed

- **The fresh-install guard test did not discriminate.**
  `test_fresh_install_guard_reads_the_real_issued_ring` asserted only that the
  streak stays at zero when nothing is in the issued ring — which stayed green
  under the very mutation it was written for: renaming
  `ForecastStore.get_issued` makes the `AttributeError` land in
  `record_actuals_outcome`'s outer handler, and a streak that never advances is
  also a streak of zero. It gained a **positive control** on the same real
  store: exactly one day is recorded via `record_issued`, and exactly that day
  must count (streak 1, `last_discard_day` equal to it) while the days on either
  side of it do not. The test now fails under the rename, under a guard stuck on
  `False`, and under a guard stuck on `True`.

## [0.23.1] - 2026-07-25

A trap in the offline backfill CLI: `--site` was optional, and omitting it
silently reconstructed the whole bootstrap against `const.DEFAULT_SITE` — the
shipped **reference** site, not yours. That flag is now required, and the
reference site is labelled honestly for what it is. The `run_bootstrap` action
was never affected (it always uses the live config).

The same reference site turned out to hide a second, quieter failure: it wires
**eight** of the reference plant's inverter entity ids, and the nightly label
gates discard the *whole* day for *both* learners as soon as one configured
channel is unusable. An install that adopted that default therefore threw away
every single night — forever — while the status entities kept reporting
`cold_start` and the learners kept reporting "active". That is now visible.

### Fixed

- **"This install is not learning" is now a repair issue, not a log line.** Two
  independent detectors (SPEC §5.1). At setup and after every config change,
  every configured plane `actual_entity` is checked for existence in this Home
  Assistant; a missing one raises `actual_entity_missing`, which **names the
  plane and the entity id** and points at Reconfigure. And when the nightly
  training discards the whole day `LEARNING_STALLED_STREAK_DAYS` times in a row,
  one of three reason-specific issues fires —
  `learning_stalled_dead_channel` / `_frozen_channel` / `_low_coverage` —
  because the three gates need three different remedies (fix the entity id,
  restart the stuck DTU, close the recorder gap). Both clear themselves: the
  first when every channel resolves again, the second on the first accepted day.
- **`scripts/backfill.py` no longer falls back to the reference site silently.**
  Without `--site` the run now aborts **before the first network call** (exit
  code 2) with a message naming the three real options: the in-process
  `balcony_solar_forecast.run_bootstrap` action (no token, always the live
  config — the recommended path), how to export your live site object to
  `site.json`, and the new opt-in flag. Reconstructing against foreign geometry
  is not detectable after the fact: `site_signature` only guards the *import*,
  and only on lat/lon + plane names, so a wrong-site bootstrap trains every
  learner on a stranger's plant while looking healthy.

### Changed

- **No false alarm during a new install's run-in phase.** A brand-new plant
  legitimately has no complete long-term-statistics days yet, and the nightly
  catch-up window reaches back before the installation existed. A discarded day
  therefore only counts toward the streak when the integration actually *issued*
  a forecast for that day — proof that we were running and the channels should
  have logged. The streak is also idempotent per day, since a discarded day is
  never recorded and is re-read every night. Both sides are covered by tests:
  the fresh install stays silent, the permanently dead channel does not.
- **The discard streak survives restarts** in a new `learning_health` store
  section (streak, last cause, channels responsible, last accepted day) — added
  **additively inside schema v3, no version bump**, following the same pattern
  as `inverter_cal_state` and `config_fingerprint`: a store written before
  0.23.1 reads back neutral and every other section stays byte-faithful.
- **One repair card per root cause.** A copied reference site would otherwise
  collect two cards for one fix: `actual_entity_missing` at setup, and a week
  later `learning_stalled_dead_channel` on top of it. While the missing-channel
  card stands, the streak keeps counting and stays visible in the diagnostics
  dump but raises no card of its own; the suppression lifts as soon as the
  channels resolve, so a stall with a genuinely different cause still surfaces.
- **The diagnostics dump answers "why is nothing being learned?"** through the
  accessors it already used, not a third code path:
  `store.learning_health` (cause, modules, streak, threshold, last accepted day)
  and `learners.state.actual_channels` (configured / missing channels, AC-meter
  status). Zero learned cells reads very differently once you can see that the
  input channels do not exist.
- **The optional AC meter stays diagnostics-only, deliberately.**
  `ac_actual_entity` is self-gating and never blocks learning, so a missing one
  is reported in the dump and logged once rather than raising a second repair
  card that would dilute the one that actually requires action.
- **New `--use-default-site` opt-in** makes the old behaviour explicit for demo,
  test and CI runs, and logs a loud WARNING that the reference site is not the
  operator's plant. `--site` wins if both are given.
- **`const.DEFAULT_SITE` is labelled honestly.** A comment block at the
  definition states that it is a structure/format example and *not* a maintained
  image of the operator's plant, and names the known deviations: the seasonal
  screen az 135–175 sits on M4/M8 there although the shademap evaluation showed
  it actually shades M2/M3; the wall edge is az 212 instead of the live az 195;
  and there are no `albedo` / `bifacial_beam_gain` / `tau_points` /
  `diffuse_tau` keys (so albedo 0.2 and beam gain 1.0 apply). **No geometry was
  changed** — the numbers stay the test anchor; the substantive rework of the
  shipped default belongs to the onboarding ADR
  (`docs/adr/ADR-0023-onboarding-standortkonfiguration.md`).

### Docs

- **New SPEC §5.1** documents the label gates' *visibility*: both detectors, the
  two repair-issue families, the run-in-phase rule as a binding requirement, the
  diagnostics fields, and why the channel check does **not** hang off the config
  fingerprint (that reconcile runs before the first refresh — at a cold boot,
  usually before the inverter integration has registered its entities — and
  `actual_entity` is deliberately not a fingerprint field). §8, §14.4 and the §0
  signpost carry the matching entries.
- **Three corrections found in the 0.23.1 SPEC review.** §5 described the
  nightly catch-up as "`NIGHTLY_CATCHUP_MAX_DAYS` back from yesterday"; it is
  really a window *ending yesterday* that starts the day after the newest
  recorded actuals day, capped at that constant — which is also why a fresh
  install's sweep necessarily reaches into pre-install history. §8 named the
  diagnostics key `snapshot_ring`/`_capacity`; it is `snapshot_ring_capacity`.
  And `async_setup`'s docstring promised "All six services" while
  `services.yaml` had grown to ten.
- **`tests/test_spec_integrity.py` gained three guards** so those three cannot
  recur: every `ISSUE_*` id in `const.py` must be named in the SPEC, must carry
  an `issues` translation in **both** shipped languages (title *and*
  description — an untranslated repair card renders as a raw slug), and
  `async_setup`'s docstring must name every service in `services.yaml` and state
  their count.
- **SPEC §6** now fixes the site semantics of a bootstrap run (action = standard
  path, `--site` mandatory, `--use-default-site` opt-in); §15.6 cross-references
  it. `docs/BACKFILL.md` follows with a rewritten "Your site (`--site`,
  required)" section, an updated flag table and a troubleshooting row.
- **SPEC currency pass (0.23.x).** SPEC §0 gained an as-built signpost that
  routes each topic to its authoritative section, and the corrections around
  action resolution, bias fallback and map provenance landed. New
  `tests/test_spec_integrity.py` guards the contract mechanically: every
  `SPEC §x.y` citation in the tree must resolve to a real heading (section
  numbers stay immutable), every action and every public site-config field must
  be named in the SPEC, and every top-level section must be reachable from the
  §0 signpost. `CLAUDE.md`, `CONTRIBUTING.md`, the PR template and the CI
  workflow carry the matching reminder.
- **ADR-0023 (onboarding / site configuration) is now in the repository**
  (`docs/adr/ADR-0023-onboarding-standortkonfiguration.md`, status *Proposed*):
  the analysis and staged plan behind the honest `DEFAULT_SITE` label — why the
  shipped reference site blocks a general release (foreign geometry *and* eight
  hardcoded entity ids that starve every learner), and the MVP/v1/expansion cut.
  `const.py`, SPEC §6 and this file point at it, so the guard below applies.
- **`tests/test_spec_integrity.py` gained a fifth guard:** every repo-relative
  `docs/…` path named in a tracked markdown file, in `custom_components/` or in
  `tests/` must resolve to a **tracked** file — a document that exists only on
  the author's disk is a dangling link in every fresh clone.

## [0.23.0] - 2026-07-25

The 320-day re-bootstrap is now a Home Assistant action —
**`balcony_solar_forecast.run_bootstrap`** in Developer Tools → Actions — so the
learner history can be rebuilt entirely in-process. No Long-Lived Token, no
`scripts/backfill.py`, no `site.json`: it uses this install's **live config**,
fetches Open-Meteo Previous-Runs weather through the integration's own aiohttp
session, and reads the recorder's long-term statistics directly. The offline CLI
still works unchanged; both paths now share the same HA-free reconstruction core.

### Added

- **Action `run_bootstrap` (`SupportsResponse.ONLY`).** Rebuilds the day-ahead
  bias, shademap and quantile learner states from the measured history in-process.
  Optional `entry_id` (omit for a single site), `start_date` / `end_date`
  (ISO `YYYY-MM-DD`; default ~400 days ago → yesterday, days without measured
  history are skipped), and `dry_run`. **`dry_run` defaults to `true`**, so the
  first call only fetches, reconstructs and returns a summary WITHOUT touching the
  learners; call again with `dry_run: false` to import (a rollback snapshot is
  taken, exactly like `import_bootstrap`). The reconstruction runs in the executor
  with progress logs and takes a few minutes; a second concurrent call — or one
  overlapping the nightly job — is rejected via a per-coordinator bootstrap lock.
  The in-process actuals read follows the epoch-**seconds** recorder-statistics
  convention (`_actuals._stat_row_hour_key`), not the WS-API milliseconds.
- **HA-free `core/bootstrap_build.py` and `core/openmeteo_backfill.py`.** The pure
  reconstruction/bootstrap math and the Open-Meteo Previous-Runs fetch were lifted
  out of `scripts/backfill.py` into token-free, HA-independent core modules shared
  by both the CLI and the new action. `scripts/backfill.py` stays a thin CLI
  wrapper (re-exports the core names) with byte-identical output.

### Changed

- **0.22 config campaign is now one click.** For the pending 0.22 config work the
  flow is simply: edit the config, then run `run_bootstrap` with `dry_run: false`.
  No external script, token or `site.json` round-trip.

## [0.22.0] - 2026-07-25

Elevation-dependent horizon τ + a diffuse-radiance override for blocked sectors
(ADR-0022 "Elevationsabhängiges Horizont-tau + Diffus-Floor/Wand-SVF", accepted).
Both reshape the RAW physics curve, so they ship as one release with a single
learning reset. Existing configs (no new fields) are **byte-identical** and are
**not** re-seeded on upgrade.

### Added

- **Inline elevation profile `tau_points` per horizon row (Thema 1 / H-A).** An
  optional `tau_points: [[el, τ], …]` makes the beam transmittance a piecewise-
  linear function of the **sun elevation** below the row's `elevation_deg` edge,
  so a semi-transparent tree crown is modeled at the physical quantity (sun
  elevation) instead of a τ(az) sun-path projection anchored to one day. It drives
  both the beam gate and the diffuse SVF (a band integral over the profile).
  `tau_points_bare` (same el raster) optionally supplies the bare-winter profile
  for a seasonal row. Validation: 1–12 pairs, `el` strictly ascending and within
  `[0, elevation_deg]`, `τ ∈ [0, 1]`. Serialised only-when-set.
- **Per-row diffuse override `diffuse_tau` (Thema 2 / D2).** An optional
  `diffuse_tau` (0…0.8) is the effective diffuse radiance of the blocked sector
  relative to the open sky (a bright plaster wall ≈ 0.5). It lifts the isotropic
  diffuse floor in the SVF **only** — the beam path stays byte-untouched — so a
  wall row can raise the M4/M8 morning/afternoon diffuse floor without
  fabricating phantom beam. It is **not** a transmission. Serialised only-when-set.

### Changed

- **Config fingerprint (A4) now hashes `tau_points`, `tau_points_bare` and
  `diffuse_tau`** (only-when-set, so a legacy config's fingerprint is unchanged).
  Editing any of them re-opens (n-caps) the day-ahead bias cells so learning
  re-accelerates against the shifted raw curve — no manual `reset_day_ahead_bias`
  needed after a `tau_points` migration or a `diffuse_tau` campaign.
- **Backfill parity.** `scripts/backfill.py::reconstruct_plane_hour` resolves the
  horizon beam gate at the true sun elevation (mirroring the engine), so a
  `tau_points` / `diffuse_tau` / `bifacial_beam_gain` setup reconstructs
  byte-identically to the live engine.
- **Shade-profile diagram** now resolves the static prior per elevation, so the
  sun-path transmittance is correct per (azimuth, elevation) sample instead of
  constant down each azimuth column.

### Deprecated

- **The interim az-ramp** (τ(az) sun-path projection anchored to one day) is
  superseded by `tau_points`. Migrate it once; do **not** re-anchor it monthly
  (SPEC §13, ADR §2.7.6). After migrating, run `reset_day_ahead_bias` (or rely on
  the automatic fingerprint n-cap) and re-run the offline bootstrap
  (docs/BACKFILL.md).

### Tests

- Bit-identity property tests for legacy rows (`transmittance_at` /
  `sky_view_factor` unchanged when neither `tau_points` nor `diffuse_tau` is set),
  `tau_points` golden values (below/between/on-knot/above-knot/above-edge, az
  interpolation between profile and scalar rows, wrap segment), SVF band-integral
  vs. brute-force quadrature, and the seasonal **regression** tests: the
  synthetic late-August dawn run yields ~0 beam with `tau_points` where the interim
  az-ramp fabricated a phantom beam — the design's core proof — plus foliage-blend
  per knot across the year boundary.

### Migration (operator)

- **One config campaign, one learning reset.** In the options flow (ObjectSelector)
  replace the interim az-ramp rows with `tau_points` in all planes and add
  `diffuse_tau` on the wall rows (M4/M8 az195–360, M1/M5 az295–360). Then run the
  `reset_day_ahead_bias` service (or rely on the automatic config-fingerprint
  n-cap) **and** re-run the offline LTS bootstrap so shademap bins that were
  learned against the old τ=0 / missing-diffuse prior are rebuilt.
- **Transition week expected.** The served 04–06Z curve overshoots for ~3–7 days
  while the day-ahead-bias cells re-learn against the shifted raw curve — this is
  the clamp cell settling, **not** a regression; do not roll back.
- **Documented open gap.** After D2, clear-morning M4/M8 stays ~×3 underestimated
  (~90–150 Wh/day site-wide, the beam-bound rear-pickup share). This is left to the
  bias learner and to a future `rear_beam_fraction` (ADR-0022 Option D3, deferred);
  it is intentionally **not** masked with inflated `diffuse_tau` values.

## [0.21.0] - 2026-07-25

7-day forensic pass (17.–24.07.2026, the first week with working nightly
learning) turned into a coherent fix tranche. The dominant defect was
**double-correction**: the day-ahead bias (θ), the intraday scalar and, on clear
mornings, the honest physics deficit all pulled on the same error, so the served
curve overshot by up to ×1.9 at 07–09 h local while the day-ahead headline
ballooned by the full scalar headroom. Each layer is now referenced against the
right baseline, seeded from the bootstrap, and observable.

### Added

- **Configurable bifacial beam gain (forensik T6/A1).** New optional "Beam gain"
  field in the setup/reconfigure flow (`site.bifacial_beam_gain`, blank = the
  shipped 1.0 = identity, no change for existing users). It multiplies **only**
  the direct (beam + circumsolar) share of the plane-of-array irradiance, applied
  in the engine after the IAM and before the ungated learner reference and the
  horizon gate, so it feeds the RAW and the corrected curve identically and lifts
  the honestly under-modeled direct beam on clear mornings (bifacial rear-side
  gain, steep east-facing geometry) into the raw physics instead of leaving the
  clamped learners (transmittance ≤ 1, day-ahead-bias cells) to absorb the
  deficit. Values are clamped to [1.0, 1.6]; the offline `scripts/backfill.py`
  bootstrap reconstructs the same physics. For the reference site ≈ 1.23 was
  validated (backtest 2026-07-16).

- **Quantile bands seeded from the offline bootstrap (forensik A6).**
  `scripts/backfill.py` now folds each daylight hour's `measured / θ-corrected`
  relative error into per-(cloud class × day part) quantile rings through the
  **live** `quantiles.train_quantiles` (identical taxonomy, clamps, date window
  and caps), and `build_bootstrap_json` emits a `quantile_state` section.
  `store.import_bootstrap` / `coordinator.async_import_bootstrap` ingest it
  **additively** — a payload without the key leaves the live ring untouched — and
  the rollback snapshot now carries the quantile state so all three learners roll
  back together. Without it only the overcast bins were trained on day 0 and every
  other band collapsed to P50 for weeks (delivers on SPEC §6's promise; the real
  fix behind the day-0 band collapse).

- **Intraday scalar ring re-armed after restart/reload (forensik A7/SCT-2).** The
  sample ring is purely in-memory, so every reload/restart left
  `compute_intraday_scalar` neutral for the whole trailing window (a ≥ 2 h
  correction blackout — costly at the observed release cadence). It is now
  reconstructed once at the first fresh tick from the recorder's 5-min site-total
  measured-DC statistics (seconds-epoch, mirroring `_actuals`) plus the last
  θ-corrected forecast curve, and degrades cleanly to neutral when stats/cache are
  missing. The modeled side is restricted to the **metered** planes (only those
  with an `actual_entity`, exactly the subset the site-total DC sensor sums) —
  otherwise a partially-metered site halved the reconstructed scalar to the clamp
  floor after every reload despite a perfect forecast. Only the scalar must never
  persist; the samples are re-derivable raw data (SPEC §5 clarified).

- **Consumer observability (forensik B3/B4/B5/SCT-4).** `get_issued_forecast`
  now returns `hourly_wh_ac` (DC × the DC→AC efficiency frozen into each snapshot
  at issue time; legacy snapshots fall back to the current learned η and flag
  `eta_source`), plus `cloud_class_by_hour` and `applied_factor_by_hour`; the DC
  curves are now documented as DC (the DC semantics had quietly flattered every
  issued ratio by ~8 %). The P10/P90 sensors and `get_forecast` gained a
  per-local-day `band_source_by_day` count (Recorder-excluded), and each
  `day_ahead_bias_status` cell reports `clamped: true` at the θ band edge.
  Config-entry **diagnostics** stop lying: `store_stats()` and
  `learner_state_summary()` are implemented (no more `available: false`), the
  quantile `trained` flag now gates on `n` **and** `effective_days` (and reports
  `days`), and the forecast block splits `daily_kwh_dc` vs `daily_kwh_ac`.

### Changed

- **Cloud classification keyed on the clear-sky index (forensik A5).**
  `classify_cloud` now derives clear/mixed/overcast from `kc = ghi /
  haurwitz(elevation)` whenever a slot has a usable GHI and the sun is above
  `CLOUD_KC_MIN_ELEVATION_DEG`, falling back to the old random-overlap layer cover
  at twilight or when GHI is missing (the fog rule is unchanged and still first).
  The layer cover counted mid/high cloud in full and routed sunny afternoon hours
  into the overcast cell, poisoning θ, the quantile bins and the scoreboard strata
  alike. `CLASSIFIER_VERSION` is folded into the day-ahead config fingerprint so
  the taxonomy change re-seeds the bias cells.

- **Day-ahead bias hygiene (forensik A4/B2).** A `config_fingerprint` (a hash over
  each plane's azimuth/tilt/Wp/efficiency/Ross/horizon — every horizon row's
  elevation AND its transmittance fields `tau`/`seasonal`/`tau_leafed`/`tau_bare`,
  since those rows are the τ-carrying screens —, the albedo, the bifacial beam gain
  (T6), the group AC limit and `CLASSIFIER_VERSION`) is persisted next to the bias
  state; on a change
  every cell is re-seeded (`bias.reseed_day_ahead_bias`) by re-opening its RLS
  covariance to `RLS_INIT_COVARIANCE` and capping `n` at
  `DAY_AHEAD_BIAS_RESEED_N`, so learning re-accelerates instead of crawling
  ~0.001/day from RLS steady state, with an INFO log and a repair issue. A first
  start with no stored fingerprint only records it. The day-ahead RLS now trains
  on `snap.slow_only_hourly_wh` (shademap ∘ physics, raw fallback) rather than pure
  raw, so it will not double-correct the shading error once the shademap learns.

- **Intraday scalar sampled against the θ-corrected curve (forensik A2/IRC-2).**
  The sampler modeled against pure raw while the served curve is raw × θ × scalar,
  so θ (1.36–1.49 in the morning) and the scalar double-corrected the same error.
  The modeled side is now scaled by the nightly-frozen θ (a new `_day_factor`
  cache); the intraday factor is never folded in (θ is frozen → no circularity).
  The genuine under-forecast day (21.07.) still yields a legitimate 1.49, so the
  weather signal survives — no hard scalar clamp.

### Fixed

- **Day-ahead headline no longer balloons under the intraday scalar (forensik
  A3/IRC-4).** The keep-ceiling headline path leaked: a re-clamped slot kept the
  served (scalar-inflated) ceiling, inflating the day-ahead-stable "today"
  headline by the full factor headroom (20.07.: +3.27 kWh at scalar 2.355). The
  clamped slot now contributes `min(prereclamp / factor, ceiling)`, i.e. the exact
  scalar-free served value capped at the physical ceiling — stable by design, with
  a synthetic 2.35-scalar unit test.

- **Daily P10 no longer rides a spike above the actual (forensik B1/FOR-7).** The
  daily P10 sensor scaled the whole band with the scalar, so a transient spike
  lifted P10 above the end-of-day actual on 3 of 6 days. The daily P10 now strips
  the transient factor asymmetrically (`min(1, scalar_free / served)` per slot);
  the daily P90 keeps it (an upward correction may widen the optimistic flank).

- **`get_forecast` band provenance coupled to the band (forensik SCT-4).** The
  response wrote `band_source` unconditionally, so a quantiles-off / cold-start
  cycle carried a `band_source` with no band block. `band_source` and
  `band_source_by_day` now ship only inside the `if bands:` block — no
  provenance without an accompanying band.

- **Scoreboard strata low-n guard (forensik C1).** `stratified_breakdown`
  suppresses `engine_vs_best_baseline_pct` (null) and sets `low_n: true` below
  `SCOREBOARD_STRATUM_MIN_N` scored days, killing absurd figures from a single
  mispaired day (e.g. −480 % at n = 2).

### Note — scoreboard catch-up window (forensik C3)

The kill-gate needs a **full** 14-day window of scored days. After the
`_actuals` epoch fix (0.19.2) only ~3 days of catch-up were recoverable; **06.–
12.07.2026 stay permanently unscorable** (no archived issued snapshots survive
for those days), so the rolling window first fills — and `kill_gate_passed`
first returns a verdict rather than `None` — around **27.07.2026**. An optional
one-off re-score service for the salvageable issued days was considered and
deferred (the issued ring holds the data). Until then `kill_gate_passed` stays
`None`, which is correct, not a failure.

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
