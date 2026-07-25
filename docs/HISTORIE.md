# Historie: Herleitung, Entscheidungen und abgelöste Fassungen

> **Nicht normativ.** Stand: 2026-07-25 (angelegt bei der SPEC-Neufassung für
> Version 0.23.1).
>
> Dieses Dokument hält fest, **wie** das Projekt zu seinem heutigen Verhalten
> gekommen ist: Gründungskontext, Ausgangsbefunde, Strategieentscheid,
> Lieferplan, Entscheidungspunkte, Betreiber-Interview, Messdatenanalysen,
> datierte Momentaufnahmen und die abgelösten Struktur- und Änderungsregeln der
> alten SPEC. **Nichts hier ist eine Verhaltenszusage.** Was die Integration
> heute tut, steht ausschließlich in `docs/SPEC.md`; im Zweifel gilt der Code.
>
> Der Text der ausgelagerten Abschnitte ist inhaltlich unverändert übernommen —
> inklusive Datumsangaben, Versionsbezügen und Zahlen, die seither überholt sind.
> Genau das ist der Zweck: eine Momentaufnahme bleibt lesbar als das, was sie
> war.
>
> **§H13 enthält die Übergangstabelle alt → neu.** Ältere Verweise aus Issues,
> Pull Requests, `CHANGELOG.md`, `docs/orders/` und Chatprotokollen nennen die
> alte SPEC-Nummerierung; die Tabelle löst sie auf.

## §H1 Gründungskontext

`docs/SPEC.md` war ursprünglich zugleich das **Gründungsdokument** des Projekts
`balcony_solar_forecast` (eigenständige HA-Custom-Integration,
danielr0815/balcony-solar-forecast). Es entstand als Synthese aus **drei
unabhängigen Designentwürfen** (Compose / Physik-Motor / ML-first) und **drei
Jury-Reviews** (Genauigkeit / Engineering / Robustheit) auf Basis von **76
recherchierten, quellenbelegten Einzelbefunden**. Das **einstimmige Jury-Urteil
(3/3)** lautete: dedizierter Physik+Lern-Motor.

Auf Betreiberwunsch wurde das Vorhaben als **eigenes Projekt** aufgesetzt, nicht
als Modul in `battery_manager` — bestehende Konsumenten koppeln nur über
Standard-HA-Schnittstellen.

Zielversionen waren v0.1.0 … v0.4.0, je Phase einzeln deploybar und mit
Abbruch-Gates; Erweiterungen ab v0.5 wurden zunächst als Addendum-Sektionen
angehängt (§14/§15 der alten Gliederung). Rollenverteilung: **Umsetzung Opus,
Planung und Prüfung Fable.**

Die Statuszeile der alten SPEC lautete zuletzt: „v0.23.0 (2026-07-25) — alle
Phasen bis v0.4 (Scoreboard/Quantile/Dashboard, §14) plus die
Betreiber-Oberfläche ab v0.5 (Verschattungsprofil, gebündelte Karten,
Wartungsaktionen, §15) umgesetzt; v0.1.0 seit 2026-07-06 live im Parallellauf."

## §H2 Ausgangslage 2026-07-05: zwei Engpässe, ein Konfigurationsdefizit

*(alte SPEC §1 — Befundlage vor Projektbeginn)*

| # | Befund | Wirkung |
|---|---|---|
| E1 | Die installierte open_meteo_solar_forecast-Instanz modelliert **1 Ebene, 1600 Wp, Horizont AUS** — real sind es **6 Ebenen, 3260 Wp** mit massiver Standortverschattung | Tagesform und -summe systematisch falsch; ESE/NNE/SSW-Profile nicht rekonstruierbar |
| E2 | Konsumenten (z. B. battery_manager) erhalten heute nur **Tages-kWh-Werte**, keine verlässliche Stundenkurve | Lastplanung (Überschusslasten so spät wie möglich) braucht die Stundenform, nicht nur die Summe |
| E3 | Open-Meteos serverseitige GTI ist **isotrop mit fixem Albedo 0,20** | auf 70–80°-Ebenen nachweislich 6–12 % zu niedrig; Schnee-Albedo nicht abbildbar |
| E4 | Horizont-Feature der Integration maskiert **nur den Direktstrahl** | Diffusanteil (dominiert Winter/Nebel!) wird nie reduziert — kein Sky-View-Faktor |

Standortbefund (PVGIS `printhorizon`, live geprüft, 414 m Höhe): Terrain-Horizont
Ost 8,8°, Südost 14–18°, **Süd 18,3°** — die Wintersonne erreicht am 21.12.
maximal **18,0°**: das Gelände allein blockiert im Hochwinter praktisch jede
Direktstrahlung. Das handgeschätzte Betreiber-Profil (Süd 30°, SSW 40°) enthält
zusätzlich Bäume + Gebäude (Nahfeld, für das 90-m-DEM unsichtbar). Beide Quellen
ergänzen sich: **PVGIS = Fernfeld, Betreiber/Lernen = Nahfeld.**

*Heute normativ:* die Sachbegründungen aus E3 stehen in SPEC §3 („warum keine
serverseitige GTI") und §4.3, die aus E4 in SPEC §5.3 (halbtransparenter
Horizont fürs Diffus).

## §H3 Referenzanlage des Betreibers (Bestandsaufnahme 2026-07-05)

*(alte SPEC §2)*

| Ebene | Azimut | Neigung | Module | Wp | Balkon |
|---|---|---|---|---|---|
| P1 | 115° | 70° | M2, M3 | 740 | unten, Front |
| P2 | ~25° | 70° | M1 | 370 | unten, links (N) |
| P3 | ~205° | 70° | M4 | 430 | unten, rechts (S) |
| P4 | 115° | 80° | M6, M7 | 860 | oben, Front |
| P5 | ~25° | 80° | M5 | 430 | oben, links (N) |
| P6 | ~205° | 80° | M8 | 430 | oben, rechts (S) |

Summe **3260 Wp** an **4× Hoymiles HMS-800W-2T** (bestätigt B1: **AC-Limit
800 VA je WR**; je 2 Module, **1 MPPT pro Port** → Module elektrisch
unabhängig). Port→Modul-Zuordnung (aus dem Energie-Dashboard des Betreibers,
B2 — systematisch: Port 1 = ungerades Modul, Entity-Suffixe `_2…_4` = WR 2–4):

| WR | Port 1 | Port 2 |
|---|---|---|
| WR1 | M1 (`sensor.inverter_port_1_dc_*`) | M2 (`sensor.inverter_port_2_dc_*`) |
| WR2 | M3 (`…_dc_*_2`) | M4 (`…_dc_*_2`) |
| WR3 | M5 (`…_dc_*_3`) | M6 (`…_dc_*_3`) |
| WR4 | M7 (`…_dc_*_4`) | M8 (`…_dc_*_4`) |

`state_class` vorhanden → **Langzeitstatistik läuft seit 2024-07** (B11:
>365 Tage; real ~24 Monate), wird nie gelöscht = warme Trainingsdaten ab Tag 1.
AC-Clipping: nur wenn beide Ports zusammen 800 VA reißen — bei diesen Neigungen
praktisch nie; trotzdem als 1-Zeilen-Clamp modelliert. Seiten-Azimute exakt 90°
zur Front (B3: 25°/205° exakt); Neigungskonvention bestätigt (B4);
Balkon-über-Balkon-Verschattung vernachlässigbar (B5, Betreiber-Entscheid).

Verschattung: (a) Hang O/SO 200–300 m (Morgen; Winter fast ganztags), (b) 2 Bäume
~10 m S (Frühjahr/Herbst, saisonale Transparenz), (c) Gebäude selbst (Fassade
115° → nachmittags kein Direktstrahl), (d) häufiger Winternebel
(Wetterfehler-Klasse, keine Geometrie).

*Heute normativ:* der Inhalt und die **bekannten Mängel** des ausgelieferten
`const.DEFAULT_SITE` stehen in SPEC §7.8; die Azimutkonvention in SPEC §20.1.

## §H4 Kernfrage und Strategie-Entscheid (2026-07-05)

*(alte SPEC §3)*

**Frage des Betreibers:** Reicht ein Aufsatz („Addon-Plugin") auf die *Ausgaben*
von Open-Meteo Solar Forecast, oder was ist die beste Gesamtstrategie?

**Antwort (einstimmig):** Ein Aufsatz auf die heutigen Ausgaben reicht **nicht**
— E1 zerstört Information, die keine nachgelagerte Korrektur rekonstruiert;
E3/E4 sind in den Eingängen der Integration strukturell verbaut; und die
Integration summiert alle Arrays in *eine* Kurve, sodass das größte Asset des
Standorts (Port-genaue Messwerte je Ebene) keinen Ansatzpunkt fände. Die
richtige Strategie ist aber **kein** neuer Datenanbieter und **kein** schweres
ML:

1. **Rohstrahlungskomponenten** (GHI/DNI/DHI + Temp + Wolken/Sicht/Schnee) aus
   **demselben freien Open-Meteo-Endpunkt** holen — *ein* Call statt sechs
   (~48 Calls/Tag, Limit 10 000).
2. **Lokale Physik** (~300 Zeilen geschlossene Formeln, stdlib-only):
   Transposition je Ebene + korrekte Horizont-/Diffusbehandlung.
3. **Lernen dort, wo die Information liegt:** je Messkanal (hier: Port), je
   Sonnenstand, gegen frei konfigurierbare Ist-Sensoren.

Die bestehende Integration wird **nicht weggeworfen**: Phase 0 rekonfiguriert sie
auf die echten 6 Ebenen (nur Konfiguration!) und sie bleibt dauerhaft als
**eingefrorene Vergleichs-Baseline** installiert — ein Motor-Bug zeigt sich dann
als „verliert gegen Baseline" statt als stiller Prognosefehler.

## §H5 Lieferplan und Umsetzungsstand

*(alte SPEC §9, die §14-Präambel und die Aufwandsschätzung)*

| Phase | Version | Inhalt | Gate/Abbruchkriterium |
|---|---|---|---|
| **0** | — (nur Konfig) | **✅ AUSGEFÜHRT 2026-07-05** (Variante „Einzelplatten" per B12): **8 separate rany2-Entries** „PV Modul 1…8" (je 1 Modul; Azimut in der HA-UI in **0=N**: 25/115/205 — der Koordinator rechnet intern −180; Neigung 70/80; Wp 370/430; η 0,96; inverter_power = Wp; ohne Horizont — Dateizugriff auf HAOS nicht verfügbar, Horizont kommt im Motor) + **4 Summen-Template-Sensoren** `sensor.pv_prognose_{heute,morgen,uebermorgen,leistung_jetzt}_alle_module`. Erste Werte plausibel (heute 6,79 kWh vs. 3,50 alt). Alt-Entry „Home-LA" (1600 Wp) läuft unverändert weiter und speist vorerst battery_manager. Das 8-Entry-Ensemble = **Baseline** | Plausibilität an 1 klaren Tag (Konventions-Checkliste), dann Konsumenten umhängen |
| **1** | v0.1.0 | Projekt-Gerüst (Config Flow: Standort, N Ebenen, Horizonttabellen-Import, WR-Gruppen, Mess-Entitäten; HACS-Struktur) + Motor `core/` (reine Physik, ohne Lernen) + Sensoren/Service/Energy-Hook + **Forecast-as-issued-Logger + Ist-Logger ab Tag 1** + Golden-Tests gegen offline erzeugte **pvlib-Referenzvektoren** (alle 6 Ebenen, Tiefstand 2–10°, Konventionsgrenzen) als Merge-Blocker; 2 Wochen Parallellauf | **Kill-Gate** (B9-gewichtet): 14-Tage-Parallellauf, **Tages-kWh-MAE ≥ 10 % unter dem 8-Entry-Baseline-Ensemble** (Primärmetrik); Taglicht-Stunden-MAE als Zweitmetrik berichtet — sonst Stopp, Baseline behalten |
| **2** | v0.2.0 | **✅ IMPLEMENTIERT 2026-07-06** (mit Phase 3 zusammen, D-P10): Intraday-Lerner (k_c-Raum, τ≈90 min, Clamp [0,25…2,5], nie persistiert) + Day-ahead-RLS je (Wolkenklasse × Tagesabschnitt) + Drift-Monitor (Auto-Abschaltung + Repair-Issue + **Auto-Restore aus dem Rollback-Ring**) + Kollaps-Detektor + `scripts/backfill.py` (Previous-Runs + LTS via HA-WS) + Services `import_bootstrap`/`dump_shademap`/`rollback_learners` | Gates werden im Parallellauf **nachträglich** ausgewertet: 14 Tage, nächste-6-h-MAE ≥ 5 % unter reiner Physik, stratifiziert (klar/bewölkt/Nebel) |
| **3** | v0.3.0 | **✅ IMPLEMENTIERT 2026-07-06** (D-P10): Shademap-Lerner — beam-referenzierte Transmittanz je (Kanal × Sonnenaz. 5° × El. 2,5° × Halbjahr), Clear-Sky-Gate elevationsabhängig, Shrinkage w=n/(n+20) mit statischem Horizont-Prior, Clamp [0…1,1], trainiert gegen **ungegatete** Beam-Referenz (sonst Selbstreferenz → √T-Fixpunkt) | dito nachträglich: 14 klare Tage, Klartag-Stunden-MAE ≥ 10 % unter reiner Physik; Polarkarte (`dump_shademap`) ≙ bekannten Hindernissen |
| **4** | v0.4.0 (opt.) | P10/P50/P90 im Service/Attributen | 80-%-Band: 70–90 % gemessene Abdeckung |

Aufwandsschätzung (Jury-korrigiert, ×2 auf Entwurfsschätzung): Phase 0 ½ Tag;
Phase 1 ~1–2 Wochen Teilzeit (Config Flow + Gerüst kommen zum Motor hinzu); 2–4
je 2–5 Tage; Lern-Konvergenz 1 Saison passiv. Nach Phase 1 oder 2 dauerhaft
stehenbleiben ist ein **kohärenter Endzustand**.

**Präambel der Phase-4-Sektion (alte SPEC §14):** Phase 4 (v0.4.0),
Betreiber-Entscheid 2026-07-06 (D-P11): drei Deliverables; der
**battery_manager-Cutover ist DEFERRED**, bis das Scoreboard das Kill-Gate
bestätigt. battery_manager und seine Entity-Verweise werden nicht angefasst.
Laufzeit bleibt **stdlib-only** (aiohttp erlaubt), `requirements` bleibt leer.

**Literatur-Erwartungswerte (alte SPEC §10):** realistische Erwartung laut
Literatur/Recherche: **30–50 % weniger Stunden-MAE** gegenüber dem Vorzustand
(E1+E2 zusammen), Intraday-Tuning zusätzlich 10–20 %.

**Baseline-Zeile der alten Azimut-Konventionstabelle (Anhang A):**
„rany2-HA-UI (Config Flow) — **0=N direkt eingeben**, der Koordinator rechnet
intern `−180` (Quellcode verifiziert 2026-07-05); P1/P4 115, P2/P5 25, P3/P6
205." Die rany2-Integration ist kein Vertragsbestandteil mehr.

*Heute normativ:* die Gate- und Abbruchkriterien stehen in SPEC §15.1/§15.4, die
pvlib-Golden-Vektoren als Merge-Blocker in SPEC §21, der Issued-Snapshot mit
beiden Stundenkurven in SPEC §16.2, die stdlib-only-Zusage in SPEC §2.

## §H6 Entscheidungspunkte D-P1 … D-P11

*(alte SPEC §11, Entscheidungslog vom 2026-07-05/06)*

- **D-P1** Paketierung: **eigenständige Custom Integration**
  `balcony_solar_forecast` (Betreiber-Entscheid 2026-07-05; überstimmt das
  Jury-Votum „Modul in battery_manager" — Kopplung nur über
  Standard-Schnittstellen).
- **D-P2** Datenquelle: Open-Meteo Rohkomponenten, 1 Call; keine neuen Anbieter
  zur Laufzeit. Solcast/forecast.solar/met.no verworfen (Ebenen-Limits,
  schrumpfende Free-Tiers, keine Strahlung). BrightSky/MOSMIX als möglicher
  zweiter freier Ensemble-Member in Reserve.
- **D-P3** Transposition: Hay-Davies (nicht Perez, nicht isotrop). stdlib.
- **D-P4** Horizont: je Ebene, mit Transmittanz + Saison; Fernfeld PVGIS,
  Nahfeld Betreiber→Lerner. Diffus über SVF, nicht nur Beam.
- **D-P5** Lernen: 2 Zeitskalen (Shademap je Messkanal × Sonnenstand,
  clear-sky-gegated; Intraday-Ratio in k_c-Raum). Kein Ridge/GBM als Primärpfad
  (Auditierbarkeit; numpy-Pinning-Risiko). ✔ Jury
- **D-P6** Baseline: rany2 6-Array-Entry bleibt dauerhaft als Watchdog.
- **D-P7** Ausgabe: P50-Kurve (15 min + stündlich) über Sensoren, Attribute,
  Service, Energy-Hook; P10/P90 = v0.4.0-Entscheid.
- **D-P8** Alles Gelernte ist clamped, gated, abschaltbar, rollbackbar;
  Degradation nie still.
- **D-P9** Generik: Ebenen, Horizonte, WR-Gruppen, Mess-Entitäten frei
  konfigurierbar; das Betreiber-Setup ist Referenzbeispiel, kein Hardcoding.
- **D-P10** (Betreiber, 2026-07-06): v0.2 + v0.3 **gemeinsam vorgezogen** gebaut
  statt sequenziell nach Gate-Auswertung. Die Gate-Logik bleibt erhalten, weil
  die **Attribution** konstruktiv gesichert ist: der nächtliche Issued-Snapshot
  speichert **beide** Stundenkurven (rohe Physik UND korrigiert) — der
  Parallellauf kann Physik- und Lernbeitrag getrennt bewerten. Absicherung:
  Shrinkage-Cold-Start (Shademap wirkt anfangs ≈ 0), Drift-Monitor mit
  Auto-Abschaltung + **Auto-Restore des Pre-Streak-Zustands aus dem
  Rollback-Ring**, Kill-Switch je Schicht, Service `rollback_learners`,
  **Tages-Idempotenzmarker** im Store. Prozess: Fable plant/reviewt/verifiziert,
  Opus implementiert; Kritisch-Fixes nach Fable-Spezifikation.
- **D-P11** (Betreiber, 2026-07-06): v0.4 = **Skill-Scoreboard +
  P10/P50/P90-Quantile + Observability-Dashboard** bauen; den
  **battery_manager-Cutover DEFERRED**, bis das Scoreboard das Kill-Gate
  bestätigt. Das Scoreboard ist das Gate, an dem der ganze Plan hängt: es misst
  nächtlich pro Vortag den Tages-kWh-Fehler des Motors **as issued** (aus dem
  Issued-Ring, nie mit heutigem Lernstand nachgerechnet) gegen jede konfigurierte
  externe Vergleichsprognose **wie sie am Vortag stand** (Recorder-Historie, nie
  der heutige Wert) gegen die gemessene Ist-Summe, stratifiziert nach
  Wetterklasse. Vergleichs-Sensoren sind **generisch + konfigurierbar** (leer
  ausgeliefert); die zwei Vergleiche des Betreibers sind in `docs/DASHBOARD.md`
  dokumentiert, nicht im Runtime-Default hardcodiert (D-P9). Quantile:
  nichtparametrische historische Simulation aus dem 90-Tage-Fehlerring, Band
  kollabiert auf P50 bei zu wenig Samples (keine Fake-Spreizung). Store-Schema
  v2→v3 **additiv**, Lernzustand des Live-Installs bleibt byte-treu erhalten.
  battery_manager wird **nicht** angefasst.

*Heute normativ:* D-P1 in SPEC §2, D-P8 in SPEC §9 (Leitsatz) und §13, D-P9 in
SPEC §2 / §7.8 / §15.3, die Attributionszusage aus D-P10 in SPEC §16.2.

## §H7 Betreiber-Antworten B1 … B12 (2026-07-05)

*(alte SPEC §12, Interviewprotokoll)*

- **B1 WR:** HMS-**800**W-2T, AC-Limit **800 VA je WR**.
- **B2 Zuordnung:** aus dem Energie-Dashboard ausgelesen → Tabelle §H3.
- **B3 Seiten-Azimute:** exakt 90° zur Front → 25°/205° exakt.
- **B4 Neigung:** bestätigt (gegen Horizontale, 90° = senkrecht).
- **B5 Balkon-über-Balkon:** nur ganz leicht/selten → **ignorieren**.
- **B6 Gebäudekante:** aus Messdaten analysiert → §H8 (Beam-Kollaps der S-Module
  bei Sonnenazimut ~205–218°).
- **B7 Bäume:** Laubbäume; aus Messdaten analysiert → §H8 (Symmetrietest:
  M4 −15–17 % Sep vs. März, M8 −4 %; Sektor ~135–175°).
- **B8 Schnee:** bleibt gelegentlich haften → Kollaps-Detektor bestätigt
  prioritär.
- **B9 Zielmetrik:** **Tages-kWh-Prognose** → Phase-1-Gate wird auf
  Tages-kWh-MAE gewichtet (Stunden-MAE als Zweitmetrik berichtet).
- **B10 Baseline:** ja, dauerhaft behalten.
- **B11 Historie:** >365 Tage (real: LTS seit 2024-07, ~24 Monate).
- **B12 Phase 0:** ja, als **Einzelplatten** (8 Entries) + zusätzliche
  **Summen-Sensoren** über alle Module → ausgeführt, siehe §H5 Phase 0.

*Heute normativ:* die Neigungskonvention aus B4 in SPEC §20.2, die Primärmetrik
aus B9 in SPEC §15.1, das AC-Limit aus B1 als Zahlenwert im Auslieferungs-Default
SPEC §7.8.

## §H8 Messdaten-Herleitung (24 Monate LTS, analysiert 2026-07-05)

*(alte SPEC §13, §13.4-Startwerte und §13.5)*

Methode: stündliche Langzeitstatistik aller 8 Port-Sensoren (137 632 Zeilen,
2024-07 … 2026-07) → **P90 je (Monat × Stunde)** ≈ Klartag-Profil (Mediane sind
wetterverschmiert); Sonnenstände per NOAA-Formel (Selbsttest gegen PVGIS:
Juni-Mittag 64,9°, Dez. 18,0° — exakt).

1. **Hang/Ost-Horizont:** M1 (N, unten) springt im Juni von 63 W (6 h, Sonne
   az 67°, el 10,8°) auf 210 W (7 h, el 20,2°) → effektiver Horizont
   **~12–15° im Sektor 60–100°** (etwas über PVGIS-Terrain 8,8° →
   Nahfeld-Zuschlag). Dezember: P90-Peak der Front-Module nur ~59 W → bestätigt
   „Terrain 18,3° > Wintersonne 18,0°" (praktisch kein Direktstrahl im
   Hochwinter).
2. **Gebäudekante:** Die S-Module kollabieren im Juni zwischen Sonnenazimut
   **~205° und ~218°** (M4: 269 W @13 h → 85 W @14 h; M8: 194 → 109 W), obwohl
   ihre Ebene Beam bis ~295° sähe → **Hauswand-Kante bei az ≈ 210–218°**,
   unterer Balkon etwas früher als oberer. Front-Module: natürliches Beam-Ende
   az ~205° (= Geometrie-Limit 115°+90°) — Gebäude für sie nicht zusätzlich
   sichtbar. N-Module: Beam-Ende az ~115° (Geometrie-Limit) ✓.
3. **Bäume (Sonnenbahn-Symmetrietest** — gleiche Sonnengeometrie, anderer
   Laubzustand): Tagesenergie Sep/März front-normalisiert: **M4 (S unten) 0,85**
   (≈ −15–17 %), **M8 (S oben) 0,99** (≈ −4 %); stärkste Stunden 10–12 h
   (Sonne az ~140–170°, el ~30–45°), M4-Transmittanz dort belaubt ≈ 0,3–0,6. →
   Baumsektor **az ~135–175°**, Baumkronen-Elevation von unten ~35–45°, von
   oben ~25–35°.

**Daraus abgeleitete initiale Horizonttabellen je Ebene** (Transmittanz τ,
saisonal wo markiert):

- Alle Ebenen, Fernfeld: az 60–100° el 13° τ0 · az 100–150° el 16° τ0 (Hang,
  PVGIS+Messung) · sonst PVGIS-Profil.
- P3/P6 (S): zusätzlich az 135–175° el 40°(unten)/30°(oben) **τ 0,45 belaubt /
  0,8 kahl** (Bäume, lernfähig) · az >212° el 90° τ0 (Hauswand).
- P1/P4 (Front): az >205° irrelevant (Geometrie-Limit); keine Zusatzeinträge
  nötig.
- P2/P5 (N): az >115° irrelevant; Fernfeld Ost besonders wichtig.

> **Wichtiger Vermerk:** der Screen-Sektor **az 135–175 auf M4/M8** ist durch die
> spätere Shademap-Auswertung (Bootstrap über LTS ab Juli 2025) **widerlegt** —
> real verschattet dieser Sektor die Front-Module M2/M3. Der ausgelieferte
> `const.DEFAULT_SITE` trägt die widerlegte Zuordnung weiterhin; das ist als
> bekannte Abweichung in SPEC §7.8 normativ festgehalten.

**Elevationsabhängige Baumkronen-Transmittanz (Herleitung von `tau_points`):**
die Ost-Baumkronen (az ~52–89) sind halbtransparent mit elevationsabhängiger
τ_eff (gepoolte 4-Tage-Messung Juli 2026: el 5–6 ≈ 0,25 · 6–7 ≈ 0,45 ·
8–9 ≈ 0,85 · ≥9 ≈ 1). Statt diese Rampe als τ(az) entlang des Sonnenpfads eines
Ankertags zu kodieren (Saisondrift ~0,3°/Tag, Phantom-Beam im Spätsommer), trägt
die Zeile ein Inline-Profil `tau_points` unterhalb der Kronen-Oberkante.

**Verschattungsgruppen (Herleitung):** Weil Hang, Baumsektor und Hauswandkante
Standort-Geometrie sind (Befunde 1–3, nicht modulspezifisch), können gleich
verschattete Ebenen desselben Balkons über eine gemeinsame `shade_group` einem
Verschattungs-Pool angehören — ein Sample eines Moduls kommt so allen
Gruppenmitgliedern zugute.

*Heute normativ:* die Feldsemantik und Validierung der Horizontzeilen in SPEC
§5.1, die Laub-Rampe und die Migrationsregel gegen die az-Rampe in SPEC §5.2, der
Inhalt des Auslieferungs-Defaults in SPEC §7.8, das Pooling in SPEC §9.2, die
Sonnenstands-Prüfanker in SPEC §4.1/§21.

## §H9 Quellenanhang (recherchiert & live verifiziert 2026-07-05)

*(alte SPEC Anhang B — mit ausdrücklichem Haltbarkeitsvorbehalt: API-Details
können sich seither geändert haben.)*

Open-Meteo Docs/Pricing/Terms (minutely_15 ICON-D2 nativ; GTI isotrop, Albedo
0,20, 1 Ebene/Call; Free-Tier 10 k/Tag; Previous-Runs- & Satellite-Radiation-API)
· PVGIS v5.3 printhorizon/seriescalc (48 Azimute, SRTM ~90 m; live: S 18,3° vs.
Wintersonne 18,0°) · rany2/open-meteo-solar-forecast (Quellcode: Multi-Array je
Entry, Horizont = Beam-only, watts/wh_period-Attribute, Ross-Modell; Deps
aiohttp/suncalc/numpy/pytz) · Hay-Davies-Fassaden-Benchmarks (EPJ PV 2024;
Mayer & Grof, Appl. Energy 2021: Separation+Transposition = kritischste
Kettenglieder) · SunPower Shade-Loss (arXiv 2209.09456) · Reno-Hansen Clear-Sky
· EMHASS adjust_pv_forecast (Residual-Regression-Muster) · Hoymiles
HMS-2T-Datenblatt (1 Eingang/MPPT) · HA-Dev-Docs (Store/async_delay_save,
recorder statistics_during_period, exclude_attributes, async_get_solar_forecast,
Service-with-Response) · DWD CDC Phänologie (Laub-Termine, optional).

*Heute normativ:* das Free-Tier-Budget und die native 15-min-Auflösung in SPEC
§3, die isotrope Server-GTI als Begründung der eigenen Transposition in SPEC §3
und §4.3.

## §H10 Datierte Momentaufnahmen aus normativen Abschnitten

- **Erst-Urteil des Kill-Gates (forensik C3, alte SPEC §14.1):** Das Gate braucht
  ein **volles** 14-Tage-Fenster gewerteter Tage. Nach dem `_actuals`-Epoch-Fix
  (0.19.2) waren nur ~3 Tage Catch-up nachholbar; **06.–12.07.2026 bleiben
  dauerhaft unscorebar** (für diese Tage existiert kein archivierter
  Issued-Snapshot mehr), daher füllt sich das rollierende Fenster erst danach und
  `kill_gate_passed` liefert erstmals **um den 27.07.2026** ein Urteil statt
  `None`. Ein einmaliger Re-Score-Service für die rettbaren Issued-Tage wurde
  erwogen und zurückgestellt (der Issued-Ring hält die Daten). Bis dahin ist
  `kill_gate_passed = None` **korrekt**, kein Fehlschlag.
- **Store-v3-KRITISCH-Absatz (alte SPEC §14.4):** der Live-Install (Entry
  `01KWT809F7MHH97F8XCKEJTZ0M`) hatte **jetzt** einen befüllten v2-Store auf
  Platte (Shademap 7 Kanäle / 851 Bins, Day-ahead 12 Zellen, Drift + Rollback +
  `trained_days`). Eine Migration, die irgendeinen Lernzustand verwirft oder
  zurücksetzt, ist ein KRITISCHER Fehler.
- **FOR-7-Belegzahlen zur Intraday-Asymmetrie (alte SPEC §14.2):** nach Spikes
  lag P10 an 3/6 Tagen über dem End-Ist.
- **IRC-4/FOR-7-Headline-Befund (alte SPEC §8):** das bloße Behalten des
  servierten Deckels ließ die Heute-Headline unter großem Skalar um die volle
  Faktor-Reserve ballonieren — 20.07.: +3,27 kWh bei Skalar 2,355.
- **Issued-Ratios der Juli-Forensik (alte SPEC §15.4):** die undokumentierte
  DC-Semantik hatte alle Issued-Ratios um ~8 % geschönt; der explizite
  AC-/DC-Ausweis behebt das.
- **Bifazialer Validierungswert (alte SPEC §4):** für den Referenzstandort
  ≈ 1,23 validiert (Backtest 16.07.); der A1-Rollout hob `bifacial_beam_gain`
  von 1,0 auf 1,25.

## §H11 Versionschronik der Einführungen

Die alte SPEC trug an vielen Stellen reine **Herkunftsvermerke**, die nur
belegen, *wann* etwas kam: „v0.5.x audit #11/#29/#30", „Seit v0.8 / v0.9 / v0.11
/ v0.13 / v0.15 / v0.16 / v0.17 / v0.19 / v0.22 / v0.23", „seit v0.2.0
implementiert, per Default aktiv", „v0.12.0-Merge-Migration", „Phase 4/v0.4.0",
„0.19.2", „0.23.1". Sie sind mit der Neufassung entfallen; die veröffentlichte
Chronik führt `CHANGELOG.md`.

**Ausdrücklich NICHT historisch** und deshalb weiterhin in `docs/SPEC.md`:
Kompatibilitätsregeln für Altzustände — Legacy-Gruppenkanäle werden mitgelesen
(SPEC §9.2), ein vor der Kanaltrennung gemischter Gruppen-Blob verwaist beim
Auflösen (SPEC §9.2), Alt-Snapshots ohne Schattenkarten-Kurve fallen auf das
gemeinsame Driftsignal zurück (SPEC §9.8), `doy=None` bedeutet die feste
Solarkonstante (SPEC §4.3), ein alter Cache ohne `slot_ceilings` behält den
servierten Deckel (SPEC §14.1), ein Rollback-Snapshot ohne Quantilfeld lädt mit
leerem Quantilzustand (SPEC §16.2).

## §H12 Abgelöste Regeln und Strukturbeschlüsse

Die alte SPEC war zugleich Vertrag **und** Gründungsdokument. Daraus folgten
Regeln, die der Betreiberauftrag vom 2026-07-25 („Die Spec soll keine
historischen Sachen beinhalten, sondern sich immer nur auf die Anforderungen
bezüglich der aktuellen Version beziehen") aufgehoben hat:

- **Die Liste „Historisch, nicht normativ" (alte §0).** Sie führte §1, §2, §3,
  §9, §11, §12, §13 und Anhang B als Abschnitte, die ausdrücklich **nicht** das
  aktuelle Verhalten beschreiben. Diese Abschnitte sind jetzt hier — die Liste
  entfällt ersatzlos.
- **Änderungsregel 4 „Nichts löschen".** Sie lautete: „Überholte Aussagen werden
  korrigiert oder als historisch markiert, nicht entfernt." Ersetzt durch SPEC
  §1.3 Regel 5: überholtes Verhalten wird **ersetzt**, die Herleitung wandert
  hierher.
- **Die Immutabilitätsregel in ihrer alten Form.** „Bestehende Top-Level- und
  Unterabschnittsnummern werden nie umnummeriert" galt für die gewachsene
  Nummerierung. Die Neufassung hat **einmalig** neu nummeriert; ab ihr gilt
  wieder append-only (SPEC §1.3 Regel 3).
- **Der Meta-Absatz der alten §15,** der die historisch gewachsene Nummerierung
  ausdrücklich beklagte („bleibt unverändert, weil der Code sie zitiert") — mit
  der thematischen Neugliederung gegenstandslos.
- **Die rany2-Zeile der Azimut-Konventionstabelle** (siehe §H5) und der Verweis
  auf die eingefrorene Baseline als Watchdog.
- **Der battery_manager-Ausblick der alten §8:** „Perspektivisch kann
  battery_manager (separates Projekt, eigene Entscheidung) seine P3-Anforderung
  ‚stündliche PV-Prognosen direkt nutzen' über den Service oder die Attribute
  erfüllen."
- **Die Anekdote zur Degradationsleiter:** „Die Sensoren gehen ehrlich auf
  `unavailable`, statt stille Altwerte zu halten (Lehre aus dem
  Fossibot-Verhalten)." Die Regel bleibt in SPEC §13, die Anekdote nicht.

## §H13 Übergangstabelle alt → neu

Ältere Verweise (`CHANGELOG.md`-Einträge vor 0.23.2, `docs/orders/`,
`docs/adr/`, Issues, PRs, Chatprotokolle) nennen die alte Nummerierung. Diese
Tabelle löst sie auf. **Achtung:** fast jede alte Nummer ist in der Neufassung
mit einem **anderen Thema** belegt — ein alter Verweis „SPEC §5" meint die
Lernschichten (heute §9), nicht den Horizont (heute §5).

| Alt | Thema (alt) | Neu |
|---|---|---|
| Titel-/Statusblock | Gründungsdokument, Zielversionen | §H1; Versionsstempel jetzt im SPEC-Kopf |
| §0 | Wegweiser, Historie-Liste, Änderungsregeln | §1 (Tabelle → §1.2, Regeln → §1.3); Historie-Liste → §H12 |
| §1 | Ausgangslage E1–E4 | §H2 (Sachbegründungen → SPEC §3, §5.3) |
| §2 | Standort-Geometrie (Referenzbeispiel) | §H3 (Default-Inhalt → SPEC §7.8, Azimut → SPEC §20.1) |
| §3 | Kernfrage & Strategie-Entscheid | §H4 |
| §4 | Zielarchitektur + Pipeline 1–8 + DC→AC-Kette | aufgeteilt: Architektur/Takte/Generik → SPEC §2; Fetch → §3; Physik → §4; Horizont → §5; Elektrik + DC→AC → §6; Store-Schreibsemantik → §16.3 |
| §4.1 | Konfigurationsschema `site` | SPEC §7 (Fingerprint-Pflicht → §7.6) |
| §5 | Lernschichten | SPEC §9; Wolken-/Zeitbinnung → §8; Re-Clamp → §6.4; Fingerprint/Reseed → §7.7 |
| §5.1 | Sichtbarkeit der Label-Gates | SPEC §10 |
| §6 | Unsicherheit + Backfill | aufgeteilt: Quantile → SPEC §11; Bootstrap/Import/Seeding → §12; `--use-default-site` → §7.8 |
| §6.1 | Ensemble-Wetter-Bänder | SPEC §11.3 |
| §7 | Degradationsleiter | SPEC §13 |
| §8 | Schnittstellen für Konsumenten | SPEC §14 (battery_manager-Ausblick → §H12) |
| §9 | Phasenplan | §H5; Gate-Kriterien → SPEC §15.1/§15.4; Golden-Vektoren → §21; Issued-Snapshot → §16.2 |
| §10 | Validierung & Metriken | SPEC §15.1 (Erwartungswerte → §H5) |
| §11 | Entscheidungspunkte D-P1…D-P11 | §H6 (D-P1 → SPEC §2, D-P8 → §9/§13, D-P9 → §2/§7.8/§15.3) |
| §12 | Betreiber-Antworten B1…B12 | §H7 (B4 → SPEC §20.2, B9 → §15.1, B1 → §7.8) |
| §13 | Messdaten-Befunde | §H8 (Laub-Rampe → SPEC §5.2, Startwerte → §7.8, Sonnenanker → §4.1/§21) |
| §13.4 | Horizonttabellen + Feldsemantik | Feldsemantik/Validierung → SPEC §5.1–§5.3 und §7.4; Startwerte → §7.8; Herleitung → §H8 |
| §13.5 | Verschattungsgruppen | SPEC §9.2 (Herleitung → §H8) |
| Anhang A | Konventionen + Checkliste | SPEC §20 (rany2-Zeile → §H5) |
| Anhang B | Quellen | §H9 |
| §14 (Präambel) | Phase-4-Deliverables | §H5 (stdlib-only → SPEC §2) |
| §14.1 | Skill-Scoreboard / Kill-Gate | SPEC §15.2–§15.5 (Erst-Urteil → §H10) |
| §14.2 | Quantile P10/P50/P90 | SPEC §11.1/§11.2 (Belegzahlen → §H10) |
| §14.3 | Observability-Dashboard | SPEC §18.1 |
| §14.4 | Store-Schema v3 | SPEC §16.1 (KRITISCH-Absatz → §H10) |
| §15 (Kopf) | Betreiber-Oberfläche, Nummerierungs-Meta | Zweck → SPEC §17-Kopf; Meta-Absatz → §H12 |
| §15.1 | Diagramm-Entitäten | SPEC §17.1 |
| §15.2 | Diagramm-Semantik | SPEC §17.2 |
| §15.3 | Diagramm-Berechnung & Tunables | SPEC §17.3 |
| §15.4 | Darstellung, Karten, `get_issued_forecast` | SPEC §18.3/§18.4/§18.5 (Antwortvertrag → §18.4, §16.2, §19) |
| §15.5 | Dashboard-Installation per Aktion | SPEC §18.2 |
| §15.6 | Re-Bootstrap per Aktion | SPEC §12.2 (+ §12.4) |
| §16 | Aktionen (Services) | SPEC §19 |
