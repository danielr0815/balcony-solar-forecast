# Software-Review — 26.09.2026
Umsetzung: [Zuordnung R01–R44 und Prüfnachweise](2026-09-26-software-review-umsetzung.md).
Die folgenden Befunde beschreiben den geprüften Ausgangsstand.


**Stand:** v0.27.2, Commit `a7991621f93e3631109a5dbc1bd8ae9a09aba47f`.
**Auftrag:** Architektur, menschliche Verständlichkeit, Wartbarkeit, Coding Guidelines, Dokumentationsstand und tatsächlicher Nutzen der Tests.
**Ergebnis:** 44 Befunde und Verbesserungsmöglichkeiten: **5 × P1, 28 × P2, 11 × P3**. Bestätigte Fehler, Schutzlücken und Designempfehlungen sind ausdrücklich getrennt.

Das Projekt besitzt eine gute fachliche Grundlage: ein HA-freier Rechenkern, bewusst modellierte Lernschichten, viele sinnvolle Invariantentests und ausführliche Dokumentation. Die größte Schwäche ist die Konsistenz zwischen den Schichten. Mehrere Fehler passieren trotz grüner Tests beim Zusammenspiel von Bootstrap und Live-Engine, Cache und Auslieferung, Tagesauswahl und Training sowie Browser und HA. Ein weiterer Schwerpunkt ist die hohe Kopplung an den Coordinator. Mehr Dokumentation oder eine höhere Coverage-Zahl allein beheben diese Probleme nicht.

## Umfang und Nachweis

Geprüft wurden die Python-Integration einschließlich Kern, Konfigurationsfluss, Persistenz, Services, Entities und Recorder, beide JavaScript-Karten, Testaufbau und repräsentative fachliche Testfälle, CI/Release/Setup sowie README, CONTRIBUTING, SPEC, BACKFILL, DASHBOARD und Projektwissensbasis. Die Prüfung kombinierte Codeinspektion, vier parallel bearbeitete Themenbereiche, vollständige lokale Prüfungen und gezielte synthetische Reproduktionen. Bei allen nachfolgenden Befunden sind Datei und Symbol beziehungsweise Dokumentabschnitt genannt; Dateilängen sind Größenmessungen, keine Fundstellen.

| Prüfung | Ergebnis |
|---|---|
| `uv run pytest tests -p no:homeassistant --cov=custom_components/balcony_solar_forecast` | **2502 bestanden, 240 übersprungen, 5 Warnungen** |
| Statement-Coverage | **96,20 %**, 7718 Statements, 293 nicht ausgeführt; keine Branchmessung |
| `uv run ruff check .` | bestanden |
| `uv run mypy` | bestanden, meldet 17 Quelldateien; acht Kernmodule unterdrücken Fehler, HA-Schicht außerhalb des Scopes |
| Frontend | Node-Harness der Suite ausgeführt; zusätzliche isolierte Repros für Registry-Rennen, Fehlercache und Zeitzonenversatz |
| Kern | zusätzliche Probes zu Bootstrap-Clamp, Temperaturpunkt, Quantilcap, Taxonomie und numerischer Deserialisierung |
| Dokumentverweise | lokale Links des neuen Berichts auflösbar; 14 SPEC-Integritätstests nach Berichtserstellung bestanden |

Die 240 Skips sind die bewusst ausgeschlossenen Low-Sun-Gleichheitsfälle der Golden-Tests; es existiert ein eigener Stabilitätstest für niedrige Sonne. Sie sind nicht pauschal fehlende Tests. Es wurde kein Live-HA verändert oder analysiert, kein Softwareupdate ausgeführt und keine Veröffentlichung vorgenommen. Der reale HA-Lebenszyklus, Windows und GitHub-Workflows wurden in diesem Review nicht ausgeführt. Nicht jede einzelne Assertion der mehr als 33.000 Testzeilen wurde semantisch einzeln bewiesen. „Komplettes Review“ bedeutet hier Prüfung aller wesentlichen Bereiche, keine Garantie, dass danach keine weiteren Fehler existieren.

Vorhandene ungetrackte Arbeitsdateien blieben unverändert. Produktcode und vorhandene Tests wurden nicht geändert; neu ist dieser Bericht. Temporäre Prüfprotokolle und Repro-Skripte liegen unter `/tmp/bsf-review-*` und `/tmp/bsf-core-*`.

## Priorisierung

- **P1:** Vor dem nächsten regulären Release bearbeiten: falsche Auslieferung, verlorene Wiederherstellung oder potenziell konkurrierende Store-Schreibvorgänge.
- **P2:** Gezielter nächster Verbesserungsblock: fachliche Randfälle, fehlende Schutzmechanismen und wichtige Wartbarkeitsprobleme.
- **P3:** Geplant nachziehen: Einarbeitung, präzisere Verträge, Barrierefreiheit und kleinere strukturelle Verbesserungen.

**Zuerst:** R01–R05. Danach die gemeinsame Lernbasis R06–R08/R19/R44 und die Kartenfehler R12–R15. Die wichtigsten Testinvestitionen sind R25–R27. Große Refactorings erst nach diesen Verhaltensnachweisen beginnen.

## Bestätigte Fehler und konkrete Verhaltenslücken

### R01 · P1 · Catch-up überspringt ältere Lücken und fehlgeschlagene Trainings

**Beleg:** [custom_components/balcony_solar_forecast/_nightly.py](../../custom_components/balcony_solar_forecast/_nightly.py)::catchup_days, `async_nightly_job`, `train_and_guard`.

Der Starttag wird aus dem neuesten gespeicherten Actuals-Datum plus eins gebildet. Ein früherer Tag ohne Messdaten oder mit bereits gespeicherten Actuals, aber fehlgeschlagenem Training, wird dadurch später nicht erneut ausgewählt. Repro: Actuals am 24. und 25.09., ungefüllte Lücke am 23.09. beziehungsweise untrainierter 24.09.; für `latest=25.09.` ergibt die Auswahl nur den 25.09. Die versprochene Nachholung verspäteter Daten gilt damit nicht durchgängig.

**Änderung und Abnahme:** Innerhalb des begrenzten Fensters fehlende Verarbeitungsschritte auswählen, nicht nur nach dem neuesten Messtag fortsetzen. Mehrtägiger Test: Messlücke/Trainingsfehler → neuerer erfolgreicher Tag → Daten nachgeliefert → genau einmal nachholen. `test_day_without_actuals_is_retried_later` ruft nur den Trainer direkt auf und beweist diese Auswahl nicht.

### R02 · P1 · Alter Wettercache kann die Erholung dauerhaft blockieren

**Beleg:** [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::BalconySolarCoordinator._async_try_fetch; [fetcher.py](../../custom_components/balcony_solar_forecast/fetcher.py)::radiation_coverage.

Die Entscheidung „reicheren Cache behalten“ vergleicht nur die Anzahl nichtleerer Strahlungswerte, ohne ihre Zeitlage. Reproduziert: Ein vollständig abgelaufenes 384-Slot-Payload vom 20.09. verhindert am 26.09. die Annahme eines neuen brauchbaren 288-Slot-Payloads. Der Status bleibt `unavailable`. Ein dauerhaft kürzerer, aber brauchbarer Providerhorizont kann den Zustand beliebig verlängern.

**Änderung und Abnahme:** Nutzbare Abdeckung relativ zu Jetzt vergleichen; einen abgelaufenen/unverwendbaren Cache nicht vor einem aktuellen Forecast bevorzugen. Test mit versetzten Zeitachsen und kürzerer neuer Zukunftsabdeckung, zusätzlich bestehende Teildegradationsfälle erhalten.

### R03 · P1 · Formgültige, aber unlesbare Wetterantwort überschreibt den letzten guten Cache

**Beleg:** [fetcher.py](../../custom_components/balcony_solar_forecast/fetcher.py)::validate_payload, `OpenMeteoFetcher.async_fetch_raw`; [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_async_try_fetch, `_cached_weather`.

Der Netzpfad prüft die Form und persistiert das neue Payload, bevor es vollständig geparst wird. Ein gleich großes Payload mit `time[0]="not-a-date"` besteht `validate_payload`, ersetzt nachweislich den guten Cache und liefert anschließend in `_cached_weather` nur noch `None`. Der vorherige funktionierende Forecast steht nicht mehr als Fallback bereit.

**Änderung und Abnahme:** Erst nach erfolgreichem Parsing und grundlegender zeitlicher Validierung atomar übernehmen. Test des vollständigen Übergangs guter Cache → defekte neue Antwort → alter Cache weiterhin nutzbar; ein isoliert gemockter Parserfehler reicht dafür nicht.

### R04 · P1 · Service und Energy-Dashboard liefern trotz Updatefehler alte Kurven

**Beleg:** [sensor.py](../../custom_components/balcony_solar_forecast/sensor.py)::_build_forecast_response; [energy.py](../../custom_components/balcony_solar_forecast/energy.py)::async_get_solar_forecast; ergänzend [diagnostics.py](../../custom_components/balcony_solar_forecast/diagnostics.py)::async_get_config_entry_diagnostics.

HA behält bei einem fehlgeschlagenen Coordinator-Refresh die letzte `data`. Die beiden Auslieferungspfade prüfen nur diese Daten, nicht `last_update_success`. Repro mit gefüllter `data` und `last_update_success=False`: `get_forecast` liefert weiterhin `[100.0]`, der Energy-Hook die alte AC-Stundenkurve. Die Sensoren melden dagegen korrekt `unavailable`. Diagnostics übernimmt außerdem alten `source_status` und Datenalter aus derselben Momentaufnahme, obwohl bereits ein aktueller Alterszugriff existiert.

**Änderung und Abnahme:** Gemeinsame Verfügbarkeits-/Provenienzentscheidung an der Auslieferungsgrenze; bei Unverfügbarkeit leere/fehlende Prognose oder ausdrücklich gekennzeichnete historische Daten. Test Erfolg → Wetter über Altersgrenze → fehlgeschlagener Refresh → alle öffentlichen Verbraucher konsistent. Diagnostics-Alter aus aktueller Uhr berechnen.

### R05 · P1 · Laufende Langzeitaufgaben sind beim Unload nicht vollständig an den Entry gebunden

**Beleg:** [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::async_start_nightly_job, `async_shutdown_extra`; [__init__.py](../../custom_components/balcony_solar_forecast/__init__.py)::async_unload_entry; [_bootstrap.py](../../custom_components/balcony_solar_forecast/_bootstrap.py)::async_run_bootstrap.

Der nächtliche Scheduler registriert die Coroutine direkt. Das Abmelden des Timers verhindert künftige Starts, beendet aber keinen bereits laufenden Job. Der lange Bootstrap-Service ist ebenfalls nicht als Entry-Aufgabe registriert. Unload beendet nur den Timer und leert transiente Daten; es wartet nicht auf solche Schreiber und sperrt deren spätere Imports nicht. Ein noch laufender alter Coordinator kann dadurch nach Flush/Reload wieder Lernzustand für denselben Store schreiben. Der Startup-Catch-up ist dagegen bereits korrekt als Entry-Hintergrundaufgabe angelegt. Bestätigt mit echter Unload-/Nightly-/Store-Logik über synthetischen I/O-Gates: Nach erfolgreichem Unload und Flush erzeugt der alte Nightly-Task zwei weitere verzögerte Save-Anforderungen. Ein entsprechend angehaltener Bootstrap importiert ebenfalls nach dem Unload noch erfolgreich. Tatsächlicher Verlust in einem Live-Dateisystem wurde nicht erzeugt.

**Änderung und Abnahme:** Alle Langzeit-Schreiber mit Entry-Lebenszyklus und gemeinsamem Abschluss-/Abbruchprotokoll verwalten. Executor-Arbeit kann gegebenenfalls weiterlaufen, darf nach Unload aber kein Ergebnis mehr importieren. Regression: Job vor Recorder-/Executor-Rückkehr anhalten, Entry unloaden/reloaden, alte Aufgabe fortsetzen; kein alter Write und keine Wiederanlage eines entfernten Stores. Kein Live-Schadensfall wird behauptet.

### R06 · P2 · Bootstrap-Quantile ignorieren den zweiten Gruppen-Clamp

**Beleg:** [core/bootstrap_build.py](../../custom_components/balcony_solar_forecast/core/bootstrap_build.py)::_process_day_impl (`corrected = theta * modeled_site`); [core/engine.py](../../custom_components/balcony_solar_forecast/core/engine.py)::compute_forecast (`cor_factored` → `clamp_groups`).

Reproduziert mit 90-W-AC-Gruppe, η=0,9, Slow-only=100 Wh DC, θ=1,5 und Ist=100 Wh: Bootstrap speichert Residuum **0,6667**. Die Live-Kurve wird erneut auf 100 Wh geklemmt; ihr Residuum wäre **1,0**. Quantile werden beim Bootstrap also gegen Energie trainiert, die live nicht ausgeliefert würde.

**Änderung und Abnahme:** Ebenen korrigieren, vollständige Gruppen wie live re-clampen, erst dann die gemeterte Teilmenge aggregieren. Test mit θ>1, mehreren Gruppen und teilgemeterter Anlage. Vorhandene Walk-forward-Tests ohne Gruppen können diesen Fall nicht entdecken.

### R07 · P2 · Bootstrap-Slow-Kurve verwendet einen anderen Temperaturpunkt als die Live-Engine

**Beleg:** [core/bootstrap_build.py](../../custom_components/balcony_solar_forecast/core/bootstrap_build.py)::reconstruct_plane_hour, `_process_day_impl`; [core/engine.py](../../custom_components/balcony_solar_forecast/core/engine.py)::_dc_split.

Bootstrap bildet Slow-only linear als `beam_wh * tau + diffuse_wh` aus dem RAW-Temperaturpunkt. Live wird die Ross-Zelltemperatur aus der durch gelerntes τ veränderten POA neu berechnet. Mit identischem Sonnenzeitpunkt, 400-W-Modul, Ross=0,056, statischem τ=0,2 und gelerntem τ=0,9 ergibt die Probe **300,674 W** gegen **265,231 W** live: **13,36 %** Unterschied. Das wurde mit echter Physik reproduziert und ist kein Effekt unterschiedlicher Zeitauflösung.

**Änderung und Abnahme:** RAW-Lernreferenz und servierte Slow-only-Physik ausdrücklich trennen; temperaturabhängige Transformation teilen. Paritätstests mit gleicher Evaluationszeit, mehreren Temperaturen und nichtneutralem gelerntem Schatten.

### R08 · P2 · Der dokumentierte Quantilcap pro Tag gilt live nicht

**Beleg:** [core/quantiles.py](../../custom_components/balcony_solar_forecast/core/quantiles.py)::train_quantiles; [_nightly.py](../../custom_components/balcony_solar_forecast/_nightly.py)::train_quantiles_day; [core/bootstrap_build.py](../../custom_components/balcony_solar_forecast/core/bootstrap_build.py)::_process_day_impl; SPEC §11.1.

Nur der Bootstrap begrenzt auf `QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN`. Der gemeinsame Trainer appendet alle Samples, der Nightly-Pfad begrenzt sie ebenfalls nicht. Repro: zwölf Samples desselben Tages/Bins erzeugen zwölf statt maximal acht Einträge. Das verletzt die zugesagte Gewichtung korrelierter Stunden und die Begründung der unabhängigen Tage für undatierte Altzustände.

**Änderung und Abnahme:** Cap in der gemeinsamen Trainingsfunktion pro Datum/Bin erzwingen, vorhandene Tageseinträge berücksichtigen. Tests mit mehr als acht Samples, zwei Tagen, mehreren Bins und Live-/Bootstrap-Parität.

### R09 · P2 · Ein unendlicher Zähler kann den Store-Ladevorgang abbrechen

**Beleg:** [core/types.py](../../custom_components/balcony_solar_forecast/core/types.py)::_safe_int, `BiasCell.from_dict`, `ShademapBin.from_dict`; [store.py](../../custom_components/balcony_solar_forecast/store.py)::ForecastStore.async_load, `validate_state`.

`_safe_int(±inf)` wirft `OverflowError`, weil nur `TypeError` und `ValueError` gefangen werden. Der Store-Ladepfad fängt diesen Fehler nicht ab. Bei einem entsprechend beschädigten/importierten numerischen Zustand kann Setup abbrechen, statt die defekte Sektion gemäß SPEC §16.4 zu neutralisieren.

**Änderung und Abnahme:** Endliche numerische Konversion und sektionsweise Fehlerisolation. Test durch `validate_state` mit defektem Zähler und zwei gültigen Nachbarsektionen: kein Crash, gültige Daten unverändert. Das Finding setzt ungültige Eingabedaten voraus; kein spontan korrupter Store wurde beobachtet.

### R10 · P2 · Konfigurationsvalidierung lässt nichtendliche Nennleistung durch

**Beleg:** [_site_validation.py](../../custom_components/balcony_solar_forecast/_site_validation.py)::validate_site; [core/types.py](../../custom_components/balcony_solar_forecast/core/types.py)::PlaneConfig.from_dict.

Für `wp` wird nur `> 0` verlangt. `float('inf')`, auch aus einem JSON-String `"inf"` konvertierbar, wird dadurch als gültige Modulnennleistung akzeptiert. Ein unendlicher physikalischer Parameter kann anschließend Berechnungen und Plausibilitätsgrenzen unbrauchbar machen. Andere Felder wie `ross_coeff` prüfen bereits ausdrücklich `math.isfinite`.

**Änderung und Abnahme:** Einheitliche finite-/Typvalidierung für physikalische Eingaben vor der Normalisierung; nicht nur weitere Einzelguards. Parameterisierte Grenzfälle mit NaN, ±Inf und numerischen Strings durch den realen Site-Validator prüfen.

### R11 · P3 · Quantiltrainer und Tests akzeptieren nichtproduktive Fachklassen

**Beleg:** [core/quantiles.py](../../custom_components/balcony_solar_forecast/core/quantiles.py)::train_quantiles; [tests/core/test_quantiles.py](../../tests/core/test_quantiles.py); [const.py](../../custom_components/balcony_solar_forecast/const.py)::DAY_PART_MIDDAY.

Der Trainer verspricht, unbekannte Klassen zu verwerfen, prüft aber nur nichtleere Strings. `QuantileSample('totally-invalid', 'noon', 100, 100)` erzeugt einen Bin. Viele positive Tests verwenden `noon`, während produktiv `midday` gilt. Damit bleiben Schnittstellenfehler unsichtbar und es entstehen Bins, die keine reale Prognose abruft.

**Änderung und Abnahme:** Zentrale Taxonomie an den Grenzen prüfen; positive Testdaten aus produktiven Konstanten, unbekannte nichtleere Werte explizit negativ testen.

### R12 · P2 · Automatische Kartenerkennung kann nach einem normalen HA-State-Push dauerhaft ausfallen

**Beleg:** Beide `frontend/*_card.js::_ensureRegistry`, `entityRegistry`.

Während einer offenen Registry-Abfrage kann HA ein neues `hass`-Objekt liefern. Die erfolgreiche Antwort wird wegen `this._hass !== hass` verworfen, `_registryRequested` bleibt aber wahr. Reproduziert an beiden Originalklassen: kein Ergebnis, kein zweiter Versuch. Auf deutschen/umbenannten IDs kann der englische Namensfallback das nicht auffangen. Auch Disconnect während der Antwort ist betroffen.

**Änderung und Abnahme:** Anfrage und Cache an die stabile Verbindung beziehungsweise ihre Generation binden; Fehler/Abbruch wiederholbar machen. Deferred-Promise-Tests mit State-Push und Disconnect/Reconnect.

### R13 · P2 · Karten-Serviceaufrufe unterstützen mehrere Anlagen nicht korrekt

**Beleg:** [power_history_card.js](../../custom_components/balcony_solar_forecast/frontend/power_history_card.js)::_issuedTotal, `_fetchIssued`; [shade_profile_card.js](../../custom_components/balcony_solar_forecast/frontend/shade_profile_card.js)::_fetchCompare; [_services.py](../../custom_components/balcony_solar_forecast/_services.py)::_resolve_single_coordinator; [_dashboard.py](../../custom_components/balcony_solar_forecast/_dashboard.py)::build_dashboard_config.

Die Karten übergeben bei den anlagenspezifischen Services keine `entry_id`. Das Backend lehnt diese Aufrufe bei mehreren geladenen Anlagen ab, auch wenn das Dashboard korrekt explizite Sensor-IDs verwendet. Die automatische Entity-Suche begrenzt ihre Einzelergebnisse außerdem nicht auf denselben Config-Entry.

**Änderung und Abnahme:** Karten explizit an einen Entry binden, Discovery darauf begrenzen und Entry-ID mitgeben. Test mit zwei Anlagen, umsortierter Registry und teilweise expliziter Kartenkonfiguration; Steuerung und Anzeige dürfen keine Anlagen mischen.

### R14 · P2 · Power-History schneidet Tage in der Browserzeitzone

**Beleg:** [power_history_card.js](../../custom_components/balcony_solar_forecast/frontend/power_history_card.js)::dayAt, `localHourOf`, `localDayKey`, `isoDateOf`, `_fetchDay`, `_ingestWeek`.

Browser-`Date` bestimmt Tagesgrenzen und Stundenraster, während Archiv und Sensoren HA-Kalendertage verwenden. Repro bei `2026-09-26T22:30Z`, Browser UTC und HA Europe/Berlin: Karten-„heute“ ist 26.09., HA-„heute“ 27.09. Recorder-Ist und Prognose können so unterschiedliche Zeitspannen vergleichen. Die Shade-Karte berücksichtigt HA-Zeit bereits an anderer Stelle.

**Änderung und Abnahme:** Eine explizite Standortkalender-API für Grenzen und Buckets. Tests Browserzone≠HA-Zone sowie Sommer-/Winterzeitwechsel; eine reine Labeländerung genügt nicht.

### R15 · P2 · Temporäre Wochenprognosefehler werden dauerhaft als Archivlücken gespeichert

**Beleg:** [power_history_card.js](../../custom_components/balcony_solar_forecast/frontend/power_history_card.js)::_issuedTotal, `_fetchWeekForecast`.

Servicefehler und `available:false` werden beide zu `null`. Der Wochen-Cache speichert diesen Wert und verhindert danach erneute Abfragen. Repro: sieben fehlgeschlagene Requests, anschließend gesunder Server; dasselbe Fenster bleibt bei sieben Lücken und insgesamt sieben Requests. Der periodische Refresh heilt den Zustand nicht.

**Änderung und Abnahme:** `available`, `missing` und `error` unterscheiden; Fehler wiederholen/kurzzeitig cachen. Test Ausfall → Erholung ohne Neuerzeugen der Karte.

### R16 · P2 · Deploymentvalidierung meldet Erfolg, wenn alle Prüfungen übersprungen werden

**Beleg:** [scripts/validation/validate.py](../../scripts/validation/validate.py)::main, `render`; [scripts/validation/bsf_checks.py](../../scripts/validation/bsf_checks.py)::run_all.

Acht `SKIP` ergeben Exit 0 und „alle Checks gruen - Deployment validiert“. Zusätzlich werden interne Check-Exceptions zu `SKIP`. Reproduziert mit einem synthetischen Offline-Paket, das nur eine Ist-AC-Stunde und keine weiteren erforderlichen Dateien enthält. Ein ausgefallener Prüfkatalog kann damit Erfolg vortäuschen.

**Änderung und Abnahme:** Vollständigkeit separat bewerten; ohne erforderliche PASS-Ergebnisse kein Validierungsurteil. Programmfehler als ERROR behandeln. Kleine synthetische PASS-/FAIL-/INCOMPLETE-Pakete und ein absichtlich werfender Check; derzeit fehlt eine Testsuite für diese Werkzeuge.

### R17 · P2 · Windows-Bootstrap verlangt den Interpreter, den er erst installieren soll

**Beleg:** [scripts/setup-env.ps1](../../scripts/setup-env.ps1), Zweig `Get-Command py`; [scripts/setup_env.py](../../scripts/setup_env.py)::main.

Ist der Python-Launcher vorhanden, startet der Wrapper zwingend `py -3.14`. Auf einem Rechner mit Launcher und nur Python 3.13 endet der Aufruf vor uv, obwohl uv laut Anleitung Python 3.14 installieren soll. Das wurde anhand des Kontrollflusses geprüft, nicht unter Windows ausgeführt.

**Änderung und Abnahme:** Vorhandenes uv direkt verwenden oder mit einem geeigneten bereits installierten Python bootstrappen. Windows-Smoke-Szenario mit Launcher, aber ohne 3.14.

## Design und Verständlichkeit

### R18 · P2 · Der Coordinator ist weiterhin der zentrale, breit gekoppelte Zustandsbehälter

**Beleg:** [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::BalconySolarCoordinator; [_nightly.py](../../custom_components/balcony_solar_forecast/_nightly.py).

Gemessen: 3276 Dateizeilen, 115 Methoden und 43 in `__init__` gesetzte Attribute. [_nightly.py](../../custom_components/balcony_solar_forecast/_nightly.py) greift auf 46 verschiedene `coord`-Attribute zu. Die ausgelagerten Funktionen nehmen weiterhin den gesamten Coordinator und rufen zurück in seine privaten Helfer. Die Dateiaufteilung hat damit noch keine klaren fachlichen Modulgrenzen geschaffen. Änderungen erfordern das Verständnis vieler impliziter Zustandsannahmen.

**Empfehlung/Abnahme:** Kleine zusammenhängende Verantwortlichkeiten: Wettercache, Forecast-Erzeugung/Ausgabe, Lernzustandsverwaltung, Nightly-Orchestrierung, Profilabfragen. Explizite Eingaben/Ergebnisse beziehungsweise schmale Ports; keine neue Klassenschicht ohne eigenen Vertrag. Nachweis zuerst durch unabhängige Szenariotests, dann schrittweise verhaltensneutral ausziehen.

### R19 · P2 · Dieselben physikalischen Transformationen werden mehrfach gepflegt

**Beleg:** [core/engine.py](../../custom_components/balcony_solar_forecast/core/engine.py)::_plane_poa_components, `_dc_split`; [core/bootstrap_build.py](../../custom_components/balcony_solar_forecast/core/bootstrap_build.py)::reconstruct_plane_hour, `_process_day_impl`; [core/shadeprofile.py](../../custom_components/balcony_solar_forecast/core/shadeprofile.py)::effective_tau_at, `shade_horizon_at`.

Transposition, IAM, Schatten, Beam-Gain, Temperatur und Korrektur-/Clamp-Reihenfolge werden in parallelen Pfaden zusammengesetzt. R06/R07 zeigen konkrete Drift. Die gemeinsame Nutzung einzelner Mathematikfunktionen garantiert noch keine identische Gesamttransformation.

**Empfehlung/Abnahme:** Kleine öffentliche Kern-API für POA-Komponenten, RAW-Lernreferenz, servierte Slow-Kurve und gruppierte Korrektur. Zeitauflösung ausdrücklich als Eingabe belassen. Gemeinsame Implementierung plus unabhängige Referenztests; keine bloß gegenseitig bestätigenden Duplikate.

### R20 · P3 · Flache Ergebnisobjekte und parallele Arrays lassen inkonsistente Kombinationen zu

**Beleg:** [core/types.py](../../custom_components/balcony_solar_forecast/core/types.py)::ForecastResult, `with_total`; [core/engine.py](../../custom_components/balcony_solar_forecast/core/engine.py)::compute_forecast, `_append_zero_slot`.

RAW-, Slow-, korrigierte, pre-clamp, DC-/AC- und Bandkurven werden in vielen parallelen Listen/Dicts geführt. Jede Erweiterung muss Nullpfad, Normalpfad, Aggregation und Konstruktor synchron ändern. `with_total` ersetzt nur einen Teil und kann ein fachlich widersprüchliches Ergebnis erzeugen; aktuell ist kein Produktaufrufer erkennbar.

**Empfehlung/Abnahme:** Zusammengehörige Slot-/Kurven-/Banddaten gruppieren, Aggregation getrennt behandeln und Array-Ausrichtung als Invariante prüfen. Teilmutationshelfer entfernen oder ihren eingeschränkten Zweck deutlich machen. Kompatibilität über Adapter erhalten.

### R21 · P3 · „Immutable“ gilt für die Dataclasses nur oberflächlich

**Beleg:** [core/types.py](../../custom_components/balcony_solar_forecast/core/types.py)::BiasState, `ShademapState`, `QuantileState`, `IssuedSnapshot`, `ForecastResult`; [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_compute.

`frozen=True` verhindert Attributzuweisung, aber keine Mutation verschachtelter Dicts/Listen. Die Executor-Sicherheitsbegründung setzt Copy-on-write voraus; das ist derzeit eine Disziplin der Aufrufer, keine Eigenschaft dieser Typen. Die bestehenden Updatefunktionen kopieren vielfach korrekt; ein aktuelles Datenrennen daraus wird nicht behauptet.

**Empfehlung/Abnahme:** Vertrag präzisieren und Ownership festlegen oder defensive Kopien/read-only Container verwenden. Test: Spätere Änderungen dürfen ausgegebene Forecasts und Rollback-Snapshots nicht verändern. Store-Roundtrips dabei erhalten.

### R22 · P2 · Produktionsschnittstellen tolerieren unvollständige Testobjekte statt klare Verträge zu verlangen

**Beleg:** [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_call_store_setter, `_async_nightly_job`, `_build_learner_hooks`; [_bootstrap.py](../../custom_components/balcony_solar_forecast/_bootstrap.py)::_bootstrap_lock; [__init__.py](../../custom_components/balcony_solar_forecast/__init__.py)::BalconySolarConfigEntry, `async_setup_entry`.

Mehrere `getattr`-Fallbacks sind ausdrücklich für per `__new__` gebaute Testobjekte vorhanden. Der Store-Setter wird per String gesucht; fehlt er, ist Persistenz still ein No-op. Das schwächt Fehlersichtbarkeit und Typprüfung an eigenen, kontrollierten Schnittstellen. Parallel ist ein typisierter ConfigEntry-Alias vorhanden, der Coordinator wird aber ausschließlich aus `hass.data` gelesen.

**Empfehlung/Abnahme:** Testobjekte an echte Konstruktoren/Ports anpassen und kontrollierte interne Verträge explizit typisieren. Die bestehende Entscheidung für eine einzige Datenhaltung dokumentieren oder vollständig migrieren; keinen zweiten Spiegel einführen. `entry.runtime_data` ist der aktuelle HA-Standard; für diese Custom-Integration ist das eine begründete Modernisierung, kein behaupteter akuter Kompatibilitätsfehler. [HA: runtime_data](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/)

### R23 · P3 · Wiederholte Fachzahlen brauchen einen gemeinsamen Vertrag

**Beleg:** [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_build_data, `_build_intraday_sample`, `_dayahead_today_kwh_over`; [_nightly.py](../../custom_components/balcony_solar_forecast/_nightly.py)::per_plane_modeled; [core/types.py](../../custom_components/balcony_solar_forecast/core/types.py)::WeatherSlot.midpoint; [_site_validation.py](../../custom_components/balcony_solar_forecast/_site_validation.py)::_validate_tau_points; beide Karten.

Die 15-Minuten-Konvention steht verteilt als `0.25`, `15` und `7 min 30 s`. Numerische Toleranzen wie `1e-6`/`1e-9` haben unterschiedliche Zwecke, die nicht überall im Namen sichtbar sind. Python- und JS-Schattenklassifikation besitzen getrennte Schwellenverträge. Das sind konkrete Pflegepunkte; physikalische Formelkoeffizienten, offensichtliche 0/1 oder jede Pixelzahl sind nicht automatisch schlechte Magic Numbers.

**Empfehlung/Abnahme:** Die vorhandene Konstante `const.SLOT_MINUTES` konsequent verwenden und Größen daraus ableiten; domänenspezifisch benannte Toleranzen und Grenzwerttests über Python/JS. Keine pauschale Literal-Ersetzung. Ruff `PLR2004` kann ergänzend Hinweise geben, ersetzt aber keine Fachprüfung. [Ruff: magic-value-comparison](https://docs.astral.sh/ruff/rules/magic-value-comparison/)

### R24 · P3 · Beide Karten bündeln Darstellung, Datenzugriff und Lebenszyklus zu stark

**Beleg:** [frontend/power_history_card.js](../../custom_components/balcony_solar_forecast/frontend/power_history_card.js), [frontend/shade_profile_card.js](../../custom_components/balcony_solar_forecast/frontend/shade_profile_card.js).

Die Karten enthalten 1815 beziehungsweise 1568 Zeilen für Registry, Requests, Caches, Kalender, SVG, Controls und Styles. Die gleiche fehlerhafte Registry-Logik existiert zweimal. Die konkrete Wartbarkeitsfolge ist gemeinsame Bugduplizierung und schwer isolierbarer asynchroner Zustand, nicht die Zeilenzahl an sich.

**Empfehlung/Abnahme:** Gemeinsame anlagengebundene Registry-/Service-Schicht, reine Kalender-/Aggregationsfunktionen und schmale Card-Controller. Lokale ES-Module können ohne Bundler oder neue Runtime-Abhängigkeiten auskommen. Gemeinsame Verträge ausführend testen.

## Testqualität und Entwicklerabsicherung

### R25 · P2 · Die Suite prüft keinen echten HA-Lebenszyklus

**Beleg:** [tests/test_setup_path.py](../../tests/test_setup_path.py)::patched_setup, `_FakeConfigEntries`, `_FakeEntry`; [tests/test_config_flow_user.py](../../tests/test_config_flow_user.py)::_flow; [.github/workflows/validate.yml](../../.github/workflows/validate.yml)::tests-ha-min.

First-Refresh, Scheduler und Plattformweiterleitung sind gefakt. Plattformweiterleitung zeichnet Argumente auf, erstellt aber keine HA-Entities. Der Fake-Hintergrundtask schließt die Coroutine. Config-Flows werden am realen Flow-Manager vorbei aufgerufen. Auch der Minimum-HA-Job nutzt diese Suite. Sie schützt daher Python-Logik gut, wesentliche Frameworkverträge aber nur indirekt.

**Empfehlung/Abnahme:** Separate kleine Linux-HA-Suite zusätzlich zur portablen Unit-Suite: Flow → Setup → erste Daten → Entities/Services → Options-Reload → Unload → Restart mit Store; außerdem zwei Entries und Fehlererholung. Nur externe I/O-Grenzen faken. HA empfiehlt ausdrücklich echte Flow-Manager-Pfade und Wiederherstellung nach Eingabefehlern. [HA: Config-flow tests](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-flow-test-coverage/)

### R26 · P2 · Ein Frontendtest behauptet Verhalten anhand eines Kommentars

**Beleg:** [tests/test_frontend_resource.py](../../tests/test_frontend_resource.py)::test_js_card_file_sanity; [shade_profile_card.js](../../custom_components/balcony_solar_forecast/frontend/shade_profile_card.js)::_plot; [tests/harness/power_card_harness.mjs](../../tests/harness/power_card_harness.mjs).

`assert "mousemove" in text` ist grün, obwohl die Karte Pointer-Events registriert und „mousemove“ nur im Kommentar vorkommt. Weitere Stringprüfungen ersetzen keinen Nachweis von Controls/Service-Lifecycle. Für Shade fehlt ein ausführender Runtime-Harness; der Power-Harness umgeht bei vielen Fällen Konstruktor und DOM.

**Empfehlung/Abnahme:** Tatsächlich Ereignisse auslösen, Requests verzögern und sichtbare Ergebnisse prüfen. Kleiner DOM-/Browser-Test für Select/Datum/Toggle, Reconnect und Tooltip. Statische Checks für echte statische Verträge behalten, etwa Asset-Auslieferung, verbotene externe Imports und Property/Method-Shadowing.

### R27 · P2 · Coverage wird als Ausführungsmaß benötigt, aber die risikoreichen Szenarien fehlen

**Beleg:** [pyproject.toml](../../pyproject.toml)::tool.coverage.run; CI-Job `tests`; Tests zu Catch-up, Cache und Plattformen.

96,20 % Statements sagen nichts darüber, ob die entscheidenden Kombinationen geprüft sind. R01–R08 sind Beispiele: Einzelschritte sind gut abgedeckt, aber ihre Verknüpfung nicht. Es fehlt eine explizite Szenariomatrix für Lernreferenzen, Zeit-/Datenlücken, Cachewechsel und Lifecycle. Branch-Coverage wird nicht gemessen.

**Empfehlung/Abnahme:** Erst Branch-Bericht ohne neuen pauschalen Prozentdruck; dann wenige semantische Tests an den nachgewiesenen Lücken. Gezielte Mutationen an Clamp-Reihenfolge, Label-Gates und Datumsauswahl müssen diese Tests brechen. Kein flächiger teurer Mutationstest als Selbstzweck. HA-Coverage-Vorgaben sind ein Schutzmechanismus, keine Definition vollständiger Testqualität. [HA: Test coverage](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/test-coverage/)

### R28 · P3 · Testhilfen sind in Testmodulen versteckt und bilden Konstruktoren nach

**Beleg:** [tests/test_coordinator_learning.py](../../tests/test_coordinator_learning.py)::_make_coordinator, `_FakeStore`; dessen Nutzer in Nightly-/Drift-/Setup-Tests; beide `conftest.py`.

Ein 3635-Zeilen-Testmodul ist zugleich Helper-Bibliothek für mindestens acht weitere Module. Fakes setzen viele private Attribute von Hand; weitere FakeHass/FakeStore-Varianten existieren parallel. Produktionsmodule werden unter verschiedenen Paketnamen geladen. Das erschwert Einarbeitung und fördert Abweichungen zwischen Fake und realem Objekt.

**Empfehlung/Abnahme:** Benannte Szenario-Builder und gemeinsame Fake-Verträge nach `tests/helpers/`; Konstruktoren an injizierbaren Grenzen verwenden. Importstrategie pro Schicht vereinheitlichen. Tests sollen Fachszenarien ausdrücken und keine Initialisierungsinventare wiederholen.

### R29 · P2 · Unabhängige Referenztests sind nicht vollständig reproduzierbar

**Beleg:** [tests/core/test_golden.py](../../tests/core/test_golden.py), `reference_vectors.json::meta`; [scripts/validation/README.md](../../scripts/validation/README.md).

Die pvlib-Vektoren sind wertvoll, ihr Generator `scratchpad/gen_reference_vectors.py` ist aber nicht versioniert. Fehlt das eigentlich verpflichtende JSON, skippt das gesamte Modul. Die anlagenspezifische Eichbasis des Validierungskatalogs ist ebenfalls nicht im Repo nachspielbar.

**Empfehlung/Abnahme:** Generator mit Provenienz und getrennten gepinnten Entwicklungsabhängigkeiten aufnehmen; Runtime bleibt stdlib-only. Fehlende Pflichtvektoren müssen einen Fehler ergeben. Für Deploymentchecks synthetische oder anonymisierte PASS-/FAIL-Daten versionieren, keine privaten Live-Dumps.

### R30 · P3 · Der Äquivalenztest vergleicht nicht mehr den gesamten Ergebnisvertrag

**Beleg:** [tests/core/test_engine_split_equivalence.py](../../tests/core/test_engine_split_equivalence.py)::_assert_result_bit_equal; [core/types.py](../../custom_components/balcony_solar_forecast/core/types.py)::ForecastResult, `PlaneResult`.

Die manuell aufgezählten Vergleiche lassen aktuelle AC- und Slow-Felder aus. Der Test bleibt für den historischen Refactoring-Scope wertvoll; die Aussage „alle Ergebnisfelder bitgleich“ gilt für den heutigen Vertrag aber nicht. Neue Felder können unbemerkt außerhalb des Orakels bleiben.

**Empfehlung/Abnahme:** Historischen Vergleichsscope explizit einfrieren und maschinell gegen eine Feldliste prüfen. Der vorhandene Ganzkurven-Test verwendet dieselbe heutige Engine mit ersetzten Physikprimitiven; dort können auch aktuelle AC-/Slow-Felder verglichen werden. Für neue Funktionalität zusätzlich eigene unabhängige Erwartungen verwenden.

### R31 · P3 · Regel 6 ist für sinnvolle Testnachrüstung zu pauschal

**Beleg:** [CLAUDE.md](../../CLAUDE.md)::Regel 6; [docs/project-knowledge/07-entwicklung-tests-release.md](../../docs/project-knowledge/07-entwicklung-tests-release.md), Testkonventionen.

„Neue Tests müssen den alten Code durchfallen lassen“ ist eine starke Bugfix-Regel, aber keine universelle Definition guter Tests. Charakterisierungstests oder nachgerüstete HA-Integrationstests für bereits korrektes Verhalten müssen zunächst grün sein können. Ihr Mehrwert liegt in der Erkennung künftiger Regressionen.

**Empfehlung/Abnahme:** Nach Testabsicht unterscheiden: Bugfix → semantischer Red/Green-Nachweis; neues Verhalten → neuer Vertrag; Refactoring → Charakterisierung/Äquivalenz; Nachrüstung → unabhängiges Orakel oder gezielte Mutation. Die vorhandene Strenge erhalten, den Anwendungsbereich präzisieren.

### R32 · P2 · Die Typprüfung blendet besonders wichtige Schnittstellen aus

**Beleg:** [pyproject.toml](../../pyproject.toml)::tool.mypy, `tool.mypy.overrides`.

Acht Kernmodule, darunter `types`, `bootstrap_build`, `bias` und `quantiles`, haben pauschal `ignore_errors=true`; die HA-Schicht liegt außerhalb des Scopes. Dies ist transparent dokumentiert, verhindert aber neue Typfehler dort nicht. Ein grüner mypy-Lauf ist daher kein Nachweis durchgängiger Typqualität.

**Empfehlung/Abnahme:** Zuerst validierte Domain-Typen von rohem JSON trennen, Ring-/Sampleformen und Glue-Ports präzisieren; Module einzeln freischalten. Baseline verkleinern statt mit beliebigen Casts/Any zu verdecken. HA nennt strikte Typisierung als fortgeschrittenes Qualitätsziel. [HA: Strict typing](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/strict-typing/)

### R33 · P2 · Die harte HA-/stdlib-Grenze besitzt kein automatisches Gate

**Beleg:** [CLAUDE.md](../../CLAUDE.md)::Regel 2; [docs/project-knowledge/07-entwicklung-tests-release.md](../../docs/project-knowledge/07-entwicklung-tests-release.md), HA-Freiheitsprüfung; CI.

Alle Kernprüfungen in CI laufen mit installiertem HA und dessen Abhängigkeiten. Ein versehentlicher HA-/numpy-/pandas-Import könnte dort funktionieren. Die dokumentierte manuelle Suche erkennt das Problem, wird aber nicht automatisch ausgeführt. Namespace-Shims allein blockieren solche Imports nicht.

**Empfehlung/Abnahme:** Kleiner AST-Importwächter für `core/` und [const.py](../../custom_components/balcony_solar_forecast/const.py), einschließlich Lazy-Imports und expliziter aiohttp-Ausnahme; ergänzend minimaler Kernjob ohne HA. Test des Wächters mit einem absichtlich verbotenen Funktionsrumpf-Import.

### R34 · P2 · Der Release-Guard läuft nach der Veröffentlichung

**Beleg:** [.github/workflows/release.yml](../../.github/workflows/release.yml)::on.release.types, `Version guard`.

Der Trigger ist `published`. Ein roter Workflow beendet keine bereits erfolgte Veröffentlichung und entfernt keinen Tag. HACS kann ihn weiter beziehen. Der Workflow verifiziert außerdem keinen grünen vollständigen Validate-Lauf auf exakt dem Release-Commit. Der bestehende Kommentar überzeichnet daher den Schutz. Die Schreibberechtigung ist für die derzeit ausschließlich prüfenden Schritte nicht begründet.

**Empfehlung/Abnahme:** Version/SPEC/Tests vor Publikation prüfen, danach Release veröffentlichen. Nachprüfung optional behalten; Zugriff minimal halten. Negativprobe mit falscher Versionsnummer darf niemals ein veröffentlichtes Release erzeugen. Es wird nicht behauptet, dass ein vorhandenes Release tatsächlich falsch veröffentlicht wurde.

### R35 · P2 · CI erzwingt den behaupteten Lockfile-Vertrag nicht

**Beleg:** [.github/workflows/validate.yml](../../.github/workflows/validate.yml), `uv sync --group dev`; [.devcontainer/devcontainer.json](../../.devcontainer/devcontainer.json).

Ohne `--locked` kann uv bei einer veralteten Lockdatei neu auflösen. Ein Manifest-Update ohne passenden committeten Lockstand kann deshalb auf einem nur in CI erzeugten Zustand grün werden. Nach dem Lauf wird kein Lockfile-Diff kontrolliert. Dieses Verhalten entspricht der uv-Dokumentation. [uv: Locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)

**Empfehlung/Abnahme:** Reguläre CI-Installation mit `--locked`, weitere Runs vor unbeabsichtigtem Sync schützen. Bewusst abweichendes HA-Minimum getrennt behandeln. Negativprobe: Dependency-Manifest ändern, Lock nicht aktualisieren → regulärer CI-Installationsschritt muss scheitern.

### R36 · P2 · Der HA-Minimum-Job testet nicht die erklärte Mindest-Patchversion

**Beleg:** [hacs.json](../../hacs.json)::homeassistant; [.github/workflows/validate.yml](../../.github/workflows/validate.yml)::tests-ha-min.

Deklariert ist `2026.3.0`, installiert wird `homeassistant==2026.3.*`. Das kann eine spätere Patchversion auswählen und beweist nicht die Kompatibilität mit `.0`. Der Wert wird zusätzlich an mehreren Stellen unabhängig gepflegt.

**Empfehlung/Abnahme:** Exakten Wert aus [hacs.json](../../hacs.json) lesen und testen, tatsächliche installierte Version protokollieren. Bei Bedarf zusätzlich neueste Patchversion der Reihe. Zusammen mit R25 prüfen, nicht nur Imports unter der älteren Distribution.

## Dokumentation und Bedienbarkeit

### R37 · P2 · BACKFILL beschreibt andere Lernziele als der aktuelle Code

**Beleg:** [docs/BACKFILL.md](../../docs/BACKFILL.md), „What it computes“, „Your site“, „Import into Home Assistant“; [core/bootstrap_build.py](../../custom_components/balcony_solar_forecast/core/bootstrap_build.py)::_process_day_impl, `_day_part_for_slot`, `site_signature`.

Die Anleitung beschreibt Quantile nach dem RLS-Schritt gegen θ×gated_model. Tatsächlich wird Walk-forward mit vorher bekanntem θ und Slow-only-Basis gerechnet. Tagesabschnitte werden als UTC-Uhrzeit beschrieben, sind aber Sonnenzeit. Die Import-Signatur soll nur Koordinaten/Namen vergleichen, hasht aber weitere Geometrie-, Horizont-, Elektrik- und Schemaangaben. `--tz` fehlt in der Flag-Tabelle.

**Änderung und Abnahme:** Fachliche Beschreibung mit aktuellem Code und SPEC §12 synchronisieren; alle CLI-Optionen mit `--help` abgleichen. Eine kurze maßgebliche Erklärung verlinken statt Signatur/Lernreihenfolge mehrfach unabhängig zu pflegen. Mechanische SPEC-Namenschecks beweisen diese Semantik nicht.

### R38 · P3 · Einstieg und ausführbare Entwicklungsanweisungen sind widersprüchlich

**Beleg:** [README.md](../../README.md), „Ergänzende Anleitungen“; [docs/BACKFILL.md](../../docs/BACKFILL.md), „Tests“; `CONTRIBUTING.md §4/§6`; [docs/project-knowledge/07-entwicklung-tests-release.md](../../docs/project-knowledge/07-entwicklung-tests-release.md); [docs/DASHBOARD.md](../../docs/DASHBOARD.md).

Konkrete Drift: README nennt beim Bootstrap zwei Lernschichten und „läuft nicht auf HA“, obwohl `run_bootstrap` drei Zustände in HA aufbaut. BACKFILL zeigt pytest ohne Pflicht-Pluginabschaltung und mit zusätzlichem `-q`. CONTRIBUTING nennt Coverage report-only und HA-Floor 2026.1.0. Knowledge/07 lässt ersetzte Setup-Kommandos unter einem Korrekturkasten stehen. DASHBOARD spricht von täglicher Mean-Statistik statt Stundenaggregation und pauschal von eingebauten Karten trotz generierter Custom Cards.

**Änderung und Abnahme:** Aktuellen kurzen Einstieg herstellen, ersetzte Handgriffe in Historie verschieben, Befehle aus einer Quelle referenzieren. Frischer Checkout nach Anleitung muss ohne internes Vorwissen zum gleichen Prüfkommando führen.

### R39 · P2 · Verbindliche Regeln und zentrale Kommentare beschreiben teils veraltete Lernbasis

**Beleg:** [CLAUDE.md](../../CLAUDE.md)::Regel 8; [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_modeled_power_for_planes; [docs/project-knowledge/03-lernschichten-und-korrekturen.md](../../docs/project-knowledge/03-lernschichten-und-korrekturen.md); [core/types.py](../../custom_components/balcony_solar_forecast/core/types.py)::SiteConfig, `ForecastResult`; [core/engine.py](../../custom_components/balcony_solar_forecast/core/engine.py)::_plane_poa_components.

Die harte Regel nennt Intraday gegen `raw × θ`; Code und neuere Erklärung verwenden Slow-only×θ. Kommentare behaupten außerdem, Schatten/Bias könnten keine Faktoren über eins darstellen, obwohl ihre Grenzen darüber liegen. Der ForecastResult-Text beschreibt AC aus serviertem DC, während die Engine bewusst den unclamped-Pfad verwendet. Solche Widersprüche sind für neue Menschen und Agenten gefährlicher als ein alter Versionsstempel.

**Änderung und Abnahme:** Aktuelle Begriffe RAW/SLOW/CORRECTED und DC/AC einmal verbindlich formulieren, Regeln daran ausrichten. Motivation/Incident als stabilen Verweis behalten, überholte Phasenprosa entfernen. Jede Aussage über Lernreferenz gegen den tatsächlichen Datenpfad prüfen.

### R40 · P2 · „P50“ und zentrale AC-Punktprognose sind nicht dasselbe

**Beleg:** [core/engine.py](../../custom_components/balcony_solar_forecast/core/engine.py)::compute_forecast (`_ac_p50_w` verworfen); [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_quantile_curves_ac; SPEC §11.2.

AC-P10/P90 werden aus empirischen Residuen gebildet, der echte AC-Median wird verworfen. Kommentare und SPEC nennen die unveränderte zentrale AC-Kurve dennoch P50. Repro bei Multiplikatoren 0,5/0,6/0,7: P10=34,788 W, Punktprognose=69,576 W, P90=48,704 W. Eine Punktprognose darf außerhalb eines empirischen Intervalls liegen; sie ist dann aber nicht dessen Median. Die öffentliche DC-P50-Ausgabe ist davon nicht betroffen.

**Entscheidung und Abnahme:** Punktprognose korrekt benennen oder echten AC-P50 separat liefern; API und Dokumentation konsistent halten. Nicht einfach Bandgrenzen zum Punktwert hin clampen. Test mit nichtneutralem empirischen Median und vollständiger DC-/AC-Bedeutungsprüfung. Dies ist ein Semantik-/Schnittstellenbefund, keine Behauptung, dass jede Punktprognose innerhalb P10/P90 liegen müsse.

### R41 · P3 · Recorderfehler und fehlende Messdaten sind in der Power-Karte nicht klar unterscheidbar

**Beleg:** [power_history_card.js](../../custom_components/balcony_solar_forecast/frontend/power_history_card.js)::_fetchDay, `_fetchWeek`, `_dayPlot`, `_weekPlot`.

Der Ladezustand `error` wird als fehlende Statistikdaten dargestellt. Vorhandene Balken bleiben dabei ohne eindeutige Stale-Kennzeichnung stehen. Nutzer können einen Transportfehler als Messlücke verstehen oder alte Balken für frisch geladen halten.

**Änderung und Abnahme:** Fehler, leere erfolgreiche Antwort und alte weiter angezeigte Daten unterscheiden; Zeitpunkt der letzten erfolgreichen Aktualisierung zeigen. Rendering-Test Erfolg → Fehler → Erholung.

### R42 · P3 · Diagrammwerte und Controls sind nur eingeschränkt per Tastatur zugänglich

**Beleg:** Beide Karten, Plot-/Control-Erzeugung; Shade `_controls`.

Detailwerte sind hauptsächlich über Pointer-/Touch-Overlay erreichbar. SVGs haben keine hilfreiche zugängliche Beschreibung; Shade-Labels sind nicht mit ihren Inputs verknüpft. Toggle-Auswahl wird über CSS, nicht durch `aria-pressed` oder vergleichbare Semantik mitgeteilt.

**Änderung und Abnahme:** Labels zuordnen, Zustände maschinenlesbar machen, fokussierbare Details oder tabellarische Alternative anbieten. Tastatur- und Accessibility-Tree-Prüfung. Dies ist eine Usability-Empfehlung, keine Aussage über eine rechtliche Pflicht.

## Ergänzende Lern- und Nebenläufigkeitsbefunde

### R43 · P2 · Reset, Import und Rollback umgehen den gemeinsamen Lern-Lock

**Beleg:** [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::async_import_bootstrap, `async_rollback_learners`, `async_reset_day_ahead_bias`; [_bootstrap.py](../../custom_components/balcony_solar_forecast/_bootstrap.py)::async_run_bootstrap; [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_async_nightly_job.

Nur Nightly und `run_bootstrap` nehmen den gemeinsamen Lock. Die anderen öffentlichen Lernmutationen können währenddessen sofort ausgeführt werden. Repro: Bootstrap am Wetter-Read anhalten, Lock ist belegt; der echte Bias-Reset kehrt erfolgreich zurück und leert die Zellen. Nach Fortsetzung des Bootstrap ist der Bias wieder importiert. Die parallele Ausführung ist bestätigt. Welche explizite Nutzeraktion bei Überlappung gewinnen soll, ist eine Produktentscheidung, momentan fehlt dafür ein klarer Konflikt-/Transaktionsvertrag.

**Änderung und Abnahme:** Einheitliches Gateway für alle Lernschreiber, Überlappung geordnet abarbeiten oder ausdrücklich ablehnen. Interne bereits gesperrte Importpfade separat halten: Ein blindes zusätzliches `async with lock` in `async_import_bootstrap` würde beim bestehenden Bootstrap-Aufruf einen nicht-reentranten Lock blockieren. Tests Bootstrap↔Reset, Nightly↔Rollback/Import und Unload↔alle Schreiber.

### R44 · P2 · Intraday lernt aus perfekter Begrenzung einen falschen Wetterverlust

**Beleg:** [coordinator.py](../../custom_components/balcony_solar_forecast/coordinator.py)::_modeled_power_for_planes, `_build_intraday_sample`; [core/bias.py](../../custom_components/balcony_solar_forecast/core/bias.py)::compute_intraday_scalar.

Die Referenz ist Slow-only×θ ohne Sättigungsbehandlung. Bei Slow=600 W DC, θ=1,5 und physischer Gruppenbegrenzung auf 600 W sind servierte Leistung und perfekte Messung 600 W, die Intraday-Referenz aber 900 W. Reproduziert mit dem echten Coordinator-Helfer und Trainer: neun Samples über zwei Stunden erzeugen einen Skalar von **0,6667**. Der Lerner interpretiert die physische Begrenzung als Wetterverlust und kann damit spätere nicht begrenzte Slots zu stark drosseln.

**Änderung und Abnahme:** Gesättigte Messungen als solche behandeln: Oberhalb der Grenze ist der Wetterfaktor aus der Messung nicht eindeutig bestimmbar. Geclippte Samples beispielsweise von exaktem Verhältnislernen ausschließen und echte Defizite unterhalb der Grenze weiterhin erkennen. Bloßes Re-Clampen des Nenners ist keine allgemein korrekte Lösung, weil der Faktor danach auf die noch ungeclippte Kurve wirkt. Test: mindestens zwei Stunden perfekt begrenzte Produktion halten den Skalar neutral; echte Minderproduktion bleibt lernbar.

## Was erhalten bleiben sollte

- Fachlich kleine Mathematikmodule und HA-freier Kern; keine unnötigen Runtime-Abhängigkeiten.
- pvlib-Golden-Vektoren, analytische Grenztests, Horizont-/SVF-Invarianten und bewusst getrennte Low-Sun-Stabilitätsprüfungen.
- Migrationstests mit gefüllten Altzuständen, Tests zu Korrekturschichtung, Copy-on-write, Kaltstart, Idempotenz und echter CLI/Core-Parität.
- SPEC-Integritätsprüfung für Zitate/Felder/Services/Versionen. Diese statischen Tests haben echten Nutzen; ihre Grenze ist die nicht geprüfte Bedeutung der Prosa.
- SHA-gepinnte Actions, grundsätzlich lesende CI-Berechtigungen, explizites Node im Hauptjob und Devcontainerprüfung.
- Sichere DOM-Textausgabe und bereits vorhandene Request-Generationstoken. Beide Karten räumen ihre periodischen Timer beim Disconnect auf; kein pauschales Timerleck wurde festgestellt.
- Viele bereits gut benannte fachliche Konstanten und Kommentare zum Warum. Kein Anlass für einen großflächigen Formatterlauf oder ein mechanisches Ersetzen sämtlicher Zahlen.

## Sinnvolle Umsetzungspakete

1. **Auslieferung und Lebenszyklus:** R01–R05 und R43, jeweils zuerst reproduzierender Verhaltenstest; anschließend gemeinsames Verfügbarkeits- und Task-Ownership-Konzept.
2. **Eine verbindliche Lernbasis:** R06–R11, R19, R39/R40/R44; echte Physik- und Gruppenfälle gegen gleiche Zeitpunkte vergleichen, SPEC parallel berichtigen.
3. **Verlässliche Karten:** R12–R15, R26, R41/R42; Tests zuerst für Deferred-Responses, zwei Entries und abweichende Zeitzonen.
4. **Vertrauenswürdige Prüfungen:** R16/R17 und R25–R36; keine höhere Prozentzahl als Ersatz für stärkere Szenarien.
5. **Einarbeitung und Struktur:** R18/R20–R24, R37/R38; kleine verhaltensneutrale Schritte auf der dann belastbareren Testsuite.

Es gibt keinen einzelnen universellen „Coding-Standard 2026“. Die verlinkten aktuellen offiziellen HA-/Ruff-/uv-Regeln dienen hier als fachlicher Maßstab. Für eine HACS-Custom-Integration ist die HA-Core-Quality-Scale keine automatisch vollständig verpflichtende Zertifizierung. Die Empfehlungen richten sich nach nachgewiesenen Problemen und Wartungsnutzen, nicht nach möglichst vielen aktivierten Lintregeln.
