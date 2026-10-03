# Konzept für einen Wetterindikator aus gemeinsamen Paneldaten

Die gemeinsame Auswertung aller Panels zusammen mit der Wetterprognose ist ein sinnvoller nächster Forschungsschritt. Zu prüfen ist, ob sie schnelle gemeinsame Einstrahlungsänderungen besser erkennt als der bisherige Summenquotient und lokale Verschattung von einem anlagenweiten Ereignis abgrenzt. **Aus PV-Leistung allein entsteht jedoch keine eindeutige Wolkenmessung.** Das erste Produkt sollte deshalb ein erklärbarer Wetter- und Störungsindikator mit „unklar“ sein. Seine Wirkung auf Prognose und Lernen wird danach getrennt geprüft.

Dieses Konzept ergänzt den [Siebentagebericht](2026-10-02-siebentageanalyse-projektreview.md) und die Pakete W1–W3 des [Umsetzungsplans](2026-10-02-umsetzungsplan-prognose-ux-open-source.md). Es beschreibt vorgeschlagene Arbeit, kein bereits implementiertes Verhalten. Produktionscode, Live-Konfiguration und SPEC bleiben unverändert.

## Was die vorhandenen Daten ermöglichen

Die acht DC-Ports besitzen drei Ausrichtungen: M1/M5 25°, M2/M3/M6/M7 115° und M4/M8 205°. Die Neigungen liegen bei 70° beziehungsweise 80°. Die vier Wechselrichtergruppen verbinden jeweils zwei Ports. Damit stehen gleichzeitig räumlich nahe Messungen, verschiedene Ausrichtungen und mehrere elektrische Gruppen zur Verfügung. Vier Ostmodule sind trotzdem keine vier unabhängigen Wetterstationen.

Für die sieben abgeschlossenen Tage liegen vollständige Fünf-Minuten-Mittel mit Minima/Maxima vor. Zusätzlich wurden für den 25.09. und 01.10. jeweils 08–12 Uhr lokal die Zustandsänderungen aller Ports ausgelesen: 343–397 Einträge pro Port/Fenster, typische Änderungslücke rund 35 Sekunden, größte Lücke knapp fünf Minuten. Diese Auflösung trägt die Untersuchung gemeinsamer kurzer Änderungen. Minima/Maxima eines Fünf-Minuten-Blocks sagen dagegen nicht, ob zwei Modulminima gleichzeitig auftraten.

Die beiden Vormittage wurden wegen unterschiedlicher angezeigter Wettermodellbedingungen ausgewählt. Sie besitzen keine unabhängigen Wolkenlabels und sind kein repräsentativer Testdatensatz für eine Erkennungsquote. Identische wiederholte Werte können im Recorder fehlen. Ein zurückblickend fortgeschriebener Wert ist deshalb weder automatisch frisch noch nachweislich ein Sensorausfall.

Ein Teil der gewünschten Funktion existiert bereits: `coordinator.py::_build_intraday_sample` vergleicht Messung und Modell auf derselben nutzbaren Modulmenge; `_modeled_power_for_planes` referenziert Slow-only × θ ohne den Intradayfaktor. `core/intraday.py::censored_sample` schützt gegen bekannte Sättigung. Der neue Indikator muss einen nachgewiesenen Zusatznutzen gegenüber diesem vorhandenen System liefern. Ein weiterer beliebiger Anlagenquotient genügt nicht.

### Erster Versuch mit den echten Panelverläufen

Ein lokaler Prototyp wurde auf den beiden dichten Vormittagsverläufen ausgeführt: Minutenraster, Vergleich mit fünf Minuten zuvor, Mindestleistung 20 W, höchstens 120 Sekunden Fortschreiben eines zuletzt aufgezeichneten Werts. Eine Stimme pro Azimutgruppe verhindert, dass die vier Ostmodule allein die Abstimmung bestimmen. Der gemeinsame Kandidat verlangt mindestens drei Ports aus zwei elektrischen und zwei Azimutgruppen sowie übereinstimmende Änderungen der beteiligten Azimutgruppen um mindestens 10 %. Dies sind explorative Regeln, keine ausgewählten Produktionsparameter.

| Vormittag | Auswertbare Minuten | Gemeinsame Kandidatenminuten | Kandidaten einer einfachen Vier-Port-Regel |
|---|---:|---:|---:|
| 25.09. | 217 | 7 | 20 |
| 01.10. | 202 | 13 | 24 |

Die Unterschiede sind **keine gemessene Fehlalarmreduktion**. Die Regeln wählen verschiedene Fälle aus; Minuten innerhalb eines Ereignisses sind stark korreliert. Viele gemeinsame Rohkandidaten liegen in morgendlichen Anstiegsphasen, in denen Sonnenbewegung und Schattenfreigaben ebenfalls plausible Ursachen sind. Ein reiner Mehrheitsalarm ist deshalb noch kein Wolkendetektor. Die Prüfung mit ausgeschlossenen Einzelmodulen ist vorhanden; der stärkere Ausschluss ganzer Wechselrichtergruppen bleibt ein nächster Versuch.

Ein weiterer Kandidat am **01.10. um 10:13 Uhr lokal** zeigt die Heterogenität: M6 fällt gegenüber fünf Minuten zuvor von rund 317,9 auf 263,9 W, M8 von 65,8 auf 53,7 W, etwa −17 % beziehungsweise −18 %. M1/M5 ändern sich nur um etwa −2/−4 %, das bereits lokal abgeschattete M7 steigt um rund 11 %. Das letzte zu diesem Zeitpunkt verfügbare MET-Wettermodell nennt 92,2 % Wolkendeckung. Das ist mit einem Wettereinfluss vereinbar; eine Regel „alle Panels müssen sinken“ würde es übersehen.

Die kritische Gegenprüfung verhindert eine zu starke Schlussfolgerung: Unter der Annahme unverschatteter Referenzen liefert die untersuchte optische Modellzerlegung zu diesem Sonnenstand maximal etwa **M6/M8 = 2,95**, gemessen sind ungefähr **4,91**. Die Referenzannahme passt damit nicht zum gewählten Modell. Zusätzliche Abschattung, Geometrie-/Modellfehler oder eine Portabweichung bleiben möglich. Aus diesen beiden Signalen lässt sich an diesem Zeitpunkt kein belastbarer absoluter DNI-/DHI-Zustand ableiten. Der Test ist ein Konsistenzcheck der Modellannahmen, keine unabhängige Feststellung des Himmelszustands.

Der Konflikt betrifft nicht nur diesen Einzelpunkt: In 76 von 81 geeigneten Minuten desselben Vormittags überschreitet M6/M8 die berechnete optische Obergrenze um mehr als 5 %. Das gilt unter der jeweiligen Annahme unverschatteter Ports, gleicher konfigurierter Wp-/Effizienz-/Ross-Werte und gemeinsamer Umgebungstemperatur. Die Minuten sind korreliert. Der Befund verlangt eine Prüfung der Referenzannahmen und benennt noch keinen bestimmten Hardware- oder Geometriefehler.

## Welche Hypothesen unterscheidbar sind

| Beobachtung | Plausible Erklärung | Was zusätzlich geprüft werden muss |
|---|---|---|
| Mehrere gut beleuchtete, elektrisch getrennte Module ändern sich zeitgleich | Gemeinsame Einstrahlungsänderung, etwa Wolkendurchzug | Gemeinsame Abregelung, Kommunikation, Modellwechsel und bewegter anlagenweiter Schatten |
| Ein Modul oder eine kleine Gruppe fällt ab, andere bleiben stabil | Lokaler Schatten, Portproblem oder elektrische Gruppengrenze | Referenzmodule müssen tatsächlich beleuchtet und aktuell sein |
| Ähnliche Änderung wiederholt sich bei gleichem Sonnenwinkel an Folgetagen | Statische Geometrie oder anlagenspezifische Betriebsregel | Mehrere Wetterlagen, Betriebsdaten und unabhängige Referenzen |
| Alle Module bleiben länger unter der Prognose | Wettermodell überschätzt Strahlung, globale Dämpfung oder Modellskalierung falsch | Gleichmäßige Bewölkung ist nicht aus zeitlicher Variabilität allein erkennbar |
| Direkt empfindliche Flächen sinken, andere Flächen reagieren schwächer | Veränderte Direkt-/Diffusverteilung | Unterschiedliche Schatten, Reflexion, Bifazialität und Temperatur können ebenfalls wirken |
| Gleiche Werte bei allen Ports, fehlende Meldungen oder Sprünge an gemeinsamen Zeitstempeln | Kommunikation oder Messaufbereitung | Transportfrische und physikalische Unveränderlichkeit getrennt behandeln |

Die Unterscheidbarkeit hat eine physikalische Grenze. In einer beamdominierten Situation kann sowohl weniger Einstrahlung als auch eine gemeinsame Leistungsbegrenzung dieselbe Beobachtung `P_i = c × P_i_ref` für alle Module erzeugen. Ohne weitere Messung sind die Ursachen dann nicht trennbar. Mehr Panels lösen diese Mehrdeutigkeit nicht automatisch.

Die Wetterprognose liefert eine Vorannahme: Erwartete DNI/DHI/GHI, Wolkendeckung, Sichtweite und die erwartete zeitliche Änderung machen bestimmte Erklärungen plausibler. Sie ist kein Beweis. Gerade bei einer verfehlten Prognose müssen die Messungen ihr widersprechen dürfen. Ein einfaches „alle Ports schwach und Prognose bewölkt = bewölkt“ würde sonst hauptsächlich die Eingangsvorhersage bestätigen.

## Zwei getrennte Signale statt einer vermeintlichen Wolkenzahl

**Signal A beschreibt gemeinsame Änderungen der gemessenen Leistung.** Es beantwortet: Bewegen sich unabhängige, geeignete Module innerhalb kurzer Zeit ähnlich, nachdem die vorhersehbare Sonnenbewegung berücksichtigt ist? Dafür sind keine vollständigen Wolkenprozentwerte erforderlich.

**Signal B beschreibt die Abweichung von der verfügbaren Prognose.** Es beantwortet: Liegt die gemessene Leistung der geeigneten Module gemeinsam unter oder über Slow-only × θ? Eine Abweichung ist ein Prognoserestfehler, nicht automatisch eine Wolke. A und B dürfen unterschiedliche Ergebnisse liefern: ruhige Bewölkung kann B verändern, während A fast neutral bleibt; eine korrekt prognostizierte Wolke kann A verändern, ohne großen Fehler in B.

Ein späteres physikalisches Zusatzsignal könnte gemeinsame Direkt- und Diffuskomponenten schätzen. Das ist anspruchsvoller: Bei ähnlichen Orientierungen oder geringer Direktstrahlung sind die Komponenten schlecht identifizierbar, und unbekannte Schattentransmissionen erhöhen die Zahl der Unbekannten. Diese Erweiterung folgt erst nach einem belegten Nutzen des einfacheren Indikators.

## Vorgeschlagene Verarbeitung

### 1 Zeitlich zusammenpassende Messungen bilden

Die HA-Schicht sammelt Portmeldungen in einem begrenzten Ring und bildet beispielsweise Minutenintervalle. Einheiten werden vorab nach W normalisiert; DC-Basis, Quelle, elektrischer Gruppe, Intervallabdeckung und Ausschlussgrund bleiben erhalten. Fehlende Werte sind unbekannt, niemals 0 W.

Für aktuelle Messungen ist eine echte letzte Meldung von einer bloßen Wertänderung zu unterscheiden. In HA aktualisiert sich `last_reported` auch bei einer identischen neuen Meldung; `last_updated` und Recorder-State-Changes reichen dafür nicht immer. Ein Sensor kann frisch melden und trotzdem physikalisch eingefroren sein. Beide Prüfungen bleiben getrennt.

Eine maximale Überbrückungsdauer, Mindestabdeckung und erlaubte Zeitspreizung werden als konfigurationsarme interne Qualitätsregeln untersucht. Beispielsweise 60-Sekunden-Frames und 90 Sekunden maximale Messalterung sind **Startwerte für den Versuch**, keine bereits belegten universellen Grenzen. Für langsam meldende Anlagen entsteht geringere Evidenz; ihre normale Prognose muss weiter funktionieren.

### 2 Geeignete Referenzmodule auswählen

Ein Port wird für das gemeinsame Wettersignal nur benutzt, wenn Messung, Mindestleistung, Sonnenhöhe und Modellreferenz ausreichend sind und kein bekannter Sättigungs-/Fehlerfall vorliegt. Bekannte Horizontübergänge und unsichere Shademap-Bins senken sein Gewicht. Niedrige Leistung nahe null darf den relativen Fehler nicht dominieren.

Die Auswahl braucht Diversität: mehrere Ports, mehrere elektrische Gruppen und nach Möglichkeit unterschiedlich empfindliche Ausrichtungen. Eine mögliche Anfangsregel lautet mindestens drei nutzbare Ports aus zwei Gruppen; höhere Evidenz verlangt zusätzlich geometrisch informative Flächen. Die Regel wird gegen tatsächliche Verfügbarkeit geprüft. Ein schlecht beleuchtetes Nordostmodul wird nicht allein wegen seiner anderen Ausrichtung zu einer guten Referenz.

Die Gewichte werden pro Wechselrichtergruppe und Orientierung begrenzt. Ein kopierter Sensor und vier eng verwandte Ostmodule dürfen nicht die Abstimmung beliebig vervielfachen. Fehlen genügend unabhängige Referenzen, lautet das Ergebnis „nicht ausreichend bestimmbar“.

Bei nur zwei Ports einer Orientierung ist ihr Median der Mittelwert beider Werte und damit kein robuster Ausreißerschutz gegen einen einzelnen Schattenrand. Vorzeichen und Streuung innerhalb der Gruppe sowie Einzelreferenzen müssen separat sichtbar sein. Wird eine widersprechende schwache Orientierung durch ein höheres Leistungsgate entfernt, kann die restliche Auswahl scheinbar geschlossener wirken. Dieser Evidenzverlust darf nicht automatisch die Sicherheit erhöhen.

Zusätzlich wird geprüft, ob die angenommenen Referenzen unter einer physikalisch plausiblen gemeinsamen Strahlungsmischung überhaupt miteinander vereinbar sind. Ein Widerspruch wie im 10:13-Beispiel reduziert die Evidenz für eine absolute Strahlungsschätzung. Das schließt eine sichtbare gemeinsame Rampe nicht aus, verhindert aber, dass ein unpassendes Referenzmodell als gemessene Wolkenabschwächung ausgegeben wird. Die Schwelle muss die Modellunsicherheit einschließlich Diffus-/Bifazialanteil berücksichtigen.

### 3 Erwartbare Bewegung und gemeinsame Änderungen trennen

Für ausreichend große positive Leistungen lässt sich eine kurze relative Änderung vergleichen:

`r_i(t) = Δ log(P_i(t) + ε_i) − Δ log(S_i(t) + ε_i)`

`S_i` beschreibt für diesen Zweck die eingefrorene erwartbare Solar-/Geometriebewegung; `ε_i` ist ein an Wp und Mindestleistung gebundener Schutz nahe null. Ein Wechsel der Wettermodellversion mitten im Fenster darf keinen künstlichen Messsprung erzeugen. Fenster werden an solchen Referenzwechseln getrennt oder konsistent neu berechnet.

Ein gruppenbalancierter gewichteter Median der `r_i` schätzt die gemeinsame Änderung. Medianabstand, Vorzeichenübereinstimmung, Zahl geeigneter Gruppen, Dauer und Messabdeckung beschreiben die Evidenz. Die übrigen Modulresiduen zeigen lokale Abweichungen. Rohsumme und einfacher Mittelwert sind als Baselines mitzuführen, damit die zusätzliche Komplexität ihren Nutzen beweisen muss.

Eine ausschließlich direkte AOI-/IAM-Normierung wurde im lokalen Versuch ebenfalls betrachtet. Sie entfernt keinen unbekannten Diffusanteil und ist bei wechselnder Direkt-/Diffusmischung keine wetterbereinigte Referenz. Das robuste Änderungsmerkmal und eine absolute Strahlungsschätzung bleiben deshalb unterschiedliche Aufgaben.

Für Signal B wird separat `P_i / (Slow-only_i × θ)` auf derselben gültigen Modulmenge betrachtet. Der vorhandene Intradayfaktor darf nicht im Nenner stehen. Die Division durch eine gemeinsame Clear-Sky-Zahl auf beiden Seiten ändert einen Quotienten algebraisch nicht und beweist keine Wetterursache.

### 4 Wetterprognose als zusätzliche Evidenz verwenden

Verwendet werden ausschließlich Wetterwerte, die zu diesem Zeitpunkt bereits verfügbar waren, einschließlich Abrufzeit und Alter. Zunächst reichen die bereits bezogenen GHI/DNI/DHI-/Wolkenfelder; ein zusätzlicher Wetterdienst ist für den Prototyp nicht nötig. Ein zweites Modell kann später als separat getestete Zusatzinformation folgen.

Die Ausgabe dokumentiert, ob prognostizierte Änderung und Panelsignal übereinstimmen. Bei Widerspruch wird dieser sichtbar, statt die Messung auf die Prognoseklasse umzubenennen. Die modellinterne Klasse `clear/mixed/overcast/fog` bleibt getrennt von einer rückblickenden Referenzklasse und vom Panelindikator.

### 5 Verständliche Zustände mit Gründen ausgeben

| Vorgeschlagener Zustand | Bedeutung für die Oberfläche |
|---|---|
| Gemeinsame Einstrahlungsänderung plausibel | Mehrere geeignete Gruppen bewegen sich gemeinsam; Wolken als mögliche Ursache |
| Lokale Abweichung plausibel | Einzelne Ports weichen gegenüber den übrigen Referenzen ab |
| Gemeinsame Dämpfung, Ursache unklar | Wetterfehler, Abregelung und andere globale Ursachen noch nicht trennbar |
| Messdaten unzureichend | Zu wenige aktuelle oder geometrisch geeignete Referenzen |
| Referenzmodell inkonsistent | Geeignete Ports passen unter den Modellannahmen nicht zu einer plausiblen gemeinsamen Strahlungsmischung |
| Übergang oder Sättigung | Bekannter Schattenrand, niedrige Sonne oder Clipping verhindert sichere Auswertung |
| Kein auffälliger gemeinsamer Wechsel | Keine erkannte kurzfristige Änderung; dies bedeutet nicht wolkenfreien Himmel |

Attribute können nutzbare Ports/Gruppen, abgedeckten Anteil, letzten Datenzeitpunkt, beobachtete Dauer und begrenzte Ausschlusscodes enthalten. Ein Prozentwert „Wolkenwahrscheinlichkeit“ wird erst angezeigt, wenn er gegen unabhängige Referenzen kalibriert ist. Hohe Messqualität ist keine 95-%-Wahrscheinlichkeit für die Ursache Wolke.

## Schutz gegen Lernrückkopplung

Das riskanteste Design wäre, aus den Panelresiduen eine Wolke zu erklären, damit dieselben Residuen aus dem Schattenlernen zu entfernen und den Erfolg anschließend anhand der bereinigten eigenen Residuen zu melden. Das wäre eine sich selbst bestätigende Auswertung.

Für eine Prüfung des Schattenlernens von Modul i wird der gemeinsame Indikator deshalb **ohne Modul i**, bei elektrischer Kopplung zusätzlich ohne dessen Gruppe, berechnet. Reichen die übrigen Referenzen nicht, entsteht keine Aussage. Dieses Verfahren verhindert unmittelbare Selbstbestätigung, ersetzt aber keine externe Beobachtung: Gemeinsame Fehler anderer Module können bleiben.

Dauerhafte Geometrieupdates benötigen wiederholte Evidenz bei ähnlichem Sonnenstand über verschiedene Tage und Wetterbedingungen. Häufigkeiten, abgelehnte Samples und Altersverteilung müssen sichtbar bleiben, damit ein neues Gate nicht unbemerkt jeden schwierigen Tag ausschließt. Der aktuelle Shademap-Pfad besitzt bereits Wetter-/Leistungs-/Stabilitätsgates; der Versuch erweitert gezielt deren Lücken.

Die drei Verwendungszwecke werden getrennt freigegeben:

1. **Diagnose:** Indikator berechnen und mitprotokollieren, ohne Forecast oder Lerner zu beeinflussen. Dies ist der erste Schritt.
2. **Kurzfristige Prognose:** Im Vergleich zum bestehenden Intradayverfahren zuerst 0–30 Minuten, danach getrennt 30–120 Minuten prüfen. Keine pauschale Übertragung eines momentanen Einbruchs auf den Resttag. Ohne Wolkenbewegungsmodell ist die Dauer einer Wolke unbekannt; die Zusatzkorrektur muss mit dem Horizont zur Wetterprognose zurücklaufen.
3. **Lernqualität:** Erst nach eigenem Nachweis einzelne Shademap-Samples niedriger gewichten oder quarantänisieren. Ein echter Wetterprognosefehler bleibt grundsätzlich ein wertvolles Label für Bias- und Quantilkalibrierung. Diese Lerner trainieren auf der zum Ausgabezeitpunkt bekannten Prognoseklasse; eine spätere beobachtete Klasse ersetzt sie nicht.

Ein neuer Kurzfristfaktor wird nicht zusätzlich auf den bereits für denselben Restfehler zuständigen Intradayskalar multipliziert. Entweder verbessert das Signal dessen Eingaben/Gewichtung oder es ersetzt eine klar abgegrenzte Komponente in einem kontrollierten Versuch. Die RAW-/Slow-only-/θ-/Intraday-Verträge bleiben explizit.

## Nachweise vor einer produktiven Wirkung

Zuerst braucht es synthetische, ursächlich kontrollierte Fälle: gemeinsame Strahlungsabsenkung mit unterschiedlicher Direkt-/Diffusreaktion; einzelner Schattenrand; Schatten nur auf allen Ostmodulen; Gruppensättigung; alle Gruppen gemeinsam abgeregelt; fehlende und eingefrorene Sensoren; langsame Sonnenbewegung; abrupt neu eingelesene Wetterprognose; Wiederanlauf nach Datenlücke. Die elektrische globale Dämpfung muss als mehrdeutig erkennbar bleiben, selbst wenn ihr Zeitverlauf einem Wolkendurchzug gleicht.

Danach folgt ein chronologischer Vergleich auf realen Daten. Referenzen können ein unabhängiger Strahlungssensor oder zeitlich korrekt ausgerichtete Satellitendaten mit Qualitätskennzeichen sein. Ohne solche Referenz darf nur die Erkennung gemeinsamer/lokaler Änderungen und die spätere Prognoseverbesserung bewertet werden, keine vermeintliche Wolkentrefferquote.

Der Versuch vergleicht vorhandenes Intradayverfahren, einfachen Summenquotienten, robustes Panelsignal ohne Wetter und robustes Panelsignal mit Wetter. Für jede Variante bleiben gültige Zeitfenster und Referenzdaten identisch. Tage werden vollständig zurückgehalten; bei Wetterereignissen zählen zusammenhängende Episoden statt hunderter korrelierter Sekunden als unabhängige Einheiten.

Alle PV-Featureintervalle müssen vor Ausgabe abgeschlossen und bereits verfügbar sein; Recorder-Publikationsverzug gehört zur Zeitachse. Bewertete Zielslots liegen vollständig nach der Ausgabe. Der Mittelwert einer späteren Zielstunde darf niemals deren eigenes Wolkenfeature werden. Eine nachträglich aus vollständigen Tagesdaten interpolierte Kurve ist kein kausal verfügbarer Onlineinput.

Zu berichten sind Fehlalarme pro Ereignis/Tag, Anteil „unklar“, Erkennungsverzögerung und verfügbare Gruppen. Für Prognosen kommen DC-/AC-MAE, Bias, Rampenfehler und Unsicherheitsgüte je Ausgabehorizont hinzu. Für Lerngates zählen zusätzlich wirksame Trainingsabdeckung und Fehler in späteren Tagen. Ein Detektor, der fast alles ausschließt, darf nicht allein durch geringe Fehlalarmzahl gewinnen.

Ein möglicher vorher festgelegter Produktmaßstab ist mindestens 5 % geringerer MAE im vorgesehenen Kurzfristhorizont gegenüber dem bestehenden Verfahren, ohne relevante Verschlechterung der übrigen Tagesabschnitte, Verfügbarkeit oder Bandgüte. Die 5 % sind eine vorgeschlagene Nutzenschwelle, kein aus dieser Woche nachgewiesener Effekt. Bei breiter Unsicherheit bleibt das Ergebnis „nicht entschieden“; die Regel wird nicht nachträglich an die Testdaten angepasst.

## Umsetzung ohne unnötige Plattformlast

Der HA-Adapter übernimmt Ereignisse, Einheiten, Frische und elektrische Gruppen. Eine pure Kernfunktion erhält fertig validierte Intervalle und eingefrorene Referenzen und liefert Indikator plus Gründe. Sie braucht weder HA-Importe noch NumPy, einen ML-Dienst oder zusätzliche Runtime-Abhängigkeiten. Fachlich getrennte Typen für `PanelFrame`, `ReferenceFrame` und `WeatherEvidence` können eingeführt werden; die Namen sind Vorschläge, keine bereits vorhandenen APIs.

Ein kurzer Ring bleibt im Arbeitsspeicher. Für die Auswertung genügen begrenzte aggregierte Merkmale und ausgewählte Ereignisausschnitte. Vollständige Portverläufe gehören nicht in jedes Sensorattribut und werden nicht bei jeder Meldung in den Integrationsstore geschrieben. Nach Neustart wird ein Anlaufzustand angezeigt; fehlende Sekundenhistorie wird nicht erfunden. Event-Abonnements werden beim Entladen vollständig gelöst.

Die normale Prognose bleibt bei ausgeschaltetem, unzureichendem oder fehlerhaftem Indikator funktionsfähig. Zuerst wird lokal und optional nur beobachtet. Eine öffentliche Aktivierung mit Prognosewirkung folgt ausschließlich nach eigenem Benchmark, SPEC-Anpassung und regulärem Release.

Implementierungspräzisierung (2026-10-03): Ausrichtungsvielfalt wird anhand der
Flächennormalen beurteilt. Deterministische Klassen enthalten nur Paare mit
weniger als 30° Abstand; Winkelrundung und der Nordübergang erzeugen keine
zusätzliche Evidenz. Änderungen der tatsächlich nutzbaren Quellen starten die
fünfminütige Rampenreferenz neu, damit Ausfälle keinen Wetterwechsel vortäuschen.
