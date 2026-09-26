# Monatsanalyse und Korrektur der Baumkanten — 26. September 2026

## Quelle, Zeitraum und Qualität

Live-Abruf am 26.09.2026 über Playwright-MCP aus der angemeldeten HA-Browserinstanz. HA 2026.9.3, Integration 0.27.1. Zeitraum **27.08.–25.09.2026**, Europe/Berlin (UTC+2); UTC-Grenzen `[2026-08-26T22:00Z, 2026-09-25T22:00Z)`. Sämtliche Energien und Leistungen dieses Berichts sind **DC**.

Daten: Recorder-Stundenstatistiken, 5-Minuten-Mittel/Minima/Maxima je Modul und die vorab archivierten Tagesprognosen aus `get_issued_forecast`. Leistungsmittel × Intervalllänge ergeben Energie. Aktuelle Live-Prognosen werden nicht als damals bekannte Tagesprognose ausgegeben.

Zwei beschädigte 5-Minuten-Intervalle: **27.08. 09:05, M3** (Mittel ca. 3,171 MW; Stundenwert plausibel) und **19.09. 06:40, M7** (Mittel ca. 3,112 MW; auch Stundenmittel ca. 259,3 kW beschädigt). In beiden Fällen beträgt das Maximum ca. 27,06 MW. Keine Messstatistiken in HA geändert.

Filter: negative, fehlende und nicht endliche Werte sowie Modulwerte über 1,25 × Wp ausschließen; im 5-Minuten-Raster auch das Maximum prüfen. Verbleiben **8.638 vollständige 5-Minuten-Intervalle** und **719 gemeinsame Vergleichsstunden**. Am 19.09. fehlt 06–07 Uhr auf beiden Seiten des Energievergleichs; fehlende Energie wird nicht als null ersetzt.

## Energie und zeitlicher Fehler

| Datenbasis | Ist kWh | Archiviert korrigiert kWh | RAW kWh | Bias korrigiert |
|---|---:|---:|---:|---:|
| 719 gültige Stunden | 207.464 | 210.200 | 217.532 | +1.32 % |
| 29 vollständige Tage (ohne 19.09.) | 199.506 | 202.416 | 209.094 | +1.46 % |

Über 379 Tageslichtstunden (aufgezeichnete Sonnenhöhe > 0): MAE **144.7 Wh pro Stunde**, WAPE **26.45 %**. Die kleine Monatssummenabweichung ist daher kein Beleg für eine zeitlich genaue Prognose.

Wochenwechsel: 10.–16.09. −8,76 %, 17.–23.09. +13,10 % auf gültigen Stunden. Wetterprognose, wechselnder Sonnenstand und historische Lernzustände wirken gemeinsam; diese Zahlen erlauben keine ausschließliche Zuordnung zur Baumkante. Historische Geometrieänderungen sind nicht vollständig archiviert.

### Alle Tagesvergleiche

| Datum | Stunden | Ist kWh | Prognose kWh | RAW kWh | Bias |
|---|---:|---:|---:|---:|---:|
| 2026-08-27 | 24 | 10.357 | 8.556 | 8.573 | -17.39 % |
| 2026-08-28 | 24 | 7.618 | 10.959 | 10.909 | +43.86 % |
| 2026-08-29 | 24 | 11.430 | 11.219 | 10.640 | -1.85 % |
| 2026-08-30 | 24 | 7.998 | 7.905 | 8.803 | -1.17 % |
| 2026-08-31 | 24 | 8.944 | 8.307 | 8.957 | -7.12 % |
| 2026-09-01 | 24 | 9.461 | 8.609 | 9.149 | -9.00 % |
| 2026-09-02 | 24 | 7.957 | 9.256 | 9.776 | +16.32 % |
| 2026-09-03 | 24 | 7.244 | 8.321 | 8.438 | +14.87 % |
| 2026-09-04 | 24 | 7.500 | 8.593 | 9.088 | +14.57 % |
| 2026-09-05 | 24 | 4.813 | 3.264 | 3.860 | -32.17 % |
| 2026-09-06 | 24 | 10.484 | 9.911 | 10.736 | -5.47 % |
| 2026-09-07 | 24 | 6.718 | 5.982 | 6.128 | -10.96 % |
| 2026-09-08 | 24 | 8.900 | 7.921 | 8.252 | -11.00 % |
| 2026-09-09 | 24 | 1.477 | 1.359 | 1.433 | -7.95 % |
| 2026-09-10 | 24 | 7.015 | 6.064 | 6.298 | -13.55 % |
| 2026-09-11 | 24 | 6.424 | 4.800 | 4.949 | -25.28 % |
| 2026-09-12 | 24 | 6.983 | 7.105 | 7.591 | +1.75 % |
| 2026-09-13 | 24 | 3.429 | 3.396 | 2.931 | -0.97 % |
| 2026-09-14 | 24 | 3.776 | 4.250 | 3.879 | +12.56 % |
| 2026-09-15 | 24 | 8.664 | 7.403 | 7.163 | -14.55 % |
| 2026-09-16 | 24 | 6.148 | 5.704 | 5.567 | -7.23 % |
| 2026-09-17 | 24 | 7.152 | 8.570 | 8.275 | +19.82 % |
| 2026-09-18 | 24 | 6.616 | 7.213 | 7.607 | +9.03 % |
| 2026-09-19 | 23 | 7.958 | 7.783 | 8.438 | -2.19 % |
| 2026-09-20 | 24 | 6.861 | 7.663 | 8.218 | +11.70 % |
| 2026-09-21 | 24 | 4.889 | 5.677 | 6.219 | +16.12 % |
| 2026-09-22 | 24 | 4.390 | 6.031 | 6.086 | +37.37 % |
| 2026-09-23 | 24 | 7.438 | 8.302 | 8.889 | +11.62 % |
| 2026-09-24 | 24 | 1.615 | 2.227 | 1.926 | +37.91 % |
| 2026-09-25 | 24 | 7.208 | 7.849 | 8.754 | +8.89 % |

### Stundenprofil über den Monat

| Ortszeit | Stunden | Ist kWh | Prognose kWh | Bias |
|---|---:|---:|---:|---:|
| 00–01 | 30 | 0.000 | 0.000 | +0.00 % |
| 01–02 | 30 | 0.000 | 0.000 | +0.00 % |
| 02–03 | 30 | 0.000 | 0.000 | +0.00 % |
| 03–04 | 30 | 0.000 | 0.000 | +0.00 % |
| 04–05 | 30 | 0.000 | 0.000 | +0.00 % |
| 05–06 | 30 | 0.000 | 0.000 | +0.00 % |
| 06–07 | 29 | 0.155 | 0.151 | -2.67 % |
| 07–08 | 30 | 4.630 | 6.484 | +40.05 % |
| 08–09 | 30 | 22.388 | 21.388 | -4.47 % |
| 09–10 | 30 | 33.877 | 31.499 | -7.02 % |
| 10–11 | 30 | 33.187 | 32.321 | -2.61 % |
| 11–12 | 30 | 28.243 | 32.194 | +13.99 % |
| 12–13 | 30 | 30.925 | 31.548 | +2.02 % |
| 13–14 | 30 | 23.698 | 25.540 | +7.77 % |
| 14–15 | 30 | 11.386 | 10.309 | -9.45 % |
| 15–16 | 30 | 7.521 | 6.757 | -10.16 % |
| 16–17 | 30 | 5.433 | 5.401 | -0.58 % |
| 17–18 | 30 | 3.845 | 3.953 | +2.80 % |
| 18–19 | 30 | 1.820 | 2.208 | +21.30 % |
| 19–20 | 30 | 0.357 | 0.448 | +25.52 % |
| 20–21 | 30 | 0.001 | 0.000 | -85.60 % |
| 21–22 | 30 | 0.000 | 0.000 | +0.00 % |
| 22–23 | 30 | 0.000 | 0.000 | +0.00 % |
| 23–24 | 30 | 0.000 | 0.000 | +0.00 % |

### Modulerträge auf denselben 719 Stunden

| Modul | Wp | DC-Ertrag kWh | kWh/kWp |
|---|---:|---:|---:|
| M1 | 370 | 12.792 | 34.6 |
| M2 | 370 | 30.626 | 82.8 |
| M3 | 370 | 27.415 | 74.1 |
| M4 | 430 | 20.647 | 48.0 |
| M5 | 430 | 15.959 | 37.1 |
| M6 | 430 | 38.319 | 89.1 |
| M7 | 430 | 36.443 | 84.8 |
| M8 | 430 | 25.263 | 58.8 |

Die Erträge verschiedener Ausrichtungen/Neigungen sind kein isoliertes Maß für Baumverluste. M3/M2 und M7/M6 sind geometrisch besser vergleichbar als M4/M8.

## Bestimmung und Gegenprüfung der Baumkontur

Sonnenstände mit `core/solpos.py::sun_position` zur Mitte jedes 5-Minuten-Intervalls neu berechnet; Standortwerte aus dem HA-Rekonfigurationsdialog stimmen mit HA überein. Die vorherige Exploration mit Recorder-Sonnenwinkeln wird durch diese Berechnung ersetzt.

Referenzpaare M2/M6, M7/M6, M3/M2 und M4/M8. Referenzleistung mindestens 150 W (M4/M8: 65 W), relative Spannweite `(max−min)/mean` höchstens 0,20, Referenz mindestens 1° oberhalb ihrer bestehenden Horizontlinie. Einfallsprojektionen > 0,15; Wp und Neigung rechnerisch berücksichtigt. M3 zusätzlich nur bei freiem M6 und normiertem M2/M6-Verhältnis > 0,90.

Normierung am Median der frühen, geeigneten Werte zwischen 110° und 118° Azimut. Quotient < 0,75: Schattenindikator; > 0,90: Freisichtindikator; dazwischen keine Klassifikation. Diese Grenzen sind Analyseentscheidungen, keine physikalische Messung der Beam-Durchlässigkeit.

5°-Azimutbereiche mit jeweils mindestens drei Schatten- und drei Freisichttagen; Höhenraster 0,5°. Minimiert wird der pro Messtag gleich gewichtete Klassifikationsfehler; bei Gleichstand unterer Median der gleich guten Rasterpunkte. Leave-one-day-out: den geprüften Tag beim Höhenfit vollständig zurückhalten. Nur bessere Bereiche behalten und anschließend die vollständige interpolierte Kurve einschließlich Randanschlüssen erneut prüfen. Isolierte Einzelbereiche werden nicht übernommen, weil sie keine belastbare Kronenbreite bestimmen.

Randanschlüsse liegen bei den äußeren Bereichsgrenzen (Zentrum ±2,5°) exakt auf der alten Kontur. Außerhalb dieser Abschnitte bleiben Höhe und Durchlässigkeit unverändert. Die vorhandenen Baum-τ-Werte werden auf neue Baumstützpunkte übertragen, nicht aus Leistungsquotienten neu geschätzt.

**Statistische Grenze:** Referenznormierung und Auswahl der verbesserten Bereiche verwenden den Monatsdatensatz. Leave-one-day-out prüft die Höhenanpassung, ist nach dieser Auswahl aber kein vollständig unabhängiger Wirksamkeitsnachweis. Die Zahlen bewerten Schattenindikatoren, keine kWh-Verbesserung.

| Modul | Referenz | Intervalle | Fehler alt | Fehler neue Kurve | Tage | SVF alt → neu |
|---|---|---:|---:|---:|---:|---|
| M2 | M6 | 519 | 64 (12.3 %) | 34 (6.6 %) | 23 | 0.798372 → 0.797315 |
| M7 | M6 | 497 | 88 (17.7 %) | 24 (4.8 %) | 24 | 0.759871 → 0.738437 |
| M3 | M2 | 339 | 68 (20.1 %) | 45 (13.3 %) | 23 | 0.757583 → 0.759715 |
| M4 | M8 | 306 | 113 (36.9 %) | 113 (36.9 %) | 23 | 0.576098 → 0.576098 |

SVF ist der Faktor für den isotropen Diffuslichtanteil (`core/horizon.py::sky_view_factor`), kein Gesamtenergie-Faktor. M7: Änderung um rund −2,14 Prozentpunkte. M4 wird unverändert belassen; der einzige isolierte Kandidat bei 160° reicht nicht zur Bestimmung einer neuen Baumkontur. M1/M5/M6/M8 bleiben ebenfalls unverändert.

### Konkreter Konfigurationsdiff

Nur die folgenden Ausschnitte der Horizonttabellen ändern sich. Schreibweise **Azimut/Höhe/τ**, Winkel in Grad, 0° Azimut = Nord, im Uhrzeigersinn. Endpunkte sind unveränderte Anschlusswerte der zuvor interpolierten Kontur. Keine Koordinaten, Entity-IDs oder Zugangsdaten in dieser Dokumentation.

**M2, Bereich 132.5°–157.5°:**

| Stand | Stützpunkte im geänderten Bereich |
|---|---|
| Vorher (vorhandene Knoten) | 139.99/15/0, 140/38.5/0.55, 150/45/0.55 |
| Nachher (einschließlich Anschluss) | 132.5/15/0, 135/37/0.55, 140/38/0.55, 145/38/0.55, 150/46/0.55, 155/46/0.55, 157.5/44.625/0.55 |

**M3, Bereich 122.5°–137.5°:**

| Stand | Stützpunkte im geänderten Bereich |
|---|---|
| Vorher (vorhandene Knoten) | 123.99/15/0, 124/40/0.3 |
| Nachher (einschließlich Anschluss) | 122.5/15/0, 125/36/0.3, 130/38/0.3, 135/40/0.3, 137.5/42.5962/0.3 |

**M7, Bereich 122.5°–157.5°:**

| Stand | Stützpunkte im geänderten Bereich |
|---|---|
| Vorher (vorhandene Knoten) | 137.99/15/0, 138/36/0.3, 150/39/0.3 |
| Nachher (einschließlich Anschluss) | 122.5/15/0, 125/31/0.3, 130/31.5/0.3, 135/33.5/0.3, 140/38/0.3, 145/43.5/0.3, 150/43.5/0.3, 155/42.5/0.3, 157.5/26.8125/0.3 |

### Referenztag 25.09.: Übergänge und verbleibende Grenzen

| Modul | Beobachteter Einbruch, ungefähr | Modellbeginn vorher | Modellbeginn nachher |
|---|---|---|---|
| M2 | 10:15 | 10:53 | 10:31 |
| M3 | 09:30 | 09:49 | 09:48 |
| M7 | 09:40–10:00 | 10:46 | 09:50 |

Die Modellzeiten stammen aus einer minutenweisen Abtastung mit dem Rechenkern; Messzeitpunkte sind Beginn der 5-Minuten-Intervalle. M2 bleibt am Referenztag noch verspätet. Bei M3 verbessert sich die Monatsklassifikation vor allem durch korrigierte Höhen bei höherem Sonnenstand; der frühe Schattenbeginn und das zu frühe Ende (weiter 11:41, beobachtete Erholung ungefähr 12:20–12:30) sind **nicht behoben**. Die dafür erforderlichen Abschnitte bestehen die gewählte Absicherung noch nicht. M7 bleibt modelliert bis 11:48 statt zuvor 11:33 verschattet.

Weitere getrennte Befunde: Morgendliche Brüstungskante bei M6/M7 am 25.09. öffnet etwa 08:25–08:30 zu spät; nicht Teil dieser Baumkorrektur. Die Gebäudekante bei M4/M8 um 13:40–13:50 liegt wesentlich näher an den Messübergängen.

## Übernahme, Nachweis und Wiederherstellung

Übernahmestatus: **LIVE GESPEICHERT UND ZURÜCKGELESEN**, 26.09.2026 um 13:46 Uhr Europe/Berlin. Alle acht Module stimmen mit der geprüften Site überein; ausschließlich M2/M3/M7 wurden geändert. Integrationsstatus: erfolgreich, frisch, nicht degradiert, kein Fehler. Alle acht DC-Kanäle vorhanden; Lernschalter unverändert. 12 Bias-Zellen und alle Verschattungskarten-Bins erhalten; Quantilhistorie leer, alle drei Drift-Fehlerfolgen null. Archivumfänge unverändert. Der schnelle Lerner befindet sich nach dem Neuladen zunächst im normalen Cold-Start.

Vor Übernahme: vorhandener Site-Validator und byte-treuer normalisierter Roundtrip erfolgreich; unveränderte Modulparameter und nicht bearbeitete Module geprüft. Außerhalb der Änderungsbereiche sind Horizonthöhe und τ auf einem 0,1°-Raster identisch. **205 bestehende Tests bestanden** (Sonnenstand, Horizont, τ-Profile, Diffuslicht und Konfigurationsvalidierung).

Die Konfigurationsänderung löst laut `coordinator.py::_reconcile_config_fingerprint` die vorgesehene Bias-Neuanpassung aus; Quantilreste und Drift-Fehlerfolgen werden auf die neue Grundlage gebracht. Kein zusätzlicher Reset oder Bootstrap. Ein Rückspielen der Site stellt die Geometrie wieder her, nicht automatisch den vorherigen Quantil-/Lernzustand.

Lokale, Git-ignorierte Sicherungen: `.ha-dev/analysis-2026-09-26/` enthält Originalmessdaten (`month.json`), ursprüngliches Rekonfigurationsformular (`reconfigure-before.json`), `site-before.json`, `site-candidate.json`, Fit-/Übergangsergebnisse und die ausführbaren Analyseskripte. Keine Passwörter, Tokens oder Browser-Sitzungsdaten exportiert.

Reproduktion: aus dem Repository `.venv/bin/python .ha-dev/analysis-2026-09-26/analyze.py`, anschließend `verify.py` und `report.py` im selben Ordner. Die Skripte arbeiten ausschließlich lokal. Vor erneutem Berichtslauf den dokumentierten Übernahmestatus beachten. Rohdaten sind absichtlich kein Bestandteil des Git-Repositories.

Rücknahme: im HA-Eintrag **Neu konfigurieren**, die gesicherte Site aus `site-before.json` einsetzen; separate Formularfelder mit `reconfigure-before.json` abgleichen. Einmal speichern und erneut aus Diagnostics zurücklesen.

Keine Änderung am Prognosealgorithmus oder `DEFAULT_SITE`. Beim Speichern wurde jedoch ein vorhandener Fehler in `config_flow.py::async_step_reconfigure` sichtbar: `async_update_entry` akzeptiert `data`, nicht `data_updates`. Der Aufruf wird auf die vollständige Zusammenführung `{**entry.data, **_structural_data(site, user_input)}` korrigiert. Name und unbekannte Felder bleiben erhalten, strukturelle Optionskopien werden weiterhin atomar entfernt. SPEC §7.1 und CHANGELOG sind ergänzt; kein Versionsbump.

## Was dieser Nachweis nicht liefert

Ein exakter rückwirkender kWh-Vergleich der neuen Geometrie ist mit dem verfügbaren `get_issued_forecast`-Export nicht möglich: Archivierte Strahlungskomponenten und Modulkurven fehlen in dessen Antwort. Aktuelle Wetterdaten oder aktuelle Lernzustände wären kein gleichwertiger Ersatz. Die Monatsfehler bleiben ein Bericht der damals archivierten Prognose.

Nachbeobachtung: Bei den nächsten drei klaren Tagen die gleichen DC-Modulpaare und Sonnenstände vergleichen, insbesondere M2 vor 10:31, M3 bei Ein- und Austritt sowie M7 nach 11:48. Zusätzlich Bias-Neuanpassung und wieder anwachsende Quantilfüllung prüfen. Für diese künftigen Messungen wird hier noch kein Ergebnis behauptet.

## Zusätzlicher Rekonfigurationsfix

Live-Fehler: HTTP 500 mit `TypeError: ConfigEntries.async_update_entry() got an unexpected keyword argument data_updates`. Der ursprüngliche Test-Fake akzeptierte beliebige Schlüssel und verdeckte den Fehler.

Neuer Regressionstest `test_reconfigure_uses_ha_data_api_and_preserves_existing_data`: semantischer Fehlschlag am unveränderten HEAD `d915bd3` im isolierten Worktree; nach dem Fix erfolgreich. Der Test prüft zusätzlich die echte HA-Methodensignatur, Erhalt von Name/Metadaten und unverändertes ursprüngliches Entry-Dict. Der gemeinsame Fake akzeptiert jetzt nur `data` und `options`.

Gesamtsuite: **2.502 bestanden, 240 erwartete Skips** für Golden-Vergleiche bei niedrigem Sonnenstand; **96,20 % Coverage**, Gate 95 % bestanden. Ruff und mypy sauber. Die JavaScript-Tests liefen mit Node.

Live wurde über das Studio-Code-Server-Terminal im Playwright-Browser ausschließlich der fehlerhafte Aufruf ersetzt; vorherige Datei unter `/config/custom_components/balcony_solar_forecast/config_flow.py.before-20260926` gesichert und neue Datei mit Python `compile` geprüft. SHA-256 der gepatchten Live-Datei: `5056713f325e6aac6740716d2c276ec3872fccc483447a2dd58f924658731f79`. Anschließend HA-Neustart zum Laden des Python-Moduls. Andere lokal abweichende Live-Dateien wurden nicht überschrieben.

Bei der ersten Live-Übernahme lag der Fix nur im Workspace und als direkter Live-Patch vor, ohne Commit oder Release. Das wich vom vorgesehenen Veröffentlichungsprozess ab. Die reguläre Auslieferung erfolgt mit **0.27.2** über Release-PR, bestandene Prüfungen, Merge und Tag/Release. Anschließend ersetzt die HACS-Installation den direkten Patch. Der Vorabvergleich aller 47 getrackten Python-/JavaScript-/JSON-/YAML-Dateien der Live-Integration gegen 0.27.1 zeigte ausschließlich die Änderung in `config_flow.py`; weitere unveröffentlichte Softwareänderungen müssen nicht übernommen werden. Die anlagenspezifischen Baumkanten bleiben HA-Konfiguration und werden nicht zum globalen Default.
