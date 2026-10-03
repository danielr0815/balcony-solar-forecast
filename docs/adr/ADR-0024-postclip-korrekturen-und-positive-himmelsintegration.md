# ADR-0024: Postclip-Korrekturen und positive Himmelsintegration

Status: Accepted für den aktuellen Arbeitsbranch. Datum: 2026-10-03.

Der bestehende DC-Vertrag ist ein Postclip-Modell: Slow-only läuft durch den
konfigurierten DC-Gruppenclip; θ/Intraday korrigieren diese Kurve und werden
anschließend erneut begrenzt. Gegen diese Referenz sind Bias und Intraday
bereits gelernt. Diesen Vertrag behalten wir bei. Eine Umdeutung vorhandener
Faktoren als Preclip-Faktoren würde insbesondere gesättigte Trainingsstunden
anders erklären und benötigt einen eigenen Zustandsübergang und Holdout.

AC wird aus dem tatsächlich servierten DC abgeleitet, je Gruppe mit damaligem
wirksamen η und AC-Grenze. Es darf keine zuvor durch den DC-Clip entfernte
Leistung wiedergewinnen. Das bisher mögliche Beispiel 50 W serviertes DC /
90 W AC bei einer Halbierung auf einer gesättigten Gruppe ist damit behoben.
Die AC-Reserve für das Entfernen des Intradayfaktors stammt entsprechend aus
der faktorkorrigierten Postclip-Slow-Kurve vor dem zweiten Clamp. Ein gelerntes
Site-η bleibt eine AC-Kalibrierung, keine rückwirkende Neuinterpretation der
DC-Lernreferenz. Der AC-Clamp kann zusätzliche η-bedingte Sättigung darstellen;
DC bleibt die nominal gruppenbegrenzte Modell-/Messreferenz.

Der neue Modellvertrag heißt `postclip-dc-ac-v2-positive-sky`. Alte Archive
werden unverändert gelesen und nicht neu berechnet. Exakte neue AC-Archive
halten den Vertrag; Legacy-AC wird ausdrücklich als Rekonstruktion bezeichnet.
Lernzustände werden weder gelöscht noch still zurückgesetzt. Ein gezielter
Bootstrap-Dryrun und vorhandene Rollbacks bleiben der Übergangsweg für
Betreiber, die eine neue Basis prüfen wollen.

Die Himmelsintegration wird unabhängig davon korrigiert: Nur positive
Einfallsgewichte sind Energiebeiträge. Das analytische Integral beginnt je
Azimut höchstens an der positiven Einfallsgrenze. Rückseitige negative
Teilintegrale dürfen weder vordere Beiträge auslöschen noch ein τ-Band mit
negativem Gewicht versehen. Unabhängige Strahlenquadratur und
Transparenzmonotonie belegen diese Korrektur. Sie ändert den Diffusanteil,
nicht direkt den gesamten Ertrag um denselben Prozentsatz. Bestehende τ/θ
werden erhalten; eine Anpassung wird nicht aus sieben intensiv ausgewerteten
Entwicklungstagen automatisch geschätzt.

Abnahme: semantischer Parent-RED für 50 W DC / 90 W AC, Kontrollfaktoren 1/1.5,
positive Strahlenquadratur, monotone Transparenz, bestehende nichtclipende
Referenzvektoren und gemeinsame DC-Äquivalenz. Ein künftiger Preclip-Lerner ist
hierdurch nicht freigegeben.
