# Reale Archivvalidierung vom 3. Oktober 2026

Die realen Daten ermöglichen einen rückblickenden Vergleich der bestehenden
Lernkorrektur mit der damals archivierten RAW-Kurve. Sie ermöglichen derzeit
**keinen Wirksamkeitsnachweis der noch unveröffentlichten Codeänderungen**.

## Quellen und Umfang

Am 03.10.2026 gegen 11:54 UTC über den angemeldeten lokalen Playwright-Browser
lesend abgefragt: `get_issued_forecast`, ausgewählte Diagnostics-Blöcke und
`recorder/statistics_during_period` für die acht konfigurierten DC-Ports.
Installiert: v0.28.0; Zeitzone Europe/Berlin. Keine Live-Einstellungen oder
Lernzustände verändert, keine Zugangsdaten exportiert.

89 abgeschlossene lokale Tage vom 06.07. bis 02.10.2026 wurden abgefragt.
Alle 89 besitzen einen Archiveintrag; der 06.07. liefert keine gemeinsam
vergleichbare explizite Kurve. 88 Tage tragen insgesamt **1.337 gepaarte
Stunden** bei. Die acht Recorderkanäle liefern je 2.136 Stundenbins in W.
Ein Stundenbin am 19.09. scheitert an der gemeinsamen Portqualitätsprüfung.
Weitere 798 Stunden besitzen keine beidseitig explizite Archivkurve und werden
nicht durch angenommene Nachtwerte ergänzt.

Die private, Git-ignorierte Evidenz liegt in
`.ha-dev/validation-2026-10-03/`: unveränderte Browserexports, ausführbares
`analyze.py`, normalisiertes Vergleichsbundle, Bericht und SHA-256-Manifest.
Das Manifest bindet Originalexports, Analyseprogramm, Benchmarkcode und Ausgaben.
Rohdaten besitzen keine öffentliche Freigabe.

## Gleiche Ziele für beide Kurven

Verglichen werden `raw_hourly_wh` und `hourly_wh` aus demselben ursprünglichen
Archiveintrag gegen die Summe derselben acht DC-Portmessungen. Beide erhalten
exakt dieselbe gültige Zielmaske. Ein endgestempelter Recorderbin muss genau eine
Stunde umfassen; Mittelwert W entspricht über dieses Intervall Wh. Mittelwert,
Minimum und Maximum müssen endlich, numerisch, nichtnegativ, geordnet und
innerhalb der Port-Plausibilitätsgrenze sein. Echte Null bleibt gültig.

Nur vollständige Stunden nach dem archivierten `issued_at` und mit expliziten
Werten beider Kurven werden gewertet. Legacyarchive tragen keine gesonderte
ursprüngliche Berechnungszeit; eine Frischebehauptung über `computed_at` ist
deshalb nicht möglich. Die Verfügbarkeit der tatsächlichen Labels wird im
Bundle konservativ auf den heutigen Abrufzeitpunkt gesetzt. Das ist eine
Bewertung eingefrorener Kurven, kein historischer Lernlauf.

## Ergebnisse der bestehenden Integration

| Bereich | RAW Stunden-MAE | Korrigiert Stunden-MAE | RAW WAPE | Korrigiert WAPE |
|---|---:|---:|---:|---:|
| Alle 1.337 gepaarten Stunden | 135,51 Wh | 128,98 Wh | 24,41 % | 23,23 % |
| Bereits analysierte Woche 25.09.–01.10., 84 Stunden | 164,46 Wh | 106,01 Wh | 30,71 % | 19,80 % |
| Zusätzlich geprüfter 02.10., 12 Stunden | 63,36 Wh | 73,98 Wh | 62,02 % | 72,41 % |

Über den gesamten historischen Bereich sinkt der Stunden-MAE um **4,82 %**.
Der RMSE sinkt von 227,18 auf 211,57 Wh. Die mittlere signierte Abweichung
(Prognose minus Ist) beträgt RAW −18,55 Wh und korrigiert −11,69 Wh.

Ein gepaarter Bootstrap über ganze lokale Tage ergibt für den MAE-Gewinn
6,53 Wh ein 95-%-Intervall von ungefähr **0,95 bis 12,20 Wh**. Die festgelegte
praktische Mindestverbesserung von 5 % wird im Punktwert nicht erreicht;
der bestehende Entscheidungshelfer meldet `inconclusive`. Die Berechnung ist
eine retrospektive Beschreibung, keine unabhängige Releasefreigabe. Ganze
Tagescluster machen benachbarte Wetterepisoden nicht automatisch unabhängig.

Am 02.10. beträgt der reale vollständige Tagesertrag **1,226 kWh DC**.
Der Fehler der Summe über die zwölf vergleichbaren Stunden sinkt von
**469,49 Wh** auf **120,25 Wh**. Gleichzeitig steigt der Stunden-MAE um
16,8 %. Eine passendere Tagessumme bedeutet an diesem Tag also keinen
besseren zeitlichen Verlauf. Ein einzelner Tag erlaubt keine allgemeine
Modellentscheidung.

## Grenzen des Nachweises

Der Split markiert bis 24.09. Entwicklung, 25.09.–01.10. bereits verwendete
Analyse-/Auswahldaten und 02.10. die zusätzliche Prüfung. Die Grenzen wurden
jetzt festgehalten, nicht vorab registriert. Der letzte Bereich enthält nur
einen Tag; der Vergleich meldet folgerichtig `insufficient_days`. Historische
Tage werden nicht nachträglich zu einem unberührten Holdout erklärt.

Über Juli bis Oktober änderten sich Software und Anlagenkonfiguration. Die
aktuellen acht Quellen und Plausibilitätsgrenzen sind bekannt; vollständige
Identitäts-/Konfigurationsprovenienz je altem Issue fehlt. Auch zeitlich
vollständige Recorder-Stundenbins garantieren keine lückenlose Übertragung
innerhalb der Stunde. Daher keine Zuschreibung des historischen Gewinns an
eine bestimmte Version oder einzelne Änderung.

Die archivierten Wetterklassen sind Prognoseklassen, keine beobachteten Wolken.
Wolkenwirkungen bleiben möglich. Eine unabhängige Boden-/Satellitenreferenz
wurde für diesen Auftrag nicht beschafft. Die ältere Schnittstelle liefert
auch keine damals ausgegebenen Slotbänder; deren Kalibrierung lässt sich damit
nicht prüfen.

## Was für die neuen Änderungen noch benötigt wird

Die verwendeten Live-Schnittstellen liefern ursprüngliche RAW-/korrigierte
Summenkurven, aber keine vollständigen ursprünglich verfügbaren
15-Minuten-Wetterinputs und keine historischen Lernzustände für alle Issues.
Der aktuelle Diagnostics-Stand darf nicht in die Vergangenheit zurückprojiziert
werden. Ein Neuabruf historischen beobachteten Wetters wäre ebenfalls kein
ursprünglicher Prognoseinput.

Damit kann der neue physikalische Replay die neuen Geometrie-/Wetter-/Lernvarianten
noch nicht kausal auf diesen alten Tagen vergleichen. Für den beobachtenden
Panelindikator fehlen zudem kontinuierlich erfasste kohärente Frames mit
Transportzeit, damaliger Slow-only-×-θ-Referenz und unabhängiger Schattenwahrheit.
Recorder-Zustandsänderungen ersetzen identische erneut gemeldete Samples nicht.

Vor einer empirischen Aktivierung neuer Prognosekorrekturen müssen mindestens
30 weitere abgeschlossene, vorab abgegrenzte Testtage mit Originalinputs und
geeigneter Wettervielfalt gesammelt werden. Dazu gehört eine eigene
Aufzeichnungsplanung; dieser Auftrag änderte weder Recorderkonfiguration noch
Produktivsoftware. Die heute vorhandenen Daten sind für Diagnose und den
rückblickenden Bestandsvergleich nutzbar. Ein Nachweis für die neuen Änderungen
bleibt offen.
