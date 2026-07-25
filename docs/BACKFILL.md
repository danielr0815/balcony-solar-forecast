# Learner Bootstrap Backfill (SPEC §6)

The re-bootstrap warm-starts the three learner states (day-ahead bias, shademap,
quantile bands) from ~2 years of history so the system does not meet its first
live winter cold. There are **two ways to run it**, sharing one HA-free core so
they emit byte-identical bootstraps:

1. **`run_bootstrap` action (the standard way, v0.23+)** — in-process, from
   Home Assistant's Developer Tools → Actions. No token, no `site.json`, no dev
   machine: it uses this install's live config, fetches the weather itself and
   reads your recorder statistics directly. **Start here** — see
   [Re-bootstrap from Home Assistant](#re-bootstrap-from-home-assistant-run_bootstrap).

2. **`scripts/backfill.py` CLI (offline / CI alternative)** — the original
   one-shot dev-machine job that writes a `bootstrap.json` you then import with
   the `import_bootstrap` action. Use it when you want the artefact on disk
   (CI, review, archival) or to bootstrap an install you cannot reach the
   Developer Tools of. The rest of this document covers the CLI.

Both paths do the same thing. The CLI:

1. fetches Open-Meteo **Previous-Runs** day-1-lead forecasts-as-issued for the
   site (archived since 01/2024);
2. reconstructs per-plane **hourly** modeled beam/diffuse/ghi/kc curves by
   importing the repo's own `core/` physics package (identical to the live
   engine — no numpy);
3. pulls measured **hourly per-module** energy from your HA long-term
   statistics over the **WebSocket API**;
4. computes a **day-ahead RLS bias** bootstrap, a **shademap** bin bootstrap
   (with the backfilled sample count `n` **capped** so live data overrides it
   quickly), and a **quantile relative-error ring** bootstrap (per-hour
   `measured / θ-corrected` folded through the SAME `train_quantiles` the live
   nightly path uses); and
5. writes a `bootstrap.json` that the
   `balcony_solar_forecast.import_bootstrap` service ingests
   (validate + clamp, rejects unknown schema).

The backfill is **"mandatory to attempt, not a blocker"** (SPEC §6): the
integration runs fully without it. If the Previous-Runs radiation is
unavailable, the script degrades to the plain **Historical Forecast API** and
prints a loud warning that the data is *analysis, not as-issued forecast*
(still useful for the geometric shademap, weaker for the weather-error bias).

---

## Re-bootstrap from Home Assistant (`run_bootstrap`)

The **standard, no-token** path. In Home Assistant → Developer Tools → Actions,
call `balcony_solar_forecast.run_bootstrap`. It runs the SAME reconstruction as
the CLI but in-process: it fetches Open-Meteo Previous-Runs weather with the
integration's own HTTP session and reads your recorder's long-term statistics for
the planes' `actual_entity` ids directly — no long-lived token, no `site.json`
(it uses this install's live config).

**Safe by default:** `dry_run` is **ON**, so the first call only fetches,
reconstructs and returns a summary — it does **not** touch the learners. Review
the summary, then run again with `dry_run: false` to actually import (a rollback
snapshot is taken first, exactly like `import_bootstrap`).

```yaml
# 1) Dry run — see what it would build (nothing changes):
action: balcony_solar_forecast.run_bootstrap
data:
  dry_run: true          # the default; shown for clarity

# 2) Apply it — imports into the live learners:
action: balcony_solar_forecast.run_bootstrap
data:
  dry_run: false
```

| Field | Required | Meaning |
|---|---|---|
| `entry_id` | no | Target config entry. Omit if a single site is configured. |
| `start_date` | no | First day `YYYY-MM-DD`. Default: ~400 days ago (days without measured history are skipped). |
| `end_date` | no | Last day `YYYY-MM-DD`. Default: yesterday (local). |
| `dry_run` | no | **Default `true`.** `false` imports the rebuilt bootstrap. |

The response is always a summary: `days_used`, `days_skipped`, `date_range`,
`weather_source` (`as_issued` or `analysis_fallback`), `bias_cells`,
`shademap_channels`/`shademap_bins`/`shademap_samples`, `quantile_bins`/
`quantile_samples`, `imported` and `duration_s`; on a dry run it also carries a
`hint` to re-run with `dry_run: false`.

**Notes.** The run takes a few minutes (~2–5 min in the HA container for ~320
days) and logs progress every ~50 reconstructed days; the CPU work runs off the
event loop. It is serialised against the nightly training job by a per-site lock,
and a second concurrent `run_bootstrap` is rejected with a clear error. Because it
reads the recorder **in-process** (epoch-seconds statistics rows, not the
WebSocket's milliseconds), it uses the same hour-key normalisation as the nightly
actuals reader. A forecast-relevant config edit (see
[Re-bootstrap after a config campaign](#re-bootstrap-after-a-config-campaign-v022-and-later))
is the main reason to run it; the `BOOTSTRAP_MAX_BIN_N` cap keeps it low-risk and
the rollback snapshot lets you undo it.

---

## Prerequisites

- **Python 3.13+** on the dev machine (matching `pyproject`'s
  `requires-python`), with `aiohttp` installed:

  ```sh
  py -3.14 -m pip install aiohttp
  ```

  (Nothing else — the physics core is stdlib-only.)

- A **Home Assistant long-lived access token**: HA profile → bottom of the page
  → *Long-Lived Access Tokens* → *Create Token*. Copy it once.

- Your HA base URL reachable from the dev machine, e.g.
  `http://homeassistant.local:8123` (or the LAN IP).

- The integration installed on HA **and your site object exported to a JSON
  file** — `--site` is **required** since 0.23.1 (see
  [Your site (`--site`, required)](#your-site---site-required)). The CLI no
  longer falls back to the shipped reference site on its own.

---

## Run it

Full 2-year backfill (LTS exists since 2024-07):

```sh
py -3.14 scripts/backfill.py \
    --ha-url http://homeassistant.local:8123 \
    --token "PASTE_LONG_LIVED_TOKEN" \
    --start 2024-07-01 \
    --end   2026-07-01 \
    --site  site.json \
    --out   bootstrap.json
```

Dry run first (fetch + reconstruct + summarise, **no file written**):

```sh
py -3.14 scripts/backfill.py \
    --ha-url http://homeassistant.local:8123 \
    --token "PASTE_LONG_LIVED_TOKEN" \
    --start 2024-07-01 --end 2026-07-01 \
    --site site.json \
    --dry-run --verbose
```

The summary line reports days used/skipped, shademap channels/bins/samples,
day-ahead cells/RLS-steps, and whether the weather source was **as-issued** or
the **ANALYSIS** fallback. A healthy 2-year run over the reference site produces
several thousand quasi-clear shademap samples and all twelve (4 cloud classes ×
3 day parts) day-ahead cells populated.

### Flags

| Flag | Required | Meaning |
|---|---|---|
| `--ha-url` | yes | HA base URL for the WebSocket LTS pull. |
| `--token` | yes | HA long-lived access token. |
| `--start` | yes | Range start `YYYY-MM-DD` (UTC calendar). |
| `--end` | yes | Range end `YYYY-MM-DD` (inclusive). |
| `--out` | no | Output path (default `bootstrap.json`). |
| `--site` | **yes*** | Your site object as JSON (`SiteConfig.from_dict` shape). |
| `--use-default-site` | no | *Opt in to the shipped **reference** site instead of `--site`. Demo / tests / CI only — it is **not** your plant; logs a warning. |
| `--dry-run` | no | Do everything except write `--out`. |
| `-v/--verbose` | no | Debug logging (per-day skip reasons). |

\* `--site` is required unless `--use-default-site` is given. Up to 0.23.0 it was
optional and the run silently reconstructed against the shipped reference site;
that trap is closed (see below).

Keep the token out of your shell history: on POSIX shells put it in an env var
and reference it (`--token "$HA_TOKEN"`); PowerShell: `--token $env:HA_TOKEN`.

---

## Import into Home Assistant

Copy `bootstrap.json` somewhere HA can read (e.g. `/config/bootstrap.json`),
then call the service (Developer Tools → Actions):

```yaml
action: balcony_solar_forecast.import_bootstrap
data:
  path: /config/bootstrap.json
```

The service **validates and clamps** every factor, rejects any
`schema_version` it does not recognise, and checks the embedded
`site_signature` against the running site (lat/lon + plane names) so a
bootstrap built for a different install is refused. Backfilled shademap bins
carry a small `n` (capped at `BOOTSTRAP_MAX_BIN_N`), so the first weeks of live
15-min data quickly outweigh them.

The import is **additive** for the quantile ring: a bootstrap that carries a
`quantile_state` section **replaces** the live ring (like the bias and shademap),
while an older backfill file **without** that section leaves the live quantile
ring untouched — an import never wipes learned bands. The rollback snapshot taken
before the swap includes all three learner states, so `rollback_learners` undoes
the import consistently.

---

## Re-bootstrap after a config campaign (v0.22 and later)

A **forecast-relevant config edit** — the v0.22 `tau_points` crown migration, a
`diffuse_tau` wall row, a τ / albedo / bifacial-beam-gain change — reshapes the
**RAW** physics curve that every learner is conditioned on. Two things must
follow it:

1. **Day-ahead bias**: handled automatically. `tau_points`, `tau_points_bare`
   and `diffuse_tau` are part of the config fingerprint, so editing them re-opens
   (n-caps) the day-ahead bias cells for fast re-adaptation on the next start
   (SPEC §5, A4). You can also force it with `reset_day_ahead_bias`.

2. **Shademap**: **re-bootstrap recommended.** The learned transmittance `T` is
   trained against the modeled diffuse floor and ungated beam; a `tau_points` /
   `diffuse_tau` edit changes both references, so bins learned under the OLD prior
   carry a now-stale meaning (e.g. a morning bin that absorbed the missing diffuse
   floor as phantom beam gain). The **easiest** way is the `run_bootstrap` action
   (above): it already uses the edited live config, so just call it with
   `dry_run: false` — no export, no token. The offline CLI equivalent (when you
   want the `bootstrap.json` on disk) re-runs the backfill **against the new site
   config** and imports it — the `BOOTSTRAP_MAX_BIN_N` cap makes either low-risk (a
   few weeks of live 15-min data outweigh the seed regardless), and the rollback
   snapshot lets you undo it if needed:

   ```sh
   # export the EDITED site object to site.json first (config-flow shape), then:
   py -3.14 scripts/backfill.py --ha-url http://homeassistant.local:8123 \
       --token "$HA_TOKEN" --start 2024-07-01 --end 2026-07-01 \
       --site site.json --out bootstrap.json
   ```

   Import as above. Expect a short transition where the served 04–06Z curve
   overshoots for a few clear days while the bias cells re-learn — this is the
   documented settle, **not** a reason to roll back (ADR §2.7).

The interim az-ramp (τ(az) sun-path projection) is **deprecated**: migrate it to
`tau_points`, do **not** re-anchor it monthly (SPEC §13, ADR §2.7.6).

---

## What it computes (and why it is coarse)

- **Reconstruction runs at HOURLY resolution.** The Previous-Runs / Historical
  Forecast APIs only expose hourly radiation, so the script evaluates the same
  physics as the live engine at each **hour midpoint** and treats the result as
  the hour's mean power (Wh = mean W × 1 h). Sub-hour geometry is lost — this
  is exactly why the backfilled bin `n` is capped (SPEC §6).

- **Shademap bins**: for each plane/hour that passes the **quasi-clear gate**
  (elevation-ramped `k_c` band, modeled beam share > 5 % of Wp,
  neighbour-hour stability), the beam-referenced transmittance
  `T = (P_measured − P_diffuse_modeled) / P_beam_modeled` is EMA-folded into the
  `(sun-az 5° × sun-el 2.5° × half-year)` bin for that module. The measured
  per-hour module energy comes straight from your hourly LTS.

- **Day-ahead RLS bias**: modeled vs. measured **site** energy is aggregated per
  `(cloud class × day part)` per day and fed through one scalar
  recursive-least-squares step per cell (forgetting factor, clamped bias band).

- **Quantile bands**: after that RLS step, each daylight hour becomes one
  `relerr = measured_site / (clamp(θ_cell) × gated_modeled_site)` sample (clamped
  to `[QUANTILE_REL_ERR_MIN, MAX]`, only where the corrected forecast exceeds
  `QUANTILE_MIN_FORECAST_WH`) in the SAME `(cloud class × day part)` bin. The ring
  is date-windowed to `QUANTILE_RING_DAYS` relative to the **last** backfill day,
  count-capped, and limited to `QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN` samples per
  bin per day so the correlated hours of one coarse hourly day never over-weight a
  band. A bin needs both enough samples and enough distinct days before it emits a
  real (non-collapsed) band — the seeding is what gets the common bins past that
  floor on day 0 instead of weeks later.

- **Cloud class / day part** in the backfill key on the **UTC** hour (the dev
  script has no site calendar). At the operator site (UTC+1/+2) this is within
  ~2 h of local — acceptable for a bootstrap that live nightly training refines.

---

## Your site (`--site`, required)

A bootstrap is only as good as the geometry it reconstructs against — and one
built against a *foreign* site looks perfectly healthy: the `site_signature`
check runs at **import** time and only compares lat/lon + plane names. So since
**0.23.1** the CLI refuses to guess:

- **`--site site.json` is required.** Export the site object your config flow
  stored: Settings → Devices & Services → **Balcony Solar Forecast** →
  *Configure* → the `site` object selector holds the live object; copy it into
  `site.json`. (The same object sits on the HA host in
  `.storage/core.config_entries` as the entry's `options.site`.) The shape is
  `SiteConfig.from_dict`: `latitude`, `longitude`, `planes[]` with
  `name`/`azimuth_deg`/`tilt_deg`/`wp`/`efficiency`/`horizon`/`actual_entity`/
  `shade_group`/`ross_coeff`, plus `groups[]`. Each plane needs its
  `actual_entity` (the LTS statistic id) for the measured side; planes without
  one are skipped.
- **Or skip the export entirely** and use the
  [`run_bootstrap` action](#re-bootstrap-from-home-assistant-run_bootstrap): it
  always uses this install's live config, so there is no site file to get wrong.
- **`--use-default-site`** is the explicit opt-in to the shipped reference site
  `const.DEFAULT_SITE`, for demo/test/CI runs. It logs a warning, and it should:
  `DEFAULT_SITE` is a structure/format **example**, not a maintained image of
  the operator's plant. Known deviations: the screen at az 135–175 sits on M4/M8
  there although the shademap evaluation showed it actually shades M2/M3; the
  wall edge is az 212 instead of the live az 195; and it carries no `albedo`,
  `bifacial_beam_gain`, `tau_points` or `diffuse_tau` keys (so albedo 0.2 and
  beam gain 1.0 apply). If both flags are given, `--site` wins.

Running without either flag aborts before the first network call with exit
code 2 and a message pointing at all three routes.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No site configuration given` (exit 2) | `--site` is required since 0.23.1. Export your site object (see [Your site](#your-site---site-required)), use the `run_bootstrap` action instead, or pass `--use-default-site` for a demo/CI run against the reference site. |
| `HA WebSocket auth failed` | Bad/expired token — regenerate the long-lived token. |
| `No weather returned for the requested range` | Range predates the archive (Previous-Runs since 01/2024). Narrow `--start`. |
| `ANALYSIS fallback (NOT as-issued)` warning | Previous-Runs radiation was empty for the range; the script used the Historical Forecast API. The bootstrap is still written but the day-ahead bias is weaker. |
| `No usable days — bootstrap would be empty` | LTS returned nothing for your `actual_entity` statistics in the range — check the entity ids and that recorder statistics exist for them. |
| Many `Day … no measured actuals, skipped` (with `-v`) | Gaps in your LTS for those days; expected and safe. |

---

## Tests

The reconstruction / bootstrap **math** is a Home-Assistant-free core module —
`custom_components/balcony_solar_forecast/core/bootstrap_build.py` — and the
Open-Meteo Previous-Runs weather fetch is likewise shared
(`core/openmeteo_backfill.py`, session injected); both are used by this CLI AND
the in-process `run_bootstrap` action. `scripts/backfill.py` is the thin CLI
wrapper that owns only the aiohttp session + the WebSocket LTS pull + JSON output
(and re-exports the shared names so `backfill.<name>` keeps working). The action
itself is covered by `tests/test_run_bootstrap.py` (schema/registration, the
dry_run default, the import path, the concurrency lock, range defaults, the
seconds-epoch recorder reduce and every error picture).

Pure-math coverage (no network) lives in
`tests/core/test_backfill_math.py` — payload parsing, per-plane reconstruction,
the quasi-clear gate / bin key / half-year helpers, daily→hourly
disaggregation, per-day accumulation, the n-credit cap, the bootstrap-JSON
contract shape, and the LTS statistics-row parser.
`tests/core/test_backfill_parity.py` additionally proves the pure core and the
(fetch-mocked) CLI path emit byte-identical bootstrap dicts. Run:

```sh
py -3.14 -m pytest tests/core/test_backfill_math.py tests/core/test_backfill_parity.py -q
```
