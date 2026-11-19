# Reproducible forecast comparisons

The community benchmark uses immutable input bundles, explicit AC/DC basis and
chronological development/selection/test partitions. Run from the repository root:

```bash
uv run --no-sync python scripts/validation/replay_benchmark.py tests/fixtures/replay/manifest.json --output /tmp/comparison.json --report /tmp/comparison.md
uv run --no-sync python -m scripts.validation.experiment_candidates tests/fixtures/replay/manifest.json --experiment persistence
```

The CC0 example is synthetic and demonstrates arithmetic only. Its few days do
not prove forecast improvement. Every model uses the same valid target mask;
availability is reported separately. Test comparisons resample paired complete
local-day clusters and need at least 30 distinct days before a decision. Day
counts are not proof of independent weather episodes. A promising result still
requires regime and availability review; nothing activates production learning.

The JSON report includes fixed lead-time and local-time strata. Optional frozen
`regime` metadata adds `sun_elevation_deg`, `forecast_weather`,
`observed_weather`, `electrical_state` (`saturated`/`unsaturated`) and
`learning_state` (`cold`/`partial`/`trained`). Weather categories are
`clear`/`mixed`/`overcast`; absent metadata stays `unknown`. Observed weather is
a separate audit axis and must have its own reference provenance. No category
is inferred from the error of the forecast being evaluated. All strata retain
the same paired-target mask; small subsets do not establish improvement.

Individual-target metrics retain multiple issues for a target. Targets must
belong wholly to one local calendar day and may end at its midnight. The
benchmark rejects overlapping targets from the same issue. For daily totals
and peaks, it selects one issue per day and issue period by the longest
planned target duration, with ties going to the latest issue. Selection uses
the planned intervals, independent of measurements and model availability.
Coverage reports planned, eligible, paired and calendar hours.

Issue periods use the site clock. Previous evening starts at 18:00 on the
prior day, early morning runs until 06:00 on the target day, and intraday
starts at 06:00. Other earlier issues form their own group. Within each
phase, pairwise comparisons select the latest planned issue for each target.
This includes rolling intraday captures with one issue per hour. The
comparison keeps identical targets with available models and the same
measured Wh. Conflicting measurements and days with overlapping shared
targets are counted and excluded. Phase energy and peak mean power describe
the shared rolling target set. The single-issue daily report keeps its
separate selection. Band and P50 comparisons use the shared targets with
each corresponding bound available in both phases.

The P10–P90 report gives width, coverage, and P50 availability by issue period
and lead-time group. Optional per-model `band_status` records group bounds as
`neutral`, `trained`, `mixed` or `ensemble`. For `served`, the benchmark
also accepts `regime.band_learning_state` from a capture. Missing status stays
`unknown`, even when width is zero.

An optional `references.previous_day` record supplies Wh for the same local
interval on the prior day with its original availability time. It and every
model are compared on the same subset where the reference was already known
at issue. The ordinary model comparison retains its original target set.

Persistence experiments require frozen `candidate_inputs` per target:
`feature_end`, `feature_available_at`, `sum_ratio`, `panel_ratio`, and optional
`forecast_class`. The original `theta` curve is the common starting point, and
`served` is the existing baseline. Missing inputs remain unavailable. Features
must be closed and available before the issue. One bounded correction fades to
identity after 120 minutes. No second correction is stacked on served intraday.

`--experiment daily-bands` consumes `daily_records`: one original issue for each
complete local calendar day, its original `available_at`, `forecast_wh`, `actual_wh`, actual availability,
`target_start`, `target_end`, `local_day`, and `issued_interval`. Partial targets
never seed daily residuals. Before 20 available distinct historical days, the
candidate is cold. Coverage, interval score, width and one-sided misses are
compared on paired test days. Summed marginal slot bands are not automatically
calibrated day intervals. Application comparisons do not simulate independently
trained learner variants.

`--experiment shade-gate` consumes DC `shade_records` with a unique label `id`,
`decision_at`, `target_source`, frozen `baseline_eligible` and ordered `frames`.
Frames carry `end`, `available_at`, `generation` and panel observations
(`source`, `group`, `orientation`, `reported_at`, DC `watts`, `reference_watts`,
optional `clipped`/`eligible`). The experimental geometric gate uses only steady
evidence after excluding the target's entire electrical group. It reports lost
training coverage and distinct days. Bias/quantile eligibility remains unchanged.
False alarms require `shadow_alarm`, `independent_shade` and a separately declared
`independent_reference_role` (`external_shade_reference`, `camera` or
`synthetic_truth`); same-panel cloud labels remain unknown. Role declaration is
provenance, not proof that the reference is perfect. Lower alarm counts from
discarding labels alone are not evidence of better forecasts. A separate causal
learning replay and future targets must measure that consequence.

`learning_replay.py` provides a separate callback boundary for learning trials:
each `LearnerVariant` has its own copied initial state and availability time.
Prediction callbacks receive timestamps, optional point forecasts, time-checked features
and issue-time weather or learning metadata. Observed weather is retained for scoring.
Prospective reports include planned, captured and jointly scoreable targets per phase.
For prospective screening, both the gain and its confidence interval use only fully
paired planned days across all phases. The predeclared minimum day count applies to
that same set. The descriptive error tables also retain partial observations.
JSON and Markdown show the three target counts for every planned phase. For learning trials, training uses
only closed labels that have become available, plus the variant's own frozen
training reference. The original inputs and live stores remain untouched.
Adapters for a physical learner must supply its exact historical reference;
the arithmetic scheduler example is not a replay of the entire HA learner.

A complete day-ahead **physical persisted-learning adapter** is also available:

```bash
uv run --no-sync python -m scripts.validation.physical_learning_replay tests/fixtures/replay/physical-manifest.json --output /tmp/physical-report.json
```

This CC0 seven-day example verifies arithmetic and lifecycle only. It begins cold
and delays recorded labels until after calendar closure; the report scores four
synthetic test days, which cannot establish predictive improvement. A minimal
Python environment without Home Assistant can run the command. The manifest
uses the same verified input hash, basis, timezone, capture time, licence and
source-role contract as the ordinary benchmark.

Input is `schema_version: 1`, `basis: DC`, `timezone`, a validated stationary
`site`, and `days`. Each day declares `local_day`, `issued_at` (before local day
start), `weather_role: issued_forecast`, `weather_available_at`, original ordered
15-minute `weather_slots`, `actuals_available_at` (after day end) and
`actuals_hourly: {stable_module_id: {UTC_hour_start: Wh}}`. An hourly mean W is
Wh for its complete one-hour interval; `actual_unit`, when provided, must be
`Wh`. Complete weather axes include local 23/24/25-hour days. Equivalent
ISO dates identify the same day. In zones with a fractional UTC offset,
training and scoring use only complete UTC recorder hours inside the local
day. Corrupt, stuck, implausible, missing or collapsed training labels
cannot seed a learner.

Default presets are `raw`, `bias`, `slow` and `full` (persistent shading, bias
and quantiles). A `variants` object can name separate candidates with
`{preset: full, site: optional_candidate_site}`. Geometry candidates must retain
the measured module IDs/sources and weather location. `days[].variant_weather`
may provide originally available forecast weather for each named model, with
the same weather fields and availability contract. Observed weather is rejected.
Each variant retains its own physical reference and independently trained state;
no current/live θ or shaded curve is borrowed into the past. Configuration,
weather and kernel hashes identify the calculation.

Optional `initial_states` supply separately archived `bias`, `shade` and
`quantile` state per variant plus `available_at` before the first issue. Absent
states are cold. State loading follows the production validate-and-clamp
contract, and the report records each origin. A declared timestamp is provenance,
not independent proof that an export represents that historical state.

With `development_end` and `selection_end`, the report includes the ordinary
paired DC error metrics and day-cluster decisions. Scoring uses exactly the
configured measured subset and complete hourly targets; curve omissions at
night become zero only because the full finite input axis was verified. Partial
metering never silently compares measured ports against all panels. Collapsed
but otherwise valid zero measurements remain scoreable, even though they cannot
train. RAW's consumed-day count describes shared label eligibility, never a RAW
state update. End-of-run labels still unavailable at the last issue remain pending.

The interval report pairs each closed hourly site DC measurement in Wh with
the engine's sums of the four 15-minute DC P10/P50/P90 energy bounds. It
reports empirical P10–P90 coverage, width, one-sided misses and P50 point
error at that hourly resolution. Summing bounds does not establish calibrated
hourly quantiles or measure live 15-minute coverage. For a variant with
unmetered planes, the report gives point errors on the measured subset and
leaves site interval coverage unavailable.

Scope is the persistent day-ahead learning experiment, **not** a complete HA
simulator: intraday, automatic drift disabling, next-day collapse freeze,
calibrated inverter η, and ensemble fusion are explicitly excluded. Dense panel
traces and independent shade references remain necessary for evaluating a new
panel-driven training policy. Historical data lacking original issued weather
or available learner states cannot be retroactively certified by this adapter.

To measure the magnitude of a physics change against another checkout:

```bash
uv run --no-sync python scripts/validation/model_contract_impact.py --baseline-root /path/to/baseline-checkout --output /tmp/model-impact.json
node scripts/validation/browser_cards.mjs /tmp/cards-preview
```

The 18-case synthetic matrix separates sky visibility, DC energy and AC energy;
kernel hashes identify the compared code. It reports model changes, not forecast
accuracy. The browser command requires the local Playwright runtime described
in `docs/PLAYWRIGHT-MCP.md`, launches a fresh synthetic-only Chromium instance
and never reuses the MCP login profile.

Live operator captures select one config entry through registry identity, support
renamed sensors, use the HA timezone and request power statistics in W. With
multiple entries `--entry-id` is required. `capture_manifest.json` contains source
roles and file hashes; loading rejects changed captured bytes. These captures
use conservative full-calendar coverage (including DST) for daily totals and
reject nonfinite, boolean or negative power labels. Without geometry, a fixed
European daylight window cannot establish complete days. Captures remain
private until the owner reviews, anonymizes and explicitly releases them.
They are not the same schema as frozen benchmark input bundles.

## Prospective lead-time captures

Use a private capture directory and define the target hours, site timezone,
DC sources and split dates before collection. Make one plan per stable site
configuration. For example, this fictional plan targets one hour.

```json
{
  "timezone": "Europe/Berlin",
  "entry_id": "fictional_site",
  "dc_sources": {"Module A": "sensor.dc_a", "Module B": "sensor.dc_b"},
  "targets": ["2026-12-01T10:00:00Z"],
  "development_end": "2026-11-15",
  "selection_end": "2026-11-30",
  "licence": "Private, no redistribution"
}
```

Set `BSF_HA_URL` and `BSF_HA_TOKEN` in the process environment. Capture
requires a release whose `get_forecast` accepts `include_capture_metadata`.
Run each capture at its actual time. The script verifies the plan's DC sensors
against the selected HA config entry and stores only the binding fingerprint.
The raw entity IDs and entry ID stay in the private plan. The tool reads forecast and Recorder data and
writes local files exclusively. It creates no jobs in HA.

```bash
# Optional previous-day benchmark. Collect closed reference hours before the next HA publication.
uv run --no-sync python -m scripts.validation.capture_lead_time plan.json capture reference 2026-12-01 previous_evening
# Previous day from 18:00 to midnight in the site timezone, after that publication
uv run --no-sync python -m scripts.validation.capture_lead_time plan.json capture issue 2026-12-01 previous_evening
# Target day before 06:00 local
uv run --no-sync python -m scripts.validation.capture_lead_time plan.json capture issue 2026-12-01 early_morning
# At most 30 minutes before the target start, from 06:00 local onward
uv run --no-sync python -m scripts.validation.capture_lead_time plan.json capture issue 2026-12-01 intraday --target 2026-12-01T10:00:00Z
# After all target intervals finish and Recorder has written their final bucket
uv run --no-sync python -m scripts.validation.capture_lead_time plan.json capture actual 2026-12-01
# Offline, also repeatable with a new output directory
uv run --no-sync python -m scripts.validation.capture_lead_time plan.json capture export replay
uv run --no-sync python scripts/validation/replay_benchmark.py replay/manifest.json --output replay-report.json
```

The tool records HA's calculation/publication timestamps and the client receipt
time. Both publication and receipt must precede the target and fall within its
phase. Synchronize the HA and capture-host clocks. The bundle hashes identify the
frozen input files and plan. They do not independently attest either clock.
Use one UTC start for each target hour, including the repeated hour at DST change.
Evening and morning collect every planned target for the specified local day.
Near-term collection selects exactly one planned target per call.
Choose targets that can be observed in all three phases.

Each closed target requires all twelve five-minute Recorder means in W from
each planned DC module. Recorder metadata must identify a power statistic
stored in W or kW, which Recorder converts to the requested W. An
unverified, incompatible or changed channel remains in the bundle as
an explicit missing label. Partial forecast hours also stay missing. Four
valid slot markers must confirm a wholly dark hour before an absent hour
becomes a forecast zero. The advance plan and `missing_forecast_captures` expose absent issue times.
Use the same target-hour intersection for direct comparisons between lead phases.
The RAW and served forecasts come from the same published DC model generation.
When all slots have bands, their sums are hourly aggregates of marginal
15-minute bands. Their evidence is recorded as neutral, trained, ensemble,
mixed, or unknown. Coverage of those sums is an empirical result.

Run `reference TARGET_DAY PHASE` separately for each phase where a previous-day
comparison is needed. It captures closed five-minute Recorder buckets for the
same local clock hour on the previous day. Use a later HA publication for
the subsequent `issue` command. Ambiguous, still open, or missing source
intervals leave the optional reference empty. References are frozen into the
issue file before offline export and do not reduce the RAW/served sample.

Optional `export --observations weather.json` adds a separate observation stratum.
The input object has a `source` description and `records` containing
`target_start`, `available_at` at or after interval close, and
`observed_weather` (`clear`, `mixed` or `overcast`). Use an independent reference,
such as a local irradiance observation. Without it the stratum stays unknown.

The following C1–C8 runbook is historical, specific to the reference installation
and July 2026 intervention. Its thresholds are not community release gates.

---

# Post-Deployment-Validierung `balcony_solar_forecast` v0.21.0

Runbook + Skriptpaket, um **~1 Woche nach dem Deployment** von v0.21.0 samt
Config-Änderungen (tau-Rampe Ost-Horizont, `bifacial_beam_gain` 1.25,
`reset_day_ahead_bias`) objektiv zu prüfen, ob die Fixes wirken — und ob sie
nichts kaputt gemacht haben.

Die Schwellen aller Checks sind gegen die **VOR-Fix-Woche 17.–24.07.2026**
kalibriert: auf diesen Daten zeigen C1–C7 FAIL und die Regressionswachen C8
PASS (Selbsttest, siehe unten). Nach einer erfolgreichen Fix-Woche müssen
C1–C7 grün werden, während C8 grün **bleibt**.

---

## 1. Voraussetzungen

- Windows (oder Linux/macOS), **Python ≥ 3.11** (getestet mit 3.13/3.14).
  Nur Standardbibliothek — kein `pip install` nötig (numpy/pandas werden
  nicht verwendet).
- Home Assistant 2026.7.x erreichbar, Integration `balcony_solar_forecast`
  v0.21.0 (das Skript erkennt und meldet, wenn noch v0.20.6 läuft).
- Netzzugang zur HA-Instanz. **IP verwenden, nicht Hostname** — `hass` ist
  aus manchen Clients nicht auflösbar: `http://10.102.10.11:8123`.
- Recorder mit Langzeit-Statistiken aktiv (Standard); Analysefenster
  mindestens 5–7 volle Tage nach dem Deployment.

### Token erzeugen

1. HA-Frontend → Profil (Avatar unten links) → Tab **Sicherheit**.
2. Abschnitt **Langlebige Zugriffstoken** → *Token erstellen*,
   Name z. B. `bsf-validation`.
3. Token einmalig kopieren. Der Token braucht ein **Admin**-Konto
   (Diagnostics-Endpoint); ohne Admin laufen alle Checks außer den
   Quantile-/Versions-Zusatzinfos trotzdem.
4. Nach der Validierung Token wieder löschen (Profil → gleiche Stelle).

---

## 2. Aufrufe

Alle Kommandos aus dem Ordner `validation/` heraus.

### Live-Validierung (Standardfall, ~1 Woche nach Deployment)

```powershell
python validate.py --ha-url http://10.102.10.11:8123 --json report.json
# Token aus der Umgebung (HA_LONG_LIVED_TOKEN) — bevorzugt: ein --token-CLI-Arg
# steht in der Prozessliste, über http:// reist er im Klartext. --token bleibt Override.
```

Das Skript zieht alle Daten (REST + WebSocket) in einen Ordner
`bsf_pull_<timestamp>/` und analysiert sie sofort. Der Ordner ist ein
vollständiger Snapshot — die Analyse lässt sich später beliebig oft offline
wiederholen:

```powershell
python validate.py --offline --data-dir bsf_pull_20260801_0900
```

### Weitere Optionen

| Option | Bedeutung |
|---|---|
| `--days N` | Analysefenster (Default 8 Tage rückwärts ab heute) |
| `--data-dir PFAD` | Zielordner für den Pull bzw. Quellordner für `--offline` |
| `--fetch-only` | nur Daten ziehen (Analyse später offline) |
| `--json PFAD` | Report zusätzlich maschinenlesbar exportieren |
| `--eta 0.9248` | DC→AC-Fallback-Wirkungsgrad (nur relevant, solange `get_issued_forecast` kein `hourly_wh_ac` liefert) |
| `--entry-id ID` | nur nötig bei mehreren konfigurierten Sites |

Gesamtstatus und Exit-Code: `0` = vollständig `PASS`, `1` = `WARN` oder
`INCOMPLETE`, `2` = `FAIL` oder `ERROR` (auch Fetch-/Datenfehler).
Fehlende Dateien, Checks oder erforderliche Teilbelege ergeben `INCOMPLETE`.
Eine interne Check-Ausnahme ist `ERROR`; die übrigen Checks laufen weiter.
`Deployment validiert` erscheint nur, wenn alle acht Prüfbereiche vollständig
und erfolgreich auswertbar sind. Der JSON-Report enthält `summary.status`
und die IDs unvollständiger Prüfbereiche in `summary.incomplete`.

### Offline-Wiederholung und synthetischer Softwaretest

```powershell
python validate.py --offline --data-dir ..\hadata
```

Erwartung auf dem ursprünglichen privaten Paket der VOR-Fix-Woche:
**C1–C7 FAIL, C8 PASS** (Exit 2). Dieses Paket ist nicht im Repository; seine
historische Kalibrierung lässt sich aus dem Checkout allein nicht nachweisen.

Reproduzierbare Softwaretests liegen in `tests/test_review_tooling_validation.py`.
`tests/helpers/validation_bundle.py` erzeugt synthetische, versionierte
Zwei-Tage-Pakete ohne private Daten: vollständig PASS, gezielte Überprognose
FAIL, fehlende Belege INCOMPLETE und eine Check-Ausnahme ERROR. Diese Tests
prüfen die Berechnung und den CLI-Status, nicht die historische Eignung der
anlagenspezifischen Juli-Schwellen:

```bash
uv run pytest tests/test_review_tooling_validation.py -p no:homeassistant
```

---

## 3. Was geprüft wird (Prüfkatalog)

| Check | Frage | PASS-Kriterium | Baseline (vor Fix) |
|---|---|---|---|
| **C1** Morgen-Peak | Ist die servierte AC-Prognose 07–10 lokal ehrlich? | Wochenmittel served/Ist 0.80–1.20 **und** ≤1 Tag mit Peak-Ratio >1.4 | 1.23; 3 Tage ~1.5–1.7 |
| **C2** Scalar-Hygiene | Muss der Intraday-Scalar morgens noch Physik kompensieren? | an Tagen mit \|Tagesfehler\|<10 %: max Scalar 08–09 lokal ≤1.25; Peaks >1.6 nur an Abweichungstagen | 2.35 am 20.07. bei 0 % Tagesfehler |
| **C3** Morgen-Physik | Hebt die tau-Rampe den Roh-Morgen, ohne Sprung? | raw 04Z ≥300 Wh (klare Morgen); raw/Ist 06–10Z 0.90–1.05; keine Prognose-Sprünge an glatten Morgen | 226 Wh; 0.78; 3/3 Sprünge (~06:44 statt Anstieg ab ~06:05) |
| **C4** Bias-Konvergenz | Löst sich der RLS von den Clamps? | clear\|morning ≤1.25 (Ziel 1.0–1.15); afternoon-Zellen ≥0.85; clamped-Flags rückläufig | 1.491 (am 1.5-Clamp); 0.58–0.71 |
| **C5** day-0-Bänder | Trägt der Tag echte p10/p90-Bänder? | >50 % der Tageslicht-Slots mit p10≠p90; kein Tag mit Intraday-p10 > End-Ist | 4/62 Slots; 3 Tage p10>Ist |
| **C6** Headline-Stabilität | Kein Korrektur-Jojo der Tagesprognose? | kein 60-min-Swing >1.5 kWh, der mit einem Scalar-Spike zusammenfällt | 2.36 kWh am 20.07. bei Scalar 2.35 |
| **C7** Abend/Vorabend | Stimmt die Vorabend-Prognose als Planungsbasis? | issued-AC-Wochenbias ±10 %; Nachmittag 12–18Z ±15 % | −10.2 %; −38.1 % |
| **C8** Regressionswachen | Haben die Fixes nichts beschädigt? | (a) Scalar korrigiert an Überprognose-Tagen weiter <0.95, (b) M4/M8-Morgen-Diffus unverändert (bekanntes ~10×-Defizit, **kein Fehler**), (c) Mittagsfenster 11–13Z raw/Ist ±10 % vs. Baseline 0.90 | alle PASS |

Anmerkungen zur Semantik (im Skript zentral behandelt):

- **DC vs. AC**: `raw_hourly_wh`/`hourly_wh` aus `get_issued_forecast` sind
  bis v0.20.6 DC-basiert. AC-Vergleiche (C2/C7) nutzen ab v0.21.0 das neue
  `hourly_wh_ac`; fehlt es, wird DC×η (Default 0.9248, Victron-gelernt)
  gerechnet und im Report ausgewiesen. C3/C8c vergleichen bewusst DC↔DC.
- **epoch-ms**: Die Recorder-WS-API liefert `start` in Millisekunden — wird
  automatisch erkannt (kein 1970er-Bug möglich).
- **Partielle Tage** (heute, Lücken) werden erkannt und aus Tagessummen-
  Checks ausgeschlossen; Morgen-Checks nutzen sie weiter.
- **Fehlende v0.21.0-Felder** (`hourly_wh_ac`, `cloud_class_by_hour`,
  `clamped`-Flag) führen nie zum Crash: der Report meldet sie im Block
  „Feature-Erkennung“ und rechnet mit dokumentiertem Fallback.

---

## 4. Interpretation & Nachjustierung („Wenn ein Check rot bleibt“)

Reihenfolge beachten: **erst C8 ansehen, dann C3, dann den Rest** — C1/C2/C6
hängen kausal an der Morgen-Physik (C3), C4/C7 am Bias-Reset.

- **C8c FAIL (Mittagsfenster verschoben)** → Stopp. Die tau-Rampe wirkt
  nicht nur bei niedriger Sonne (Config-Fehler: Rampe reicht in hohe
  Elevationen). Horizont-YAML prüfen, bevor irgendetwas anderes
  nachjustiert wird. (+1–2 % durch beam_gain 1.25 sind erwartbar und PASS.)
- **C3 FAIL, raw/Ist 06–10Z weiter <0.90** → Morgen-Physik hebt zu wenig:
  `bifacial_beam_gain` 1.25 → **1.30** anheben (ein Schritt, dann erneut
  eine Woche messen). Liegt nur `raw 04Z` unter 300 Wh, ist eher der
  Horizont-/tau-Start zu spät → tau-Rampe eine Stufe früher beginnen lassen.
- **C3 FAIL, weiterhin Sprung ~06:44** → tau-Rampe greift nicht (Deployment/
  Config nicht aktiv?). Diagnostics im Report prüfen: läuft wirklich
  v0.21.0? Horizont der OSO-Planes (M2/3/6/7) kontrollieren.
- **C3 PASS, aber C1/C2 weiter FAIL** → Physik stimmt, aber der Scalar
  überschießt noch (Gedächtnis der alten Woche oder zu aggressive
  Verstärkung). Erst 2–3 weitere Tage abwarten; bleibt es, Intraday-Clamp/
  Tau in der Integration prüfen (kein Config-Knopf — Issue aufmachen).
- **C4 FAIL: clear|morning wieder >1.4** → Der RLS lernt erneut gegen ein
  Physikdefizit an → C3 ist in Wahrheit nicht gelöst (siehe dort). Nach
  `reset_day_ahead_bias` brauchen Zellen ~5 Tage (`n≥5`), vorher meldet der
  Check INFO „cold start“ — das ist kein Fehler.
- **C4 FAIL: afternoon-Zellen bleiben ≤0.75** → Nachmittag wird real
  überprognostiziert (nicht mehr Bias-Artefakt). AC-Limit-Clipping (800 W
  pro WR) und Wand-Schatten az≈212° ab ~14:20 lokal gegen die Kurven halten;
  ggf. Screen-/Shademap-Zuordnung revisited (bekannte M2/M3-Fehlzuordnung).
- **C5 FAIL: Bänder weiter kollabiert** → Quantile-Seeding-Bootstrap nicht
  gelaufen oder Bins weiter untrained (Report listet trained/untrained).
  Bootstrap erneut ausführen; danach müssen mindestens die clear-Bins
  trained sein.
- **C5 FAIL: p10 > End-Ist** trotz Band → p10-Kopplung an den Scalar-Spike
  besteht noch; zusammen mit C2 lesen (gleiche Wurzel).
- **C6 FAIL** → nur relevant, wenn der Swing mit einem Scalar-Spike
  zusammenfällt (steht in der Zeile). Reine Wetter-Refresh-Swings zählen
  nicht — bei Verdacht die im Report genannte Uhrzeit gegen die
  Weather-Fetch-Zeiten (30-min-Raster) halten.
- **C7 FAIL: Nachmittag weiter < −15 %** → solange C4-afternoon noch am
  alten Wert klebt: Bias-Reset wirklich ausgeführt? Wenn C4 grün und C7
  trotzdem rot → echte Nachmittags-Physik (siehe C4-afternoon-Punkt).
- **C8b WARN (M4/M8-Morgen-Diffus stark verändert)** → nicht Teil der
  0.21-Fixes; prüfen, ob versehentlich Screen-/Shademap-Änderungen
  deployt wurden. Das ~10×-Defizit selbst ist **erwartet und akzeptiert**.

Generell: **eine Stellschraube pro Woche** (beam_gain ODER tau-Rampe ODER
Reset), sonst ist die nächste Messwoche nicht attribuierbar.

---

## 5. Paketinhalt

| Datei | Zweck |
|---|---|
| `validate.py` | CLI: Fetch + Analyse + Report (Tabelle, `--json`) |
| `bsf_fetch.py` | Datenbezug: REST (`/api/states`, `history/period`, Service `get_issued_forecast` mit `return_response`, Diagnostics) + minimaler stdlib-WebSocket-Client für `recorder/statistics_during_period` (hour + 5minute) |
| `bsf_data.py` | Laden/Normalisieren (epoch-ms, minimal_response, Zeitzone Europe/Berlin inkl. Fallback ohne tzdata) |
| `bsf_checks.py` | Prüfkatalog C1–C8 inkl. Schwellen und Baseline-Konstanten |
| `README.md` | dieses Runbook |

Der Pull-Ordner enthält dieselben fünf JSON-Dateien wie das Referenzpaket
`hadata/` (`actuals_hourly_stats.json`, `fiveminute_stats.json`,
`entities_now.json`, `forecast_sensor_history.json`,
`issued_forecasts_and_diag.json`) — Live- und Offline-Analyse sind dadurch
identisch und jeder Report reproduzierbar.

## 6. Bekannte Annahmen / Grenzen

- Der WebSocket-Client ist minimal (RFC 6455, nur was HA braucht); bei
  Proxys/HTTPS mit Sonderkonfiguration ggf. direkt gegen die interne
  HTTP-IP gehen.
- η=0.9248 ist der Victron-gelernte Gesamtwirkungsgrad; sobald v0.21.0
  `hourly_wh_ac` liefert, wird η ignoriert.
- „Klare Morgen“ ohne `cloud_class_by_hour` (v0.20.6) per Heuristik
  (Ist-DC 05–09Z ≥70 % Wochenmax) — mit v0.21.0 automatisch exakt.
- C8b/C8c vergleichen gegen fest einkodierte Baseline-Konstanten der
  Juli-Woche (2254 Wh bzw. 0.90); bei stark anderer Jahreszeit sind diese
  beiden Wachen nur noch orientierend (Saisongang), die übrigen Checks
  bleiben gültig.
- Die Weather-Refresh-Erkennung in C6 ist indirekt (Scalar-Koinzidenz);
  echte Refresh-Events loggt die Integration nicht in den Recorder.

Panel frames may include `azimuth_deg` and `tilt_deg`. When geometry is supplied,
orientation cohorts use surface normals with a 30-degree separation criterion;
invalid or incomplete geometry is excluded. Legacy frames without geometry use
their declared `orientation` labels. Any change in usable sources restarts the
five-minute ramp reference, including after leave-target-group-out filtering.
