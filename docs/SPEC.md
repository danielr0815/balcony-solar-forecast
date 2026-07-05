# Spezifikation: Balcony Solar Forecast — Mehrebenen-PV-Prognose mit Selbstlernen

> Status: **Entwurf — wartet auf Betreiber-Feedback** (2026-07-05)
> Gründungsdokument des Projekts `balcony_solar_forecast` (eigenständige
> HA-Custom-Integration, danielr0815/balcony-solar-forecast). Synthese aus
> drei unabhängigen Designentwürfen (Compose / Physik-Motor / ML-first) +
> drei Jury-Reviews (Genauigkeit / Engineering / Robustheit) auf Basis von
> 76 recherchierten, quellenbelegten Einzelbefunden. **Einstimmiges
> Jury-Urteil (3/3): dedizierter Physik+Lern-Motor.** Auf Betreiberwunsch
> als **eigenes Projekt**, nicht als Modul in battery_manager — bestehende
> Konsumenten koppeln nur über Standard-HA-Schnittstellen.
> Zielversionen v0.1.0 … v0.4.0, je Phase einzeln deploybar, mit
> Abbruch-Gates. Umsetzung: Opus 4.8 Ultracode; Prüfungen: Fable 5.

## 1. Ausgangslage: zwei Engpässe, ein Konfigurationsdefizit

| # | Befund | Wirkung |
|---|---|---|
| E1 | Die installierte open_meteo_solar_forecast-Instanz modelliert **1 Ebene, 1600 Wp, Horizont AUS** — real sind es **6 Ebenen, 3260 Wp** mit massiver Standortverschattung | Tagesform und -summe systematisch falsch; ESE/NNE/SSW-Profile nicht rekonstruierbar |
| E2 | Konsumenten (z. B. battery_manager) erhalten heute nur **Tages-kWh-Werte**, keine verlässliche Stundenkurve | Lastplanung (Überschusslasten so spät wie möglich) braucht die Stundenform, nicht nur die Summe |
| E3 | Open-Meteos serverseitige GTI ist **isotrop mit fixem Albedo 0,20** | auf 70–80°-Ebenen nachweislich 6–12 % zu niedrig; Schnee-Albedo nicht abbildbar |
| E4 | Horizont-Feature der Integration maskiert **nur den Direktstrahl** | Diffusanteil (dominiert Winter/Nebel!) wird nie reduziert — kein Sky-View-Faktor |

Standortbefund (PVGIS `printhorizon`, live geprüft, 414 m Höhe): Terrain-
Horizont Ost 8,8°, Südost 14–18°, **Süd 18,3°** — die Wintersonne erreicht
am 21.12. maximal **18,0°**: das Gelände allein blockiert im Hochwinter
praktisch jede Direktstrahlung. Das handgeschätzte Betreiber-Profil (Süd
30°, SSW 40°) enthält zusätzlich Bäume + Gebäude (Nahfeld, für das 90-m-DEM
unsichtbar). Beide Quellen ergänzen sich: **PVGIS = Fernfeld, Betreiber/
Lernen = Nahfeld.**

## 2. Standort-Geometrie (Referenzbeispiel des Betreibers)

Die Integration ist generisch (N Ebenen, frei konfigurierbare
Mess-Entitäten); das konkrete Setup dient als Referenz und Testfall.
Azimut-Konvention **0 = Nord** (intern überall; Umrechnung nur an
API-Grenzen, siehe Anhang A).

| Ebene | Azimut | Neigung | Module | Wp | Balkon |
|---|---|---|---|---|---|
| P1 | 115° | 70° | M2, M3 | 740 | unten, Front |
| P2 | ~25° | 70° | M1 | 370 | unten, links (N) |
| P3 | ~205° | 70° | M4 | 430 | unten, rechts (S) |
| P4 | 115° | 80° | M6, M7 | 860 | oben, Front |
| P5 | ~25° | 80° | M5 | 430 | oben, links (N) |
| P6 | ~205° | 80° | M8 | 430 | oben, rechts (S) |

Summe **3260 Wp** an **4× Hoymiles HMS-800W/1000W-2T** (je 2 Module,
**1 MPPT pro Port** → Module elektrisch unabhängig). HA liefert pro Port
`sensor.inverter_port_{1,2}_dc_power/_dc_daily_energy/_dc_total_energy`
(Entity-Suffixe `_2…_4` für WR 2–4)
(`state_class` vorhanden → **Langzeitstatistik läuft bereits**, wird nie
gelöscht = Trainingsdaten ab Installation). AC-Clipping: nur wenn beide
Ports zusammen das WR-AC-Limit reißen — bei diesen Neigungen praktisch nie;
trotzdem als 1-Zeilen-Clamp modelliert (echte Limits: Frage B1).

Verschattung: (a) Hang O/SO 200–300 m (Morgen; Winter fast ganztags),
(b) 2 Bäume ~10 m S (Frühjahr/Herbst, saisonale Transparenz),
(c) Gebäude selbst (Fassade 115° → nachmittags kein Direktstrahl),
(d) häufiger Winternebel (Wetterfehler-Klasse, keine Geometrie).

## 3. Kernfrage & Strategie-Entscheid

**Frage des Betreibers:** Reicht ein Aufsatz („Addon-Plugin") auf die
*Ausgaben* von Open-Meteo Solar Forecast, oder was ist die beste
Gesamtstrategie?

**Antwort (einstimmig):** Ein Aufsatz auf die heutigen Ausgaben reicht
**nicht** — E1 zerstört Information, die keine nachgelagerte Korrektur
rekonstruiert; E3/E4 sind in den Eingängen der Integration strukturell
verbaut; und die Integration summiert alle Arrays in *eine* Kurve, sodass
das größte Asset des Standorts (Port-genaue Messwerte je Ebene) keinen
Ansatzpunkt fände. Die richtige Strategie ist aber **kein** neuer
Datenanbieter und **kein** schweres ML:

1. **Rohstrahlungskomponenten** (GHI/DNI/DHI + Temp + Wolken/Sicht/Schnee)
   aus **demselben freien Open-Meteo-Endpunkt** holen — *ein* Call statt
   sechs (~48 Calls/Tag, Limit 10 000).
2. **Lokale Physik** (~300 Zeilen geschlossene Formeln, stdlib-only):
   Transposition je Ebene + korrekte Horizont-/Diffusbehandlung.
3. **Lernen dort, wo die Information liegt:** je Messkanal (hier: Port),
   je Sonnenstand, gegen frei konfigurierbare Ist-Sensoren.

Die bestehende Integration wird **nicht weggeworfen**: Phase 0
rekonfiguriert sie auf die echten 6 Ebenen (nur Konfiguration!) und sie
bleibt dauerhaft als **eingefrorene Vergleichs-Baseline** installiert —
ein Motor-Bug zeigt sich dann als „verliert gegen Baseline" statt als
stiller Prognosefehler.

## 4. Zielarchitektur: eigenständige Integration `balcony_solar_forecast`

Paketierung als **eigene HACS-Custom-Integration** (Betreiber-Entscheid
2026-07-05; ersetzt das ursprüngliche Jury-Votum „Modul in
battery_manager"). Konsequenzen:

- **Saubere Grenze:** Konsumenten koppeln ausschließlich über
  Standard-HA-Schnittstellen (§8) — battery_manager bleibt unverändert
  und zeigt lediglich seine vorhandenen Forecast-Entity-Picker auf die
  neuen Sensoren.
- **Bewährtes Repo-Muster wird übernommen:** HA-freier Kern
  `custom_components/balcony_solar_forecast/core/` (reine Funktionen,
  pytest-Golden-Tests in `tests/core/`), HA-Glue (Coordinator, Config
  Flow, Sensoren) darüber. `requirements` bleibt **leer** (aiohttp ist
  HA-Core; sonst stdlib `math` — musl-/Update-sicher, kein
  numpy/pandas/pvlib zur Laufzeit).
- **Generik statt Hardcoding:** Ebenen (Azimut/Neigung/Wp/η), Horizont-
  tabellen, WR-Gruppen (Ports→AC-Limit) und Ist-Mess-Entitäten sind
  Konfiguration; nichts ist an Hoymiles oder diesen Standort gebunden.

Pipeline (reine Funktionen über 15-min-Slots × N Ebenen, <50 ms/Lauf):

1. **fetcher.py** — 1 Call/30 min: `minutely_15=shortwave_radiation,
   direct_normal_irradiance,diffuse_radiation,temperature_2m` +
   `hourly=cloud_cover_low/mid/high,visibility,snowfall,snow_depth`,
   `models=icon_seamless`, `forecast_days=3` (ICON-D2 nativ 15-min für
   Mitteleuropa, live verifiziert). Payload-**Schema**-Validierung (nicht
   nur HTTP-Status), Last-Good-Cache **im Store** (übersteht Neustart),
   Backoff mit Jitter.
2. **solpos.py** — NOAA-Sonnenstand (geschlossene Form, <0,1°).
3. **clearsky.py** — Haurwitz-GHI + Clear-Sky-Index k_c (nur als Lern-Gate
   und Normierung, keine Prognosequelle).
4. **transpose.py** — **Hay-Davies** je Ebene (Benchmark-Sieger auf
   Fassaden; Perez unnötig): Beam + zirkumsolar (Anisotropie-Index) +
   isotroper Rest × **SVF** + **Bodenreflex** albedo·GHI·(1−cos β)/2
   (bei 70–80° immerhin ~7–8 % der GHI; Albedo 0,20, **0,50 bei
   Schneedecke**, gedeckelt). Pflicht-Fixes: R_b-Deckel (≤10) bzw.
   Zirkumsolar = 0 unter 3° Sonnenhöhe; Intervallmittel-vs.-Instant-
   Semantik empirisch verifizieren (klarer Morgen als Unit-Test).
5. **horizon.py** — je Ebene Tabelle `(Azimut, Elevation, Transmittanz)`
   in 10°-Schritten, linear interpoliert: Fernfeld aus PVGIS + Betreiber-
   Profil; Nahfeld je Ebene differenziert (Gebäudekante hart, Baumsektor
   ~170–230° auf P3/P6 mit **saisonaler Transmittanz** ~0,7 laublos /
   ~0,2 belaubt, Kosinus-Rampe April/November). Unter Horizontlinie:
   Beam+zirkumsolar × Transmittanz; Iso-Diffus statisch × ebenen-eigenem
   SVF (behebt E4). Tabellen liegen **versioniert im Repo/Config-Export**,
   nicht nur in `.storage`.
6. **shademap.py** — langsamer Lerner (§5).
7. **electrical.py** — Ross-Zelltemperatur, −0,34 %/K, η konfigurierbar
   (Default 0,96), AC-Clamp je konfigurierter WR-Gruppe.
8. **bias.py / quantiles.py** — schneller Lerner + P10/P50/P90 (§5/§6).

HA-Glue: `DataUpdateCoordinator` (Fetch 30 min, Rechnen 15 min,
Training nächtlich ~01:30 im Executor). Ein `Store` (versioniert,
`async_delay_save`, ≤3 gebündelte Writes/Tag — eMMC-Schonung):
Horizonttabellen-Cache, Lernzustände, 90-Tage-Fehlerringpuffer,
Forecast-as-issued-Log. Schreibsemantik explizit: gebündelt per
`async_delay_save` + Flush bei HA-Stop; nach einem **harten Crash**
dürfen Last-Good-Cache und As-issued-Log bis zu einige Stunden
verlieren — akzeptiert, die Degradationsleiter (§7) greift.

## 5. Lernschichten (beide numpy-frei, beide abschaltbar)

**Langsamer Lerner — geometrisches Transmissionsfeld** (SunPower-Muster,
arXiv 2209.09456): je **Messkanal** (hier: WR-Port), je Bin (Sonnenazimut
5° × Elevation 2,5° × **Halbjahr** vor/nach Sommersonnenwende — sonst
aliasen April (laublos) und August (belaubt) im selben Sonnenstands-Bin)
eine EMA (α 0,15) der **beam-referenzierten Transmittanz**
`T = (P_gemessen − P_diffus_modelliert) / P_beam_modelliert` —
bewusst NICHT das Gesamtverhältnis gemessen/modelliert: im Schatten
enthält die Messung weiter den Diffus-Sockel; ein Gesamt-Ratio auf den
Beam angewandt würde verschattete Bins systematisch überschätzen und
diffus-unabhängige Verluste (Soiling, η-Fehler) dem Beam zuschreiben.
Nur **quasi-klare Samples** (k_c-Gate **elevationsabhängig** — Haurwitz
ist bei Tiefstand grob; plus Nachbarslot-Stabilität; plus modellierter
Beam-Anteil > 5 % Wp). Die gelernte Karte **ersetzt** die statische
Horizont-Transmittanz des Bins; Clamp [0,0 … 1,1] — **volle Okklusion
muss darstellbar sein** (Hauswand!). Cold-Start: Bins erben den
**statischen Horizont-Prior**, Übergang per **Shrinkage** w = n/(n+20)
statt hartem Min-Sample-Schalter. Lernt Hang, Bäume je Halbjahr,
Gebäudekante, Geländer — und korrigiert das handgemachte Horizontprofil
über eine Saison. Diagnose: Service, der die Karte als **Polartabelle**
ausgibt (visuell gegen bekannte Hindernisse prüfbar).

**Schneller Lerner — Wetterfehler intraday:** exponentiell abklingendes
Verhältnis (τ ≈ 90 min) gemessen/prognostiziert der letzten 2–4 h,
**im k_c-Raum konditioniert** (Geometrie/Saison herausnormiert), auf die
nächsten ~6 h abklingend angewandt, Clamp [0,25 … 2,5], nach HA-Neustart
Re-Init auf 1,0 (nie alten Zustand laden). Rettet Nebelmorgen ohne
falsche Geometrie. Optional später: 1 RLS-Bias-Skalar je
(Wolkenklasse × Tagesabschnitt) für Day-ahead.

**Schutzmechanismen (Jury-Auflagen, verbindlich):**
- Label-Gates im Trainer: eingefrorene Sensoren (unverändert + altes
  `last_updated` = fehlend), Energie-Monotonie, Messkanal-Dropout ⇒
  ganzen Tag verwerfen; nächtlicher Job **idempotent** (datums-gekeyt,
  doppelt laufbar).
- **Drift-Monitor**: rollierende 7-Tage-MAE korrigiert vs. reine Physik;
  verliert der Lerner 7 Tage in Folge → Auto-Abschaltung + HA-Repair-
  Issue; letzte 3 Lernstände für Rollback; Store validate-and-clamp beim
  Laden (korrupt ⇒ Faktoren 1,0, nie Setup-Crash).
- **Kollaps-Detektor**: alle Kanäle ≈ 0 bei hoher Prognose (Schnee auf
  Modulen, Total-Dropout) ⇒ beide Lerner für den Tag einfrieren, nur der
  geclampte Intraday-Skalar reagiert.
- Kill-Switches je Lernschicht im Options-Flow.

## 6. Unsicherheit (Phase 4, optional)

Nichtparametrische historische Simulation: empirische P10/P50/P90 aus dem
90-Tage-Fehlerringpuffer, konditioniert auf (Wolken-/**Nebelklasse** ×
Tagesabschnitt); Nebelklasse = Sicht < 1000 m ∨ (cloud_cover_low > 85 %
∧ Okt–Feb), nach erster Saison auf gemessene Abdeckung geprüft. Adaptive
konforme Nachführung für 80-%-Abdeckung. Nutzung durch Konsumenten:
P50 = Planung; P10 für konservative Reserven; P90 fürs Load-Timing
(Überschusslasten so spät wie möglich, ohne Export).
Der **Previous-Runs-API-Backfill** (geliefert in **Phase 2**, §9):
Forecasts as-issued ab 01/2024 gegen LTS-Ist-Werte — einmaliger
Offline-Job auf dem Dev-Rechner — füllt Bias-/Quantilspeicher vor dem
ersten Live-Winter. Verbindlichkeit: **Pflicht zu versuchen, kein
Blocker** — das System muss ohne diese API voll funktionieren.

## 7. Degradationsleiter (nie still!)

frische Prognose → Last-Good-Cache (Store, konfigurierbare Altersgrenze)
→ Reine-Physik-Kurve aus letztem gültigen Wetterbild → `unavailable`
(Konsumenten entscheiden selbst über ihre Fallbacks — battery_manager
hat seinen eigenen Staleness-Pfad). Jede Stufe sichtbar (binary_sensor
„degraded" bzw. Repair-Issue). Die Sensoren gehen ehrlich auf
`unavailable`, statt stille Altwerte zu halten (Lehre aus dem
Fossibot-Verhalten).

## 8. Schnittstellen für Konsumenten (Standard-HA, keine Kopplung)

- **Sensoren:** `energy_production_today / _tomorrow / _d2` (kWh) —
  bewusst kompatibel zum Muster der bestehenden Integration, sodass
  battery_manager **ohne Code-Änderung** nur seine drei Entity-Picker
  umstellt. Dazu `power_production_now` (W) und Diagnose (Baseline-MAE,
  Degradationsstatus, Lernstatus).
- **Volle Kurve:** 15-min-`watts`- und `wh_period`-Attribute auf den
  Energy-Sensoren (per `exclude_attributes` vom Recorder ausgeschlossen)
  **und** Service-with-Response `balcony_solar_forecast.get_forecast`
  (15-min/stündlich, P10/P50/P90 sobald vorhanden) — das saubere Muster
  nach dem Vorbild `weather.get_forecasts`.
- **Energy-Dashboard:** Energy-Platform-Hook `async_get_solar_forecast`
  (`wh_hours`).
- Perspektivisch kann battery_manager (separates Projekt, eigene
  Entscheidung) seine P3-Anforderung „stündliche PV-Prognosen direkt
  nutzen" über den Service oder die Attribute erfüllen.

## 9. Phasenplan (je Phase einzeln deploybar, mit Gates)

| Phase | Version | Inhalt | Gate/Abbruchkriterium |
|---|---|---|---|
| **0** | — (nur Konfig, **sofort**, unabhängig vom Projekt) | Bestehenden rany2-Entry auf **6 Arrays** umkonfigurieren (Komma-Listen; Azimut in 0=S-Konvention: −65,−155,+25,−65,−155,+25; Neigung 70,70,70,80,80,80; kWp 0.74,0.37,0.43,0.86,0.43,0.43), Horizont je Array aktivieren, partial_shading an. Konsumenten profitieren sofort; Entry wird zur eingefrorenen **Baseline**. Sonnentag-Checkliste (Anhang A) gegen Vorzeichenfehler | Plausibilität an 1 klaren Tag |
| **1** | v0.1.0 | Projekt-Gerüst (Config Flow: Standort, N Ebenen, Horizonttabellen-Import, WR-Gruppen, Mess-Entitäten; HACS-Struktur) + Motor `core/` (Schritte 1–5, 7 — reine Physik, ohne Lernen) + Sensoren/Service/Energy-Hook + **Forecast-as-issued-Logger + Ist-Logger ab Tag 1** + Golden-Tests gegen offline erzeugte **pvlib-Referenzvektoren** (alle 6 Ebenen, Tiefstand 2–10°, Konventionsgrenzen) als Merge-Blocker; 2 Wochen Parallellauf | **Kill-Gate:** 14-Tage-Parallellauf, Taglicht-Stunden-MAE ≥ 10 % unter der 6-Array-Baseline (Schwelle vor Codierungsbeginn fixiert, Gewichtung nach B9-Antwort) — sonst Stopp, Baseline behalten |
| **2** | v0.2.0 | Schneller Lerner + Degradationsleiter + Drift-Monitor + Previous-Runs-Backfill | 14 Tage: nächste-6-h-MAE ≥ 5 % unter Phase 1, stratifiziert berichtet (klar/bewölkt/Nebel) |
| **3** | v0.3.0 | Langsamer Lerner (Shademap) — **explizit bedingt** auf stratifizierte Phase-1/2-Auswertung („nicht aus Momentum bauen") | 14 klare Tage: Klartag-Stunden-MAE ≥ 10 % unter Phase 2; Polarkarte ≙ bekannten Hindernissen |
| **4** | v0.4.0 (opt.) | P10/P50/P90 im Service/Attributen | 80-%-Band: 70–90 % gemessene Abdeckung |

Aufwandsschätzung (Jury-korrigiert, ×2 auf Entwurfsschätzung): Phase 0
½ Tag; Phase 1 ~1–2 Wochen Teilzeit (Config Flow + Gerüst kommen zum
Motor hinzu); 2–4 je 2–5 Tage; Lern-Konvergenz 1 Saison passiv. Nach
Phase 1 oder 2 dauerhaft stehenbleiben ist ein **kohärenter Endzustand**.

## 10. Validierung & Metriken

Taglicht-Stunden-MAE/nRMSE (normiert auf Anlagen-kWp) + Tages-kWh-Fehler,
**stratifiziert**: klar / bewölkt / Nebel / Winter. Dauerhafte Diagnose-
Sensorik: Motor vs. eingefrorene Baseline vs. gemessene Summe (~30
Zeilen). Realistische Erwartung laut Literatur/Recherche: **30–50 %
weniger Stunden-MAE** gegenüber heute (E1+E2 zusammen), Intraday-Tuning
zusätzlich 10–20 %; Day-ahead-Ziel nRMSE ≤ ~10 % der installierten
Leistung (kWp), Tages-kWh-
MAE ≤ ~15 % an Mischtagen. Nebel bleibt die härteste Klasse (ehrlich:
dort hilft v. a. Intraday + breite Quantile).

## 11. Entscheidungspunkte

- **D-P1** Paketierung: **eigenständige Custom Integration**
  `balcony_solar_forecast` (Betreiber-Entscheid 2026-07-05; überstimmt
  das Jury-Votum „Modul in battery_manager" — Kopplung nur über
  Standard-Schnittstellen, §8).
- **D-P2** Datenquelle: Open-Meteo Rohkomponenten, 1 Call; keine neuen
  Anbieter zur Laufzeit. Solcast/forecast.solar/met.no verworfen
  (Ebenen-Limits, schrumpfende Free-Tiers, keine Strahlung). BrightSky/
  MOSMIX als möglicher zweiter freier Ensemble-Member in Reserve.
- **D-P3** Transposition: Hay-Davies (nicht Perez, nicht isotrop). stdlib.
- **D-P4** Horizont: je Ebene, mit Transmittanz + Saison; Fernfeld PVGIS,
  Nahfeld Betreiber→Lerner. Diffus über SVF, nicht nur Beam.
- **D-P5** Lernen: 2 Zeitskalen (Shademap je Messkanal × Sonnenstand,
  clear-sky-gegated; Intraday-Ratio in k_c-Raum). Kein Ridge/GBM als
  Primärpfad (Auditierbarkeit; numpy-Pinning-Risiko). ✔ Jury
- **D-P6** Baseline: rany2 6-Array-Entry bleibt dauerhaft als Watchdog.
- **D-P7** Ausgabe: P50-Kurve (15 min + stündlich) über Sensoren,
  Attribute, Service, Energy-Hook; P10/P90 = v0.4.0-Entscheid.
- **D-P8** Alles Gelernte ist clamped, gated, abschaltbar, rollbackbar;
  Degradation nie still.
- **D-P9** Generik: Ebenen, Horizonte, WR-Gruppen, Mess-Entitäten frei
  konfigurierbar; das Betreiber-Setup ist Referenzbeispiel, kein
  Hardcoding.

## 12. Offene Betreiber-Fragen (bitte beantworten)

1. **B1 — WR-Varianten:** HMS-**800**W-2T oder HMS-**1000**W-2T (je
   Gerät)? → AC-Clamp-Wert. (Typenschild/S/N; Device-Info ist ambivalent.)
2. **B2 — Port→Modul-Zuordnung:** Die 4 WR erscheinen in HA als
   `sensor.inverter_port_{1,2}_dc_*` mit Entity-Suffixen `_2…_4` für das
   2.–4. Gerät. Welche (Gerät, Port)-Kombination gehört zu welchem Modul
   M1–M8 (WR-Seriennummern helfen)? Vorschlag: an einem klaren Morgen je
   Modul kurz abdecken und Zuordnung notieren (15 min) — ohne korrekte
   Zuordnung lernt Ebene A die Schatten von Ebene B.
3. **B3 — Seiten-Azimute:** Sind die Seitenmodule exakt 90° zur Front
   (25°/205°) montiert oder angestellt/abweichend (±10° egal, ±30° nicht)?
4. **B4 — Neigungs-Konvention:** 70°/80° = Winkel gegen die Horizontale
   (90° = senkrecht)? Bitte bestätigen.
5. **B5 — Balkon-über-Balkon:** Verschattet der obere Balkonboden bei
   hoher Sommersonne die oberen Kanten der unteren Module (P1)? Falls ja:
   eigener Horizont-Eintrag „oben" für P1–P3 (Elevations-Obergrenze).
6. **B6 — Gebäudekante:** Ab wann (Uhrzeit an einem Sommertag genügt)
   verschwindet die Sonne für die Front-Module hinter der Hauswand?
   → initiale Azimut-Grenze je Ebene.
7. **B7 — Bäume:** grober Azimut-Sektor und Höhenwinkel der 2 Bäume von
   jedem Balkon aus (Handy-Kompass + Daumenpeilung reicht); Laubbäume?
8. **B8 — Schnee:** Bleibt auf den fast senkrechten Modulen je Schnee
   haften (Erfahrung)? → Kollaps-Detektor-Priorität.
9. **B9 — Zielmetrik:** Was schmerzt mehr — Tages-kWh (Ladeplanung) oder
   Stundenform morgens/abends (Load-Timing)? → Gewichtung der
   Phase-1-Gate-Schwelle.
10. **B10 — Baseline behalten:** rany2-Entry dauerhaft als Watchdog ok
    (≈23 Sensoren bleiben)?
11. **B11 — Historie:** Seit wann laufen die Hoymiles-Port-Sensoren?
    (LTS-Tiefe bestimmt, wie warm die Lerner starten können.)
12. **B12 — Phase 0 sofort?** Die 6-Array-Umkonfiguration ist risikolos
    (Optionsdialog seit 2026-07-05 wieder bedienbar) und verbessert die
    Tageswerte ohne Code — freigeben?

## Anhang A: Konventionen & Kommissionierungs-Checkliste

Drei Azimut-Konventionen im Spiel — **eine** interne (0=N), Konvertierung
nur an Grenzen, je mit Unit-Test:

| Kontext | Konvention | P1/P4 Front | P2/P5 links | P3/P6 rechts |
|---|---|---|---|---|
| Standort/Spec/intern | 0=N, 90=O | 115° | ~25° | ~205° |
| Open-Meteo GTI & rany2-Config | 0=S, −90=O | **−65** | **−155** | **+25** |
| PVGIS printhorizon | 0=S, −90=O | (Terrain: S≙0) | | |

Checkliste klarer Tag (Phase 0 und Phase 1, Pflicht): (1) Peak-Zeit je
Ebene: P2/P5 früh vormittags, P1/P4 ~10–11 Uhr Sonnenzeit, P3/P6 früher
Nachmittag — Reihenfolge muss stimmen; (2) Nachmittags-Cutoff sichtbar
(Gebäude); (3) modellierte vs. gemessene Port-Leistung an 2–3
Sonnenständen ±20 %; (4) kein Output nachts/Winterflaute plausibel.

## Anhang B: Quellen (Auswahl, recherchiert & live verifiziert 2026-07-05)

Open-Meteo Docs/Pricing/Terms (minutely_15 ICON-D2 nativ; GTI isotrop,
Albedo 0,20, 1 Ebene/Call; Free-Tier 10 k/Tag; Previous-Runs- &
Satellite-Radiation-API) · PVGIS v5.3 printhorizon/seriescalc (48
Azimute, SRTM ~90 m; live: S 18,3° vs. Wintersonne 18,0°) · rany2/
open-meteo-solar-forecast (Quellcode: Multi-Array je Entry, Horizont =
Beam-only, watts/wh_period-Attribute, Ross-Modell; Deps aiohttp/suncalc/
numpy/pytz) · Hay-Davies-Fassaden-Benchmarks (EPJ PV 2024; Mayer & Grof,
Appl. Energy 2021: Separation+Transposition = kritischste Kettenglieder)
· SunPower Shade-Loss (arXiv 2209.09456) · Reno-Hansen Clear-Sky ·
EMHASS adjust_pv_forecast (Residual-Regression-Muster) · Hoymiles
HMS-2T-Datenblatt (1 Eingang/MPPT) · HA-Dev-Docs (Store/async_delay_save,
recorder statistics_during_period, exclude_attributes,
async_get_solar_forecast, Service-with-Response) · DWD CDC Phänologie
(Laub-Termine, optional).
