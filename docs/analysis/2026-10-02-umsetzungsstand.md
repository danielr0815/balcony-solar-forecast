# Umsetzung des Prognose- und Community-Plans

Stand: 2026-10-03. Auftrag: gesamter
[Umsetzungsplan](2026-10-02-umsetzungsplan-prognose-ux-open-source.md).
Arbeitsbranch: `feat/forecast-quality-and-weather-evidence`, Basis `315b4309`.
Arbeitsprotokoll, keine Releasefreigabe oder zusätzliche Ist-SPEC.

## Implementierter Stand

| Paket | Umsetzung und Grenze |
|---|---|
| K1 Zeitvertrag | Historische Strahlungs-Endstempel werden auf Intervallstarts verschoben; Wetter und Recorder teilen lokale Tagesgrenzen einschließlich 23/25-Stunden-Tagen. CLI und HA verwenden dieselbe Zeitzone. |
| K2 Labelqualität | Gemeinsame numerische, Frozen- und Kollapsgates; Recorder verlangt W, aktuelle Zustände normalisieren W/kW. Bootstrap prüft Kanalabdeckung und trainiert akzeptierte Tage je Lauf nur einmal. Produktive HA-/CLI-Läufe verlangen Wetter für alle geometrischen Tageslichtstunden und mindestens 75 % vollständige Tageslicht-Labels je Kanal; der gemeinsame Kern berücksichtigt DST und nichtganzzahlige UTC-Offsets. |
| K3/K4 Physik | AC aus serviertem Postclip-DC; diffuse Sicht aus positiven einfallenden Strahlen. ADR-0024 dokumentiert Übergang, erhaltenes Lernen und Rollback. Ein reproduzierbarer synthetischer Bericht quantifiziert beide Änderungen getrennt. |
| E1 Archive | Exakte AC-Kurve, ursprünglicher Berechnungszeitpunkt, Archivzeitpunkt, begrenzte Provenienz und ausgegebene Slotbänder. Identität bindet vor dem Executor an den Berechnungszustand; überholte Archivierungen werden verworfen. Legacyfelder bleiben optional. |
| P1 Datenschutz | Providerfehler enthalten weder Standortquery im Text noch verketteten Traceback. Export löst Registryrollen eines ausgewählten Entries auf, berücksichtigt Umbenennungen und verwendet HA-Zeitzone/W-Statistik. Capturehashes verhindern unbemerkte Änderungen. |
| E0/E2 Reproduktion | Lizenzierte synthetische Hashbundles, chronologische Splits, kausale Verfügbarkeit, gemeinsame Qualitäts-/Zielmaske, MAE/RMSE/WAPE/Bias und Intervallscores. Auswertungen nach Horizont, Tagesabschnitt, Sonnenhöhe, Forecast-/beobachtetem Wetter, Sättigung und Lernstatus. Tagescluster-Konfidenzintervalle aktivieren kein Modell. |
| U1 Karten | Fehlende/unavailable Werte bleiben unbekannt; Null bleibt Null. Tabellen, Teilabdeckung und tatsächlicher Archivzeitpunkt sichtbar. Registryänderungen aktualisieren beide Karten über eine Subscription. Fokus/offene Tabellen bleiben erhalten. |
| U2 Lernstatus | η-Kalibrierung verlangt wirksame Evidenz. Quantile zeigen datierte Tage, positive Slots und kalt/teilweise/gelernt; Wetterdegradation bleibt getrennt sichtbar. |
| U3 Einstieg | Geführter Standort-/Modul-/Review-Dialog mit gemeinsamem Gruppenvertrag und alternativem erweitertem Editor; neutrales Ein-Modul-Beispiel, optionale Messquellen und Metadaten-/Einheitenprüfung mit eindeutigen DC-/AC-Rollen. Rekonfiguration zeigt Module/Lernfolgen vor atomarem Speichern. Bestehende `name`-Schlüssel bleiben stabile Modul-IDs; `display_name` ist getrennt editierbar, ohne Fingerprint-/Lern-/Archivmigration. Auswahl stellt über `module_id` wieder her (ADR-0025). Gruppenlabels bleiben reine Labels. |
| A1 Struktur | Messqualität, Presenter, Modellfingerprint, Quantilbereitschaft, Trainingsentscheidungen und Archivdataclass haben eigene Verantwortungen. Historische Imports bleiben kompatibel. Issued-Referenzen, meterte Teilmengen, Bias-Samples und geometrischer Trainer sind gemeinsam in `core/learning_inputs`; der Sampletyp hat einen einzigen Eigentümer. Weitere Typen können bei fachlichen Änderungen folgen. |
| T1 Tests/Plattformen | Echte SQLite-Recorder→Nightly→Store→HA-Reload-Kette für kW, DST und Lücken. Minimaler Kern-Testdependency-Graph und Windows-CI-Job. Reale Chromiumprüfung mit synthetischen Daten, isoliert von MCP-Profilen. Windows/HA-Minimum-CI müssen auf der Zielplattform noch laufen. |
| W1 Beobachtung | Zeitkohärente, gruppen-/orientierungsbalancierte Indikatoren; unbekannt bei fehlender Vielfalt, Sättigung oder alten/ungültigen Quellen. Geometrische Klassen nach Flächennormalen statt Winkelrundung; Quellenwechsel starten den Rampenvergleich neu. RAM-Diagnosesignal ohne Prognose-/Lernmutation. Abregelung bleibt mit Wolken verwechslungsfähig. |
| W2/F2 Experimente | Ausführbarer Vergleich Summen-/Panelsignal/Panel+Wetter mit genau einer begrenzten Restkorrektur. Separate vollständige lokale Tagesresidualbänder mit kausal verfügbaren Tagen und kaltem Zustand. Keine produktive Aktivierung ohne Holdout-Nutzen. |
| W3/F1 Forschungsgrenzen | Physikalischer Replay mit getrennten persistenten Lernzuständen, ursprünglichem Wetter, verfügbaren Anfangszuständen, eigenen eingefrorenen Referenzen sowie Geometrie-/Wettervarianten ist ausführbar. Gemeinsame DC-Auswertung über chronologische Splits. Intraday/Drift/η/Ensemble/Next-day-Freeze sind explizit ausgeschlossen. Reale dichte Panel-/unabhängige Schattenreferenzen und Holdouts bleiben erforderlich. |
| O1 Open Source | Englischer Einstieg/Architektur, Beitragsanleitung, Securityweg und Issueformulare. Daten-/Codelizenz getrennt. Personengebundene Co-Authorpflicht im Entwicklerhandbuch entfernt. |
| D1 Browserisolation | Benannte MCP-Instanzen mit getrennten Profil-/Outputpfaden. Zwei Prozessstubs belegen Pfade und Lebensdauer. Der echte Kartenbrowser verwendet eine frische Instanz. Zwei gleichzeitig angemeldete MCP-Instanzen wurden nicht als Live-Abnahme getestet. |

## Nachweise

Neue Regressionstests auf unverändertem Parent-Code:

- Messqualität/Zeitvertrag: **13 semantische Fehler, 1 Kontrolle grün**;
  nach Reparatur **14 grün**.
- Archiv/Providerfehler: **3 semantische Fehler** (AC-Verlust,
  fehlgeschlagenes Update als frischer Stand, Standortquery im Fehlertext).
- Elektrischer Vertrag: **1 semantischer Fehler, 2 Kontrollen grün**;
  nach Reparatur alle drei grün.
- Karten: **4 semantische Parent-Fehler** für bool/Leerstring/negative Messwerte und leere Archivsumme; korrigierter Node-Harness grün.
- Exportnormalisierung: **4 semantische Parent-Fehler** für bool/nichtendliche Zahlen, echte Null als grüne Kontrolle.
- η-Sichtbarkeit: **2 semantische Fehler** bei unzureichender Evidenz.
- Bootstrap: wiederholter Tag und unzureichende Kanalabdeckung
  **2 semantische Fehler**, nach Reparatur grün.
- Nichtganzzahliger UTC-Offset: **1 semantischer Parent-Fehler** (falsche Stundenidentität), nach Reparatur grün.
- Scoreboard: teilweise beschädigte Kurven (`invalid`, bool, negativ)
  **3 semantische Fehler**, nach Reparatur grün; Scoreboardsuite **18 bestanden**.

Zusätzlicher semantischer Nachweis gegen die eingefrorene Vorfassung des neuen
Panelindikators (nicht gegen den Main-Parent, der ihn noch nicht enthält):
Ein Ausfall hoch messender Kanäle erzeugte bei unveränderten verbleibenden
Messwerten eine falsche gemeinsame Rampe; die korrigierte Fassung startet neu.
Geometrische Grenztests sichern Rundungsgrenzen, Nordübergang und horizontale
Flächen gegen künstliche Ausrichtungsvielfalt ab.

Refactoring-Äquivalenz: Fingerprint **250 geseedete Fälle** identisch zum Parent;
Archivdataclass **1.000 geseedete Fälle** identisch zur vor dem Schnitt
eingefrorenen Fassung. Lernreduktionen und geometrischer Trainer: **250 geseedete vollständige Ausgaben** identisch zur eingefrorenen Vorfassung, einschließlich Teilmessung und vier Zeitzonen. Nightlyentscheidungen sind zusätzlich über Kombinationen
von Idempotenz, Labels, Kollaps und Freeze abgesichert.

Abschließend geprüft: **2.748 portable Tests bestanden**, 240 übersprungene
Golden-Randfälle, **96,27 % Statement-Coverage**. Echte HA-Suite: **8 bestanden**.
Chromium: Fokus, Tastatur, offene Tabellen, 375-Pixel-Ansicht, keine Seitenfehler;
öffentliche synthetische Screenshots stehen in `docs/images/`.
Ruff, mypy (30 Quellen), Kern-Importgrenze (31 Module) und Typ-Ratchet sind grün.
Minimales Testumfeld ohne HA: **1.654 Kerntests bestanden**, 241 übersprungen
(einschließlich optionalem YAML-Test). SPEC-Vertrag: **14 Tests bestanden**.
Typbaseline nach Archivaufteilung: **49 explizite Altfehler**, zuvor 51.

## Empirische Freigaben und offene Arbeit

Die sieben intensiv ausgewerteten Tage sind Entwicklungsdaten, kein
abschließender Modelltest. Ergänzend wurde am 03.10. ein
[realer Archivvergleich](2026-10-03-live-validierungsnachweis.md) über 88
beitragende Tage / 1.337 Stunden ausgeführt: bestehende Korrektur −4,82 %
Stunden-MAE gegenüber issued RAW. Der zusätzliche 02.10. verbessert die
Tagessumme, verschlechtert jedoch den Stunden-MAE. Das ist kein Nachweis der
noch unveröffentlichten Änderungen und kein unabhängiger 30-Tage-Holdout. W2/W3/F1/F2 benötigen ein chronologisch getrenntes,
mindestens 30 abgeschlossene Tage umfassendes Testfenster und ausreichende
Wetter-/Saisonvielfalt. Synthetische Bundletage beweisen Arithmetik,
keine Prognoseverbesserung.

Offen: reale unabhängige Schattenreferenzen, dichte Paneltraces und Feldversuche,
freigegebene Fremdanlagen-Bundles sowie mindestens 30 unabhängige Testtage mit
weiteren Wetter-/Anlagenregimen. Der physikalische persistente Lernadapter und
statische Geometrie-/Wettervarianten sind umgesetzt. Originale historische
Wetterbilder/Anfangszustände können bei fehlender Erfassung nicht nachträglich
als ursprüngliche Inputs erfunden werden. Die übrigen Laufzeitregler sind
absichtlich außerhalb dieses day-ahead Forschungsadapters. Der geführte Editor
und identitätserhaltende Anzeigeumbenennung sind umgesetzt; keine ID-Neuvergabe
oder Storemigration ist dafür nötig.

Releasevorbereitung: synchroner Versionsstand 0.29.0 im Release-PR. Keine Installation,
Softwarepatches oder Konfigurationsänderungen an der Live-Anlage.
