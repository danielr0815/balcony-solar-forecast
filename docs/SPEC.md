# Spezifikation: Balcony Solar Forecast — Mehrebenen-PV-Prognose mit Selbstlernen

> Status: **v0.16.0 (2026-07-11)** — alle Phasen bis v0.4 (Scoreboard/
> Quantile/Dashboard, §14) plus v0.5 (Verschattungsprofil, §15) umgesetzt;
> v0.1.0 seit 2026-07-06 live im Parallellauf. Historie in CHANGELOG.md.
> Gründungsdokument des Projekts `balcony_solar_forecast` (eigenständige
> HA-Custom-Integration, danielr0815/balcony-solar-forecast). Synthese aus
> drei unabhängigen Designentwürfen (Compose / Physik-Motor / ML-first) +
> drei Jury-Reviews (Genauigkeit / Engineering / Robustheit) auf Basis von
> 76 recherchierten, quellenbelegten Einzelbefunden. **Einstimmiges
> Jury-Urteil (3/3): dedizierter Physik+Lern-Motor.** Auf Betreiberwunsch
> als **eigenes Projekt**, nicht als Modul in battery_manager — bestehende
> Konsumenten koppeln nur über Standard-HA-Schnittstellen.
> Zielversionen v0.1.0 … v0.4.0 (je Phase einzeln deploybar, mit
> Abbruch-Gates); Erweiterungen ab v0.5 als Addendum-Sektionen (§15 ff.).
> Umsetzung: Opus 4.8 Ultracode; Prüfungen: Fable 5.

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

Summe **3260 Wp** an **4× Hoymiles HMS-800W-2T** (bestätigt B1:
**AC-Limit 800 VA je WR**; je 2 Module, **1 MPPT pro Port** → Module
elektrisch unabhängig). Port→Modul-Zuordnung (aus dem
Energie-Dashboard des Betreibers, B2 — systematisch: Port 1 = ungerades
Modul, Entity-Suffixe `_2…_4` = WR 2–4):

| WR | Port 1 | Port 2 |
|---|---|---|
| WR1 | M1 (`sensor.inverter_port_1_dc_*`) | M2 (`sensor.inverter_port_2_dc_*`) |
| WR2 | M3 (`…_dc_*_2`) | M4 (`…_dc_*_2`) |
| WR3 | M5 (`…_dc_*_3`) | M6 (`…_dc_*_3`) |
| WR4 | M7 (`…_dc_*_4`) | M8 (`…_dc_*_4`) |

`state_class` vorhanden → **Langzeitstatistik läuft seit 2024-07** (B11:
>365 Tage; real ~24 Monate), wird nie gelöscht = warme Trainingsdaten ab
Tag 1. AC-Clipping: nur wenn beide Ports zusammen 800 VA reißen — bei
diesen Neigungen praktisch nie; trotzdem als 1-Zeilen-Clamp modelliert.
Seiten-Azimute exakt 90° zur Front (B3: 25°/205° exakt); Neigungs-
Konvention bestätigt (B4); Balkon-über-Balkon-Verschattung vernachlässigbar
(B5, Betreiber-Entscheid).

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
- **Marken-Icon lokal:** die PNGs unter `custom_components/<domain>/brand/`
  (`icon.png`/`icon@2x.png`/`logo.png`/`logo@2x.png`) werden vom **lokalen
  Brands-Proxy** von HA ≥ 2026.3 ausgeliefert — **bewusst keine** Einreichung
  ins `home-assistant/brands`-Repo (Betreiber-Entscheid F-L4), sodass die
  Custom-Integration ihr Icon ohne Upstream-PR mitbringt.

Pipeline (reine Funktionen über 15-min-Slots × N Ebenen, <50 ms/Lauf):

1. **fetcher.py** — 1 Call/30 min: `minutely_15=shortwave_radiation,
   direct_normal_irradiance,diffuse_radiation,temperature_2m` +
   `hourly=cloud_cover_low/mid/high,visibility,snowfall,snow_depth`,
   `models=icon_seamless`, `forecast_days=4` (ICON-D2 nativ 15-min für
   Mitteleuropa, live verifiziert) — heute/morgen/d2 **plus ein Puffertag**,
   weil der Fetch mit `timezone=UTC` läuft und `forecast_days` UTC-Tage ab dem
   aktuellen UTC-Datum zählt: am lokalen Abend (UTC+2) läge der lokale d2 sonst
   teils jenseits eines 3-UTC-Tage-Fensters (live beobachtet: d2 = 0,0 kWh um
   01:00 lokal), der Extratag deckt den lokalen 3-Tage-Horizont immer ab.
   Payload-**Schema**-Validierung (nicht
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
   **Sonnendistanz-Exzentrizität (v0.5.x, audit #30):** Der Anisotropie-Index
   `Ai = DNI / E0n` teilt durch die extraterrestrische Normalstrahlung
   `E0n = 1361·(1 + 0,033·cos(2π·doy/365))` (Spencer/Duffie-Beckman) statt der
   festen Solarkonstante — der Erd-Sonne-Abstand schwankt ±3,3 % übers Jahr
   (Perihel Anfang Januar), sonst wird die saisonale Zirkumsolar-Gewichtung
   verzerrt (bis ~1,9 % vs. pvlib). Die Engine reicht den Slot-`doy` durch
   (Live/Backfill-Parität); `doy=None` bleibt die alte feste Konstante
   (rückwärtskompatibel für reine Aufrufer).
   **Einfallswinkel-Modifikator (v0.5.x):** ASHRAE-IAM
   `f = 1 − b₀·(1/cos θ − 1)`, b₀ = 0,05 (`IAM_B0`), auf Beam+Zirkumsolar —
   angewandt in der **Engine** (pvlib-analog nach der reinen Transposition,
   damit die pvlib-Golden-Vektoren vergleichbar bleiben) und **vor** der
   ungegateten Trainer-Referenz, sonst absorbiert die Shademap den
   Glasreflexionsverlust (5–15 % bei AOI > 60°; auf den 70–80°-Fassaden
   großer Tagesanteil) als AOI-förmige Phantom-Verschattung.
5. **horizon.py** — je Ebene Tabelle `(Azimut, Elevation, Transmittanz)`
   in 10°-Schritten, linear interpoliert: Fernfeld aus PVGIS + Betreiber-
   Profil; Nahfeld je Ebene differenziert (Gebäudekante hart bei
   az ≈ 212° für S-Ebenen, Baumsektor az ~135–175° auf P3/P6 mit
   **saisonaler Transmittanz** ≈ 0,8 kahl / ≈ 0,45 belaubt, Kosinus-Rampe
   April/November — **alle Startwerte messdatenbasiert, §13**). Unter Horizontlinie:
   Beam+zirkumsolar × Transmittanz; Iso-Diffus × ebenen-eigenem SVF (behebt
   E4). **Halbtransparenter Horizont fürs Diffus (v0.5.x, audit #11):** der
   Himmel UNTER der Horizontlinie geht mit der (saisonal per `doy` aufgelösten)
   Transmittanz τ statt als Wand in den SVF ein — eine Baumreihe (τ 0,45/0,8)
   verdunkelt das Diffus nicht mehr wie eine Hauswand; τ=1 ⇒ SVF unverändert,
   τ=0 ⇒ alte opake Reduktion. Der SVF ist damit `doy`-abhängig (Foliage-Rampe);
   die Engine memoisiert ihn je (Ebene, `doy`). Tabellen liegen **versioniert im
   Repo/Config-Export**, nicht nur in `.storage`.
6. **shademap.py** — langsamer Lerner (§5).
7. **electrical.py** — Ross-Zelltemperatur `Tcell = Tamb + k·POA`, −0,34 %/K,
   η konfigurierbar (Default 0,96), AC-Clamp je konfigurierter WR-Gruppe. Der
   Ross-Koeffizient `k` ist **je Ebene überschreibbar** (`ross_coeff`, audit
   #29): die Montage bestimmt ihn (~0,02 freistehend/gut hinterlüftet …
   ~0,056 fassadenparallel/schlecht hinterlüftet, Ross/Skoplaki-Literatur;
   Default `ROSS_COEFF` = 0,0342). Site-Validierung: endlicher Wert in
   [0,005, 0,12], sonst `bad_ross_coeff`.
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
Warm-up der EMA mit adaptivem α = max(α, 1/(n+1)): junge Bins sind so das
arithmetische Mittel ihrer Samples statt vom Seed dominiert.
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

**Verschattungsgruppen (`shade_group` je Ebene, optional):** Die
Verdeckungsgeometrie (Gebäudekante bei 210°, Baumreihe) ist eine Eigenschaft
des **Standorts**, nicht eines Moduls — alle Ebenen desselben Balkons sehen
dieselbe Himmelsokklusion; nur der **Impact** unterscheidet sich je
Ausrichtung, und den behandelt der Motor bereits pro Ebene über den
Beam-Anteil. Die Schattenkarte je **Messkanal** zu lernen verschwendet
Samples 8-fach und lässt das Nordmodul ignorant über das, was das Südmodul
bewiesen hat. Ebenen mit gleicher `shade_group` gehören daher demselben
**Verschattungs-Pool** an (`PlaneConfig.shade_channel = shade_group or name` —
die einzige Definition der Zuordnung; Default: kanalweise,
rückwärtskompatibel). Die **Messung und alle Gates bleiben pro Ebene**
(beam-referenziertes T, Quasi-klar-Gates). **Die Speicherung ist immer je
Modul-Kanal** (Ebenenname) — jede Ebene lernt ihre Schattenkarte einzeln und
für immer. Das **Pooling geschieht ausschließlich beim Lesen** (im
`beam_tau`-Hook des Motors und im Schattenprofil-Diagramm): der gelernte τ eines
Bins ist der **n-gewichtete Mittel** über alle Pool-Kanäle
(`tau_pool = Σ nᵢ·τᵢ / Σ nᵢ`, `n_pool = Σ nᵢ`), auf den dann dasselbe
gemeinsame Shrinkage gegen den statischen Prior wirkt (`w = n_pool/(n_pool+K)`).
So kommt ein Sample eines Moduls allen Pool-Mitgliedern zugute — **ohne** die
Einzel-Historien zu verschmelzen. Damit ist **Gruppieren und Auflösen jederzeit
verlustfrei reversibel**: eine aufgelöste Gruppe liest sofort wieder nur den
eigenen Kanal jeder Ebene, ohne dass Daten verloren gehen. Weil verschiedene
Balkonpositionen unterschiedlich verschattet sein können, ist die Gruppe
**konfigurierbar** statt einer globalen Karte. Validierung: eine `shade_group`
darf nicht dem **Namen** einer Ebene entsprechen, die nicht selbst diese Gruppe
trägt (Alias-Schutz — sonst kollidiert der Eigen-Kanal eines Nichtmitglieds
mit dem Pool); eine nach einem eigenen Mitglied benannte Gruppe ist erlaubt.
**Alt-Gruppenkanäle** aus der früheren (v0.12.0) Merge-Migration werden als
**Legacy-Quelle mitgelesen** — ein vorhandener Gruppenkanal fließt zusätzlich in
den Pool seiner Mitglieder ein, sodass seine bereits gepoolte Evidenz
weiterzählt, bis sie von den kanalweisen Live-Daten verdünnt ist. **Caveat zur
verlustfreien Reversibilität:** die kanalweise Per-Ebenen-Historie (seit v0.13)
bleibt beim Auflösen einer Gruppe stets erhalten; nur ein **vor v0.13 gemischter
Gruppen-Blob** (nach der Gruppe benannt, nicht nach einer Ebene) wird beim
Auflösen verwaist und unlesbar — wiederherstellbar per `rollback_learners` oder
durch erneutes Gruppieren unter demselben Namen. Das
**Schattenprofil-Diagramm zeigt beide Sichten** (Gruppen- und Einzelsicht per
Umschalter), damit der Betreiber die individuelle Karte jedes Moduls gegen die
gepoolte vergleichen und über Gruppierungen entscheiden kann.

**Gruppenvorschlag (`suggest_shade_groups`-Service):** Weil jede Ebene ihren
Kanal einzeln lernt, lässt sich die Gruppierung datengetrieben belegen statt am
Diagramm abzuschätzen. Der Service vergleicht je Ebenenpaar die beiden Kanäle
**bin-weise über die gemeinsam besuchten Bins**: die **Ähnlichkeit** ist die
n-gewichtete mittlere τ-Differenz (`mean_abs_diff = Σ w·|τ_a − τ_b| / Σ w` mit
`w = min(n_a, n_b)`). Ein Paar gilt als *ähnlich*, wenn es mindestens
`min_common_bins` gemeinsame Bins hat **und** `mean_abs_diff ≤ max_diff`,
sonst als *verschieden* bzw. bei zu wenig gemeinsamer Evidenz als *unzureichend*.
Aus den ähnlichen Paaren wird per **Complete-Linkage-Agglomeration** (aufsteigend
nach `mean_abs_diff`; zwei Cluster verschmelzen nur, wenn **jedes** Kreuzpaar
ähnlich ist — kein Verketten A~B~C bei zu großem A↔C) ein Vorschlag gebildet;
Ebenen ohne Evidenz bleiben als `insufficient_data` Einzelgänger. Beide
**Schwellen sind pro Service-Feld konfigurierbar** (Defaults
`SHADE_SIM_MAX_MEAN_DIFF` / `SHADE_SIM_MIN_COMMON_BINS`); die Antwort enthält
Matrix, Vorschlag und die **aktuelle Gruppierung** zum direkten Abgleich.

**Schneller Lerner — Wetterfehler intraday:** exponentiell abklingendes
Verhältnis (τ ≈ 90 min) gemessen/prognostiziert der letzten 2–4 h,
**im k_c-Raum konditioniert** (Geometrie/Saison herausnormiert), auf die
nächsten ~6 h abklingend angewandt, Clamp [0,25 … 2,5], nach HA-Neustart
Re-Init auf 1,0 (nie alten Zustand laden). Rettet Nebelmorgen ohne
falsche Geometrie. Day-ahead-Bias (seit v0.2.0 implementiert, per Default
aktiv): 1 RLS-Bias-Skalar je (Wolkenklasse × Tagesabschnitt), nächtlich
trainiert und über den Options-Flow abschaltbar.
Alle Lerner-Korrekturen (Intraday-Skalar, Day-ahead-Bias) und die
Quantilbänder (P10/P50/P90) werden als **letzte Stufe erneut auf das
WR-AC-Limit geclampt** (`clamp_groups` läuft nach dem Slot-Faktor ein
zweites Mal): eine Hochkorrektur (Faktor > 1) kann die ausgelieferte Kurve
nie über die konfigurierte Wechselrichtergrenze heben. Ebenen ohne WR-Gruppe
haben keine konfigurierte Obergrenze und passieren beide Clamps unverändert.

**Schutzmechanismen (Jury-Auflagen, verbindlich):**
- Label-Gates im Trainer: eingefrorene Sensoren (unverändert + altes
  `last_updated` = fehlend), Energie-Monotonie, Messkanal-Dropout ⇒
  ganzen Tag verwerfen; nächtlicher Job **idempotent** (datums-gekeyt,
  doppelt laufbar).
- **Drift-Monitor**: rollierende 7-Tage-MAE korrigiert vs. reine Physik;
  verliert der Lerner 7 Tage in Folge → Auto-Abschaltung + HA-Repair-
  Issue; letzte 10 Lernstände für Rollback (`LEARNER_SNAPSHOT_RING`, bewusst
  größer als der 7-Tage-Verluststreak, damit ein Rollback stets auf einen
  Stand VOR dem Streak zugreift; `DRIFT_ROLLBACK_SNAPSHOTS = 3` ist ein
  Legacy-Alias); Store validate-and-clamp beim
  Laden (korrupt ⇒ Faktoren 1,0, nie Setup-Crash). Der nächtliche
  Snapshot hält zusätzlich die reine Schattenkarten-Kurve fest
  (Slow ∘ Physik, ohne Day-ahead-Faktor); ein Verlusttag wird der
  schuldigen Schicht zugeordnet — Slow: Schattenkarte vs. Physik,
  Day-ahead/Fast: korrigiert vs. Schattenkarten-Kurve — mit
  unabhängigen Streaks, sodass eine unschuldige Schicht nicht
  mitabgeschaltet wird. Alt-Snapshots ohne Schattenkarten-Kurve fallen
  auf das gemeinsame korrigiert-vs-Physik-Signal zurück.
- **Kollaps-Detektor**: alle Kanäle ≈ 0 bei hoher Prognose (Schnee auf
  Modulen, Total-Dropout) ⇒ beide Lerner für den Tag einfrieren, nur der
  geclampte Intraday-Skalar reagiert.
- Kill-Switches je Lernschicht im Options-Flow.

## 6. Unsicherheit (Phase 4, optional)

Nichtparametrische historische Simulation: empirische P10/P50/P90 aus dem
90-Tage-Fehlerringpuffer, konditioniert auf (Wolken-/**Nebelklasse** ×
Tagesabschnitt); Nebelklasse = Sicht < 1000 m ∨ (cloud_cover_low > 85 %
∧ Okt–Feb), nach erster Saison auf gemessene Abdeckung geprüft. Der Ring ist
**datumsfensterbasiert** (jedes Sample trägt das ISO-Datum seines Trainingstags,
Fenster = QUANTILE_RING_DAYS relativ zum Trainingstag) und ein Band verlangt
zusätzlich Evidenz aus mindestens QUANTILE_MIN_DAYS **verschiedenen Tagen** —
korrelierte Stundensamples eines Tages sind keine unabhängigen Beobachtungen;
alt-ungestempelte Samples zählen über die Per-Tag-Cap-Untergrenze mit. Die
Umsetzung ist eine **reine empirische historische Simulation** (empirische
Multiplikatoren aus dem Ring, Cold-Start-Kollaps auf P50 statt Fake-Spreizung) —
keine adaptive konforme Nachführung; die maßgebliche Beschreibung des
ausgelieferten Verfahrens steht in §14.2. Nutzung durch Konsumenten:
P50 = Planung; P10 für konservative Reserven; P90 fürs Load-Timing
(Überschusslasten so spät wie möglich, ohne Export).
Der **Previous-Runs-API-Backfill** (geliefert in **Phase 2**, §9):
Forecasts as-issued ab 01/2024 gegen LTS-Ist-Werte — einmaliger
Offline-Job auf dem Dev-Rechner — füllt Bias-/Quantilspeicher vor dem
ersten Live-Winter. Verbindlichkeit: **Pflicht zu versuchen, kein
Blocker** — das System muss ohne diese API voll funktionieren.

### 6.1 Ensemble-Wetter-Unsicherheitsbänder (v0.16, optional, Standard AUS)

Die gelernten P10/P50/P90 (§14.2) kommen aus dem Residuenring je (Wolkenklasse ×
Tagesabschnitt): pro Wetterklasse **im Mittel** gut kalibriert, aber **blind für
die spezifische Unsicherheit von HEUTE**. Die Open-Meteo-**Ensemble-API**
(`ensemble-api.open-meteo.com/v1/ensemble`, Modell `icon_seamless`, 40 Mitglieder
= Kontrollmember unter dem nackten `shortwave_radiation`-Schlüssel + 39 gestörte
`…_memberNN`, 72 Stundenstempel bei `forecast_days=3`) liefert gestörte Läufe,
deren Streuung **die heutige Wetterunsicherheit IST**.

**Formel (v1, bewusst approximiert).** Pro Stunde bildet der Parser aus jedem
Member-GHI und dem deterministischen GHI (dem Stundenmittel der aktuellen
`WeatherSeries`, gleich verschlüsselt — Stundenstempel markieren das Intervallende,
also −1 h auf den Intervallstart) den **relativen** Faktor
`f_m = clamp(GHI_member / GHI_det, 0…3)`; `(f10, f90)` sind die Typ-7-Perzentile
0,1/0,9 dieser Faktoren. Das ist eine **per-Slot-RELATIVspreizung, KEIN voller
Engine-Durchlauf je Member** — die Beam/Diffus-Rekomposition je Member ist
zweitrangig und wird bewusst weggelassen (die Ensemble-Streuung liefert die FORM
der Unsicherheit, nicht eine absolute Kurve). Ehrlich benannte Näherung: der
GHI-Faktor wird auf das DC-Leistungsband angewandt, als skaliere Leistung linear
mit GHI — nur in erster Ordnung wahr (Temperatur, IAM, Beam/Diffus-Split biegen es).

**Fusion per ENVELOPE-MAX (nie multipliziert).** Pro Slot gewinnt das breitere
Band: `p10 = min(gelernt.p10, f10)`, `p90 = max(gelernt.p90, f90)`, `p50` bleibt
der gelernte Median. **Warum nicht multiplizieren:** der gelernte Residuenring
enthält den Wetterfehler der Klasse bereits — ein Produkt würde den Wetteranteil
**doppelt zählen**; die Hüllkurve addiert nur die zusätzliche Spreizung, die das
Ensemble HEUTE über die Klimatologie hinaus sieht. **Cold-Start-Gewinn:** ist das
gelernte Band noch die neutrale Identität (alle 1,0), liefert das Ensemble die
ganze Spreizung um p50 = 1,0 — echte Wetterstreuung, bevor der Ring Evidenz hat.
(Die Dispersions-Kalibrierung — die Ensemble-Spreizung vor der Fusion mit einem
gelernten Reliabilitätsfaktor zu skalieren — bleibt ein dokumentierter Zukunftspfad.)

**Nie tragend.** P50/Headline/Scoreboard/Kill-Gate bleiben **unberührt**; jeder
Ausfall/jede Abwesenheit degradiert **nahtlos** auf die gelernten Bänder. Das
Ensemble wird **nur im Speicher** gecacht (nicht persistiert, kein Store-Schema-
Bump), auf eigener ~3-h-Kadenz gefetcht (Ensembles aktualisieren ~6-stündlich),
und ist ein **Opt-in-Schalter, Standard AUS**. Eine Stunde mit < 10 nutzbaren
Membern oder deterministischem GHI < 20 W/m² fällt auf das gelernte Band zurück.
Ein `band_source`-Attribut auf den P10/P90-Sensoren fasst die heutigen Slots
zusammen: `learned` (nur Ring), `envelope` (Ensemble hat irgendwo geweitet) oder
`ensemble` (gelernt überall kollabiert, Ensemble lieferte die ganze Spreizung).

## 7. Degradationsleiter (nie still!)

frische Prognose → Last-Good-Cache (Store, konfigurierbare Altersgrenze)
→ Reine-Physik-Kurve aus letztem gültigen Wetterbild → `unavailable`
(Konsumenten entscheiden selbst über ihre Fallbacks — battery_manager
hat seinen eigenen Staleness-Pfad). Jede Stufe sichtbar (binary_sensor
„degraded" bzw. Repair-Issue). Die Sensoren gehen ehrlich auf
`unavailable`, statt stille Altwerte zu halten (Lehre aus dem
Fossibot-Verhalten). Das **Ensemble-Wetter** (§6.1) ist **keine Stufe dieser
Leiter**: sein Fehlen weitet nur die Bänder nicht und ist nie ein
Degradationsgrund — die Kurve läuft unverändert auf den gelernten Bändern weiter.

## 8. Schnittstellen für Konsumenten (Standard-HA, keine Kopplung)

- **Sensoren:** `energy_production_today / _tomorrow / _d2` (kWh) —
  bewusst kompatibel zum Muster der bestehenden Integration, sodass
  battery_manager **ohne Code-Änderung** nur seine drei Entity-Picker
  umstellt. Dazu `power_production_now` (W) und Diagnose (Baseline-MAE,
  Degradationsstatus, Lernstatus). Die Heute-Headline ist eine **stabile
  Day-ahead-Erwartung**: der transiente Intraday-Skalar wird aus den Slots des
  aktuellen Tages wieder herausgerechnet (die servierte `watts`/`wh_period`-Kurve
  behält ihn). **Clamp-Interaktion:** auf einem Slot, dessen hochkorrigierte
  Gruppenleistung die WR-AC-Obergrenze trifft (der Re-Clamp greift, servierter
  Wert = Deckel), wird der Skalar NICHT herausdividiert, sondern der Deckelwert
  unverändert übernommen — sonst würde eine nie angewandte Korrektur wieder
  abgezogen und die Headline untertrieben (bis Faktor 2,5).
- **Ist-Messung:** `measured_dc_power_total` (W, `MEASUREMENT` → Langzeit-
  statistik) — die **ereignisgesteuerte Summe** der `actual_entity`-Sensoren
  aller Ebenen (abonniert die Quellsensoren direkt, rechnet bei jeder Änderung
  neu, **unabhängig vom Prognose-Zyklus**) und bleibt verfügbar, solange
  mindestens eine Quelle meldet (Grundwahrheit muss auch bei degradierter
  Prognose weiterlaufen). Wird **nur erzeugt, wenn** mindestens eine Ebene eine
  `actual_entity` konfiguriert hat.
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
| **0** | — (nur Konfig) | **✅ AUSGEFÜHRT 2026-07-05** (Variante „Einzelplatten" per B12): **8 separate rany2-Entries** „PV Modul 1…8" (je 1 Modul; Azimut in der HA-UI in **0=N**: 25/115/205 — der Koordinator rechnet intern −180, siehe Anhang A!; Neigung 70/80; Wp 370/430; η 0,96; inverter_power = Wp; ohne Horizont — Dateizugriff auf HAOS nicht verfügbar, Horizont kommt im Motor) + **4 Summen-Template-Sensoren** `sensor.pv_prognose_{heute,morgen,uebermorgen,leistung_jetzt}_alle_module`. Erste Werte plausibel (heute 6,79 kWh vs. 3,50 alt). Alt-Entry „Home-LA" (1600 Wp) läuft unverändert weiter und speist vorerst battery_manager. Das 8-Entry-Ensemble = **Baseline** | Plausibilität an 1 klaren Tag (Anhang-A-Checkliste), dann Konsumenten umhängen |
| **1** | v0.1.0 | Projekt-Gerüst (Config Flow: Standort, N Ebenen, Horizonttabellen-Import, WR-Gruppen, Mess-Entitäten; HACS-Struktur) + Motor `core/` (Schritte 1–5, 7 — reine Physik, ohne Lernen) + Sensoren/Service/Energy-Hook + **Forecast-as-issued-Logger + Ist-Logger ab Tag 1** + Golden-Tests gegen offline erzeugte **pvlib-Referenzvektoren** (alle 6 Ebenen, Tiefstand 2–10°, Konventionsgrenzen) als Merge-Blocker; 2 Wochen Parallellauf | **Kill-Gate** (B9-gewichtet): 14-Tage-Parallellauf, **Tages-kWh-MAE ≥ 10 % unter dem 8-Entry-Baseline-Ensemble** (Primärmetrik); Taglicht-Stunden-MAE als Zweitmetrik berichtet — sonst Stopp, Baseline behalten |
| **2** | v0.2.0 | **✅ IMPLEMENTIERT 2026-07-06** (mit Phase 3 zusammen, D-P10): Intraday-Lerner (k_c-Raum, τ≈90 min, Clamp [0,25…2,5], nie persistiert) + Day-ahead-RLS je (Wolkenklasse × Tagesabschnitt) + Drift-Monitor (Auto-Abschaltung + Repair-Issue + **Auto-Restore aus dem Rollback-Ring**) + Kollaps-Detektor + `scripts/backfill.py` (Previous-Runs + LTS via HA-WS) + Services `import_bootstrap`/`dump_shademap`/`rollback_learners` | Gates werden im Parallellauf **nachträglich** ausgewertet: 14 Tage, nächste-6-h-MAE ≥ 5 % unter reiner Physik, stratifiziert (klar/bewölkt/Nebel) |
| **3** | v0.3.0 | **✅ IMPLEMENTIERT 2026-07-06** (D-P10): Shademap-Lerner — beam-referenzierte Transmittanz je (Kanal × Sonnenaz. 5° × El. 2,5° × Halbjahr), Clear-Sky-Gate elevationsabhängig, Shrinkage w=n/(n+20) mit statischem Horizont-Prior, Clamp [0…1,1], trainiert gegen **ungegatete** Beam-Referenz (sonst Selbstreferenz → √T-Fixpunkt) | dito nachträglich: 14 klare Tage, Klartag-Stunden-MAE ≥ 10 % unter reiner Physik; Polarkarte (`dump_shademap`) ≙ bekannten Hindernissen |
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
- **D-P10** (Betreiber, 2026-07-06): v0.2 + v0.3 **gemeinsam vorgezogen**
  gebaut statt sequenziell nach Gate-Auswertung. Die Gate-Logik bleibt
  erhalten, weil die **Attribution** konstruktiv gesichert ist: der
  nächtliche Issued-Snapshot speichert **beide** Stundenkurven (rohe
  Physik UND korrigiert) — der Parallellauf kann Physik- und Lernbeitrag
  getrennt bewerten. Absicherung: Shrinkage-Cold-Start (Shademap wirkt
  anfangs ≈ 0), Drift-Monitor mit Auto-Abschaltung + **Auto-Restore des
  Pre-Streak-Zustands aus dem Rollback-Ring**, Kill-Switch je Schicht,
  Service `rollback_learners`, **Tages-Idempotenzmarker** im Store (ein
  Neustart-Catch-up darf denselben Tag nicht doppelt trainieren —
  Verify-Befund 2026-07-06). Prozess: Fable plant/reviewt/verifiziert,
  Opus implementiert; Kritisch-Fixes nach Fable-Spezifikation.
- **D-P11** (Betreiber, 2026-07-06): v0.4 = **Skill-Scoreboard +
  P10/P50/P90-Quantile + Observability-Dashboard** bauen; den
  **battery_manager-Cutover DEFERRED**, bis das Scoreboard das Kill-Gate
  bestätigt. Das Scoreboard ist das Gate, an dem der ganze Plan hängt (§9/§10):
  es misst nächtlich pro Vortag den Tages-kWh-Fehler des Motors **as issued**
  (aus dem Issued-Ring, nie mit heutigem Lernstand nachgerechnet) gegen jede
  konfigurierte externe Vergleichsprognose **wie sie am Vortag stand**
  (Recorder-Historie, nie der heutige Wert) gegen die gemessene Ist-Summe,
  stratifiziert nach Wetterklasse. Vergleichs-Sensoren sind **generisch +
  konfigurierbar** (leer ausgeliefert); die zwei Vergleiche des Betreibers sind
  in `docs/DASHBOARD.md` dokumentiert, nicht im Runtime-Default hardcodiert
  (D-P9). Quantile: nichtparametrische historische Simulation aus dem
  90-Tage-Fehlerring (§6), Band kollabiert auf P50 bei zu wenig Samples (keine
  Fake-Spreizung). Store-Schema v2→v3 **additiv**, Lernzustand des Live-Installs
  bleibt byte-treu erhalten (§14). battery_manager wird **nicht** angefasst.

## 12. Betreiber-Antworten (2026-07-05 — alle 12 beantwortet)

- **B1 WR:** HMS-**800**W-2T, AC-Limit **800 VA je WR**.
- **B2 Zuordnung:** aus dem Energie-Dashboard ausgelesen → Tabelle §2.
- **B3 Seiten-Azimute:** exakt 90° zur Front → 25°/205° exakt.
- **B4 Neigung:** bestätigt (gegen Horizontale, 90° = senkrecht).
- **B5 Balkon-über-Balkon:** nur ganz leicht/selten → **ignorieren**.
- **B6 Gebäudekante:** aus Messdaten analysiert → §13 (Beam-Kollaps der
  S-Module bei Sonnenazimut ~205–218°).
- **B7 Bäume:** Laubbäume; aus Messdaten analysiert → §13
  (Symmetrietest: M4 −15–17 % Sep vs. März, M8 −4 %; Sektor ~135–175°).
- **B8 Schnee:** bleibt gelegentlich haften → Kollaps-Detektor (§5)
  bestätigt prioritär.
- **B9 Zielmetrik:** **Tages-kWh-Prognose** → Phase-1-Gate wird auf
  Tages-kWh-MAE gewichtet (Stunden-MAE als Zweitmetrik berichtet).
- **B10 Baseline:** ja, dauerhaft behalten.
- **B11 Historie:** >365 Tage (real: LTS seit 2024-07, ~24 Monate).
- **B12 Phase 0:** ja, als **Einzelplatten** (8 Entries) + zusätzliche
  **Summen-Sensoren** über alle Module → ausgeführt, siehe §9 Phase 0.

## 13. Messdaten-Befunde (24 Monate LTS, analysiert 2026-07-05)

Methode: stündliche Langzeitstatistik aller 8 Port-Sensoren (137 632
Zeilen, 2024-07 … 2026-07) → **P90 je (Monat × Stunde)** ≈ Klartag-Profil
(Mediane sind wetterverschmiert); Sonnenstände per NOAA-Formel
(Selbsttest gegen PVGIS: Juni-Mittag 64,9°, Dez. 18,0° — exakt).

1. **Hang/Ost-Horizont:** M1 (N, unten) springt im Juni von 63 W (6 h,
   Sonne az 67°, el 10,8°) auf 210 W (7 h, el 20,2°) → effektiver
   Horizont **~12–15° im Sektor 60–100°** (etwas über PVGIS-Terrain 8,8°
   → Nahfeld-Zuschlag). Dezember: P90-Peak der Front-Module nur ~59 W →
   bestätigt „Terrain 18,3° > Wintersonne 18,0°" (praktisch kein
   Direktstrahl im Hochwinter).
2. **Gebäudekante:** Die S-Module kollabieren im Juni zwischen
   Sonnenazimut **~205° und ~218°** (M4: 269 W @13 h → 85 W @14 h; M8:
   194 → 109 W), obwohl ihre Ebene Beam bis ~295° sähe → **Hauswand-
   Kante bei az ≈ 210–218°**, unterer Balkon etwas früher als oberer.
   Front-Module: natürliches Beam-Ende az ~205° (= Geometrie-Limit
   115°+90°) — Gebäude für sie nicht zusätzlich sichtbar. N-Module:
   Beam-Ende az ~115° (Geometrie-Limit) ✓.
3. **Bäume (Sonnenbahn-Symmetrietest** — gleiche Sonnengeometrie,
   anderer Laubzustand): Tagesenergie Sep/März front-normalisiert:
   **M4 (S unten) 0,85** (≈ −15–17 %), **M8 (S oben) 0,99** (≈ −4 %);
   stärkste Stunden 10–12 h (Sonne az ~140–170°, el ~30–45°), M4-
   Transmittanz dort belaubt ≈ 0,3–0,6. → Baumsektor **az ~135–175°**,
   Baumkronen-Elevation von unten ~35–45°, von oben ~25–35°.
4. **Initiale Horizonttabellen je Ebene** (Startwerte für §4 Schritt 5;
   Transmittanz τ, saisonal wo markiert):
   - Alle Ebenen, Fernfeld: az 60–100° el 13° τ0 · az 100–150° el 16° τ0
     (Hang, PVGIS+Messung) · sonst PVGIS-Profil.
   - P3/P6 (S): zusätzlich az 135–175° el 40°(unten)/30°(oben)
     **τ 0,45 belaubt / 0,8 kahl** (Bäume, lernfähig) · az >212° el 90°
     τ0 (Hauswand).
   - P1/P4 (Front): az >205° irrelevant (Geometrie-Limit); keine
     Zusatzeinträge nötig.
   - P2/P5 (N): az >115° irrelevant; Fernfeld Ost besonders wichtig.
   Seit v0.5.x (audit #11) wirken diese τ auch auf das **Diffus**: der Himmel
   unter der Horizontlinie geht τ-gewichtet (saisonal per `doy`) in den SVF ein.
   Eine belaubte Baumreihe (τ 0,45) verdunkelt das Diffus im Sommer stärker als
   kahl (τ 0,8), also ist der Sommer-SVF der S-Module kleiner als im Winter;
   die harte Hauswand (τ0) dunkelt Beam UND Diffus weiterhin voll ab.
5. **Verschattungsgruppen:** Weil Hang, Baumsektor und Hauswandkante
   Standort-Geometrie sind (Befunde 1–3, nicht modulspezifisch), können
   gleich verschattete Ebenen desselben Balkons über eine gemeinsame
   `shade_group` einem **Verschattungs-Pool** angehören (§5, Pooling beim
   Lesen) — ein Sample eines Moduls kommt so allen Gruppenmitgliedern zugute.

## Anhang A: Konventionen & Kommissionierungs-Checkliste

Drei Azimut-Konventionen im Spiel — **eine** interne (0=N), Konvertierung
nur an Grenzen, je mit Unit-Test:

| Kontext | Konvention | P1/P4 Front | P2/P5 links | P3/P6 rechts |
|---|---|---|---|---|
| Standort/Spec/intern | 0=N, 90=O | 115° | 25° | 205° |
| Open-Meteo API direkt (GTI-Param, eigener Motor) | 0=S, −90=O | **−65** | **−155** | **+25** |
| **rany2-HA-UI (Config Flow)** | **0=N direkt eingeben** — der Koordinator rechnet intern `−180` (Quellcode verifiziert 2026-07-05) | 115 | 25 | 205 |
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

## 14. v0.4 — Scoreboard + Quantile + Dashboard (D-P11)

Phase 4 (v0.4.0), Betreiber-Entscheid 2026-07-06 (D-P11): drei Deliverables;
der **battery_manager-Cutover ist DEFERRED**, bis das Scoreboard das Kill-Gate
bestätigt. battery_manager und seine Entity-Verweise werden nicht angefasst.
Laufzeit bleibt **stdlib-only** (aiohttp erlaubt), `requirements` bleibt leer.

### 14.1 Skill-Scoreboard (das Kill-Gate, §9/§10)

Nächtlich, pro Vortag, berechnet der Koordinator den **Tages-kWh-Fehler** von
(a) der Motor-Prognose **as issued** für den Vortag, (b) jeder konfigurierten
externen **Vergleichsprognose**, jeweils gegen die **gemessene** Ist-Summe des
Standorts, plus die **Stunden-MAE** des Motors — **stratifiziert** nach der
dominanten Wetterklasse des Vortags (clear/mixed/overcast/fog; der Koordinator
klassifiziert diese bereits, wird wiederverwendet). Rollierendes Fenster
(Default **14 Tage**, konfigurierbar).

**Fairness / kein Leakage (kritisch):**
- die **Motor**-Zahl kommt aus der **as issued**-Prognose des Vortags (im
  Issued-Ring gespeichert, am Vortag geloggt) — **nie** mit heutigem Lernstand
  nachgerechnet;
- die **Vergleichs**-Zahl ist der Wert **wie er am Vortag stand** (aus der
  Recorder-Historie des Vergleichs-Sensors für den Vortag gelesen) — **nie** der
  heutige Wert;
- die **Ist**-Zahl ist die Summe der 8 Modul-Ist-Werte aus dem Actuals-Ring.

**Sensorik:** die ausgelieferten Objekt-IDs sind **unpräfixiert** —
`daily_kwh_mae` und `vs_best_baseline_pct` (positiv = Motor besser als bester
Baseline), je Vergleich `comparison_daily_kwh_mae_<slug>`: der Geräte-Slug trägt
bereits `balcony_solar_forecast`, das Präfix-Weglassen vermeidet bewusst den
`…_forecast_*`-Stutter (die internen DATA-Keys der Scoreboard-Summary behalten
dagegen die `engine_*`-Form). Dazu `binary_sensor.kill_gate_passed` (Motor ≥
`DEFAULT_SCOREBOARD_GATE_MARGIN`, Default **0,10**, besser auf Tages-kWh über ein
**volles** Fenster; `None`, solange das Fenster nicht voll ist), plus eine
**Diagnose-Aufschlüsselung je Wetterstratum**.

**Vergleichs-Sensoren generisch + konfigurierbar** (`CONF_COMPARISON_SENSORS`:
Liste von `{name, daily_entity}`), **leer** ausgeliefert (D-P9). Die zwei
Vergleiche des Betreibers sind in `docs/DASHBOARD.md` + einem Config-Beispiel
dokumentiert, **nicht** im Runtime-Default hardcodiert: name „8-Entry Baseline"
→ `sensor.pv_prognose_heute_alle_module`; name „Alt 1600W" →
`sensor.energy_production_today_4` (der alte rany2-„Home-LA"-Heute-Sensor).

### 14.2 Quantile P10/P50/P90 (§6/§10)

Historische Simulation: ein **90-Tage-Ring** stündlicher **relativer** Fehler
(gemessen / korrigierte-Prognose), gekeyt nach (Wetterklasse × Tagesabschnitt) —
dieselbe Bin-Taxonomie wie der Day-ahead-Bias. Zur Prognosezeit werden die
empirischen P10/P50/P90-**Multiplikatoren** je Stunde auf die korrigierte Kurve
angewandt. **Cold Start:** zu wenige Samples in einem Bin → Band kollabiert auf
P50 (keine Fake-Spreizung). Ausgabe über die `get_forecast`-Service-Response
(plane-agnostische Gesamt-P10/P50/P90 in 15 min + stündlich), optionale
Tages-P10/P90-Sensoren, `wh_period`-P10/P90-Attribute. Enable-Flag Default
**AN**, Kill-Switch im Options-Flow. Nächtlich trainiert aus
issued(korrigiert) vs. Ist — die bestehenden Ringe werden wiederverwendet.

### 14.3 Observability-Dashboard

Ein Lovelace-View-YAML unter `dashboards/balcony_solar_forecast.yaml`, **nur mit
Bordmitteln** (funktioniert ohne Custom-Cards): History-Graph Motor-Gesamt vs.
gemessen (und je Ebene wo praktikabel), Entities-Card für Lernstatus/Drift-MAE/
Quellenstatus/Kill-Gate, ein Gauge für `engine_vs_best_baseline_pct`, ein
Markdown mit dem Kill-Gate-Verdikt. Die Shademap-Polarsicht als bestmögliche
Bordmittel-Darstellung (kompakte Transmittanz-Tabelle je Kanal via Template/
Markdown) **plus** die rohen Polardaten über den bestehenden
`dump_shademap`-Service (dokumentiert, dass daraus ein reicherer Polarplot
gerendert werden kann). Installationsschritte in `docs/DASHBOARD.md`. Das per
`install_dashboard` **generierte** Dashboard bettet an Stelle des
per-Modul-History-Graphs die gebündelte `custom:balcony-power-history-card`
(§15.4) ein; das kopierbare Referenz-YAML bleibt bewusst reine Bordmittel.

### 14.4 Store-Schema v3 (additiv über v2)

Inneres Schema **v2 → v3 ADDITIV**; die äußere HA-`Store`-Hülle
(`STORAGE_VERSION`) bleibt auf 1 gepinnt. **KRITISCH:** der Live-Install
(Entry `01KWT809F7MHH97F8XCKEJTZ0M`) hat **jetzt** einen befüllten v2-Store auf
Platte (Shademap 7 Kanäle / 851 Bins, Day-ahead 12 Zellen, Drift + Rollback +
`trained_days`). Eine Migration, die irgendeinen Lernzustand **verwirft oder
zurücksetzt, ist ein KRITISCHER Fehler**. Die Migration ist rein
inner-schema: jeder v2-Schlüssel wird **byte-treu** durchgereicht, die drei
neuen v3-Sektionen (`quantile_state`, `scoreboard_state`, `comparison_ring`)
werden leer default-injiziert.

## 15. v0.5 — Verschattungsprofil-Diagramm (Sonnenbahn vs. gelernte Verschattung)

**Zweck:** Für ein wählbares Modul und ein wählbares lokales Datum die
aktuell bekannte Verschattung sichtbar machen: die Sonnenbahn (Elevation über
Sonnen-Azimut) mit der **effektiven** Beam-Transmittanz τ, die die Prognose an
jeder Sonnenposition tatsächlich anwendet (statischer Konfig-Horizont, per
Shrinkage geblendet mit der gelernten Shademap), plus zwei Horizontlinien
(statisch und gelernt). Interaktives Gegenstück zur `dump_shademap`-Polartabelle.

### 15.1 Entitäten (Gerät „Balcony Solar Forecast")

| Entität | Rolle | Default |
|---|---|---|
| `select.…_shade_profile_module` | Modul-/Kanalwahl | **Front-Ebene** (die Ausrichtung, die die meisten Ebenen teilen — Referenzanlage: M2/115°); manuelle Wahl wird via RestoreEntity über Neustarts gehalten |
| `date.…_shade_profile_date` | Datumswahl (lokaler Kalendertag) | **immer heute** — bewusst NICHT restauriert; jede Neustart/Reload öffnet auf dem aktuellen Tag |
| `sensor.…_shade_profile` | State = verschatteter Tageslicht-Anteil in % (τ < `SHADE_PROFILE_TAU_THRESHOLD`); Kurven-Arrays als Attribute | — |

Attribute des Sensors (Recorder-ausgeschlossen wie die Energie-Kurven):
`time`/`azimuth`/`sun_elevation`/`transmittance` (ein Eintrag je
Tageslicht-Sample der Sonnenbahn) — `transmittance` ist die **gepoolte**
effektive τ; parallel dazu trägt `transmittance_individual` (v0.13) die τ des
**eigenen** Kanals des Moduls allein, sodass der Betreiber die Einzel- gegen die
Gruppensicht vergleichen kann (leere Liste bei ungruppierter Ebene, dann
== gepoolte Sicht — formstabil); zusätzlich `sample_n` (v0.15) — je Sample die
**gepoolte Shademap-Bin-Evidenz** (Sample-Zahl n) hinter der effektiven τ,
0 = nur statischer Prior, aus derselben Read-Pool-Menge wie die τ summiert
(`shademap.pooled_bin_n`, kann so nie von der gezeigten Transmittanz abweichen);
die Karte skaliert jeden Punkt danach (Confidence-Visualisierung) — und
`horizon_azimuth`/`static_horizon`/`shade_horizon` (Horizontlinien auf einem
Azimut-Raster über die Tageslicht-Spanne). Zusammenfassung:
`shaded_fraction`, `mean_transmittance`, `has_learned_data`/`learned_bins`
(NUR Bins des visualisierten **Halbjahrs** — Bins des anderen Halbjahrs können
die gezeigte Kurve nie beeinflussen), `sunrise`/`sunset`, `max_elevation`.

### 15.2 Semantik (Engine-exakt, nie „schöner als die Prognose")

Die Transmittanz je Sonnenposition repliziert die Engine-Gate-Logik
(`engine._plane_poa_components`) **exakt**: statischer Prior =
`horizon.transmittance_at` nur bei Sonne ≤ interpolierte Horizontlinie, sonst
1,0; darüber blendet `shademap.effective_tau`. **Slow-Active-Kopplung:** die
gelernte Shademap fließt NUR ein, wenn der Slow-Learner aktiv ist
(Kill-Switch an, nicht drift-deaktiviert, nicht collapse-eingefroren, Bins
vorhanden) — exakt das `slow_active`-Gate der Learner-Hooks. Ist er inaktiv,
zeigt das Diagramm die rein statische Verschattung, genau wie die servierte
Prognose. Das Ergebnis wird auf `(Modul, Datum, slow_active, Shademap-Objekt)`
memoisiert (der O(Azimut×Elevation)-Scan läuft je Änderung einmal, nicht je
15-min-Tick).

### 15.3 Berechnung & Tunables (const, `core/shadeprofile.py` — pur, HA-frei)

Sonnenbahn: lokaler Tag in `SHADE_PROFILE_STEP_MINUTES`-Schritten (5 min),
nur Samples mit Elevation > 0. Horizontlinien: Azimut-Raster
`SHADE_PROFILE_AZ_STEP_DEG` (1°) über die Tageslicht-Azimutspanne; die
gelernte Verschattungshorizont-Linie ist je Azimut die höchste Elevation mit
effektivem τ < `SHADE_PROFILE_TAU_THRESHOLD` (0,5), gescannt in
`SHADE_PROFILE_EL_SCAN_DEG`-Schritten (1°). Day-of-Year (Halbjahres-Split +
Laub-Rampe) stammt vom lokalen Kalenderdatum (dokumentierte Näherung; für
CET/CEST identisch mit der Engine).

### 15.4 Darstellung

Bordmittel-Karten (Modul-/Datumswahl + Anteil-Headline) im mitgelieferten
Dashboard; das eigentliche Diagramm ist das EINE bewusst optionale
HACS-Artefakt (`dashboards/shade_profile_apexcharts.yaml`,
`custom:apexcharts-card`): x = Sonnen-Azimut, y = Grad, Sonnenbahn nach τ
eingefärbt (Schwellen an `SHADE_PROFILE_TAU_THRESHOLD` ausgerichtet), beide
Horizontlinien überlagert. Details in `docs/DASHBOARD.md` §4b.

Seit v0.7 liefert die Integration **zwei eigene, abhängigkeitsfreie
Lovelace-Karten** aus (vanilla `HTMLElement` + programmatisches SVG, keine
HACS-Frontend-Installation nötig), geführt in der `_frontend._CARDS`-Liste:
`custom:balcony-shade-profile-card`
(`/balcony_solar_forecast/frontend/shade_profile_card.js`) und
`custom:balcony-power-history-card`
(`/balcony_solar_forecast/frontend/power_history_card.js`). **Beide Karten**
werden unter dem gemeinsamen Prefix `/balcony_solar_forecast/frontend/` als
statische Pfade ausgeliefert und — im Lovelace-Storage-Modus — beim Start
automatisch je als Dashboard-Ressource registriert (Modul-Typ), sodass sie
direkt im Kartenwähler erscheinen. Jede Ressourcen-URL ist per
`?v=<INTEGRATION_VERSION>` cache-gebustet (einziger Cache-Busting-Mechanismus);
im YAML-Lovelace-Modus wird statt der Registrierung ein INFO-Hinweis mit den
manuell einzutragenden Ressourcen-Zeilen geloggt. Die Registrierung ist ein
Zusatznutzen, nie ein Setup-Blocker: jeder Fehler wird geschluckt und
protokolliert.

Die **Power-History-Karte** (`custom:balcony-power-history-card`) zeigt im Stil
des Energie-Dashboards **gestapelte stündliche Balken der gemessenen Produktion
je Modul** (aus den Recorder-Stundenstatistiken der `actual_entity`-Sensoren),
überlagert von einer **gestrichelten Prognoselinie** (aus dem `wh_period`-Attribut
des Heute-Sensors); ein **Hover-Panel** zeigt je Stunde die Werte je Modul plus
Gesamt und Prognose. Die Modulkanäle werden über die
`sources`/`source_names`-Attribute von `measured_dc_power_total`
**auto-discovered** (keine YAML-Konfiguration der Kanäle nötig); die Karte
aktualisiert sich alle **5 min** — aber nur in der Live-Ansicht (Heute /
aktuelle Woche); eine Vergangenheits-Ansicht ist statisch und wird nicht
nachgeladen.

Seit v0.15 bietet die Karte (karten-lokale, nicht persistierte) **Tages-/
Wochennavigation**: eine Kopfzeile `◀ [Label] ▶` blättert den gewählten Tag
(Heute / Gestern / lokales Datum; ▶ deaktiviert am heutigen Tag), ein
**Tag|Woche**-Umschalter zeigt eine **Wochenansicht** mit sieben gestapelten
Tagesbalken der Tagesproduktion je Modul (aus `period: "day"`-Mittelwert-
statistiken, Mittel-W × 24 h = Tages-Wh; das Fenster endet am gewählten Tag und
springt in 7-Tages-Schritten). Für **vergangene Tage** zeigt die gestrichelte
Linie im Tagesmodus die Prognose **wie ausgegeben** aus dem 90-Tage-Ausgabe-Ring
— gelesen über die schreibgeschützte Aktion `get_issued_forecast` (SPEC §9).
Wichtig gegen Leakage: das ist der **eingefrorene ~01:30-Stand ohne Rückschau**
(nie aus dem heutigen gelernten Zustand nachgerechnet), sodass ein direkter
Vergleich „ausgegeben vs. gemessen" ehrlich bleibt; fehlt ein archivierter
Snapshot, entfällt die Linie mit dezentem Hinweis. Die **Wochenansicht zeichnet
bewusst keine Prognoselinie** (Mischung aus ausgegebener Vergangenheits- und
Live-Heute-Kurve wäre irreführend).

Seit v0.9 fixiert die Karte ihre x-Achse auf die **jahresstabile**
Tageslicht-Azimutspanne (Minimum/Maximum aus beiden Sonnenwenden, Python-seitig
als `axis_azimuth_min`/`axis_azimuth_max` berechnet und mit der Tages-Datenspanne
defensiv vereinigt), sodass die Sonnenbahn über Datumswechsel hinweg vergleichbar
bleibt statt saisonal umzuskalieren. Zusätzlich zeigt ein **Hover-Cursor** (SVG-
Overlay über der Plot-Fläche, per Maus/Touch) am nächstgelegenen Bahn-Sample ein
Fadenkreuz plus eine feste Ablese-Zeile mit Uhrzeit, Azimut samt Himmelsrichtung,
Verschattung in % (τ) und Elevation.

Seit v0.15 bietet die Karte zwei weitere Komforts. **Confidence:** jeder
Bahn-Punkt wird nach `sample_n` skaliert — n=0 (nur statischer Prior) als kleiner
**hohler** Ring in der τ-Farbe, n>0 als gefüllter Punkt, dessen Radius mit der
Evidenz bis zur Sättigung bei `N_SAT` (12) Samples wächst; die Ablese-Zeile
ergänzt `· n=<x>`. **Vergleichsdatum:** ein **karten-lokaler** Datumswähler
(„Vergleich“, per × löschbar; ändert NIE die geteilte Datums-Entität) blendet
eine ZWEITE Sonnenbahn desselben Moduls für ein anderes Datum als gestrichelte
Linie mit hohlen τ-Ringen ein (deren Verschattungshorizont wird bewusst NICHT
gezeichnet), mit Legendenzeile „── <Primärdatum>  - - <Vergleichsdatum>“; die
Ablese-Zeile hängt das azimut-nächste Vergleichssample an
(`· vs <Datum>: <%> (τ …)`). Die Vergleichsdaten liefert die neue, rein lesende
Aktion `balcony_solar_forecast.get_shade_profile` (`SupportsResponse.ONLY`): sie
berechnet das Profil für ein Modul/Datum (Vorgaben = aktuelle Diagrammauswahl)
über `coordinator.build_shade_profile_for`, OHNE die Live-Auswahl zu ändern oder
den Ein-Slot-Memo zu verdrängen (Ad-hoc-Abfrage, uncached). Die Karte ruft sie
über das stabile Low-Level-Websocket-`call_service` mit `return_response` auf.

### 15.5 Dashboard-Installation per Aktion (Ein-Klick)

Statt das Referenz-YAML zu kopieren und die Objekt-IDs von Hand anzupassen,
richtet die Aktion `balcony_solar_forecast.install_dashboard` (SPEC §14.3) das
Observability-Dashboard mit den **echten Entity-IDs dieser Installation** ein.
Ablauf: der Operator legt EINMAL über die UI ein leeres Dashboard an
(Einstellungen → Dashboards → Hinzufügen, URL `balcony-solar`, mit Bindestrich)
und ruft dann die Aktion auf. Die reine Konfig-Erzeugung (`_dashboard.py`,
HA-frei, bare unit-getestet) spiegelt die Karten des mitgelieferten YAML,
ersetzt das opt-in-ApexCharts-Snippet durch die gebündelte
`custom:balcony-shade-profile-card` und lässt Karten/Zeilen mit fehlenden
Entitäten weg (Teilinstallation rendert weiterhin). Die IDs stammen aus der
Entity-Registry (`{entry_id}_{key}` → reale entity_id), die Vergleichs-MAE-Zeilen
und die gemessenen Modul-Sensoren aus Coordinator/Site-Config.

Geschrieben wird ausschließlich über die vorhandene
`LovelaceStorage.async_save(config)` des jeweiligen `url_path` aus
`hass.data[LOVELACE_DATA].dashboards` — NIE über einen neuen
Dashboard-Registry-Eintrag oder eine zweite `DashboardsCollection` (die beim
späteren UI-Bearbeiten Einträge löschen könnte). Jede geschriebene Konfig trägt
oben den Marker `bsf_managed: <version>`. Der **Safety-Gate**: ist das Ziel-
Dashboard nicht im Storage-Modus, wird abgelehnt (YAML nicht schreibbar); trägt
eine bereits vorhandene, nicht-leere Konfig den Marker NICHT und wird `overwrite`
nicht gesetzt, wird abgelehnt (kein Überschreiben fremd erstellter Dashboards);
eine leere oder marker-tragende Konfig wird frei überschrieben — das ist der
idempotente Refresh (z. B. nach einem Integrations-Update). Die Antwort meldet
`dashboard`, `views`, `cards` und die weggelassenen `missing_entities`.
