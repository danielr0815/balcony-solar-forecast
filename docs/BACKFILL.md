# Learner Bootstrap Backfill (SPEC §6)

`scripts/backfill.py` warm-starts the two learning layers from ~2 years of
history so the system does not meet its first live winter cold. It is a
**one-shot, dev-machine** job (never runs on Home Assistant). It:

1. fetches Open-Meteo **Previous-Runs** day-1-lead forecasts-as-issued for the
   site (archived since 01/2024);
2. reconstructs per-plane **hourly** modeled beam/diffuse/ghi/kc curves by
   importing the repo's own `core/` physics package (identical to the live
   engine — no numpy);
3. pulls measured **hourly per-module** energy from your HA long-term
   statistics over the **WebSocket API**;
4. computes a **day-ahead RLS bias** bootstrap and a **shademap** bin bootstrap
   (with the backfilled sample count `n` **capped** so live data overrides it
   quickly); and
5. writes a `bootstrap.json` that the
   `balcony_solar_forecast.import_bootstrap` service ingests
   (validate + clamp, rejects unknown schema).

The backfill is **"mandatory to attempt, not a blocker"** (SPEC §6): the
integration runs fully without it. If the Previous-Runs radiation is
unavailable, the script degrades to the plain **Historical Forecast API** and
prints a loud warning that the data is *analysis, not as-issued forecast*
(still useful for the geometric shademap, weaker for the weather-error bias).

---

## Prerequisites

- Python **3.14** (or any 3.11+) on the dev machine, with `aiohttp` installed:

  ```sh
  py -3.14 -m pip install aiohttp
  ```

  (Nothing else — the physics core is stdlib-only.)

- A **Home Assistant long-lived access token**: HA profile → bottom of the page
  → *Long-Lived Access Tokens* → *Create Token*. Copy it once.

- Your HA base URL reachable from the dev machine, e.g.
  `http://homeassistant.local:8123` (or the LAN IP).

- The integration installed on HA (the reference site is the shipped
  `DEFAULT_SITE`; if your planes/entities differ, export a `--site` JSON — see
  below).

---

## Run it

Full 2-year backfill (LTS exists since 2024-07):

```sh
py -3.14 scripts/backfill.py \
    --ha-url http://homeassistant.local:8123 \
    --token "PASTE_LONG_LIVED_TOKEN" \
    --start 2024-07-01 \
    --end   2026-07-01 \
    --out   bootstrap.json
```

Dry run first (fetch + reconstruct + summarise, **no file written**):

```sh
py -3.14 scripts/backfill.py \
    --ha-url http://homeassistant.local:8123 \
    --token "PASTE_LONG_LIVED_TOKEN" \
    --start 2024-07-01 --end 2026-07-01 \
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
| `--site` | no | Site JSON override (defaults to the shipped reference site). |
| `--dry-run` | no | Do everything except write `--out`. |
| `-v/--verbose` | no | Debug logging (per-day skip reasons). |

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

- **Cloud class / day part** in the backfill key on the **UTC** hour (the dev
  script has no site calendar). At the operator site (UTC+1/+2) this is within
  ~2 h of local — acceptable for a bootstrap that live nightly training refines.

---

## Custom site (`--site`)

If your install is not the shipped reference site, export the site object your
config flow stored (the `SiteConfig.from_dict` shape: `latitude`, `longitude`,
`planes[]` with `name`/`azimuth_deg`/`tilt_deg`/`wp`/`efficiency`/`horizon`/
`actual_entity`, and `groups[]`) to a JSON file and pass `--site site.json`.
Each plane needs its `actual_entity` (the LTS statistic id) for the measured
side; planes without one are skipped.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `HA WebSocket auth failed` | Bad/expired token — regenerate the long-lived token. |
| `No weather returned for the requested range` | Range predates the archive (Previous-Runs since 01/2024). Narrow `--start`. |
| `ANALYSIS fallback (NOT as-issued)` warning | Previous-Runs radiation was empty for the range; the script used the Historical Forecast API. The bootstrap is still written but the day-ahead bias is weaker. |
| `No usable days — bootstrap would be empty` | LTS returned nothing for your `actual_entity` statistics in the range — check the entity ids and that recorder statistics exist for them. |
| Many `Day … no measured actuals, skipped` (with `-v`) | Gaps in your LTS for those days; expected and safe. |

---

## Tests

Pure-math coverage (no network) lives in
`tests/core/test_backfill_math.py` — payload parsing, per-plane reconstruction,
the quasi-clear gate / bin key / half-year helpers, daily→hourly
disaggregation, per-day accumulation, the n-credit cap, the bootstrap-JSON
contract shape, and the LTS statistics-row parser. Run:

```sh
py -3.14 -m pytest tests/core/test_backfill_math.py -q
```
