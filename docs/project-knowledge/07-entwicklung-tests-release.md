# Entwicklung, Tests & Release

**Worum es geht:** Die Arbeitsanleitung für Code-Änderungen an
`balcony_solar_forecast` — Repo-Layout, die harte HA-Freiheits-Regel für `core/`,
wie die Testsuite aufgebaut ist und mit welchem Kommando sie auf Windows läuft, was
als *Vertrag* gilt (SPEC, Store-Schema, Config-Serialisierung, Fingerprint) und wie
ein Release exakt abläuft.
**Wann du es brauchst:** Bevor du die erste Zeile änderst, und noch einmal bevor du
taggst. Fachliche Inhalte stehen in `01`–`06`; hier geht es ausschließlich um
Handwerk und Prozess.

Stand: `main` @ **v0.23.0** (2026-07-25). Belege sind *Datei + Funktions-/
Konstantenname* (keine Zeilennummern — die veralten sofort). Zusätzlich verbindlich:
`CONTRIBUTING.md` im Repo-Root.

> **Update 2026-07-30 — Dev-Setup 2026 (uv):** Das Dev-Environment läuft jetzt
> über **uv** mit committetem `uv.lock` (einzige Wahrheit für Tool-Versionen,
> auch in CI; `uv.lock` wird bewusst **nicht** gitignoriert). Setup:
> `uv sync --group dev` (bzw. `make install` — das Makefile ist nur noch eine
> dünne Hülle um uv; `scripts/setup-env.{sh,ps1}`/`scripts/setup_env.py`
> installieren uv, falls es fehlt). Python ist **3.14** (`.python-version`,
> `requires-python >= 3.14.2`), HA-Floor der Dev-Tools `homeassistant>=2026.7.4`;
> `pytest-homeassistant-custom-component` bleibt als einziges Paket voll gepinnt,
> weil es die HA-Kopplung führt (Begründung im `pyproject.toml`-Kommentar).
> Alle Kommandos heißen jetzt `uv run …` (`uv run pytest tests
> -p no:homeassistant`, `uv run ruff check .`, `uv run mypy`) — die
> Kommando-Blöcke in §3 und die CI-Tabelle in §6 unten zeigen noch den
> Pip-Vorgänger und sind durch diesen Kasten ersetzt (in 00 bereits
> aktualisiert). Neu dazu: **mypy-Baseline** auf `core/` (acht ältere
> Module mit bekannten Fehlern sind im `[tool.mypy]`-Kommentar namentlich
> ausgenommen, die übrigen müssen sauber bleiben), **Coverage**-Config in
> `pyproject.toml` (zunächst report-only; inzwischen mit 95-%-Gate in CI,
> siehe §6.3), **Devcontainer**
> (`.devcontainer/`, Python-3.14-Image + Node-Feature für den JS-Harness in
> `tests/harness/`, postCreateCommand = `pip install uv && uv sync --group dev`),
> **pre-commit** nur mit `ruff-check --fix` (kein `ruff-format` — Verbot
> unverändert), `.editorconfig`/`.gitattributes` (LF-Zwang, `brand/*.png`
> binär), CI auf `checkout@v7`/`setup-uv@v9.0.0` mit uv-Cache plus einem
> `devcontainer`-Job, der die volle Suite im Container fährt. An den Regeln
> dieses Dokuments (HA-Freiheit, SPEC-Vertrag, `-p no:homeassistant`, kein
> `ruff format`, drei Versionsstellen) ändert das nichts.

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
| `scripts/setup_env.py` | pure-stdlib `.venv`-Bootstrap; einzige Implementierung hinter `make` und `setup-env.{sh,ps1}` | |
| `tests/` · `tests/core/` | HA-Layer-Tests · reine Kern-Tests | §3 |
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
   3.13 — kein HA-Setup, keine Event-Loop, keine Fixtures.
2. **Wiederverwendung im Backfill.** `scripts/backfill.py` importiert den Kern über
   einen Namespace-Package-Shim direkt aus dem Repo (ohne das HA-importierende
   Paket-`__init__`). Seit **0.23** liegt auch die Bootstrap-Rekonstruktion dort
   (`core/bootstrap_build.py`), damit CLI und In-App-Aktion `run_bootstrap`
   **dieselbe** Mathematik ausführen.
3. **Keine Runtime-Dependencies.** `manifest.json` hat `requirements: []`; im Kern
   erscheinen ausschließlich stdlib-Importe (`math`, `dataclasses`, `datetime`,
   `functools.lru_cache`, `logging`, `hashlib`, `json`, `pathlib.Path`,
   `collections.abc`, `zoneinfo`) plus `from __future__ import annotations` —
   `typing` braucht der Kern nicht (PEP 604/585: `X | None` statt `Optional[X]`).
   Kein numpy/pandas/pvlib — auch nicht „nur kurz".

**Die einzige dokumentierte Ausnahme:** `core/openmeteo_backfill.py` ist das eine
Kern-Modul, das Netzwerk anfasst. Es importiert `aiohttp` **lazy innerhalb** der
Fetch-Funktion und bekommt die Session injiziert (CLI: eigene
`aiohttp.ClientSession`; Aktion: `aiohttp_client.async_get_clientsession(hass)`).
Es bleibt HA-frei — genau deshalb können beide Aufrufer nicht auf dem
Provider-Vertrag auseinanderdriften.

### Prüfkommando

```powershell
# muss LEER sein (nur Kommentare/Docstrings dürfen das Wort enthalten):
Select-String -Path custom_components\balcony_solar_forecast\core\*.py,`
                    custom_components\balcony_solar_forecast\const.py `
             -Pattern '^\s*(import|from)\s+homeassistant'
```

Bash-Äquivalent: `grep -rnE '^\s*(import|from)\s+homeassistant' custom_components/balcony_solar_forecast/core/ custom_components/balcony_solar_forecast/const.py`

Ergänzend die Fremd-Import-Prüfung — sie **muss führenden Whitespace zulassen**, auf
Spalte 0 verankert entgeht ihr sonst der Lazy-Import im Funktionsrumpf:
`grep -rhE '^[[:space:]]*(import [A-Za-z_]|from [A-Za-z_][A-Za-z0-9_.]* import )' custom_components/balcony_solar_forecast/core/*.py | grep -vE 'from \.' | sed 's/^[[:space:]]*//' | sort -u`
Einziger zulässiger Nicht-stdlib-Treffer: `import aiohttp  # lazy` in `core/openmeteo_backfill.py`.

`make test-core` allein beweist die HA-Freiheit **nicht**, wenn HA im `.venv`
installiert ist (der Import würde einfach gelingen). Die Greps sind der Beweis.

---

## 3. Tests: Aufbau und Kommandos

### Umfang (gemessen am aktuellen Stand)

| Menge | gesammelt | grün | übersprungen |
|---|---|---|---|
| `tests/` gesamt | 2480 | 2240 | 240 |
| davon `tests/core/` | 1810 | 1570 | 240 |
| daraus HA-Layer (`tests/*.py`) | 670 | 670 | 0 |

Die 240 Skips sind **keine Lücken**: es sind die Parametrisierungen von
`tests/core/test_golden.py` mit Sonnenhöhe ≤ `LOW_SUN_CUTOFF_DEG` (3°), wo bewusst
keine pvlib-Gleichheit behauptet wird (eigener „no-explosion"-Test). Gesamtlaufzeit
~25 s (Kern allein ~10 s).

### Struktur

* **`tests/core/`** — reine Kern-Tests. `tests/core/conftest.py` registriert
  `balcony_solar_forecast` und `balcony_solar_forecast.core` als *Namespace-Pakete*,
  die auf die echten Verzeichnisse zeigen, **ohne** das HA-importierende
  Root-`__init__.py` auszuführen. Deshalb laufen sie auch ohne HA-Installation.
* **`tests/`** — HA-Layer. Unit-Stil gegen Fakes/`monkeypatch`; es wird nie eine
  echte `hass`-Instanz gestartet. `tests/conftest.py` lädt zusätzlich `const`,
  `core.types` und `fetcher` per `importlib` unter einem synthetischen Paket, damit
  die reinen Fetcher-Tests ohne HA laufen. Module, die HA wirklich brauchen
  (`store.py` u. a.), skippen sich, wenn HA fehlt.

### Kommandos

```powershell
# Windows / PowerShell, ohne make:
.\.venv\Scripts\python.exe -m pytest tests -p no:homeassistant           # volle Suite
.\.venv\Scripts\python.exe -m pytest tests\core -p no:homeassistant      # nur Kern
.\.venv\Scripts\python.exe -m ruff check .                               # Lint
```

Mit `make` (ruft `scripts/setup_env.py` auf, identisch auf allen OS):
`make install` · `make test` · `make test-core` · `make lint` ·
`make format` (= `ruff check --fix .`, **nicht** `ruff format`) · `make clean`.

### Warum `-p no:homeassistant`?

Das Plugin `pytest-homeassistant-custom-component` (PHACC) wird **abgeschaltet**,
weil die Suite es nicht braucht und es zwei Schäden anrichtet (begründet in
`CONTRIBUTING.md` §4 und im Docstring von `scripts/setup_env.py::test`):

* Sein Import zieht das **POSIX-only `fcntl`** — auf Windows nicht importierbar,
  die Sammlung stirbt sofort.
* Seine **autouse**-Fixtures rufen beim Setup `asyncio.get_event_loop()`, was ab
  Python 3.12 für die synchronen Tests wirft.

Da kein Test PHACC-Fixtures benutzt, bricht das Plugin nur eine Suite, die es nie
verwendet. Abgeschaltet läuft die **volle** Suite identisch auf Linux, macOS, WSL
und Windows; `pytest-asyncio` (kommt via PHACC mit) treibt die async-Tests weiter.
CI schaltet das Plugin identisch ab (`.github/workflows/validate.yml`, Job `tests`)
und hängt nur die Coverage-Flags an — **kein** `-q` (der Job-Kommentar begründet
das: `pyproject.toml` setzt bereits `addopts = "-q"`, ein zweites verschluckt die
Ergebniszeile); CI wertet ausschließlich den Exit-Code aus.

### `addopts` enthält bereits `-q`

`pyproject.toml` → `[tool.pytest.ini_options] addopts = "-q"`. Wer auf der
Kommandozeile **noch** ein `-q` anhängt, bekommt effektiv `-qq` — und dann
**verschluckt pytest die Abschlusszeile „N passed"** komplett (empirisch
reproduziert). Genau daran ist in einem Review die Testzahl falsch berichtet
worden. Konsequenz:

* Beim manuellen Lauf **kein** `-q` anhängen.
* Brauchst du eine maschinenlesbare Zahl: `--junitxml=<pfad>` verwenden oder
  `$LASTEXITCODE` prüfen. (CI hängt ebenfalls kein `-q` an und verlässt sich
  auf den Exit-Code.)

### Lint

`ruff check .` ist enforced (CI-Job `lint`), `ruff format` ist **verboten** —
der Code ist absichtlich handformatiert (ausgerichtete Argumentlisten, gesetzte
Umbrüche), deshalb ist `E501` in `[tool.ruff.lint] ignore` bewusst aus.
Aktive Regelsets: `E, F, I, UP, B, SIM`; `tests/**` hat `F811` ausgenommen (das
Fixture-Import-Muster von pytest). Nie „format on save" auf dieses Repo loslassen
und nie Zeilen umbrechen, die du ohnehin nicht anfasst.

---

## 4. Test-Konventionen: was ein Test hier beweisen muss

### Neue Tests müssen den ALTEN Code durchfallen lassen

Ein Test, der auch vor deiner Änderung grün gewesen wäre, beweist nichts. Die im
Projekt etablierte Technik (durchgängig in den Reviews der Forensik-Tranchen T4–T9
angewandt und protokolliert):

1. Wegwerf-Worktree auf den **Parent-Commit** legen:
   `git worktree add ../bsf-parent <parent-sha>`
2. Nur die **neuen Testdateien** dorthin kopieren.
3. Suite im Worktree laufen lassen — die neuen Tests **müssen** fehlschlagen
   (`TypeError` auf neue Signaturen zählt, aber schwächer als ein
   *semantischer* Fehlschlag auf einem diskriminierenden Eingabevektor).
4. Worktree wieder entfernen: `git worktree remove ../bsf-parent`.

Variante für Fixes ohne Signaturänderung: den Einzeiler-Fix im Baum reverten und
prüfen, dass **genau** der neue Test fällt (Mutationstest), danach den Baum
restaurieren und `git status` verifizieren.

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
| `tests/core/test_golden.py` | Sonnenstand + Hay-Davies gegen **pvlib**-Referenzvektoren (`tests/core/reference_vectors.json`, offline in einer Wegwerf-pvlib-venv erzeugt). Toleranzen: 0,5° Winkel, `max(2 W/m², 0,5 %)` POA |
| `tests/core/test_season_regression.py` | Design-Beweis für `tau_points` (Saisondrift der abgelösten τ(az)-Rampe) |
| `tests/test_store_v2.py`, `tests/test_store_v3_migration.py` | Store-Migrationen, additiv + byte-treu (§5.2) |
| `tests/test_config_flow_validation.py` | jeder Fehlercode des Validators hat einen Übersetzungsschlüssel; `de.json`/`en.json` sind schlüsselgleich; jeder `translation_key` einer Entität ist übersetzt |
| `tests/core/test_dashboard_yaml.py` | ausgeliefertes Dashboard nutzt nur Built-in-Karten und referenziert die tragenden Entity-IDs (fängt Umbenennungen in `sensor.py`) |
| `tests/test_frontend_harness.py` | fährt die echte Karten-JS unter minimalen DOM-Stubs in **Node**; skippt, wenn `node` nicht im PATH ist |

### Post-Deployment-Validierung: `scripts/validation/`

Die pytest-Suite beweist Code-Verhalten; ob ein Deployment auf der **echten
Anlage** wirkt, prüft dieses separate stdlib-Werkzeug (nicht Teil der Suite, kein
Import aus `custom_components/`). Vier Module mit klarer Rollenteilung — die drei
`bsf_*`-Module importieren sich **flach** gegenseitig, das Skript ist also aus
`scripts/validation/` heraus aufzurufen:

| Datei | Rolle |
|---|---|
| `validate.py` | CLI + Orchestrierung + Report. `--offline --data-dir <pfad>` **oder** `--ha-url` + `--token` (live); optional `--days` (Default 8), `--eta`, `--entry-id`, `--json <datei>`, `--fetch-only`. Ruft `fetch_all` → `load_bundle` → `run_all` → `render`. Exit-Code **0 = alles grün, 1 = mind. ein WARN, 2 = mind. ein FAIL** (oder Fetch-/Datenfehler) — CI-/Skript-tauglich. Enthält selbst **keine** Prüflogik |
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
eintragen; Renderer, Gesamtübersicht und JSON-Report ziehen sich daraus
automatisch. Fehlende Daten mit `SKIP`/`INFO` quittieren, nie mit `FAIL` — nur
PASS/WARN/FAIL bewerten den Check. `run_all` fängt Exceptions je Check ab und
degradiert ihn zu `SKIP`: ein kaputter Check reißt nie den ganzen Lauf.

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
Datenquelle, nicht als Eich-Nachweis. Details und die Nachjustierungs-Matrix
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
| Scoreboard / Kill-Gate | **§15** | Metrikdefinitionen, Fairness/Leakage, Vergleichsliste, Gate-Schwellen, Sensorik |
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
`bifacial_beam_gain`, jede Inverter-Gruppe mit **Name** und `ac_limit_w`
(`grp:{name}:ac{…}`) sowie `CLASSIFIER_VERSION` (Wolkenklassen-Taxonomie).
Bewusst **nicht** enthalten: Entity-IDs, Shade-Grouping, Meter-Vorzeichen — eine
harmlose Bearbeitung darf nie Lernen zurücksetzen. **Achtung:** Weil Namen
mithashen, kippt schon eine reine **Umbenennung** von Ebene oder Gruppe den
Fingerprint und re-seedet die Bias-Zellen — ebenso ein verschobener Horizont-Azimut.

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

### 6.1 Die Versionsnummer steht an drei Stellen

| Datei | Feld |
|---|---|
| `custom_components/balcony_solar_forecast/manifest.json` | `version` |
| `pyproject.toml` | `[project] version` |
| `custom_components/balcony_solar_forecast/const.py` | `INTEGRATION_VERSION` |

Alle drei müssen **gleich** sein. Zwei Wächter erzwingen das:

* **CI-Guard** — `.github/workflows/validate.yml`, Schritt *Version consistency* im
  Job `lint`: liest die drei Werte und `assert mf == pp == const`. Läuft bei jedem
  Push/PR, nächtlich und manuell.
* **Release-Guard** — `.github/workflows/release.yml`, Schritt *Version guard*:
  vergleicht zusätzlich den **Tag** (`refs/tags/v…`, `v`-Prefix entfernt) gegen die
  drei Strings und lässt das Release fehlschlagen, wenn etwas abweicht.

**Warum der Tag mitgeprüft wird:** `hacs.json` hat `zip_release: false`, HACS
installiert also den **Zipball des Tags**. Was im getaggten Commit steht, ist das,
was bei jedem Nutzer landet. Ein Bump-Commit *nach* dem Tag landet nur auf `main`
und liefert stillschweigend die alte Version aus. `INTEGRATION_VERSION` ist dabei
nicht kosmetisch: `_frontend.py` hängt `?v=<INTEGRATION_VERSION>` an die
Karten-Ressourcen-URLs — das ist der **einzige** Cache-Busting-Mechanismus für die
ausgelieferten Lovelace-Karten; `sensor.py` setzt es als `sw_version` des Geräts.

### 6.2 Reihenfolge

1. Alle drei Versionsstrings synchron bumpen.
2. `CHANGELOG.md`: `[Unreleased]` zu `## [x.y.z] - YYYY-MM-DD` machen (Keep a
   Changelog + SemVer; Rubriken `### Added`/`### Changed`/`### Fixed`, davor ein
   Prosa-Absatz, der das Release in 3–6 Zeilen erklärt — siehe 0.22.0/0.23.0).
3. SPEC-Nachtrag und betroffene Docs (`BACKFILL.md`, `DASHBOARD.md`, `README.md`).
4. Commit, PR, CI grün, Merge nach `main`.
5. **Erst dann** auf GitHub ein Release mit Tag `vx.y.z` publizieren →
   `release.yml` läuft (HACS-Validation, hassfest, Version-Guard).

Bumpen oder CHANGELOG-Pflege *nach* dem Tag ist zu spät.

### 6.3 CI-Jobs im Überblick (`validate.yml`)

Alle Actions sind per **Commit-SHA gepinnt** (der Versionskommentar steht
dahinter, Dependabot zieht die SHAs hoch); Top-Level gilt
`permissions: contents: read`, mehr deklariert ein Job lokal.

| Job | Inhalt |
|---|---|
| `validate-hacs` | `hacs/action` (SHA-gepinnt), `category: integration`, `ignore: brands` (Brand-Assets liegen bewusst lokal unter `custom_components/.../brand/`); läuft nur gegen **öffentliche** Repos (Sichtbarkeits-Guard, weil die Action `hacs.json` ohne Auth von raw.githubusercontent.com lädt) |
| `validate-hassfest` | `home-assistant/actions/hassfest` (Master-SHA gepinnt, hassfest hat keine brauchbaren Release-Tags) — Manifest-/Struktur-Konformität |
| `spec-reminder` | nur bei PRs, **advisory** (`continue-on-error`): warnt, wenn sich `custom_components/` ohne `docs/SPEC.md` ändert — der harte Teil des Vertrags läuft als `tests/test_spec_integrity.py` im `tests`-Job |
| `lint` | `ruff check .` **plus** `mypy` (Baseline auf `core/`) **plus** der Versions-Konsistenz-Check (manifest == pyproject == const) |
| `tests` | `uv run pytest tests -p no:homeassistant --cov --cov-fail-under=95 --cov-report=term-missing:skip-covered --cov-report=xml`; `actions/setup-node` (Node 22) stellt sicher, dass der JS-Karten-Harness (`tests/harness/`) nicht still skippt; das Coverage-XML landet als Build-Artefakt |
| `tests-ha-min` | dieselbe Suite gegen die deklarierte HA-Untergrenze: `uv pip install "homeassistant==2026.3.*"` über das Lockfile drüber (Pin-Konflikt mit PHACC im Job-Kommentar dokumentiert), dann `uv run --no-sync pytest` |
| `devcontainer` | baut `.devcontainer/` und fährt Suite + `ruff check` + `mypy` im Container |

Coverage ist **gegatet**: `--cov-fail-under=95` bricht den `tests`-Job unter 95 %
(Review 0.23.x — die Suite stand bei ~92 %, und jede neue Verhaltensänderung
kommt per CLAUDE.md-Regel 6 mit ihrem Test; das Gate hält das so).

`hacs.json` pinnt `"homeassistant": "2026.3.0"` — die HA-Untergrenze, gegen die die
Config-Flow-Selectors und Entity-Konventionen validiert sind und gegen die der
Job `tests-ha-min` die Suite fährt (ein Floor, der nicht getestet wird, ist
keiner). Bewusst anheben (wenn du eine neuere API brauchst und darauf getestet
hast), **nie** absenken ohne Gegenprobe.

---

## 7. Konventionen

**Commit-Messages.** `<typ>: <Betreff> (<Kontext>)` — im Repo verwendete Typen:
`feat`, `fix`, `test`, `refactor`, `revert`, `chore` (`docs:` kommt nicht vor;
Doku-Änderungen laufen unter `feat`/`fix` der jeweiligen Tranche). Der Kontext in
Klammern nennt die Tranche/das Thema, z. B. `(0.23 run_bootstrap service)` oder
`(forensik A4 B2)`; Release-Commits heißen `chore: release vX.Y.Z` (ältere) bzw.
`feat: release X.Y.Z … (X.Y release)` (neuere). Danach ein **Body in Fließtext**,
der erklärt *was* und *warum* (deutsch oder englisch — beides kommt vor, innerhalb
einer Message konsistent bleiben). Letzte Zeile:

```
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```

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
  `.\scripts\setup-env.ps1` oder `python scripts/setup_env.py <install|test|test-core|lint|format|clean>`.
* Immer `.\.venv\Scripts\python.exe -m pytest …` aufrufen, nicht ein globales
  `pytest` — sonst fehlt HA und die halbe Suite skippt still.
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
