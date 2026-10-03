# Entwicklung, Tests & Release

**Worum es geht:** Die Arbeitsanleitung für Code-Änderungen an
`balcony_solar_forecast` — Repo-Layout, die harte HA-Freiheits-Regel für `core/`,
wie die Testsuite aufgebaut ist und mit welchem Kommando sie auf Windows läuft, was
als *Vertrag* gilt (SPEC, Store-Schema, Config-Serialisierung, Fingerprint) und wie
ein Release exakt abläuft.
**Wann du es brauchst:** Bevor du die erste Zeile änderst, und noch einmal bevor du
taggst. Fachliche Inhalte stehen in `01`–`06`; hier geht es ausschließlich um
Handwerk und Prozess.

Stand: **v0.27.2**, überarbeitet am 2026-09-26. Belege sind Datei +
Funktions-/Konstantenname, keine Zeilennummern. Zusätzlich verbindlich:
`CONTRIBUTING.md` im Repo-Root.

Setup: `uv sync --locked --group dev` erzeugt `.venv` aus dem committeten
`uv.lock` und installiert Python 3.14 bei Bedarf. Ohne uv helfen
`scripts/setup-env.sh` bzw. `scripts/setup-env.ps1` (Bootstrap: Python 3.10+).
Der Devcontainer führt dasselbe Setup aus und installiert Node für den
Karten-Harness. Die Mindestversionen stehen in `pyproject.toml`, exakte
Versionen im Lockfile; Änderungen daran sind bewusste Dependency-Updates.

---

## 1. Repo-Layout: was gehört wohin

| Pfad | Inhalt | Regel |
|---|---|---|
| `custom_components/balcony_solar_forecast/core/` | **reiner Rechenkern** — Physik, Lerner, Scoreboard, Quantile, Bootstrap-Mathematik | HA-frei, stdlib-only (§2) |
| `custom_components/balcony_solar_forecast/` (Rest) | **HA-Glue** — `coordinator.py`, `config_flow.py`, `sensor.py`, `store.py`, `_services.py`, `_nightly.py`, `_actuals.py`, `diagnostics.py`, … | darf HA importieren |
| `custom_components/balcony_solar_forecast/const.py` | einzige Quelle für Domain, Config-Keys, Defaults, Tunables, Store-Keys, `INTEGRATION_VERSION` | **muss HA-frei bleiben** — `core/` importiert nur `..const` |
| `.../frontend/` | zwei abhängigkeitsfreie Lovelace-Karten (`shade_profile_card.js`, `power_history_card.js`) | Vanilla `HTMLElement` + SVG, kein Build-Step |
| `.../translations/` | `de.json`, `en.json` | Tests erzwingen Deckungsgleichheit (§4) |
| `scripts/backfill.py` | dünner CLI-Wrapper um `core/bootstrap_build.py` (aiohttp + HA-WebSocket-LTS) | keine Mathematik hier |
| `scripts/validation/` | Post-Deployment-Validierung (stdlib, REST/WS gegen die Live-Instanz) | §4 (Ende) · `scripts/validation/README.md` |
| `scripts/setup_env.py` | pure-stdlib uv-Bootstrap hinter `setup-env.{sh,ps1}`; `make` ruft uv direkt auf | |
| `tests/` · `tests/core/` · `tests/integration/` | portable HA-Unit-Tests · reine Kern-Tests · echte HA-Lifecycle-Tests | §3 |
| `tests/helpers/` | gemeinsame Fakes und versionierte synthetische Datengeneratoren | keine Imports zwischen Testmodulen |
| `docs/SPEC.md` · `docs/adr/` | **der Vertrag** (deutsch) · Architecture Decision Records | §5 |
| `dashboards/` | ausgeliefertes Lovelace-YAML | von `tests/core/test_dashboard_yaml.py` bewacht |
| `.github/workflows/` | `validate.yml` (CI) und `release.yml` (Release-Guard) | §6 |

Nicht im Git (`.gitignore`): `.venv/`, `scratchpad/`, `.ha-dev/`, Caches. `.ha-dev/`
ist die lokale Wegwerf-HA-Instanz (`configuration.yaml` mit `default_config:` und
Debug-Logger für die Domain); `.claude/launch.json` startet sie als `ha-dev` über
`.venv/Scripts/hass.exe -c .ha-dev` auf Port 8123.

---

## 2. Die harte Regel: `core/` ist Home-Assistant-frei

`core/__init__.py` sagt es im Docstring: „Nothing in this package imports from
`homeassistant`". Das ist keine Stilfrage, sondern trägt drei Dinge:

1. **Testbarkeit.** Der gesamte Kern läuft mit blankem `pytest` auf jedem Python
   3.14 — kein HA-Setup und keine laufende Event-Loop erforderlich.
2. **Wiederverwendung im Backfill.** `scripts/backfill.py` importiert den Kern über
   einen Namespace-Package-Shim direkt aus dem Repo (ohne das HA-importierende
   Paket-`__init__`). Seit **0.23** liegt auch die Bootstrap-Rekonstruktion dort
   (`core/bootstrap_build.py`), damit CLI und In-App-Aktion `run_bootstrap`
   **dieselbe** Mathematik ausführen.
3. **Keine Runtime-Dependencies.** `manifest.json` hat `requirements: []`; im Kern
   erscheinen ausschließlich stdlib-Importe (`math`, `dataclasses`, `datetime`,
   `functools.lru_cache`, `logging`, `hashlib`, `json`, `pathlib.Path`,
   `collections.abc`, `zoneinfo`, `typing`) plus `from __future__ import annotations`.
   Typen nutzen PEP 604/585 (`X | None` statt `Optional[X]`).
   Kein numpy/pandas/pvlib — auch nicht „nur kurz".

**Die einzige dokumentierte Ausnahme:** `core/openmeteo_backfill.py` ist das eine
Kern-Modul, das Netzwerk anfasst. Es importiert `aiohttp` **lazy innerhalb** der
Fetch-Funktion und bekommt die Session injiziert (CLI: eigene
`aiohttp.ClientSession`; Aktion: `aiohttp_client.async_get_clientsession(hass)`).
Es bleibt HA-frei — genau deshalb können beide Aufrufer nicht auf dem
Provider-Vertrag auseinanderdriften.

### Prüfkommando

```bash
uv run python scripts/check_core_imports.py
```

Der AST-Guard läuft in CI und prüft auch verzögerte Imports in Funktionen:
stdlib und relative Kernmodule sind erlaubt, HA-/Glue-/Fremdimporte werden
abgewiesen. Nur `core/openmeteo_backfill.py` darf `aiohttp` innerhalb einer
Funktion laden. `const.py` darf seinerseits keinen HA-Glue importieren.
Eine grüne Kern-Testsuite allein beweist die Importgrenze nicht, wenn HA im
Test-Environment installiert ist.

---

## 3. Tests: Aufbau und Kommandos

* `tests/core/`: reine Rechenkern-Tests. Ein Namespace-Package-Shim umgeht das
  HA-importierende Root-`__init__.py`.
* `tests/*.py`: portable HA-Schicht-Tests gegen Fakes und `monkeypatch`.
  Gemeinsame Fakes liegen in `tests/helpers/`, nicht in anderen Testmodulen.
* `tests/integration/`: echter HA-Bootstrap, Flow-Manager, Entitäten, Services,
  Reload und Store-Lifecycle auf Linux. Der getrennte Aufruf mit `--confcutdir`
  vermeidet die Package-Shims aus `tests/conftest.py`.
* `tests/harness/`: gebündeltes Karten-JavaScript unter Node mit kleinen DOM-Stubs.
  CI installiert Node 22, damit diese Tests nicht still übersprungen werden.

Die aktuelle Testzahl steht im pytest-Ergebnis; eine statische Zahl im Runbook
veraltet. Golden-Parametrisierungen unterhalb der definierten Sonnenhöhe werden
bewusst übersprungen und durch eigene Grenztests ergänzt. Fehlende Golden-Dateien
sind dagegen ein Fehler, kein Skip.

```bash
uv run pytest tests --ignore=tests/integration -p no:homeassistant
uv run pytest tests/core -p no:homeassistant
uv run pytest --confcutdir=tests/integration tests/integration -p no:homeassistant
uv run ruff check .
uv run mypy
uv run python scripts/check_mypy_baseline.py
```

Diese Kommandos funktionieren auch in PowerShell; nur der reale HA-Lifecycle-Lauf
setzt Linux voraus. `make test`, `make test-core`, `make lint` sind uv-Wrapper.
`make format` bedeutet `ruff check --fix`, **niemals** `ruff format`.

### Warum `-p no:homeassistant`?

Keine Suite benutzt PHACC-Fixtures. Dessen Autouse-Fixtures können mit
synchronen Tests und der Event-Loop kollidieren; der Import zieht zudem das
POSIX-only `fcntl`. PHACC bleibt deshalb auch beim echten HA-Lauf abgeschaltet.
`pytest-asyncio` führt die Async-Tests weiterhin aus.

`pyproject.toml` setzt bereits `addopts = "-q"`. Kein zweites `-q` ergänzen,
sonst fehlt die Abschlusszeile. Maschinenlesbare Ergebnisse über
`--junitxml=<pfad>` erzeugen, Erfolg über den Exit-Code prüfen.

### Lint und Typen

`ruff check .` ist verpflichtend; `ruff format` bleibt verboten. Aktive
Regelsets sind `E, F, I, UP, B, SIM`, `E501` ist bewusst ausgenommen.
`F811` ist für pytest-Fixture-Imports unter `tests/` ausgenommen.

`mypy` bewacht die bereits sauberen Kernmodule. Der ergänzende
`scripts/check_mypy_baseline.py` entfernt die alten Modulunterdrückungen und
prüft den gesamten Kern sowie die in `HA_BOUNDARIES` genannten kritischen
HA-Module. `scripts/mypy_baseline.json` dokumentiert jede bekannte Diagnose
mit Pfad und Symbol. Neue Fehler und nicht entfernte, bereits behobene
Einträge lassen CI scheitern. `--write-baseline` ist ausschließlich für eine
bewusst geprüfte Schuldenänderung gedacht. Keine pauschalen `Any` oder Casts,
um Meldungen kosmetisch zu verstecken.

---

## 4. Test-Konventionen: was ein Test hier beweisen muss

### Die Testabsicht bestimmt den Nachweis

* **Bugfix:** Ein neuer Test muss den alten Fehler semantisch nachweisen.
  Im Parent-Worktree nur die neuen Tests ausführen; Importfehler oder neue
  Signaturen zählen nicht als RED-Beleg für das Verhalten.
* **Feature:** Den neuen Vertrag mit relevanten Grenzen und ungültigen Eingaben
  prüfen; erwartete Ergebnisse unabhängig vom Produktionsalgorithmus bestimmen.
* **Refactoring:** Gleichheit alter und neuer Ergebnisse über repräsentative
  und geseedete Eingaben nachweisen.
* **Charakterisierung/Regression:** Darf vorher schon grün sein, wenn ein bisher
  ungesicherter Vertrag oder eine unabhängige Referenz geschützt wird.
  Gezielt eingebaute semantische Fehler können die Testwirksamkeit belegen.

`scripts/mutation_smoke.py` führt drei solche Experimente in einer temporären
Kopie aus: entfernter Gruppen-Clamp, akzeptierte veraltete Leistungssamples und
übersprungene Catchup-Lücken. Die unveränderten Tests müssen zuerst bestehen;
jede Mutation muss dann einen Assertion-Fehler erzeugen. Collection-/Importfehler
zählen nicht. Aufruf: `uv run python scripts/mutation_smoke.py`.

Coverage dient zum Auffinden ungesicherter Entscheidungen, nicht zum Erzeugen
von Tests ohne Verhaltensaussage. Der separate Branch-Bericht hat kein
Prozent-Gate; der vorhandene Statement-Grenzwert bleibt unverändert.

### Bit-Identitäts-Tests für Abwärtskompatibilität

Wenn eine Änderung *verhaltensneutral* sein soll, wird das bewiesen, nicht
behauptet. Muster im Repo:

* `tests/core/test_engine_split_equivalence.py` — hält eine **eingefrorene wörtliche
  Kopie** der Vor-Refactor-Funktion (`_plane_poa_split`) im Testmodul und vergleicht
  über tausende geseedet-zufällige Eingaben mit `==` (bit-exakt, nie `approx`) —
  sowohl die Primitive als auch die komplette `compute_forecast`-Ausgabe.
* `tests/core/test_backfill_parity.py` — schickt einen Satz synthetischer Inputs
  durch **Kernmodul** und **(fetch-gemockte) CLI** und behauptet byte-identische
  Bootstrap-Dicts; einzig `generated_at` wird vorher entfernt.
* Für „Alt-Config bleibt byte-identisch": `SiteConfig.from_dict(x).to_dict() == x`,
  plus `repr()`-Dumps von `horizon.transmittance_at`/`sky_view_factor` aus zwei
  Worktrees diffen.

### Weitere tragende Testarten

| Datei | bewacht |
|---|---|
| `tests/core/test_golden.py` | Sonnenstand + Hay-Davies gegen **pvlib**-Referenzvektoren (`tests/core/reference_vectors.json`, reproduzierbar mit separat gesperrtem Generator-Environment). Toleranzen: 0,5° Winkel, `max(2 W/m², 0,5 %)` POA |
| `tests/core/test_season_regression.py` | Design-Beweis für `tau_points` (Saisondrift der abgelösten τ(az)-Rampe) |
| `tests/test_store_v2.py`, `tests/test_store_v3_migration.py` | Store-Migrationen, additiv + byte-treu (§5.2) |
| `tests/test_config_flow_validation.py` | jeder Fehlercode des Validators hat einen Übersetzungsschlüssel; `de.json`/`en.json` sind schlüsselgleich; jeder `translation_key` einer Entität ist übersetzt |
| `tests/core/test_dashboard_yaml.py` | ausgeliefertes Dashboard nutzt nur Built-in-Karten und referenziert die tragenden Entity-IDs (fängt Umbenennungen in `sensor.py`) |
| `tests/test_frontend_harness.py` | fährt die echte Karten-JS unter minimalen DOM-Stubs in **Node**; skippt, wenn `node` nicht im PATH ist |


Golden-Referenzen reproduzieren:

```bash
uv run --script --locked scripts/generate_reference_vectors.py --check
```

Der Generator nutzt pvlib 0.15.2 und explizite Solarposition-/Hay-Davies-Parameter.
Eingaben stehen in `scripts/reference_inputs.json`, die optionale Umgebung in
`scripts/generate_reference_vectors.py.lock`. `--write` regeneriert die
Referenzdatei nach einer bewussten Modell-/Eingabeänderung; numerische Diffs
prüfen. Normale Tests benötigen pvlib nicht.

### Post-Deployment-Validierung: `scripts/validation/`

Die pytest-Suite beweist Code-Verhalten; ob ein Deployment auf der **echten
Anlage** wirkt, prüft dieses separate stdlib-Werkzeug (nicht Teil der Suite, kein
Import aus `custom_components/`). Vier Module mit klarer Rollenteilung — die drei
`bsf_*`-Module importieren sich **flach** gegenseitig, das Skript ist also aus
`scripts/validation/` heraus aufzurufen:

| Datei | Rolle |
|---|---|
| `validate.py` | CLI + Orchestrierung + Report. `--offline --data-dir <pfad>` **oder** `--ha-url` + `--token` (live); optional `--days` (Default 8), `--eta`, `--entry-id`, `--json <datei>`, `--fetch-only`. Ruft `fetch_all` → `load_bundle` → `run_all` → `render`. Exit-Code **0 = vollständig PASS, 1 = WARN oder INCOMPLETE, 2 = FAIL oder ERROR** (auch Fetch-/Datenfehler) — CI-/Skript-tauglich. Enthält selbst **keine** Prüflogik |
| `bsf_fetch.py` | nur im Live-Modus. REST per `urllib` (`/api/states`, `/api/history/period`, Aktion `get_issued_forecast` mit `?return_response` je Tag, `/api/diagnostics/config_entry/<id>`) plus ein **minimaler RFC-6455-WebSocket-Client** für `recorder/statistics_during_period` (`period: hour` und `5minute`, `types: ["mean"]`) — Langzeitstatistiken gibt es nicht über REST. Diagnostics sind optional/non-fatal |
| `bsf_data.py` | Laden + Normalisieren in die `Bundle`-Dataclass und die Semantikfallen zentral erschlagen: epoch-**Millisekunden** der Recorder-WS-API automatisch erkannt, `minimal_response`-Historien expandiert, Zeitzone Europe/Berlin mit eigener EU-DST-`tzinfo` als Fallback (Windows ohne `tzdata`), Erkennung **partieller Tage**, dazu die Feature-Erkennung (`hourly_wh_ac`, `cloud_class_by_hour`, `clamped`-Flag, Quantil-Bins in den Diagnostics) samt dokumentierter Fallbacks |
| `bsf_checks.py` | der Prüfkatalog **C1–C8** (Morgen-Peak, Scalar-Hygiene, Morgen-Physik, Bias-Konvergenz, day-0-Bänder, Headline-Stabilität, Vorabend-Prognose, Regressionswachen) inklusive aller Schwellen und Baseline-Konstanten |

**Snapshot-/Bundle-Format.** Ein Pull-Ordner enthält genau fünf JSON-Dateien:
`actuals_hourly_stats.json`, `fiveminute_stats.json`, `entities_now.json`,
`forecast_sensor_history.json`, `issued_forecasts_and_diag.json`. `load_bundle`
liest sie tolerant: eine fehlende Datei erzeugt eine **Notiz** im Report und lässt
die davon abhängigen Checks entfallen, statt zu crashen.

**Offline vs. Live.** Live schreibt zuerst denselben Snapshot (Default-Ordner
`bsf_pull_<zeitstempel>`) und analysiert ihn danach — die Analyse läuft in beiden
Modi über exakt denselben Pfad, jeder Live-Lauf ist als Offline-Lauf
reproduzierbar (`--fetch-only` trennt beides bewusst).

**Neuen Check ergänzen.** Eine Funktion `check_c9(b: Bundle, eta: float) ->
CheckResult` schreiben, Messwerte mit `c.add(name, value, threshold, status)`
anhängen (`_band(...)` liefert PASS/WARN/FAIL aus einem Pass- und einem
Warn-Intervall), eine `interpretation` setzen, `return c.finalize()` — `finalize`
nimmt den **schlechtesten** bewerteten Status. Dann in die Liste `ALL_CHECKS`
eintragen und bei einem Pflichtcheck `REQUIRED_CHECK_IDS` ergänzen; Renderer
und JSON-Report übernehmen den neuen Check automatisch. Fehlende Daten mit `SKIP`/`INFO` quittieren, nie als fachlichen `FAIL` — nur
PASS/WARN/FAIL bewerten den fachlichen Check; ERROR kennzeichnet einen internen Fehler. `run_all` fängt Exceptions je Check ab und
meldet ihn als `ERROR`: die übrigen Checks laufen weiter, aber der Gesamtstatus
ist nicht erfolgreich. `summarize` verlangt C1–C8 mit auswertbaren Belegen;
fehlende Checks oder Pflichtbelege ergeben `INCOMPLETE`, niemals PASS.

**Eichung der Baselines.** Die Schwellen sind gegen die **VOR-Fix-Woche
17.–24.07.2026** kalibriert: auf diesem Referenzpaket muss `--offline` C1–C7 auf
FAIL und C8 auf PASS bringen (Exit 2) — der Nachweis, dass die Checks die bekannten
Defekte erkennen und die Regressionswachen nicht fälschlich anschlagen. Die harten
Zahlen stehen als benannte Konstanten in `bsf_checks.py`
(`BASELINE_MIDDAY_RAW_OVER_ACT`, `BASELINE_M4M8_MORNING_WH`) plus `DEFAULT_ETA`
(Victron-gelernter DC→AC-Wirkungsgrad, nur Fallback solange `get_issued_forecast`
kein `hourly_wh_ac` liefert; per `--eta` überschreibbar). **Achtung:** das
Referenzpaket (`hadata/`) liegt **nicht im Git** — ohne es ist der
Kalibrierungs-Selbsttest nicht nachspielbar; ein frischer Live-Pull ersetzt es als
Datenquelle, nicht als Eich-Nachweis. Der versionierte synthetische Generator
`tests/helpers/validation_bundle.py` sichert PASS/FAIL/INCOMPLETE/ERROR als
Softwarevertrag über den CLI-Pfad; er ersetzt keine Anlagenkalibrierung. Details und die Nachjustierungs-Matrix
(„wenn ein Check rot bleibt") stehen in `scripts/validation/README.md`.

---

## 5. Contracts: was du mitziehen musst

### 5.1 `docs/SPEC.md` ist der Vertrag

Die SPEC ist keine Hintergrundlektüre, sondern die Quelle, gegen die der Code
geschrieben ist; Code-Kommentare zitieren sie (`SPEC §4`, `SPEC §9.1`, …).

* Die SPEC ist eine **Ist-Spezifikation**: sie beschreibt ausschließlich das
  Verhalten der aktuellen Version. Ihr Kopf trägt „Gilt für Version: <X>" und
  wird maschinell gegen `const.INTEGRATION_VERSION` geprüft.
* Jede Verhaltensänderung **im selben PR** in der SPEC nachziehen.
* Neues Verhalten wird **thematisch einsortiert** — als Unterabschnitt am Ende
  des zuständigen §, oder als neuer Top-Level-§ mit thematischem Titel. Kein
  versionierter Nachtrag, kein „seit v0.x" im Text.
* Abschnittsnummern sind seit der Neufassung (0.23.x) **append-only**. Ändert
  sich doch einmal die Gliederung, **korrigiere die `SPEC §…`-Zitate im Code** —
  diese Kommentare sind tragend.
* Historie, Herleitung und die Zuordnung der **alten** Abschnittsnummern stehen
  in `docs/HISTORIE.md` (nicht normativ); Release-Chronik in `CHANGELOG.md`.

**SPEC-Landkarte** (wo steht was — Abschnittsnummern aus `docs/SPEC.md` in der
aktuellen Fassung):

| Thema | SPEC-Abschnitt(e) | Inhalt dort |
|---|---|---|
| Vertrag, Änderungsregeln, Wächter | **§1**, **§21** | Versionsstempel, Wegweiser, Änderungsregeln, die neun Guards von `tests/test_spec_integrity.py` |
| Architektur, Modulschnitt, Takte | **§2** | HA-freier Kern, stdlib-only, Generik, Modulkarte, Fetch-/Rechen-/Nightly-Kadenz |
| Wetterbezug | **§3** | Open-Meteo-Call, Schema-Validierung, Last-Good-Cache, Retry, Budget |
| Physik | **§4** | Sonnenstand, Haurwitz/k_c, Hay-Davies, IAM, `bifacial_beam_gain`, Albedo, Intervallsemantik |
| Horizont & SVF | **§5** | Feldsemantik der Horizontzeilen, `tau_points`, `diffuse_tau`, Laub-Rampe, halbtransparenter SVF |
| Elektrik / DC→AC | **§6** | Ross, η, `clamp_groups_ac`, Re-Clamp, Trennung DC-Lernen / AC-Ausgabe |
| Config-Schema | **§7** | `site`/`planes`/`horizon`/`groups`-Tabellen, Fehlercodes, Fingerprint + Reseed, `DEFAULT_SITE` samt bekannter Mängel |
| Wetterklassen & Zeitbinnung | **§8** | `classify_cloud`, `CLASSIFIER_VERSION`, Sonnenzeit-Tagesabschnitte, Zellschlüssel |
| Lernschichten | **§9** | Shademap, Pooling, `suggest_shade_groups`, Intraday-Skalar, Day-ahead-RLS, η-Kalibrierung, Nightly-Job, Schutzmechanismen |
| Lern-Sichtbarkeit | **§10** | Messkanal-Präsenz, Verwurfssträhne, Repair-Issues, Anlaufphase |
| Unsicherheit / Bänder | **§11** | Quantilring und Gates, Servieren, Ensemble-Hüllkurve (Standard AUS) |
| Bootstrap / Backfill | **§12** | `run_bootstrap`, `scripts/backfill.py`, gemeinsamer Kern, Import-Semantik, Quantil-Seeding |
| Degradationsleiter | **§13** | frisch → Last-Good-Cache → reine Physik → `unavailable`, jede Stufe sichtbar |
| Konsumenten-Schnittstellen | **§14** | Sensor-Namen, AC-Standard vs. `*_dc`, Headline-Semantik, Mess-Sensoren, Diagnostics-Dump, Energy-Hook, Statusehrlichkeit |
| Scoreboard | **§15** | Metrikdefinitionen, Fairness/Leakage, Sensorik |
| Store / Persistenz | **§16** | Schema v3 + Migrationsinvariante, Ringe, Schreibsemantik, Lade-Robustheit |
| Verschattungsprofil | **§17** | Entitäten, engine-exakte Semantik, `core/shadeprofile.py`-Tunables |
| Dashboard / Karten | **§18** | Referenz-YAML, `install_dashboard`, die zwei gebündelten Karten, Auslieferung |
| Aktionen (Services) | **§19** | vollständiges Inventar, Registrierung, Lese-/Schreibgrenze |
| Konventionen / Checkliste | **§20** | Azimut 0=N, Neigung, Inbetriebnahme-Checkliste |

### 5.2 Store-Schema-Migrationen

`store.py` trennt zwei Versionen sauber:

* Der **äußere HA-`Store`-Envelope** `STORAGE_VERSION` bleibt **für immer 1**.
* Migriert wird die **innere** Schemaversion unter dem Schlüssel
  `schema_version` (`STORAGE_DATA_VERSION_V2` / `_V3`).

Einstiegspunkt ist `store.validate_state`: nicht-dict → leerer Neutralzustand, v1 →
`_migrate_v1_to_v2` → `_migrate_v2_to_v3`, v2 → `_migrate_v2_to_v3`, v3 →
`_validate_v3`, unbekannt/zukünftig → verworfen mit Warnung. **`validate_state`
wirft nie** — „validate-and-clamp beim Laden" (SPEC §16.4): jede Lerner-Sektion geht
durch ihr `from_dict`, das kaputte Werte auf neutrale Defaults klemmt, statt Setup
zu killen.

Regeln für eine neue Migration:

1. **Additiv.** Jeder alte Schlüssel wird byte-treu durchgereicht, neue Sektionen
   werden leer/neutral injiziert. Die Live-Instanz hat einen *populierten* Store
   (Shademap mit hunderten Bins, Bias-Zellen, Drift, Rollback-Ring) — ein Reset ist
   ein kritischer Fehler, kein Schönheitsfehler. Envelope **nicht** anfassen.
2. Test nach dem Muster `tests/test_store_v3_migration.py`: ein realistisch
   populiertes Alt-Dict migrieren und Feld für Feld auf Erhalt prüfen, plus
   Korruptions-Fälle („eine Sektion kaputt → nur diese wird neutral").
3. Kleine additive Felder *innerhalb* einer Version sind erlaubt, wenn sie neutral
   defaulten — so kam `inverter_cal_state` in v3 dazu, ohne Bump.

`ingest_bootstrap` ist die einzige Store-Funktion, die absichtlich wirft
(`ValueError`): bei falschem `BOOTSTRAP_SCHEMA_VERSION`, Nicht-Dict oder
abweichender **Site-Signatur**. Alles innerhalb eines wohlgeformten Payloads wird
geklemmt, nie abgelehnt.

### 5.3 Optionale Config-Felder: „nur wenn gesetzt" serialisieren

Muster (`core/types.py`, `HorizonRow.to_dict`, ebenso `PlaneConfig.to_dict` für
`shade_group` und `SiteConfig.to_dict` für Meter, `albedo`, `bifacial_beam_gain`):

```python
def to_dict(self) -> dict:
    d: dict = {CONF_HZ_AZIMUTH: self.azimuth_deg, ...}   # Pflichtfelder immer
    if self.tau_points:                                   # optional: NUR wenn gesetzt
        d[CONF_HZ_TAU_POINTS] = [[el, t] for el, t in self.tau_points]
    if self.diffuse_tau is not None:
        d[CONF_HZ_DIFFUSE_TAU] = self.diffuse_tau
    return d
```

**Begründung (Alt-Config-Bit-Identität):** Eine Konfiguration, die das neue Feld nie
gesetzt hat, muss nach dem Upgrade **exakt dasselbe Dict** ergeben wie vorher — kein
neuer Schlüssel, kein `null`. Sonst ändert sich die serialisierte Form, der
Config-Fingerprint kippt und die Lernschichten werden ohne fachlichen Grund
zurückgesetzt. `from_dict` liest komplementär mit `d.get(...) is None → None`.

### 5.4 Fingerprint-Pflicht bei neuen physikrelevanten Feldern

Zwei Digests im `coordinator.py`, nicht verwechseln:

| Methode | Inhalt | Zweck |
|---|---|---|
| `_site_signature` | gerundete Lat/Lon + Ebenennamen | verhindert den Import eines Bootstraps, der für einen **anderen Standort** gebaut wurde |
| `_config_fingerprint` | alle Felder, die die **modellierte Kurve** formen | löst Re-Seed der Day-ahead-Bias-Zellen aus, wenn die Geometrie sich geändert hat |

`_config_fingerprint` deckt aktuell ab: je Ebene **Name**/Azimut/Tilt/Wp/
`efficiency`/`ross_coeff` (`_plane_sig`) und **jede Horizontzeile** (`_hz_row`:
**Azimut** als erstes Feld, Elevation, `tau`, `seasonal`, `tau_leafed`,
`tau_bare`, `tau_points`, `tau_points_bare`, `diffuse_tau`), dazu `albedo`,
`bifacial_beam_gain`, Standortkoordinaten, Shade-Pool-Mitgliedschaft und jede
Inverter-Gruppe mit Mitgliedern, `ac_limit_w` und konfiguriertem Wirkungsgrad,
sowie `CLASSIFIER_VERSION` (Wolkenklassen-Taxonomie). Die gemeinsame Funktion
liegt in `core/config_fingerprint.py::site_fingerprint`; der Coordinator
delegiert und die Rekonfigurationsvorschau benutzt denselben Vertrag.
Bewusst **nicht** enthalten: Entity-IDs, Shade-/Inverter-Gruppenlabels und
Meter-Vorzeichen. **Achtung:** `name` bleibt die stabile Modul-/Kanalidentität;
seine Änderung entspricht einem Entfernen/Anlegen und verändert den
Fingerprint. Für Anzeigeumbenennungen wird ausschließlich das optionale
`display_name` geändert; es bleibt wie Gruppenlabels außerhalb des Fingerprints
und erhält Lernen sowie Archivschlüssel (ADR-0025). Die Vorschau zeigt
die betroffenen Identitäten vor dem Speichern.

Wenn du ein Feld hinzufügst, das die RAW-Kurve verändert, **musst du es hier
hashen** — sonst behalten die Bias-Zellen ein Theta, das auf eine veraltete
Geometrie passt. Beim Hashen die beiden bestehenden Disziplinen einhalten:

* **Runden** (`round(x, 2)` bzw. `round(x, 4)`), damit reine Float-Re-Serialisierung
  den Hash nicht zufällig kippt.
* **Nur-wenn-gesetzt anhängen** (Spiegelbild von §5.3), damit eine Alt-Config, deren
  Kurve byte-identisch bleibt, ihren alten Fingerprint behält und **nicht**
  re-seeded wird. Sentinels kollisionsfrei wählen: `_hz_row` schreibt `,tl{tl}` mit
  `-` für „nicht gesetzt" (→ `tl-`) gegen den gerundeten Wert ohne Trennzeichen
  (`tau_leafed = 0.5` → `tl0.5`) — der Sentinel darf nie **Präfix** eines Werts sein.

`_reconcile_config_fingerprint` läuft beim Setup: `stored is None` → nur speichern
(ein bestehender Install wird durch die *Einführung* des Features nicht bestraft);
`stored != current` → `bias.reseed_day_ahead_bias` (n gedeckelt auf
`DAY_AHEAD_BIAS_RESEED_N`), neuen Fingerprint speichern, INFO loggen und ein
Repair-Issue (`ISSUE_CONFIG_CHANGED_BIAS_RESEED`) stellen.

---

## 6. Release-Prozess (exakt)

### 6.1 Metadaten und Reihenfolge

Die Version steht synchron in `manifest.json` (`version`), `pyproject.toml`
(`[project] version`) und `const.py` (`INTEGRATION_VERSION`). Die SPEC trägt
denselben Versionsstempel. HACS installiert den Zipball des Tags; alle Angaben
müssen im getaggten Commit stimmen. `_frontend.py` nutzt die Version zusätzlich
für das Cache-Busting der gebündelten Karten.

1. Release-PR mit den drei Versionswerten, SPEC-Stempel, datiertem
   `## [x.y.z] - YYYY-MM-DD`-Changelog und betroffenen Docs vorbereiten.
2. Nach Review und grüner PR-CI nach `main` mergen. Den erfolgreichen
   **Push-Lauf** von `validate.yml` für genau den entstandenen Commit abwarten.
3. Den Workflow **Release** von `main` manuell starten: Version ohne `v`,
   vollständiger Commit-SHA mit 40 Zeichen.
4. Read-only-Preflight prüft Checkout, Main-Abstammung, alle Metadaten und den
   jüngsten Validate-Push-Lauf dieses SHAs. Ein älterer grüner Lauf ersetzt
   keinen neueren fehlgeschlagenen oder noch laufenden Versuch.
5. Erst der Publish-Job erhält Schreibrechte, wiederholt die Prüfungen und
   erzeugt Tag und Release mit den passenden Changelog-Notizen. Tags werden
   niemals erzwungen verschoben.

Umsetzung: `.github/workflows/release.yml` und `scripts/release_guard.py`.
Keine manuellen Tags/Veröffentlichungen vor diesem Gate. Ein Metadaten-Bump
nach der Veröffentlichung korrigiert den ausgelieferten Tag nicht.

### 6.2 CI-Jobs (`validate.yml`)

Alle Actions sind per Commit-SHA gepinnt. Top-Level gilt
`permissions: contents: read`; zusätzliche Rechte werden pro Job vergeben.
Setup verwendet `uv sync --locked --group dev`, spätere Befehle
`uv run --no-sync`. Eine veraltete Lockdatei fällt auf, statt still neu
aufgelöst zu werden.

| Job | Nachweis |
|---|---|
| `validate-hacs` | HACS-Struktur, für öffentliche Repos (Upstream liest Dateien ohne Auth) |
| `validate-hassfest` | Manifest-/HA-Struktur-Konformität |
| `spec-reminder` | nur PR-Hinweis; der harte SPEC-Vertrag läuft in pytest |
| `lint` | Ruff, sauberer mypy-Scope, expliziter Typfehler-Ratchet, AST-Importgrenze, synchrone Versionen |
| `tests` | portable Suite + Node-Harness, **95 % Statement-Coverage**, XML-Artefakt |
| `branch-coverage` | eigener Branch-Bericht ohne Prozent-Gate und drei semantische Mutationstests |
| `ha-lifecycle` | echte HA-Instanz ohne Unit-Shims, getrennter Linux-Lauf |
| `tests-ha-min` | portable Suite und echter HA-Lifecycle gegen **exakt** den Wert aus `hacs.json` |
| `windows-bootstrap` | PowerShell-Smoke mit vorhandenen uv-/Python-Launcher-Varianten, ohne Installation |
| `golden-reproduction` | nächtlich/manuell: Referenzen mit separat gesperrtem pvlib-Environment reproduzieren |
| `devcontainer` | Container bauen, portable Suite + Ruff + mypy darin ausführen |

`hacs.json` deklariert `2026.3.0`. Der Minimum-Job installiert diese exakte
Version nach dem normalen Setup und verwendet anschließend `--no-sync`, damit
uv nicht wieder die Lockfile-HA installiert. PHACC bleibt deaktiviert; dessen
HA-Pin ist nur für das reguläre Dev-Environment maßgeblich. Den Floor bewusst
anheben oder erst nach einer erfolgreichen Gegenprobe senken.

---

## 7. Konventionen

**Commit-Messages.** `<typ>: <Betreff> (<Kontext>)` — im Repo verwendete Typen:
`feat`, `fix`, `test`, `refactor`, `revert`, `chore` (`docs:` kommt nicht vor;
Doku-Änderungen laufen unter `feat`/`fix` der jeweiligen Tranche). Der Kontext in
Klammern nennt die Tranche/das Thema, z. B. `(0.23 run_bootstrap service)` oder
`(forensik A4 B2)`; Release-Commits heißen `chore: release vX.Y.Z` (ältere) bzw.
`feat: release X.Y.Z … (X.Y release)` (neuere). Danach ein **Body in Fließtext**,
der erklärt *was* und *warum* (deutsch oder englisch — beides kommt vor, innerhalb
einer Message konsistent bleiben). Co-Author-Trailer sind optional und nennen
nur tatsächlich beteiligte Personen oder Werkzeuge. Es gibt keine Verpflichtung
auf ein bestimmtes KI-Werkzeug oder eine personengebundene Signatur.

**Branches.** `feat/<version-oder-thema>`, `fix/<thema>`, gelegentlich
`release/v<x.y.z>` — Beispiele: `feat/0.23-run-bootstrap-service`,
`feat/0.22-elevation-tau-diffuse-floor`, `fix/forensik-2026-07-24`. Nie direkt auf
`main` committen; `main` wird über PR-Merges bewegt — **seit PR #40** als echte
Merge-Commits (`Merge pull request #51 from …`), #1–#39 waren Squash-Merges
(Betreff-Suffix `(#39)`), die ersten Commits gingen direkt auf `main`. An der
**aktuellen Praxis** orientieren, nicht an der Historie.

**Kein Force-Push.** Auf `main` niemals; auf Feature-Branches nur, wenn niemand
sonst darauf arbeitet — im Zweifel ein zusätzlicher Commit statt umgeschriebener
Historie. (Betriebs-Konvention, kein Branch-Schutz im Repo.)

**PR-Checkliste** (`CONTRIBUTING.md` §7): volle Suite grün · `ruff check` sauber ·
CHANGELOG-Eintrag unter `[Unreleased]` · SPEC aktualisiert bei Verhaltensänderung,
inklusive verschobener `SPEC §…`-Zitate.

---

## 8. Praktische Fallen

**Windows / PowerShell**

* `make` ist auf der Betreibermaschine nicht garantiert. Ersatz:
  `.\scripts\setup-env.ps1` für das Setup, danach `uv run …` für Prüfungen.
* Tests mit `uv run pytest …` ausführen, nicht mit einem globalen `pytest`,
  damit das gesperrte Projekt-Environment verwendet wird.
* `-p no:homeassistant` ist auf Windows **nicht optional** (`fcntl`, siehe §3).
* PowerShell kennt kein `&&`/`||`-Verketten (Windows PowerShell 5.1): `A; if ($?) { B }`.
* Kein zusätzliches `-q` (§3) — sonst fehlt die Ergebniszeile.

**Floats beim Erzeugen von Config-YAML/JSON — niemals runden**

Beim Bauen von Site-Konfigurationen (Kampagnen-YAML, `site.json` für den Backfill,
Testfixtures) Werte **unverändert** durchreichen: Pythons `str(float)` /
`repr(float)` ist round-trip-exakt. Das Kampagnen-Skript der 0.22-Umstellung setzt
dafür extra `num = str  # Python float repr is round-trip exact; never round
coordinates`. Rundest du stattdessen, verschiebst du reale Geometrie (Koordinaten,
τ-Knoten, Azimute): die Physik ändert sich still, und der `_config_fingerprint` kann
kippen und ungefragt die Bias-Zellen re-seeden. Gegenprobe nach dem Erzeugen:
`SiteConfig.from_dict(parsed).to_dict() == SiteConfig.from_dict(original).to_dict()`
— plus die Prüfung, dass optionale Schlüssel in beiden Dicts gleich
vorhanden/abwesend sind (§5.3).

**Diagnostics: DC vs. AC nicht verwechseln**

In `diagnostics.py::_forecast_summary` heißen die Tagessummen seit **v0.21.0**
getrennt (der Docstring dort nennt „v0.20.7" — das ist eine interne Review-Marke,
kein Release; das CHANGELOG springt von 0.20.6 auf 0.21.0):
`daily_kwh_dc` ist die **DC**-Seite (modellintern, Wahrheit für Lerner und
Scoreboard), `daily_kwh_ac` die servierte **AC**-Schwester (nach
Wirkungsgrad/AC-Clamp). Vorher war beides ein einziges `daily_kwh`, das gegenüber
der betreiberseitigen AC-Energie ~8 % zu hoch las. Wer in einer Analyse DC-Zahlen
gegen AC-Messwerte hält, produziert genau diesen Fehler wieder — beim Lesen von
Diagnostics-Dumps immer auf das Suffix achten. Dieselbe Trennung gilt bei den
Entitäten (siehe `04-ha-integration-entities-services.md`).

**Weitere Kleinigkeiten**

* `scratchpad/` und `.ha-dev/` sind git-ignoriert — Analyse-Artefakte dort sind
  **Hinweise, keine Belege**; für jede Verhaltensaussage den Code lesen.
* Neue Fehlercodes des Site-Validators brauchen sofort Einträge in `de.json`
  **und** `en.json`, sonst fällt `tests/test_config_flow_validation.py`.
* Neue oder umbenannte Entitäten im ausgelieferten Dashboard brauchen einen
  Abgleich mit `dashboards/balcony_solar_forecast.yaml`.
