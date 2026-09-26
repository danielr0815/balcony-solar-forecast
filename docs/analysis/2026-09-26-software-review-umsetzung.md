# Umsetzung des Software-Reviews vom 26. September 2026

Bezug: [Review mit R01–R44](2026-09-26-software-review.md).
Arbeitsbranch: `fix/software-review-2026-09-26`, Ausgangscommit
`a7991621f93e3631109a5dbc1bd8ae9a09aba47f`.

Die Befunde wurden in fachlichen Korrekturen, abgegrenzten Komponenten,
wirksamen Tests und aktualisierten Verträgen umgesetzt. Die Versionsnummer
wurde für den beauftragten Release synchron auf 0.28.0 angehoben.
Die Installation auf der Live-Anlage gehört zum anschließenden Betriebsablauf. Lokale Untersuchungsnotizen und die vorhandene
Workspace-Datei bleiben außerhalb der Änderungen.

## Zuordnung aller Befunde

| Befund | Umsetzung und maßgeblicher Nachweis |
|---|---|
| R01 | Festes, ältester-zuerst durchlaufenes Nachholfenster. Eigener persistierter Tagesmarker für Inverterkalibrierung. Späte alte Labels überschreiben keine neuere Gesundheitsmeldung. `test_review_orchestration.py`, `test_review_health_order.py`. |
| R02 | Cachevergleich zählt noch nutzbare Zukunftsintervalle; nach normaler Altersgrenze hat ein alter Payload kein Vetorecht. `test_review_weather.py`. |
| R03 | Vollständiges Parsing und regelmäßige, aufsteigende Zeitachsen vor dem Cachewechsel; kein nutzbares Zukunftsintervall ist ein Fehlversuch. Letztes gutes Wetter bleibt atomar erhalten. `test_review_weather.py`. |
| R04 | Gemeinsame Verfügbarkeitsgrenze für Service und Energy; Diagnostics berichtet aktuelle Provenienz. Keine alte Kurve nach fehlgeschlagenem Update. `test_review_weather.py`. |
| R05 | Entry-Arbeit wird zentral erfasst, beim Unload/Stop abgebrochen und vor dem finalen Flush abgewartet. Auch laufender Service-Bootstrap kann nicht verspätet importieren. `test_review_lifecycle.py`. |
| R06 | Bootstrap korrigiert komplette Gruppen mit θ und zweitem Clamp, bevor die gemeterte Teilmenge aggregiert wird. Mehrgruppen-/Teilmessungsregression. |
| R07 | Slow-only-POA wird am tatsächlichen Temperaturpunkt neu in DC umgerechnet. Mehrere Ross-/Temperaturfälle prüfen Bootstrap/Live-Parität. |
| R08 | Tages-/Bin-Cap liegt im gemeinsamen Quantiltrainer und berücksichtigt bereits gespeicherte Samples desselben Tages. |
| R09 | Überläufe numerischer Store-Konversion ergeben neutrale Werte; gültige Nachbarsektionen bleiben erhalten. `test_review_core_state.py`. |
| R10 | Endliche positive Modul-Wp und endliche saisonale Elevationsraster werden erzwungen. Config- und Kernregressionen. |
| R11 | Produktive Wetter-/Tagesklassen werden zentral geprüft; Positivtests verwenden die wirkliche Taxonomie. |
| R12 | Registry-Cache gehört zur Verbindung und überlebt normale State-Pushes/Detach. Tatsächliche Kartenklassen werden im DOM-Harness ausgeführt. |
| R13 | Karten und Dashboard tragen die Entry-Auswahl durch alle Serviceaufrufe; mehrdeutige automatische Auswahl zeigt einen Hinweis. Mehranlagen-Szenarien. |
| R14 | HA-Zeitzone steuert Kalender und Archive; DST-Tage haben 23/25 getrennte Stunden. Harness läuft unter zwei Browserzeitzonen. |
| R15 | Nur bestätigte Archivantworten werden gecacht; temporäre Fehler bleiben sichtbar und werden erneut versucht. |
| R16 | Validierung unterscheidet PASS, FAIL, INCOMPLETE und ERROR in Text, JSON und Exitstatus. Synthetische vollständige und lückenhafte Zweitagespakete prüfen echte Checks. |
| R17 | PowerShell nutzt vorhandenes uv oder ein vorhandenes Python via `py -3`/`python`; erst uv installiert den Projektinterpreter. Separater Windows-Smoke prüft den Startpfad. |
| R18 | `WeatherCache` besitzt Wetter-/Abrufzustand; `LearnerOperations` besitzt Task-Lebensdauer und Mutations-Lock; Verfügbarkeitszugriff und Intraday-Referenz haben eigene Grenzen. Der Coordinator delegiert ohne zweite Zustandskopie. |
| R19 | `core.plane_physics` ist der gemeinsame Transpositions-/Horizont-/Gate-/Temperaturpfad. Historischer Physikvergleich und vollständige Engine-Ergebnisvergleiche sichern die Extraktion. |
| R20 | `GroupCorrection` und `EnergyTotals` bündeln zusammengehörende Resultate; unsicheres ungenutztes `with_total` entfernt. Öffentliches Ergebnisformat bleibt kompatibel. |
| R21 | Frozen-Attribute und verschachtelter Datenbesitz sind getrennt dokumentiert. Copy-on-write sowie Snapshot-/Import-/Export-Isolation werden geprüft. |
| R22 | Erforderliche Store-Setter sind direkte Schnittstellen und schlagen bei Fehlern sichtbar fehl. Testdoubles erfüllen den Vertrag; Produktionscode erzeugt keine fehlenden Testlocks mehr nachträglich. |
| R23 | Slot-Minuten/-Sekunden/-Stunden und Mittelpunkte werden zentral abgeleitet. Fachliche Clipping-Reserve ist benannt und begründet. |
| R24 | Gemeinsame Kartenmodule für Daten/Registry, HA-Kalender und zugängliche UI-Bausteine. Karten behalten Darstellung und lokale Interaktion. |
| R25 | Separate echte HA-Suite: Config-Flow, Plattformen/Registry, Services/Energy, Options-Reload, Disk-Store, zwei Entries und Unload. Kein PHACC-/sys.modules-Ersatz für den Laufzeitkern. |
| R26 | Kommentar-/Quelltextbehauptung durch echte Klassen-, Service- und DOM-Szenarien ersetzt. |
| R27 | Risikoregressionen für Cache, Nebenläufigkeit, Gruppen-Clipping, Ausfälle und Zeitzonen; separater Branch-Coverage-Bericht plus gezielte semantische Mutationen. |
| R28 | Gemeinsame Coordinator-, Wetter-, Store- und Validierungshelpers unter `tests/helpers/`; echte HA-Tests ergänzen die gezielt kleinen Unit-Doubles. |
| R29 | Eingecheckter Generator, getrennte Inputs und gepinnte pvlib-Umgebung reproduzieren Referenzvektoren. Fehlendes Golden-Artefakt ist ein Fehler. |
| R30 | Engine-Äquivalenz vergleicht den vollständigen aktuellen Ergebnisvertrag inklusive AC und Slow-only; gezielt veränderte Felder werden erkannt. |
| R31 | Regel 6 unterscheidet Bugfix-RED, Refactoring-Äquivalenz und Nachrüstung vorhandener korrekter Verträge mit geeignetem Wirksamkeitsnachweis. |
| R32 | Ergänzender ununterdrückter mypy-Ratchet erfasst bisher ausgeblendete Kernmodule und kritische HA-Grenzen. Neue Fehler scheitern; bestehende Diagnosen stehen explizit in einer prüfbaren Baseline. |
| R33 | AST-Gate erzwingt HA-/stdlib-Grenze einschließlich funktionslokaler Imports; lazy aiohttp-Ausnahme bleibt eng begrenzt. |
| R34 | Read-only Release-Preflight prüft Versionen, SPEC, Changelog und grünen Validate-Lauf des exakten Main-Commits vor dem Veröffentlichungsjob. Keine Veröffentlichung während dieser Umsetzung. |
| R35 | CI-/Devcontainer-Setup verwendet `uv sync --locked`; Prüfungen laufen mit `--no-sync`. |
| R36 | Mindestversion wird exakt aus `hacs.json` installiert, keine gleitende Patchversion. Separate Gegenprobe einschließlich echter HA-Lifecycle-Suite. |
| R37 | BACKFILL erklärt eingefrorene Walk-forward-Zustände, Slow-only-Temperatur, zweiten Clamp, Sonnenzeit, vollständige Signatur und `--tz`. |
| R38 | README, Setup-/Test-/Release-Anleitungen und Dashboard-Dokumentation beschreiben denselben Arbeitsstand; ersetzte Anweisungen entfernt. |
| R39 | Regeln, Kernkommentare und Wissensbasis unterscheiden RAW, Slow-only × θ und unclamped AC-Pfad konsistent. |
| R40 | AC-Punktprognose wird nicht mehr P50 genannt. Empirische Bandgrenzen bleiben unabhängig; öffentliche DC-P50 bleibt Median. |
| R41 | Recorderfehler, echte Datenlücke und weiterhin angezeigte alte Daten sind unterscheidbar; Zeitpunkt der letzten erfolgreichen Aktualisierung wird sichtbar. |
| R42 | Verknüpfte Labels, gedrückte Zustände, Diagrammbeschreibung und native ausklappbare Wertetabellen für beide Karten. |
| R43 | Reset, direkter Import, Rollback, Nightly und Bootstrap teilen denselben Mutations-Lock; gezielte Reentranz nur innerhalb desselben Tasks. |
| R44 | Gesättigte Messungen sind zensierte Labels und senken den Intraday-Skalar nicht. Echte ungesättigte Verluste behalten den vor dem Clamp liegenden Modellbezug. Live- und Recorder-Rearm-Regressionen. |

## Nachweismethode

Neue Fehlerregressionen wurden auf einem separaten Worktree des Ausgangscommits
ausgeführt. Dort scheitern unter anderem zehn Wetter-/Verfügbarkeitsfälle,
drei Nebenläufigkeitsfälle, zwei Catch-up-/Kalibrierungsfälle, drei
Gesundheitschronologie- und zwei Clippingfälle semantisch. Im Kern zeigen zwölf
zusätzliche Regressionen die Defekte; zwei Wächter beweisen die zuvor übersehene
AC-/Slow-only-Ergebnisabweichung. Die Frontendtests reproduzieren elf
Verhaltensfehler und die fehlende Entry-ID des Generators am alten Stand.

Verhaltensneutrale Extraktionen sind separat abgesichert: 4.000 deterministische
Physikfälle gegen eingefrorene alte Berechnungen sowie 1.200 Slot-Aggregationen
über UTC, Berlin und Auckland. Der unabhängige Generator reproduziert alle
64 Sonnenstands- und 768 POA-Referenzen mit der gepinnten pvlib-Version exakt
auf die eingecheckten vier Dezimalstellen.

## Abschließende lokale Prüfungen

Stand: 26. September 2026, Python 3.14.4.

| Prüfung | Ergebnis |
|---|---|
| Portable Gesamtsuite, gelocktes HA 2026.7.4 | 2.589 bestanden, 240 übersprungen; 96,18 % Statement-Coverage, 95-%-Gate erfüllt. |
| Separater Branch-Coverage-Lauf | 2.589 bestanden; 2.188 von 2.434 Zweigen erfasst (89,89 %), kombinierte Statement-/Branch-Coverage 94,70 %. |
| Portable Gesamtsuite, exakt HA 2026.3.0 | 2.589 bestanden, 240 übersprungen. |
| Echte HA-Lifecycle-Suite | Je ein umfassendes Szenario unter HA 2026.7.4 und 2026.3.0 bestanden. |
| Ruff | Ohne Befunde. |
| Bestehender mypy-Scope | 20 Dateien ohne Fehler. |
| Erweiterter Typ-Ratchet | Bestanden; 51 explizite Alt-Diagnosen (47 Kern, vier HA-Grenzen), keine neuen. |
| Kern-Importgrenze | 21 Module bestanden. |
| SPEC-Vertrag | 14 Tests bestanden. |
| Patchprüfung | `git diff --check` ohne Befunde. |
| Gezielte semantische Mutationen | Alle drei erkannt: Gruppen-Clamp, eingefrorenes Label und Catch-up-Lücke. |
| Unabhängige pvlib-Referenzen | Alle 64 Sonnenstands- und 768 POA-Vektoren reproduziert. |
| Frontend-/DOM-Regressionsauswahl | 44 Tests bestanden, einschließlich unterschiedlicher Zeitzonen. |

Die 240 übersprungenen Fälle sind vorhandene parametrisierte Referenzfälle bei
zu niedrigem Sonnenstand. Die jeweils fünf Warnungen stammen aus bestehenden
HA-/backoff-Deprecations. Die portable Suite und der echte HA-Lifecycle werden
bewusst getrennt gestartet, damit die Unit-Test-Doubles den HA-Lauf nicht
beeinflussen.

Windows-Smoke und GitHub-Workflows sind konfiguriert, aber lokal nicht
auf ihren Zielrunnern ausgeführt. Es wurde weder veröffentlicht noch auf der
Live-Anlage installiert.

## Bewusste Grenzen

Die Typbaseline macht bestehende Schulden sichtbar und verhindert neue; sie
behauptet keine vollständig strikte Typisierung. Frozen Dataclasses sind keine
tief unveränderlichen Container: der dokumentierte Datenbesitz und unabhängige
Snapshots sichern den jetzigen Vertrag ohne inkompatible Store-Umstellung.
Der Coordinator bleibt Orchestrator mehrerer Lernschichten; die neu getrennten
Zustandsbesitzer sind die Ausgangspunkte für weitere gezielte Vereinfachungen.

Die echte HA-Suite startet keinen Recorder; Recorder-Lese-/Labelverträge
werden gezielt in der portablen Suite geprüft. Der Node-DOM-Harness prüft
Kontrollen, Tabellen und Ereignishandler, ersetzt aber keinen eigenständigen
Screenreader-/Browser-Accessibility-Audit.
