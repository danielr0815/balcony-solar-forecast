# Siebentageanalyse und Projektreview vom zweiten Oktober 2026

Die Lernkorrekturen verbessern die Prognose der untersuchten Woche deutlich. Die archivierte Tagesprognose liegt insgesamt **4,52 % über dem DC-Ist**, die reine Physik **21,64 %**. Die zeitliche Form bleibt die wichtigste Genauigkeitsbaustelle: morgens fehlt Leistung, während einzelne späte Vormittagsstunden deutlich überschätzt werden. Für eine breite öffentliche Nutzung haben korrekte historische Lerninputs, nachvollziehbare Datenqualität und ein einfacher Anlagenstart Vorrang vor zusätzlichen Modellparametern.

Dieses Review verbindet aktuelle HA-Messdaten mit der Prüfung von Kern, HA-Schicht, Frontend, Spezifikation, Tests und Entwicklungswerkzeugen. Es enthält bestätigte Fehler und getrennt davon Vorschläge, deren Nutzen erst durch Experimente belegt werden muss. **Es wurden keine Prognosealgorithmen, Tests, SPEC oder Live-Einstellungen geändert.** Der Bericht ist eine Entscheidungsvorlage für fokussierte PRs.

**Vertiefung nach der Wolkenrückfrage:** Die frühere Bezeichnung als „überwiegend klare Woche“ war durch die Daten nicht gedeckt. Die Wetterklassen stammen aus Prognosen; tatsächliche Wolken sind damit weder ausgeschlossen noch unabhängig gemessen. Die folgenden Ergänzungen prüfen zusätzliche Wettermodellverläufe, die Verteilung der Stundenfehler und die Empfindlichkeit der Modulvergleiche. Der [detaillierte Umsetzungsplan](2026-10-02-umsetzungsplan-prognose-ux-open-source.md) ersetzt die frühere grobe Reihenfolge durch getrennte Arbeitspakete mit Abhängigkeiten, Tests und Abnahmekriterien.

## Datenbasis und Aussagegrenzen

Live-Abruf am **02.10.2026, etwa 17:15 bis 17:24 Uhr Europe/Berlin**, ausschließlich über die angemeldete lokale Playwright-MCP-Browserinstanz. HA **2026.9.4**, aktuell installierte Integration laut Diagnostics **0.28.0**. Geprüfter Repository-Commit: `315b4309e71fbdf4830a2ec89d22cc57d079870d`.

Hauptfenster sind die **sieben abgeschlossenen lokalen Tage 25.09.–01.10.2026**, UTC-Grenzen `[2026-09-24T22:00Z, 2026-10-01T22:00Z)`. Der laufende 02.10. wird separat behandelt. Es handelt sich nicht um ein bis zum Abrufzeitpunkt verschobenes 168-Stunden-Fenster.

Datenquellen: Diagnostics des Config-Entries, aktuelle Integrationssensoren, `get_issued_forecast`, `dump_shademap`, gezielte Recorder-Stunden- und Fünf-Minuten-Statistiken aller acht DC-Ports sowie AC, aktuelle Prognoseleistung, Intraday-Skalar und Sonnenwinkel; außerdem Recorder-Zustandsverläufe von Prognose-, Band-, Lern- und Wetterstatussensoren. Es wurden keine Tokens, Passwörter, Cookies oder Browser-Sitzungsdaten ausgelesen oder exportiert. Rohdaten und lokale Auswertung liegen Git-ignoriert unter `.ha-dev/analysis-2026-10-02/`.

**Energie- und Prognosevergleiche sind DC**, sofern ausdrücklich AC angegeben. Recorder-Leistungsmittel werden mit der Intervalllänge integriert. Modulmeans müssen endlich, nichtnegativ und höchstens `1,25 × Wp` sein; vorhandene Maxima dürfen diese Grenze ebenfalls nicht überschreiten. Alle **168 gemeinsamen Stunden und 2.016 gemeinsamen Fünf-Minuten-Intervalle** sind vollständig und bestehen diese Plausibilitätsregeln. Keine Messwerte wurden verändert oder ausgeschlossen. Vollständige Intervalle beweisen keine unabhängige Kalibrierung der Messhardware.

„Vollständig“ bezeichnet hier die vorhandenen Statistikzeilen aller benötigten Quellen. Es beweist nicht, dass innerhalb jedes Intervalls lückenlos frische Rohmessungen eintrafen; ein Recorder-Mittelwert allein enthält diesen Abdeckungsnachweis nicht. Diese Grenze wird im künftigen Messvertrag ausdrücklich erfasst.

Zur Gegenprüfung wurden anschließend auch **53.711 aufgezeichnete Zustände aller acht DC-Ports über die gesamte Woche** gelesen. Darin findet sich kein negativer, nichtendlicher, unbekannter oder unavailable-Zustand. Dies stützt die numerische Wochenbasis. Stille Transportlücken und wiederholte identische Heartbeats lassen sich daraus weiterhin nicht vollständig beurteilen.

Für Tagesvergleiche werden die damals archivierten, unverändert zurückgelesenen Prognosen verwendet. Aktuelle Lernzustände oder nachträglich bezogenes Wetter ersetzen keinen damaligen Forecast. Das Archiv enthält keine vollständige Software-/Konfigurationshistorie pro Ausgabe. Insbesondere wurden am 26.09. mittags Baumkonturen geändert; die Ausgaben für 25.09. und 26.09. stammen davor. Die gesamte Woche kann daher nicht als kontrolliertes Experiment einer einzigen Version oder Geometrie gewertet werden.

Die archivierten Ausgaben entstanden überwiegend um 01:30 Uhr lokal, am 27.09. bereits um 00:04 und am 29.09. um 00:13. **„Issued“ ist hier der konkrete archivierte Nachtstand**, keine durchgehend identische Vorabend-Lead-Time.

Wettergrundlagen stammen von [Open-Meteo](https://open-meteo.com/); die PV-Kurven entstehen durch lokale Transposition und Lernkorrekturen. Hinweise zur Datenattribution stehen in der [Open-Meteo-Datenlizenz](https://open-meteo.com/en/licence).

## Prognosequalität der sieben Tage

### Tagesenergie

| Lokaler Tag | DC Ist kWh | DC korrigiert kWh | DC RAW kWh | Fehler korrigiert | Stunden MAE bei Tageslicht Wh |
|---|---:|---:|---:|---:|---:|
| 25.09.2026 | 7,208 | 7,849 | 8,754 | +8,89 % | 129,0 |
| 26.09.2026 | 6,891 | 7,654 | 8,535 | +11,08 % | 126,6 |
| 27.09.2026 | 6,657 | 6,940 | 7,800 | +4,26 % | 111,8 |
| 28.09.2026 | 6,203 | 6,198 | 7,604 | −0,09 % | 89,4 |
| 29.09.2026 | 6,068 | 6,375 | 7,679 | +5,07 % | 74,9 |
| 30.09.2026 | 6,006 | 5,913 | 7,139 | −1,55 % | 86,0 |
| 01.10.2026 | 5,950 | 6,088 | 7,208 | +2,32 % | 113,9 |
| **Gesamt** | **44,983** | **47,018** | **54,719** | **+4,52 %** | **104,8** |

Mittlerer absoluter Tagesfehler: **0,319 kWh korrigiert**, gegenüber **1,391 kWh RAW**. Mittlerer absoluter prozentualer Tagesfehler: **4,75 %**. Die im HA-Scoreboard angezeigten 0,640 kWh betreffen dagegen dessen 14 gewertete Tage und widersprechen diesem Wochenwert nicht.

Tageslichtstunden werden für diesen Bericht über ein positives aufgezeichnetes Sonnenhöhenmaximum bestimmt: **85 Stunden**, davon **78 als clear prognostiziert**, fünf mixed, jeweils eine fog und overcast. Die Verteilung beschreibt die modellinterne Wetterklasse bei Ausgabe, nicht beobachtete Wolkenfreiheit. Daraus folgt keine allgemeine Aussage für tatsächlich klare, neblige oder wechselnd bewölkte Bedingungen und keinen Winterbetrieb.

| Vergleich auf denselben Tageslichtstunden | MAE Wh pro Stunde | RMSE Wh pro Stunde | WAPE | Energiefehler |
|---|---:|---:|---:|---:|
| Archiviert RAW | 162,6 | 250,3 | 30,72 % | +21,65 % |
| Archiviert korrigiert | **104,8** | **177,3** | **19,80 %** | **+4,53 %** |
| Zeitgleich aufgezeichnete aktuelle DC-Prognoseleistung | 118,0 | 184,3 | 22,30 % | −3,46 % |

WAPE ist die Summe absoluter Stundenfehler geteilt durch die gemessene Energie. Die aufgezeichnete aktuelle Prognoseleistung wird stündlich zeitgemittelt; sie hat einen anderen Ausgabehorizont und kann bereits auf Messungen reagieren. Ihr Vergleich ist **kein fairer Day-ahead-Benchmark und keine isolierte Intraday-Ablation**. Er zeigt jedoch, dass die jeweils aktuelle Kurve in dieser Woche trotz besserer Gesamtsumme nicht durchgehend die genauere Stundenform hatte. Eine schnellere oder aggressivere Intraday-Regel ist daher nicht bereits als Verbesserung belegt.

### Wiederkehrender Fehler im Stundenverlauf

| Lokales Stundenintervall | DC Ist über sieben Tage kWh | Fehler archiviert korrigiert |
|---|---:|---:|
| 08–09 Uhr | 4,285 | **−36,1 %** |
| 09–10 Uhr | 8,762 | −12,4 % |
| 10–11 Uhr | 5,324 | **+31,8 %** |
| 11–12 Uhr | 4,553 | **+60,9 %** |
| 12–13 Uhr | 9,909 | +0,6 % |
| 13–14 Uhr | 6,786 | +9,9 % |
| 15–16 Uhr | 1,275 | −22,8 % |

Die kleine Tagesabweichung verdeckt gegenläufige Stundenfehler. Ein globales Anheben oder Absenken von `bifacial_beam_gain`, Albedo oder θ würde diese Form nicht zielgenau beheben. Als nächste Untersuchung eignen sich Modulresiduen nach Sonnenazimut/-höhe, insbesondere morgendliche Brüstung und Baum-Eintritt/-Austritt bei M2/M3. Die Stundenfehler allein beweisen diese Ursachen noch nicht; archivierte modulweise Wetter-/Korrekturprovenienz fehlt im öffentlichen Export.

Die vertiefte Auswertung lokalisiert **80,85 % der absoluten Tageslicht-Stundenfehler zwischen 08 und 12 Uhr**. Über alle Tageslichtstunden addieren sich die absoluten Fehler auf **8,908 kWh**; die Summe der absoluten Tagesfehler beträgt dagegen **2,232 kWh**, der saldierte Wochenfehler **+2,035 kWh**. Diese drei Größen bewerten unterschiedliche Eigenschaften. Ein guter Tagessaldo genügt für die zeitliche Planung von Verbrauchern nicht.

| Lokales Intervall | Stunden MAE RAW Wh | Stunden MAE korrigiert Wh | Wiederholung über sieben Tage |
|---|---:|---:|---|
| 08–09 | 193,6 | 235,6 | Korrigiert an sechs Tagen zu niedrig |
| 09–10 | 98,0 | 154,9 | Korrigiert an allen sieben Tagen zu niedrig |
| 10–11 | 398,7 | 241,9 | Beide Varianten an allen sieben Tagen zu hoch |
| 11–12 | 586,3 | 396,4 | Beide Varianten an allen sieben Tagen zu hoch |
| 12–13 | 271,8 | 29,1 | Korrektur deutlich näher am Ist |

Zwischen 09 und 10 Uhr beträgt der saldierte RAW-Fehler nur +1,29 %, korrigiert dagegen −12,37 %. Zwischen 11 und 12 Uhr liegt das gemessene DC-Ist an allen sieben Tagen in einem engen Bereich von 623–681 Wh, während der korrigierte Fehler +284 bis +544 Wh beträgt. Das wiederkehrende Muster begründet eine gezielte Formprüfung. Es identifiziert noch nicht Baumkante, Wettermodell oder einzelne Lernschicht: Der vorhandene RAW-/Korrigiert-Vergleich trennt Shademap und θ nicht vollständig.

### Was über Wolken bekannt ist und was offenbleibt

Wolken sind im Prognosemodell berücksichtigt: `fetcher.py::parse_weather` übernimmt prognostizierte GHI/DNI/DHI und Wolkenfelder; die Physik verarbeitet direkte und diffuse Strahlung getrennt. `core/bias.py::classify_cloud` verwendet bei ausreichender Sonnenhöhe überwiegend den prognostizierten Clear-Sky-Index, zusätzlich Nebelregeln und einen Wolkendeckungs-Fallback. **`clear` heißt daher auch fachlich nicht zwingend „keine Wolke am Himmel“.** Ein hoher Wolkenflächenanteil allein bestimmt weder Wolkenoptik noch die tatsächliche Einstrahlung auf das Modul.

Im HA-Entity-Inventar wurde kein unabhängiger lokaler Strahlungs- oder Wolkenmesssensor gefunden. Zusätzlich ausgewertet wurden **171 Recorder-Zustände einschließlich Attributänderungen** von `weather.forecast_home`, Quelle MET Norway. `last_updated` wurde für die Attributzeitachse verwendet; `last_changed` allein würde Wolkenänderungen bei gleichbleibendem Wetterzustand übersehen. Auch diese Quelle liefert ein Wettermodell, keine unabhängige Bodenbeobachtung.

| Tag | MET-Modell Wolkendeckung 09–10 Uhr | MET-Modell Wolkendeckung 10–11 Uhr |
|---|---:|---:|
| 25.09. | 0,0 % | 0,0 % |
| 26.09. | 0,0 % | 0,2 % |
| 27.09. | 45,0 % | 31,7 % |
| 28.09. | 0,0 % | 0,0 % |
| 29.09. | 2,4 % | 1,6 % |
| 30.09. | 93,0 % | 95,3 % |
| 01.10. | 92,2 % | 84,3 % |

Die Werte sind zeitgewichtete Mittel der in HA angezeigten Modellwerte. Elf als `clear` archivierte Tageslichtstunden überlappen mit mindestens 50 % Wolkendeckung dieses zweiten Modells. Wegen der unterschiedlichen Definitionen ist das kein formaler Klassifikationsfehler, aber ein weiterer Grund, die Woche nicht als beobachtet wolkenfrei zu behandeln. Das Vormittagsmuster besteht sowohl bei geringer als auch bei hoher modellierter Wolkendeckung. Zufällige Wolkendurchzüge sind damit als alleinige Erklärung weniger überzeugend; wiederkehrende Wettermodellfehler und tatsächliche Wolkeneffekte bleiben möglich.

Für eine unabhängige Gegenprüfung eignet sich satellitengestützte Strahlung am Standort, etwa DWD MTG über die [Open-Meteo Satellite Radiation API](https://open-meteo.com/en/docs/satellite-radiation-api). Die dokumentierte Auflösung liegt bei etwa 2,5 km und zehn Minuten; Werte beziehen sich rückwärts auf ihr Mittelungsintervall. Satellitenwerte sind abgeleitete Flächenwerte mit eigener Unsicherheit, keine direkte Messung am Balkon. Ein angefragter Abruf wurde bislang nicht ausgeführt: Die automatische Freigabeprüfung verlangt eine konkrete Zustimmung zur zusätzlichen Übermittlung der privaten Standortkoordinaten. Die Freigabe ist angefragt; bis dahin basiert keine Schlussfolgerung auf vermeintlichen Satellitenbeobachtungen.

Eine vollständige Aufteilung „x % Wetterfehler, y % Verschattung“ ist mit den gegenwärtig gesicherten Daten nicht identifizierbar. Dafür fehlen insbesondere unabhängige Strahlungsreferenzen und die vollständigen damaligen Wetter-/Modulinputs. Später beobachtetes Wetter darf bei einer Diagnose die Ursachen eingrenzen, aber nicht nachträglich als vermeintlich damals verfügbares Prognosewissen in Training oder Benchmark eingehen.

Auch die vorhandenen Shademap-Gates sind kein unabhängiger Wolkenbeobachter. `_nightly.py::day_is_measured_clear` und `core/shademap.py::is_quasi_clear` verbinden unter anderem ein globales Ist/RAW-Tagesgate, prognostiziertes `kc` und Stabilität der Ist/Modell-Ratio. Das 80-%-Tagesgate würde bei sechs der sieben Tage passieren. Mehrere gleichmäßig bewölkte Stunden können innerhalb eines insgesamt hellen Tages trotzdem eine stabile Ratio erzeugen. Der Weg zu einer fälschlich gelernten Schattendämpfung ist damit fachlich möglich; ein konkretes Training solcher Zellen wurde für diese Woche nicht nachgewiesen. Für Bias/Quantile bleiben echte Wetterprognosefehler dagegen wichtige Kalibrierlabels.

Der ergänzende Nutzervorschlag, alle Panelverläufe gemeinsam mit der allgemeinen Wetterprognose zu verwenden, ist fachlich sinnvoll als **lokaler Wetter- und Störungsindikator**. Für zwei Vormittage wurden zusätzlich 343–397 Zustandsänderungen pro Port/Fenster gelesen; die mediane Änderungslücke liegt bei etwa 35 Sekunden, die größten Lücken bei bis zu 279 Sekunden. Daraus folgt noch keine gleichmäßige Messfrequenz: Der Recorder protokolliert Zustandsänderungen, nicht zwingend jeden identischen neu gemeldeten Wert. Das [kritisch geprüfte Konzept](2026-10-02-konzept-wolkenindikator-aus-paneldaten.md) beschreibt Signalbildung, Grenzen, Rückkopplungsschutz und Tests. Eine verlässlich gemessene Wolkenbedeckung lässt sich aus PV-Leistung allein nicht versprechen.

Intraday-Skalar in den aufgezeichneten Fünf-Minuten-Tageslichtmitteln: insgesamt ungefähr **0,669 bis 1,368**, ohne Mittelwerte an den 0,5-/1,5-Clamps. Die täglichen Mediane steigen von 0,861 am 25.09. auf ungefähr 1,0 am Ende der Woche. Das frühere Muster eines dauerhaft extremen Morgen-Skalars ist hier nicht belegt. Einzelne kurzzeitige Spitzen innerhalb eines Fünf-Minuten-Mittels werden damit nicht ausgeschlossen.

### AC und Modulerträge

AC-Ist aus dem konfigurierten, bereits vorzeichenkorrigierten Gesamtsensor: **41,467 kWh**. Archivierter AC-Export: **43,840 kWh**, entsprechend **+5,72 %**. Die sieben Exporte verwenden jeweils das zum Ausgabezeitpunkt gespeicherte η. Die unten beschriebene allgemeine AC-Archivschwäche begrenzt die Interpretation dieses Exports; aus dieser Woche folgt kein Nachweis, dass Clipping sie hier tatsächlich verursacht hat.

| Modul | Wp | Gemessener DC Ertrag kWh |
|---|---:|---:|
| M1 | 370 | 2,489 |
| M2 | 370 | 6,090 |
| M3 | 370 | 5,049 |
| M4 | 430 | 5,158 |
| M5 | 430 | 3,378 |
| M6 | 430 | 10,034 |
| M7 | 430 | 7,173 |
| M8 | 430 | 5,612 |

Unterschiedliche Ausrichtungen und Neigungen verhindern die Interpretation dieser Ertragsrangfolge als reine Verschattungsrangfolge.

### Baumkonturen an neuen Tagen

Der heutige Site-Stand stimmt, abgesehen von redigierten Koordinaten, exakt mit der am 26.09. geprüften Kandidatenkonfiguration überein. Die fünf vollständig folgenden Tage **27.09.–01.10.** wurden ohne neue Höhenanpassung geprüft. Sonnenstände wurden aus aktuellen HA-Standortkoordinaten am Intervallmittelpunkt neu berechnet. Referenzpaare, Leistungs-/Stabilitätsgates sowie die Quotientenschwellen 0,75/0,90 und die September-Normierung bleiben unverändert; die Einzelheiten stehen im [Septemberbericht](2026-09-26-monatsanalyse-baumkanten.md).

| Modul und Referenz | Geeignete neue Intervalle | Fehlklassifikationen alte Kontur | Fehlklassifikationen aktuelle Kontur |
|---|---:|---:|---:|
| M2 gegen M6 | 117 | 36 | **22** |
| M7 gegen M6 | 128 | 56 | **6** |
| M3 gegen M2 | 59 | 19 | **16** |
| M4 gegen M8 als unveränderte Kontrolle | 61 | 7 | 7 |

Die neue Kontur beschreibt den gewählten M7-Leistungsindikator in diesen zeitlich zurückgehaltenen Folgetagen deutlich besser. M2 verbessert sich, M3 nur gering. **Das ist eine Prüfung von Schattenindikatoren, kein Nachweis einer bestimmten eingesparten Prognoseenergie oder vermessener Baumhöhen.** Die beiden Konturen haben außerdem unterschiedliche Diffusfaktoren. Die fünf Tage sind nicht als wolkenfrei belegt; daraus folgt keine neue automatische Konturanpassung.

Die ursprünglichen Gates berücksichtigen Wolken indirekt: zeitgleicher Referenzport, Mindestleistung, höchstens 20 % Spannweite innerhalb eines Fünf-Minuten-Intervalls und Referenz über dessen konfiguriertem Horizont. Ruhige gleichmäßige Bewölkung passiert solche Gates. M7/M6 besitzen dieselbe Ausrichtung und Neigung, M3/M2 ebenfalls; gemeinsame Einstrahlungsänderungen heben sich in solchen Quotienten eher auf. Bei M2/M6 unterscheiden sich die Neigungen 70°/80°. Eine reine Einfallswinkel-Normierung entfernt deren unterschiedliche Reaktion auf Direkt-/Diffusanteile nicht.

Die zusätzliche Sensitivitätsprüfung verändert keine Kontur und passt keine Normierung neu an:

| Prüfung | M2 alte→aktuelle Fehler / n | M7 alte→aktuelle Fehler / n | M3 alte→aktuelle Fehler / n |
|---|---:|---:|---:|
| Ursprüngliche Auswahl | 36→22 / 117 | 56→6 / 128 | 19→16 / 59 |
| Referenzspannweite höchstens 10 % | 21→15 / 93 | 41→6 / 105 | 16→14 / 48 |
| Beide Portspannweiten höchstens 10 % | 5→1 / 57 | 14→0 / 71 | 1→1 / 30 |
| Zusätzlich stabile benachbarte Referenzintervalle | 13→10 / 72 | 32→4 / 83 | 16→14 / 38 |

M7 bleibt auch bei veränderten Quotientenschwellen und ±5 % September-Normierung deutlich besser. M2 ist schwächer gestützt, bei M3 verschwindet der Vorteil in einigen strengeren Auswahlen. Ein Filter auf stabile Zielmodule entfernt allerdings auch reale Schattenübergänge: Er ist eine Sensitivitätsprüfung, kein grundsätzlich besserer Wahrheitsmaßstab. Die benachbarten Intervalle sind außerdem korreliert; 128 Intervalle sind keine 128 unabhängigen Wettertage. Neue Gates wurden nach Sichtung der Woche untersucht und benötigen ihrerseits neue Bestätigungstage.

Zusätzliche Auswahlgrenze bei M3: `geometry_check.py::refok` prüft M2 gegen dessen aktuelle, selbst geänderte Kontur. Der alte/neue M3-Vergleich verwendet zwar dieselben gewählten Intervalle, die Auswahl ist aber nicht unabhängig von der Kandidatengeometrie. Künftige Vergleiche müssen die Referenzauswahl vorher einfrieren und alte, aktuelle sowie konservativ gemeinsame Referenzmasken getrennt ausweisen.

### Lernzustand und Unsicherheit

Aktuelle Diagnostics: alle acht DC-Kanäle vorhanden, kein Verwurfstreak, letzter angenommener Tag 01.10.; Fast, Slow und Day-ahead aktiv, keine aktiven Drift-Verluststreaks. 12 Biaszellen; Shademap-Bins je Modul M1/M5 deutlich seltener besetzt als bei den übrigen Modulen. Aktuelles effektives η **0,9308** aus **1.741** angenommenen Samples. Das sind Momentaufnahmen vom 02.10., keine historischen Wochenzustände.

Nur `clear|morning` und `clear|midday` erfüllen aktuell die Quantil-Trainingsgrenzen: 27 bzw. 24 Samples auf jeweils sechs Tagen. `clear|afternoon` hat 16 Samples; weitere Klassen tragen nur wenige Beobachtungen. Nach der Geometrieänderung am 26.09. wurden abhängige Residuen erwartungsgemäß geleert. Die historischen P10-/P90-Sensoren waren für vier der sieben geprüften Nachtzeitpunkte unbekannt. Drei auswertbare Headline-Bänder umschließen den AC-Ist-Ertrag. Sie wurden allerdings bei **`issued_at + 5 Minuten`** aus den Sensorspuren rekonstruiert und sind keine exakt mit dem Forecast eingefrorenen Bänder. **Weder drei Treffer noch diese zeitlich angenäherte Rekonstruktion belegen eine 80-%-Kalibrierung.** Archivierte vollständige Slotbänder fehlen ohnehin.

Im Sensorabzug des 02.10. um etwa 17:12 Uhr tragen **0 von 46 positiven heutigen DC-Slots** ein nichttriviales P10/P90-Band; morgen **4 von 46**, übermorgen **16 von 48**. Trotzdem lautet das Bandquellenattribut `learned`. Die Headline nennt heute ungefähr 1,125–1,150 kWh AC. Der sichtbare Begriff „gelernt“ braucht deshalb eine Aussage zum tatsächlich abgedeckten Tageslichtanteil; ein schmales oder neutrales Band bedeutet in diesem Zustand keine hohe Sicherheit.

Zustandsverlauf: rund **30 Minuten cached/degraded** in der Woche, zusammen aus zwei Ereignissen; etwa eine Sekunde unavailable beim Reload. Kein anhaltender Wetterausfall. Der laufende 02.10. lieferte bis zum letzten vollständig ausgewerteten Fünf-Minuten-Intervall ungefähr **1,166 kWh DC / 1,099 kWh AC**. Die archivierte DC-Tagesprognose beträgt 1,346 kWh. Der Tag ist noch unvollständig und geht nicht in die Wochenfehler ein.

## Bestätigte Fehler und problematische Verträge

Die Gegenbeispiele verwenden synthetische Inputs am aktuellen Code. Sie beweisen den genannten Pfad, **keine Ursache eines konkreten Livefehlers**. P1 bedeutet zuerst beheben, P2 einen relevanten Folgefehler, P3 eine kleinere Transparenz-/Dokumentationslücke. Dies sind Reviewprioritäten, keine formalen Security-Einstufungen.

### Historischer Bootstrap hat die höchste fachliche Priorität

**P1 — Stundenversatz beim Wetterimport.** `core/openmeteo_backfill.py::parse_hourly_payload` übernimmt den Providerstempel unverändert als Intervallstart. Historische Strahlungswerte sind jedoch Mittel der vorausgehenden Stunde; korrekt ist `start = stamp − 1 h`. `core/bootstrap_build.py::reconstruct_plane_hour` berechnet aktuell den Sonnenstand weitere 30 Minuten danach und verbindet die falsche Recorderstunde. Live- und Ensembleparser normalisieren diese Grenzen bereits.

Gegenbeispiel: APIstempel 10:00Z wird als 10:00 statt 09:00 eingelesen. Bei einem synthetischen 400-Wp-Südmodul ändern sich dadurch Sonnenazimut von richtig 150,36° auf 167,87° und Modellenergie von 252,56 auf 269,96 Wh; der korrekt unter 09:00 abgelegte Recorderwert wird nicht trainiert. Parser-/CLI-Paritätstests sichern derzeit teilweise denselben falschen Stempel. Providerdefinition nachgeprüft in [Previous Runs](https://open-meteo.com/en/docs/previous-runs-api) und [Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api).

**Abnahme:** unabhängiger API-Endstempel→Intervallstart→Recorderkey→Sonnenmittelpunkt-Test, einschließlich Tagesrand und beider Backfillquellen. Danach vollständiger Tagesverlauf. Vor einem späteren Re-Bootstrap vorhandene Importhistorie, Sicherung und abhängige Lernzustände berücksichtigen; kein pauschaler Live-Reset allein aufgrund dieses Reviews.

**P1 — Kollaps und eingefrorene Labels gelangen in Bias und Quantile.** `core/bootstrap_build.py::_process_day_impl` beschränkt vorhandene Ablehnungen teilweise auf Shademap. Ein vollständig ausgefallener Modelltag mit 2.321 Wh Prognose und 0 Wh Ist erzeugt dennoch drei θ-Zellen am 0,5-Clamp und acht Quantilresiduen. Acht bytegleiche 200-W-Werte werden korrekt als frozen erkannt, trainieren aber anschließend ebenfalls drei Biasupdates und acht Quantilsamples. Live quarantänisieren `_actuals.py::_actuals_from_stats` und `_nightly.py::train_and_guard` diese Fehlerklasse wesentlich früher.

**Abnahme:** gemeinsame pure Labelentscheidung vor jeglicher Lernmutation; vollständige Vergleiche von Bias, Shademap, Quantilen und Nutzungsmarkern bei Kollaps, Frozenkanal und gesundem Kontrolltag. Der vorhandene Frozen-Test prüft hauptsächlich Shademap und entdeckt die Folgelücke nicht.

**P2 — Lokaler Kalender wird beim Bootstrap nicht durchgehend verwendet.** `accumulate_days`, `_group_by_day` und `_filter_actuals_for_day` gruppieren nach UTC, obwohl `--tz` laut `docs/BACKFILL.md` den lokalen Tageskalender bestimmt. Zwei Stunden desselben lokalen Tages in Los Angeles werden in zwei Trainings-/Quantiltage aufgeteilt. Für die Referenzanlage liegt der UTC-Rand meist nachts; der Vertrag bleibt für andere Standorte falsch. Wetter, Actuals und Evidenzdatum gemeinsam nach HA-/CLI-Zeitzone gruppieren, Stundenkeys weiterhin in UTC halten. Europa, Auckland, Los Angeles und DST prüfen.

### Messdaten und elektrische Basis

**P2 — Negative und nichtendliche Statistiklabels werden angenommen.** `_actuals.py::_actuals_from_stats` konvertiert Means per `float`, ohne vollständig `finite && >= 0` zu erzwingen. Ein negativer Stundenwert senkt eine ansonsten gültige Tagesenergie; NaN passiert das obere Plausibilitätsgate. Einige nachfolgende Lerner verwerfen solche Inputs, der Readervertrag und die archivierten Actuals sind trotzdem unzuverlässig. Gemeinsame numerische Labelvalidierung mit Coverage-/Ablehnungsgrund einführen und für Bootstrap, Live und Store prüfen. Ungültige Stunden dürfen nicht zu null werden. In dieser Livewoche wurde kein solches Label gefunden.

**P2 — Exportierte AC-Archivkurve ist nicht immer die tatsächlich ausgegebene Kurve.** `_nightly.py::snapshot_issued` und `core/types.py::IssuedSnapshot` speichern DC plus einen η-Skalar; `_services.py::_handle_get_issued_forecast` rekonstruiert AC als `DC × η`. Die Engine benutzt Gruppen-η und einen anderen Clipping-/Korrekturpfad. Echte Engine-Gegenbeispiele liefern bei individueller Gruppen-η eine Exportabweichung von **+7,22 %** und in einem clipenden Fall **−20 %**. Diese Zahlen gelten für die synthetischen Fälle, nicht pauschal für die Anlage.

**Abnahme:** die echte `ac_hourly_wh` additiv im Snapshot persistieren, neue Exporte direkt daraus bilden und Legacy-Rekonstruktion ausdrücklich kennzeichnen. Engine→Snapshot→Service über Gruppen-η, gelernte η und f<1/f>1 mit/ohne Clamp prüfen. Altzustände erhalten, SPEC im selben PR präzisieren.

**P2 — Snapshotzeit kann alte Berechnungen als neu ausgegeben erscheinen lassen.** `snapshot_issued` liest `coord.data` auch bei `last_update_success=False`, übernimmt dessen alten Status und setzt `issued_at` auf die aktuelle Speicherzeit; `computed_at` wird nicht erhalten. Ein Gegenbeispiel erzeugt so einen neuen Snapshot mit `status=fresh`, während `current_forecast` denselben Coordinator als nicht verfügbar behandelt. Ein früher verfügbarer Forecast darf historisch weiter relevant sein, benötigt aber seine ursprüngliche Ausgabe-/Berechnungszeit und den Zustand zum Archivierungszeitpunkt. Speichern, Berechnen, Wetterabruf und gegebenenfalls Provider-Modelllauf sind getrennte Zeiten. Ein Modelllaufzeitpunkt darf bei fehlender Providerangabe unbekannt bleiben.

**P2 — Dokumentierte DC-/AC-Reihenfolge hat einen physikalischen Widerspruch.** `core/engine.py::compute_forecast` korrigiert die DC-Ausgabe nach einem ersten Clamp, AC aber vor seinem Clamp. Synthetisch: η=0,9, AC-Limit 90 W, zunächst 100 W geclampte DC; Korrekturfaktor 0,5 erzeugt **50 W DC und 90 W AC**, obwohl η·DC nur 45 W ist. Die vorhandene SPEC beschreibt die Reihenfolge; dies ist somit eine fachliche Vertragsentscheidung, kein bloß übersehener Implementierungsverstoß.

Vor einer Änderung Anteil betroffener Slots messen und einen gemeinsamen verfügbaren DC-Punkt als Grundlage beider Ausgaben definieren. Ein gegen postclip DC gelerntes θ ist nicht automatisch ein Faktor für preclip DC. Clippingzensierung, Lernerreferenzen, Fingerprint-/Residualinvalidation und SPEC gehören zu dieser Änderung. Ein isolierter Clamp-Patch wäre unzureichend.

**P2 — Diffuse Sichtbarkeit verletzt die Monotonie.** `core/horizon.py::_inner_elevation_integral` klemmt nach dem Integrieren statt unsichtbare Richtungen vor der Integration auszuschließen. Bei einer gültigen profilierten 70°-Ebene verringert ausschließlich mehr Transparenz in einem hinteren Himmelssegment den SVF von **0,100000 auf 0,098926**. Das widerspricht positiver Strahlungsgewichtung. Bei einer anderen synthetischen 30°-Geometrie ergibt eine unabhängige positive-only-Integration 0,696152 statt 0,750000. Dies ist ein Fehler des Diffusfaktors, kein nachgewiesener Gesamtenergiefehler dieser Anlage.

**Abnahme:** sichtbare Integrationsbereiche analytisch oder per unabhängiger Quadratur prüfen; höheres τ darf SVF nicht senken, höherer opaker Horizont ihn nicht erhöhen. Golden-Vektoren ohne solche Geometrie und Gleichheit mit der Altimplementierung entdecken diesen Fehler nicht.

### Frontend und Diagnose müssen die vorhandene Wahrheit darstellen

| Priorität | Konkreter Befund | Codebeleg | Überprüfbare Korrektur |
|---|---|---|---|
| P2 | Unavailable-Prognosesensor mit alten Attributen erscheint weiter als „live“ | `frontend/power_history_card.js::_recomputeForecast`, `_liveForecastTotal` | Zustand prüfen; Linie entfernen oder mit altem Zeitpunkt kennzeichnen; Tag/Woche/Erholung testen |
| P2 | Fehlende Messstunden erscheinen als 0 Wh und Teilresultat als vollständig | `PowerHistoryCard._ingestDay`, `_ingestWeek`, `_table` | Validitätsmaske, null/„—“, Teilabdeckung; echte Null, fehlender Kanal, Outlier und laufender Tag unterscheiden |
| P2 | Registrycache bleibt nach Entity-Umbenennung auf der alten ID | `frontend/card_data.js::entityRegistry`, `ensureRegistry` | Registryänderungen oder gezielte Revalidierung fehlender IDs berücksichtigen; normale State-Pushes weiterhin ohne zusätzliche Abfrage |
| P3 | η-Quelle lautet „learned“ schon bei n=1, obwohl ab n=20 angewandt | `sensor.py::PowerNowSensor.extra_state_attributes`, `core/inverter_cal.py::effective_eta` | Provenienz aus effektivem Wert ableiten; Kandidat/Fortschritt separat darstellen |
| P2 vor öffentlicher Diagnosefreigabe | Providerfehler können Koordinaten im Diagnostics-Errorstring und Log behalten | `fetcher.py::OpenMeteoFetcher._request_once`, `WeatherCache.async_refresh`, `diagnostics.py::async_get_config_entry_diagnostics` | Sichere Fehlercodes oder Query-Redaktion an HTTP-Grenze; gesamten Dump und Log nach synthetischen Koordinaten prüfen |
| P2 | Validierungs-CLI kann mit `--entry-id` Prognose B gegen fest kodierte Messwerte A prüfen | `scripts/validation/bsf_fetch.py::fetch_all`, `HOURLY_STAT_IDS`; `bsf_data.py::LOC`, `SID_*` | Registryrollen, konfigurierte Quellen und HA-Zeitzone je Entry auflösen; Zwei-Site-/Rename-Test und rollenbasierter Offline-Loader |

Alle Tabellenbefunde wurden mit Produktionsfunktionen bzw. vorhandener echter Kartenklasse und DOM-Harness reproduziert. Beispiel zur Privacy-Lücke: Entry-Koordinaten sind korrekt redigiert, ein echtes `aiohttp.ContentTypeError` übernimmt aber die synthetischen Werte aus seiner URL in `state.last_error`. Es wurden dabei keine tatsächlichen Zugangsdaten oder Standorte exportiert.

## Prognoseverbesserungen mit überprüfbarem Nutzen

**Zuerst eine allgemeine Messgrundlage schaffen.** Das bestehende Scoreboard und die umfangreichen Softwaretests sind wertvoll. Es fehlt ein öffentlich reproduzierbarer, zeitlich zurückgehaltener Prognosebenchmark. Der nächste sinnvolle Ausbau ist ein versioniertes Datenbundle mit Ausgabestand, Wetterquelle/Lead-Time, Config-Fingerprint, Modulkurven und RAW/Slow-only/θ/servierter Kurve. DC und AC explizit archivieren; Qualitätsmasken und Zeitzone mitführen. Gegen identische Stunden und Tage evaluieren, beispielsweise morgens fester Stand für die nächsten zwei Stunden und separater Vorabendstand für den Folgetag.

**Tagesunsicherheit eigenständig kalibrieren.** `core/quantiles.py::train_quantiles` lernt Stundenresiduen; das Addieren marginaler Slot-P10/P90 ergibt im Allgemeinen keine Tagesquantile. Zuerst empirische Deckung, Über-/Unterschreitungen, Bandbreite und Pinball-/Interval-Score nach Horizont/Wetterklasse auswerten, mit Anzahl unabhängiger Tage. Danach Tagesresidualring oder tageweise gemeinsame Residualverläufe gegen das bestehende Band testen. Für Automationen ist eine belastbare Tagesreserve nützlicher als ein nur breiter gezeichnetes Band. §11.3 sollte behauptete Kalibrierung nur so weit beschreiben, wie sie wirklich nachgewiesen ist.

**Formfehler vor globalen Parametern untersuchen.** M2/M3-Baumränder und die morgendliche Brüstung mit neuen klaren Referenztagen prüfen. Vorschläge nach Sonnenstand, Vergleichsmodul und Evidenz ausgeben. Keine gleichzeitig auf diese sieben Tage abgestimmten Änderungen an Albedo, Ross, Beam-Gain und τ. Ein besserer statischer Prior kann kompensierende Lerner entlasten; ein dauerhafter Gewinn muss in nachfolgenden Tagen bestehen.

**Shademap-Vertrauen um Alter ergänzen.** `ShademapBin` trägt kumulatives n, aber kein Alter/letzten Besuch. EMA verändert Werte, während altes hohes n weiterhin Shrinkage und Pooling beeinflusst. Ein zeit-/saisonabhängiges effektives Gewicht wäre eine begründete Forschungsoption bei wechselnden Baumkanten; zuerst Alter und Residuen messen, dann additive Migration und Holdout-Experiment. Ein aktueller Livefehler daraus ist nicht bewiesen.

**Zusätzliche Modelle nur nach Residualnachweis.** Per-Modul-Temperaturkoeffizient oder Montageparameter können für fremde Module sinnvoller sein als globale Lernerkorrekturen. Unterschiedliche Wettermodelle erst auf identischen Ausgabehorizonten und zurückgehaltenen Perioden vergleichen. [Previous Runs](https://open-meteo.com/en/docs/previous-runs-api) liefern feste Lead-Time-Offsets; [Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api) verbindet die ersten Stunden einzelner Läufe. Diese Datenquellen sind deshalb keine beliebig austauschbaren damaligen Vorabendprognosen. Neue Runtime-Abhängigkeiten oder komplexere Modelle sind aktuell nicht als notwendiger erster Schritt belegt.

## Benutzererfahrung für eine breite Veröffentlichung

Der größte Einstiegshinderungsgrund ist bereits in [ADR 0023](../adr/ADR-0023-onboarding-standortkonfiguration.md) beschrieben und weiterhin offen: `config_flow.py::_current_values` kopiert die komplette achtmodulige `DEFAULT_SITE` mit fremden Messentity-IDs und Horizonten. Eine neue Anlage erhält zunächst eine falsche Geometrie und nicht passende Messquellen. Die Engine ist konfigurierbar, ihr Erstkontakt bleibt jedoch stark auf die Referenzanlage ausgerichtet.

1. **Neutraler Start und geführte kleine Anlage.** Eine Ebene, Standort aus HA, offener Himmel, keine fremden Messsensoren. Referenzanlage als ausdrücklich gewähltes Beispielpreset. Module/Wechselrichter/Messsensoren mit passenden Pickern erfassen; Objekteditor als erweiterte Ansicht erhalten. Bestehende Config-Entries und Fingerprints dabei nicht neu erzeugen.
2. **Messsensorvertrag im Setup prüfen.** Leistung statt Energie, DC-/AC-Basis, W/kW, Statistikfähigkeit und vorhandene Quelle prüfen. Mehrere Leser behandeln Zahlen heute als W. Ein 0,4-kW-Sensor würde ohne geeignete Normalisierung wie 0,4 W gelesen; eine obere Plausibilitätsgrenze entdeckt diese Größenordnungsunterschätzung nicht. Doppelte Kanalzuordnung explizit unterstützen oder ablehnen. Existenz allein reicht nicht.
3. **Bereitschaft und Unsicherheit verständlich erklären.** „Prognose verfügbar“, „Messdaten fehlen“, „Kalibrierung lernt“, „Wetter veraltet“ und letzter angenommener Tag. Bandstatus mit Anteil tatsächlich trainierter Tageslichtintervalle und Zahl unabhängiger Tage; neutrale Intervalle als fehlende Evidenz behandeln. Erwarteter Ertrag, bisher produziert und verbleibender Ertrag sind verschiedene Aussagen.
4. **Konfigurationsfolgen vor dem Speichern zeigen.** Relevanter Geometriediff, betroffene Lernbasis, Bias-Reseed und erwarteter Anlauf. Export/Rücknahme und optionaler Bootstrap-Dryrun als konkreter Weg; Vorschläge werden vom Betreiber bewusst übernommen. Das senkt unnötige Resets nach erwarteter Kaltstartphase.
5. **Einfaches Betriebsdashboard und getrennte Diagnoseansicht.** Die aktuelle Liveansicht mischt englische Überschriften und technische Lernerdetails mit Ertragsinformationen; bei schmalen Karten werden Namen abgeschnitten. Drei verständliche Tageswerte, sinnvoller Bandstatus, Ist/Prognose mit echter Standzeit und Datenlücken gehören nach vorn. Diagnose bleibt erreichbar. Sprachstrings vereinheitlichen; „P50“ im Bandtitel präzisieren, wenn tatsächlich die Punktprognose dargestellt wird.
6. **Controls bei Updates erhalten.** Beide Karten ersetzen große Shadow-DOM-Bereiche. Stabile Datums-/Modulcontrols, Tastaturfokus und ausgeklappte Tabellen bei relevanten Updates erhalten. Ein isolierter echter Browsercheck für Tastatur und Smartphonebreite ergänzt den DOM-Harness; das Live-Dashboard wurde hier nicht umkonfiguriert oder mit künstlichen Fehlerzuständen manipuliert.
7. **Wetterquelle sichtbar zuordnen.** In Frontend/Dashboard und passenden Sensorattributen fehlt derzeit ein sichtbarer Open-Meteo-Quellenlink. Datenquelle, lokale Transformation und [Datenlizenz](https://open-meteo.com/en/licence) benennen; die MIT-Code-Lizenz getrennt davon erklären. Die Anbieterhinweise verlangen Attribution neben angezeigten Daten.

## Lesbarkeit und Struktur des Codes

Die Trennung HA-freier Kern/HA-Schicht, gemeinsame Physik- und Aggregationsfunktionen, `WeatherCache`, `LearnerOperations`, zentrale Testhelpers und der Importgrenztest sind gute vorhandene Grundlagen. Die am 26.09. bereits umgesetzten Reviewpunkte werden nicht erneut als fehlend behauptet.

Der nächste Strukturgewinn entsteht durch **kleinere explizite Verträge**, nicht durch eine weitere pauschale Dateiverschiebung:

- **Training besitzt einen begrenzten Kontext.** `coordinator.py` hat etwa 3.222 Zeilen; `_nightly.py` greift auf 46 unterschiedliche Coordinator-Attribute/-Methoden zu. Ein typisierter Trainingskontext mit Eingaben und Ergebnis/Änderungsplan macht Qualität, Mutation und Persistenz unabhängig nachvollziehbar. Coordinator taktet und beschafft Daten; eine Fachkomponente entscheidet über gültige Labels und Lernupdates.
- **Messadapter teilen dieselben Regeln.** Numerik, Einheit, Zeitintervall, Messbasis, Coverage und Ablehnungsgrund einmal definieren. Provider-Endstempel in klare Start-/Endintervalle überführen. Die mehrfachen Sanitizer in `_actuals`, `_bootstrap`, `_glue_util`, Sensoren und Karte sollten denselben fachlichen Vertrag abbilden.
- **Ausgabeverträge besitzen Basis und Provenienz.** `TypedDict`/Dataclasses für Forecast, Issued, Calibration und Diagnostics; `basis`, Einheit, Zeitpunkt, Verfügbarkeit und Rekonstruktion explizit. `_build_forecast_response` aus dem großen `sensor.py` in einen schmalen Presenter verschieben, wenn dieser Vertrag bearbeitet wird. Legacy-Fallbacks bleiben an klaren Kompatibilitätsgrenzen.
- **Typen fachlich gruppieren.** `core/types.py` verbindet auf etwa 1.645 Zeilen Config, Wetter, Prognosen, Lerner und Persistenz. In kleinen Schritten entlang konkreter Änderungen aufteilen; Reexports erhalten bestehende Imports. Unvalidierte externe Inputs als `object` übernehmen und einmal zu einem gültigen Typ konvertieren. Den transparenten 51-Einträge-mypy-Ratchet schrittweise abbauen.
- **Frontend unterscheidet Datenzustand und Darstellung.** Kleine Modelle für known/unknown/error/stale mit Coverage und Standzeit; Kalender/Registry/I/O davon getrennt; stabile Controls und Diagrammprojektion. Das verlangt weder neues Framework noch zusätzlichen Buildprozess.
- **Dokumentation beschreibt die tatsächliche Referenz.** `IntradaySample`/`DayAheadSample` nennen teils noch RAW statt Slow-only×θ/Slow-only; `PlaneResult` beschreibt Attribute teils als preclip, die ausgegebene Attribution ist postclip. `PlaneConfig` erläutert Pooling als gemeinsamen Trainingskanal, obwohl Speicherung pro Ebene erfolgt. Solche Docstrings zuerst korrigieren, damit neue Beiträge die Schichtung nicht erneut brechen.

Weitere Vertragsklarheit: SPEC §15.1 sollte kWh- und Wh-Felder des `DayScore` sauber trennen. `core/scoreboard.py::_window_days_list` wählt aktuell die letzten N **gewerteten Tage**, keine strikt letzten N Kalendertage. Nach Ausfällen können alte Scores im „14-Tage“-Fenster bleiben. Entweder diese Semantik/letzten gewerteten Tag deutlich anzeigen oder ein datumsbasiertes Fenster bewusst als Verhaltensänderung einführen.

Verhaltensneutrale Strukturänderungen behalten repräsentative und geseedete vollständige Ergebnisgleichheit als Abnahme. Physikänderungen brauchen dagegen unabhängige Referenzen und neue Fehlervektoren; Gleichheit mit einer alten falschen Formel ist kein Physiknachweis.

## Tests und Spezifikation

### Ausgeführte Prüfungen

| Prüfung | Ergebnis |
|---|---|
| Portable Suite mit Node-Harness und deaktiviertem HA-Plugin | **2.589 passed, 240 skipped** |
| Separate echte HA-Lifecycle-Suite | **1 passed** |
| Statement-Coverage | **96,18 %**, über dem 95-%-Gate |
| Branch-Coverage | **89,89 %**, Berichtsmetrik ohne lokales Prozentgate |
| Ruff | sauber |
| Reguläres mypy | sauber, 20 Quelldateien |
| Vollkern-/HA-Typ-Ratchet | sauber gegen **51 explizite Altfehler** |
| HA-/stdlib-Importgrenze | sauber, 21 Kernmodule |
| Drei vorhandene semantische Mutationstests | alle drei erkannt |
| Gesperrte unabhängige pvlib-Reproduktion | **64 Sonnenstands- und 768 POA-Vektoren reproduziert** |

Python **3.14.7**, gelocktes Test-HA **2026.7.4**, lokales Node **24.21.0**. CI benutzt Node 22; dessen Lauf wurde hier nicht behauptet. Die 240 Skips betreffen die bestehenden Golden-Vergleiche bei ungeeigneter niedriger Sonnenhöhe. Fünf DeprecationWarnings stammen aus Test-HA/backoff; keine Testfehler. Die defekte lokale Python-Verknüpfung wurde durch `uv sync --locked --group dev` repariert; Lockfile und Abhängigkeitsmetadaten blieben unverändert.

Der vorhandene CI-Job ist für die exakte Prüfung des deklarierten HA-Mindeststands 2026.3.0 konfiguriert. Dieser separate Minimumlauf, Windows-Runner und Remote-CI-/Branchschutz wurden hier nicht erneut ausgeführt bzw. verifiziert. Die echte lokale HA-Suite prüft reale Flows, Entities, Registry, Services, Reload und Disk-Store; sie startet ausdrücklich keinen Recorder. Die code-/dokumentweite Prüfung erfasst das gesamte Inventar und die Verträge, vertieft risikoreiche Grenzen und führt alle vorhandenen lokalen Suites aus. Sie ist keine formale Verifikation jeder möglichen Ausführung und kein Versprechen einer manuellen Zeile-für-Zeile-Prüfung sämtlicher Testcodezeilen.

### Nächste Tests mit hohem fachlichem Wert

1. Provider-Intervall→Recorder→Sonnenmittelpunkt und gemeinsame Live-/Bootstrap-Labelquarantäne. Jede Korrektur mit semantischem Parent-RED, gesundem Gegenfall und SPEC-Update.
2. Echte Recorder-Statistik→Nightly→Persistenz→Reload in wenigen getrennten Linux-HA-Szenarien: vollständiger Kanal, fehlende Tageslichtstunde, Einheit/Metadaten und DST. Die vorhandene Lifecycle-Suite beibehalten.
3. DC-/AC-Invarianten bei Clipping und korrekter Snapshotexport; negative/nichtendliche Labels über alle Grenzen.
4. Unabhängige sichtbare-Himmels-Quadratur und τ-/Horizontmonotonie; horizontale/senkrechte Ebenen und relevante Höhen 2–10°. Golden-Matrix über weitere Geometrien erweitern.
5. Frontend mit teilweise fehlenden Daten, unavailable plus Altattributen, Registryrename sowie echtem Fokus bei Updates.
6. Öffentliche chronologische Prognosebenchmarks und Bandkalibrierung auf mehrere Saisons; unabhängige Tage zählen. Ein Modellvergleich bekommt seine eigene Erfolgsmessung außerhalb der zur Auswahl genutzten Daten.
7. Echter Windows-Kerntest zusätzlich zum vorhandenen Bootstrap-Launcher-Smoke. Referenzgenerator bei PRs prüfen, die dessen Inputs/Generator/Artefakt ändern.

Die anlagenspezifischen C1–C8-Validierungsschwellen aus Juli sind **kein allgemeines Oktober- oder Community-Qualitätsgate**. Sie enthalten feste Morgenenergien und private Modulbaselines. Für dieses Review wurde die nachvollziehbare allgemeine Auswertung verwendet, keine pauschale PASS-/FAIL-Aussage anhand dieser historischen Schwellen.

## Open Source Einstieg und Zusammenarbeit

MIT-Lizenz, HACS-Struktur, englische CONTRIBUTING-Datei, reproduzierbare Umgebung, Releaseguards, CI und Diagnostik sind bereits vorhanden. Der nächste Schritt ist, das vorhandene Wissen für einen Betreiber einer anderen Anlage und einen neuen Contributor zugänglich zu machen.

- Englischer kurzer Quickstart: Voraussetzungen, minimale HA-Version, neutrale Ein-Modul-/Zwei-Ausrichtungs-Beispiele, W-/DC-Messvertrag, optionaler AC-Zähler, Lernanlauf und Screenshots. Der deutsche normative SPEC-Vertrag bleibt zunächst die einzige bindende Detailfassung; ein englisches Glossar/Architekturüberblick ist günstiger als eine zweite parallel gepflegte Voll-SPEC.
- Kurze Bugreport-/Feature-Formulare, Supportgrenzen und privater Security-Meldeweg. Bugreport fordert HA-/Integrationsversion, lokalen Tag, Messbasis und geprüfte redigierte Diagnostics; keine Tokens oder vollständigen Browser-/Storedateien.
- `README.md` nennt noch v0.27.2. Der kopierfertige Wissensindex nennt teils v0.23.0, alte Setupkommandos und eine pauschale Test-RED-Regel. Verbindliche Regeln auf CLAUDE/CONTRIBUTING konzentrieren; datierte Analysen als Historie markieren. Aktuelle Versionsbehauptungen reduzieren oder gezielt prüfen. Eine bestimmte Claude-Co-Author-Zeile sollte keine allgemeine Contributorpflicht sein.
- Architekturkarte mit Datenfluss und kleinem Glossar für RAW, Slow-only, Day-ahead, Intraday, issued, DC und AC. Für Beiträge benannte Fachgrenzen und einige tatsächlich kleine Good-first-Issues vorbereiten.
- Die [HA Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) als Orientierung für Konfiguration, Fehlererholung, Übersetzungen, Diagnostik und Tests verwenden. Daraus wird hier kein offizieller Qualitätsrang der Custom-Integration abgeleitet.

## Empfohlene Umsetzung in kleinen PRs

Der [überarbeitete Umsetzungsplan](2026-10-02-umsetzungsplan-prognose-ux-open-source.md) ist die maßgebliche nächste Arbeitsliste. Gegenüber der ersten Fassung beginnt die Sicherung vergleichbarer Ausgaben früher; Modellforschung wartet nicht auf die gesamte UX-/Open-Source-Arbeit. Zeitversatz, Kalender und Quarantäne erhalten jeweils eigene Fehlernachweise statt eines großen Sammelpatches.

| Strang | Nächste Pakete | Konkreter Abschlussnachweis |
|---|---|---|
| Korrektheit | K1 Zeit/Kalender, K2 Labels, P1 sichere Diagnose-/Validierungsquellen | Unabhängige Parent-REDs, gültige Kontrollfälle, keine unerlaubte Lernmutation |
| Vergleichbarkeit | E0 Quellenmanifest, E1 echte Archive/Zeiten, E2 Benchmark | Gleiche historische Ausgaben, kausale Featureverfügbarkeit, reproduzierbare Metriken |
| Physik | K3 elektrischer Vertrag, K4 sichtbarer Himmel | Begründeter Modellvertrag, unabhängige Invarianten/Referenzen, Migrationsplan |
| Nutzer und Community | U1–U3 Karten/Evidenz/Onboarding, O1 Einstieg, D1 Browserisolation | Ehrliche Zustände, fremde Anlage einrichtbar, reproduzierbarer Beitragspfad |
| Forschung | W1 zunächst beobachtender Panelindikator, danach W2/W3; F1/F2 Geometrie/Bänder | Zusätzlicher Nutzen auf neuen Daten, keine Selbstbestätigung oder doppelte Korrektur |
| Struktur und Absicherung | A1 Fachverträge, T1 echte Recorder-/Plattformgrenzen | Äquivalenz, begrenzte Kopplung, gezielte Tests statt bloßer Coverage |

Jede Verhaltensänderung zieht die SPEC im selben PR nach. Bestehende Stores/Fingerprints bleiben geschützt. Software später über regulären Release-/HACS-Prozess ausliefern; Anlagengeometrieänderungen separat behandeln. Dieser Bericht autorisiert keine automatische Liveübernahme.

## Lokale Reproduktion

Privater Analyseordner `.ha-dev/analysis-2026-10-02/`: `week.json`, `history.json`, `entities.json`, `diagnostics.json`, `solar-site.json`, `metrics.json`, `fine-clean.json`, `geometry-heldout.json`; `analyze.py` und `geometry_check.py` lesen diese lokalen Dateien, ändern HA nicht. Die Geometrieprüfung benötigt zusätzlich die ursprünglichen September-Normierungen und Konfigurationen im benachbarten Septemberordner. Rohdaten sind bewusst kein Bestandteil des öffentlichen Repositorys.

Die vollständigen Teilreviews und synthetischen Repros sind im selben privaten Ordner gesichert. `uv run --no-sync python .ha-dev/analysis-2026-10-02/core-probes.py` und `uv run --no-sync python .ha-dev/analysis-2026-10-02/tests-oss-repro.py` zeigen die beschriebenen Kern-/Tooling-Gegenbeispiele ohne Livezugriff. Zusätzlich sind `ha-ux-probes.py` und `ha-ux-probes.mjs` gesichert: mit `uv run --no-sync python .ha-dev/analysis-2026-10-02/ha-ux-probes.py /workspaces/balcony-solar-forecast` und `node .ha-dev/analysis-2026-10-02/ha-ux-probes.mjs /workspaces/balcony-solar-forecast` ausführen. Diese Skripte sind Fehlernachweise des aktuellen Standes und nach einer späteren Reparatur entsprechend zu aktualisieren.

Die Vertiefung ergänzt `weather-entities.json`, `met-weather-history.json`, `panel-dense-history.json`, `panel-week-history.json`, `panel-week-quality.py/.json`, `cloud-hour-review.py/.json`, `cloud-review-sensitivity.py/.json`, `cloud-review-common-ramps.py/.json`, `cloud-review-reference-bounds.py/.json`, `deeper-ha-probes.py` und die gesonderten Methoden-/HA-Notizen. Neue Auswertungen lesen die Quellen unverändert. `input-manifest.json` dokumentiert die lokal verwendeten Quellen einschließlich Septemberabhängigkeiten und Skripthashes; der bisherige `source_hashes`-Block allein war dafür nicht vollständig genug. Rohdaten bleiben privat. Die Wolkenkonzeptversuche liefern Indizien und Gegenbeispiele, keine nachgewiesenen meteorologischen Klassen.

Playwright startete hier erfolgreich mit einem leeren eigenen Tab, erreichte die angemeldete HA-Sitzung und meldete keinen Profilkonflikt. Der Launcher verwendet jedoch den festen projektlokalen Profilpfad. **Zwei MCP-Server desselben Checkouts dürfen dieses Profil nicht gleichzeitig öffnen.** Für dauerhaft parallele VSCode-Instanzen getrennte Profil-/Outputpfade verwenden; laufende Browser/Lockdateien nicht löschen. In diesem Review wurde die andere Instanz nicht gesteuert oder beendet. Eine Störung wurde nicht beobachtet, vollständige Isolation anderer Clients ist damit nicht bewiesen.
