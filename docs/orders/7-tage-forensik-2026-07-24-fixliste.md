# 7-Tage-Forensik 17.–24.07.2026 — priorisierte Fix- und Verbesserungsliste

> **Abgeschlossener Auftrag, historisch.** Die `SPEC §…`-Verweise in diesem
> Dokument beziehen sich auf die ALTE SPEC-Gliederung (vor der Neufassung
> für 0.23.2). Übersetzung alt → neu: `docs/HISTORIE.md` §H13.

Erstellt 2026-07-24 (Fable 5 Ultracode, 9-Agenten-Workflow `wf_c3d5b74f-576`, 44 Findings,
39 adversarial CONFIRMED / 1 REFUTED / 2 DUPLICATE / 1 PLAUSIBLE). **Nur Analyse — Umsetzung
erfolgt in einem zweiten Schritt (Opus), Abschlussprüfung via Fable.** Datengrundlage:
Live-Abzug der HA-Instanz (Scratchpad `hadata/`, Session 1451a6e6) + Repo v0.20.6.
Der externe BM-Auftrag (`battery-manager-ha/docs/orders/balcony-solar-forecast-auftrag-2026-07-24.md`)
wurde einzeln begutachtet (Abschnitt E).

## Wochenbilanz (Kontext)

Erste Woche mit funktionierendem Nightly-Learning überhaupt (Epoch-Bug bis 16.07., dann
Config-Fixes + 311-Tage-Bootstrap). Vorabend-Prognose (issued, auf AC umgerechnet ×η 0.9211)
vs. Ist (Victron-AC):

| Datum | issued→AC kWh | Ist AC kWh | Fehler |
|---|---|---|---|
| 17.07 | 4.35 | 5.69 | −23.6 % |
| 18.07 | 9.57 | 10.74 | −10.9 % |
| 19.07 | 5.93 | 4.84 | +22.5 % |
| 20.07 | 11.39 | 11.43 | −0.4 % |
| 21.07 | 8.30 | 11.65 | −28.8 % |
| 22.07 | 10.21 | 10.67 | −4.3 % |
| 23.07 | 7.41 | 8.88 | −16.6 % |
| **Woche** | **57.15** | **63.90** | **−10.6 %** (6/7 Tage unter Ist) |

Scoreboard (DC/DC, 11 Tage): Tages-MAE 1.44 kWh, **+5.5 % besser** als beste Baseline
(clear +30 %, overcast +27 %, mixed n=2 nicht aussagekräftig). Spec-Audit: weitgehend konform
(32/32 Entities, Bias-Blend, Clamps, Issued-Ring 8/8 Tage, η im Band, 0 Open-Meteo-Ausfälle).

## Bestätigte Ursachenkette (Kurzfassung)

1. **Rohphysik Morgen** −20 % (05–08Z) bzw. raw/Ist 0.44 um 04Z: Beam-Untermodellierung
   ×1.25–1.35 (~1.0–1.6 kWh/Tag, bekannter Bifacial-Befund) + Ost-Horizont az52–140 el10–15 τ0
   blockt die erste ~1.5 h komplett, real fließen 400–570 W (~140–230 Wh/Tag).
2. **Bias-Zellen passen nicht zur Live-Config**: 311-Tage-Bootstrap lief am 16.07. VOR den
   späteren Config-Edits (el5/6→el10–15, Screen-Reassignments). clear|afternoon θ=0.70 drückt
   eine raw-Kurve, die real schon −15.6 % unter Ist liegt → issued 12–18Z −33 %. RLS λ=0.98
   lernt ~0.001/Tag → Monate bis Konvergenz. clear|morning klemmt am Clamp 1.5.
3. **Intraday-Scalar = Doppelkorrektur**: Samples messen Ist/RAW, serviert wird raw×θ×scalar →
   θ (1.36–1.49 morgens) und Scalar (Peaks 1.33–2.36, immer 08:10–09:20 lokal) korrigieren
   denselben Fehler zweimal → served/Ist 07–09Z bis ×1.9.
4. **Headline-Jojo** (20.07.: 9.68→12.96→9.26 kWh): Scalar-Strip der Tages-Headline existiert,
   aber der Keep-Ceiling-Pfad (MED-1) behält bei re-geclampten Forward-Slots die Ceiling →
   +3.27 kWh Balloon bei Scalar 2.355.
5. **Cloud-Klassifikation** (random-overlap-Total, mid/high zählen voll): sonnige Stunden landen
   in overcast-Zellen (afternoon 11/20 h) → falsche θ-Routung, Quantil-Fehlzuordnung,
   Scoreboard-Strata wertlos (6/11 Sonnentage als „overcast").
6. **Quantile-Cold-Start**: Bootstrap füttert den Quantile-Ring nicht (SPEC §6 verspricht es);
   nur overcast-Bins trained → day-0-Stundenbänder kollabieren auf p10==p90 (BM-F7 bestätigt,
   aber NICHT als day-0-Codepfad-Bug — Bin-Cold-Start + Klassifikations-Artefakt).
7. **Scalar-Blackouts**: In-Memory-Sample-Ring + 120-min-Warmup ⇒ jeder Reload/Restart kostet
   ≥2 h Korrektur. 19.07. (+29.5 %-Tag): 8h15 neutral durch Reload-Kaskade. ≥8 ungeklärte
   Config-Entry-Reloads in 8 Tagen.

---

## A. Priorität 1 — Kern-Fixes (Ertrags-Impact hoch)

### A1. Morgen-Rohphysik: Untersuchung + zweistufiger Fix (IRC-1, GUT-F6.1)
- **Zuerst Untersuchung, kein Blindfix**: per-Plane-Validierung raw vs. Modul-DC 03:35–06:00Z
  für M2/3/6/7 (Skript, kein Release). Klären: Ist der 04Z-Fehlbetrag Beam durch die el10-Kante
  (Bäume real, evtl. τ 0→~0.4 statt el senken) oder Diffus-/Albedo-Unterschätzung? Die el10-Kante
  stammt aus Operator-Emergenzmessungen (16.07.) — nicht einfach verwerfen.
- **Code**: optionaler Config-Faktor `bifacial_beam_gain` (Default 1.0, Operator ~1.23) in
  `core/engine.py` auf Beam+Circumsolar-POA — verlagert die validierte ×1.23-Korrektur in die
  RAW-Physik; θ-Zellen konvergieren dann Richtung 1.0–1.1, Clamp bekommt Headroom.
- Erwartung: raw 05–08Z von −22 % auf ~−4 %; Morning-θ löst sich vom 1.5-Clamp.

### A2. Intraday-Scalar bias-referenziert samplen (IRC-2)
- `coordinator._build_intraday_sample`: modeled-Seite von `pr.raw_watts` auf die
  θ-korrigierte Kurve stellen (raw×day_factor bzw. served/intraday-Faktor via
  `_intraday_factor_for_slot`-Helper, coordinator.py:1507). θ bleibt nightly-frozen → keine
  Zirkularität. SPEC §5 um einen Satz anpassen.
- Sim-validiert: Peaks 1.78→1.23, 1.45→1.12, 1.36→1.14; der ECHTE Unterschätzungstag 21.07.
  bleibt bei 1.49 → Wettersignal bleibt erhalten. KEIN harter Scalar-Clamp (BM-Position bestätigt).

### A3. Headline-Jojo: Keep-Ceiling-Pfad ersetzen (IRC-4, GUT-EXTRA-1)
- `coordinator._dayahead_today_kwh_over` (Z. 1470–1505): im Clamped-Zweig
  `min(prereclamp[i]/factor, ceiling_i)` verwenden (slot_ceilings/ac_slot_ceilings existieren,
  engine.py:772/600) statt served Ceiling zu behalten. `prereclamp/factor` ist der EXAKTE
  scalar-freie Wert (Docstring-Claim „liegt dazwischen" ist falsch, engine.py:723–726).
- Effekt: +3.27-kWh-Balloon → ~0; Headline day-ahead-stabil by design. Unit-Test mit
  synthetischem 2.35-Scalar-Fall. Kleinster Diff, größter Headline-Effekt.

### A4. Bias-Zellen-Neustart nach Config-Änderungen (IRC-6, FOR-4)
- **Operator, sofort (kein Code)**: nach Umsetzung von A1 den Service `reset_day_ahead_bias`
  ausführen — 8 Live-Lerntage schlagen 311 Tage Fremd-Config.
- **Code, mittelfristig**: Config-Fingerprint (Hash über planes/horizon/screens/albedo) im Store;
  bei Änderung effektives n der θ-Zellen auf ~min(n,20) deckeln (`_nightly.py`, `core/bias.py`),
  optional Repair-Issue „Re-Bootstrap empfohlen".
- Erwartung: afternoon-θ → ~0.9–1.1, served 12–18Z von −33 % auf ±10 % (~+0.5 kWh/Tag).

### A5. Cloud-Klassifikation auf Clear-Sky-Index umstellen (SCT-1)
- `classify_cloud` (core/bias.py:392–442): statt random-overlap-Total (mid/high zählen voll)
  kc = forecast_ghi / haurwitz_ghi(elevation) je Slot (Größen vorhanden: WeatherSlot.ghi,
  core/clearsky.py, core/solpos.py); Schwellen ~kc>0.65 clear / <0.3 overcast / sonst mixed;
  fog-Regel behalten. Minimal-Alternative: Layer attenuationsgewichtet (low 1.0/mid 0.6/high 0.3).
- Betrifft Bias-Routing, Quantil-Bins UND Scoreboard-Strata konsistent. **Zusammen mit A4/A6
  umsetzen** (gelernte Zellen-Inhalte passen nach Umstellung nicht mehr zur Klassenbedeutung).

### A6. Quantile-Bootstrap-Seeding (QRC-2, SPEC-1) — der eigentliche F7-Fix
- `scripts/backfill.py`: pro Stunde corrected_wh = clamp(θ_cell)×gated_modeled_wh, relerr =
  measured/corrected ([0,5], ≥5 Wh) als [iso_date, relerr] in per-(class×part)-Ringe
  (90-Tage-Fenster, Cap 720); `QuantileState.to_dict()` ins Bootstrap-JSON;
  `store.import_bootstrap` + `coordinator.async_import_bootstrap` additiv erweitern
  (Schema-Version, Rollback-Snapshot).
- Alternative/Ergänzung (kleiner): gepoolter Fallback für untrained Bins (gleiche day_part über
  alle Klassen) — bewusste SPEC-§6-Änderung, dokumentieren.
- Ohne Seeding: clear|midday (n=4) braucht ~1 Monat; BM-Abnahmekriterium Teil 1 unerreichbar.

### A7. Intraday-Scalar-Blackouts entschärfen (SCT-2)
- (a) Sample-Ring beim Setup rekonstruieren (5-min-Recorder-Statistiken von
  measured_dc_power_total + letzte Raw-Kurve, Executor-Job analog `_actuals`) ODER Ring mit
  kurzer TTL (~4 h) im Store persistieren (SPEC §5 verbietet nur die Persistenz des SCALARS —
  Satz präzisieren).
- (b) Optional: `INTRADAY_MIN_TRAILING_MINUTES` 120→~60 mit Dämpfung Richtung 1.0 bei kleinem
  Span statt hartem Gate.
- (c) **Operator**: Quelle der ≥8 Config-Entry-Reloads in 8 Tagen klären (Automation/HACS/
  Options-Flow?) — jede Reload kostet ≥2 h Korrektur-Blindheit.

## B. Priorität 2 — mittlere Verbesserungen

### B1. Tages-p10/p90 unter Intraday-Scalar asymmetrisch skalieren (FOR-7)
p10 mit min(scalar, 1) skalieren bzw. Bandbreite ∝ |scalar−1| aufweiten — aktuell schiebt ein
Spike das GESAMTE Band hoch (3/6 Tage p10 > End-Ist um 09:00 lokal). Ergänzend zu A3.

### B2. Day-ahead-Bias auf `slow_only_hourly_wh` trainieren (SCT-3)
`_nightly.day_ahead_samples`: modeled-Seite von `snap.raw_hourly_wh` auf
`snap.slow_only_hourly_wh` (Fallback raw). Ein-Zeilen-Fix des bekannten
Doppelkorrektur-Designs (θ trainiert vs. pure raw, wirkt auf shademap-korrigierte Kurve).
Aktuell ~harmlos (Shademap-Leg neutral, Δ0.03 %), wird relevant sobald die Shademap lernt.

### B3. get_issued_forecast: AC-Kurve + Metadaten ergänzen (IRC-5, SCT-4)
- `hourly_wh_ac` (×η_learned zum Issue-Zeitpunkt) ergänzen oder Felder explizit als DC
  dokumentieren (SPEC §15.4) — die DC-Semantik hat in dieser Analyse alle issued-Ratios um
  +8 % geschönt und BMs Forensik verwirrt.
- `cloud_class_by_hour` (+ optional applied_factor_by_hour) ausliefern — liegt im Snapshot,
  reine Serialisierung.

### B4. Beobachtbarkeit für Konsumenten (SCT-4)
- get_forecast/Sensor-Attribute: per-Slot `band_source`/`bin_trained`-Flag (Info existiert beim
  Band-Bau, coordinator.py:898–935).
- `day_ahead_bias_status`: `clamped: true` je Zelle bei θ an MIN/MAX (clear|morning klebt seit
  Bootstrap am 1.5-Deckel — wichtiges Diagnosesignal).

### B5. Diagnostics reparieren (SPEC-2, QRC-4, SPEC-4)
- `store_stats()` / `learner_state_summary()` am Coordinator implementieren oder Blöcke als
  `{'implemented': false}` kennzeichnen (aktuell Statuslüge `available: false`).
- Quantile-`trained`-Flag: zusätzlich effective_days ≥ QUANTILE_MIN_DAYS prüfen
  (coordinator.py:1947) + days-Feld ausgeben.
- `forecast.daily_kwh` → `daily_kwh_dc` umbenennen (oder AC zusätzlich; erklärt die
  12.37-vs-13.44-Verwirrung).

### B6. Overcast-Quantilbänder beobachten (QRC-5)
p10×0.45–p90×2.65 enthalten Lern-Transienten + Fehlklassifikation der ersten Woche. Nach A5/A6
neu bewerten; falls p90 nach ~4 Wochen weiter >2×: Samples aus der Bias-Einschwingphase
untergewichten.

## C. Priorität 3 — klein / Doku / Prozess

- **C1** (SPEC-5): Strata-vs_best-Prozent bei n<3 unterdrücken bzw. low_n-Flag (−480 %-Anzeige).
- **C2** (SCT-5): Scoreboard-Strata zusätzlich nach OBSERVED-Klasse (Tages-kc aus Actuals);
  nach A5 ggf. nur dokumentieren.
- **C3** (SPEC-6): Dokumentieren, dass nur 3 Tage Catch-up nachgeholt wurden und das Kill-Gate
  erstmals ~27.07. urteilt; optional einmaliger Re-Score-Service für 06.–12.07. (Issued-Ring
  hat die Daten).
- **C4** (IRC-7): Scalar-Warmup NICHT weiter anheben; optional dawn-relativ (60 min ab erstem
  gate-fähigen Sample) — nur falls Interim-Besserung vor A1 nötig.
- **C5** (IRC-3/GUT-F6.3): `INTRADAY_APPLY_HORIZON_MINUTES` 360→180/240 NUR falls nach A1+A2
  messbarer Rest-Overshoot bleibt (Gutachter-Dissens: f6-Agent „flankierend sinnvoll",
  Gutachter „Symptomknopf" — nach A1/A2 neu bewerten, eine Woche messen).
- **C6**: Antwort an das BM-Repo (siehe E) inkl. Korrektur der zwei Attributionsfehler.

## D. Ausdrücklich NICHT umsetzen

- **Harter Intraday-Scalar-Clamp 1.3–1.5** — 21.07. brauchte legitim >1.6 (BM-Position bestätigt).
- **Pauschaler konservativer Abend-/Mean-Aufschlag** (BM-F7.3 Teil 1) — hätte die 3
  Überprognose-Tage (19./20./22.07.) verschlimmert; der systematische Anteil wird von A1+A4 getragen.
- **BM-F7.2 „Scalar auf p10/p90 mitanwenden statt Bänder verwerfen"** — No-Op: Bänder werden nie
  verworfen (coordinator.py:898–935 jeden Tick, alle Slots) und der Scalar steckt bereits in
  p10/p90 (engine.py:859–878 band_curve_from_corrected auf total_watts).
- **`INTRADAY_MIN_MODELED_WH` absolut anheben** (BM-F6.2 Teil 1) — Konstante skaliert mit
  Anlagengröße; ~100–150 Wh nötig für diesen 3.56-kWp-Standort würde kleine 800-W-Anlagen
  anderer Nutzer fast ganztägig vom Sampling ausschließen. Stattdessen A2 (+ optional
  Elevations-Gate/Energie-Gewichtung: `w = exp(−age/τ) × cs_ref_wh`; Docstring
  „energy-weighted" ist aktuell faktisch falsch, bias.py:215–218).
- **Tagessummen-Slew-Limit/Hysterese** — versteckt echte Weather-Refresh-Sprünge; nach A2+A3 unnötig.

## E. Gutachten zum BM-Auftrag (Einzelurteile)

| BM-Maßnahme | Urteil | Kern |
|---|---|---|
| F6.1 Morgen-Shape korrigieren | **Übernehmen** (Prio 1) | Ursache aber: Config-Ost-Horizont + Beam-Gain, NICHT „Shademap deckt Morgen nicht ab" → A1 |
| F6.2 MIN_MODELED_WH anheben / El-Gate | **Modifiziert** | Absolute Schwelle nein (bricht kleine Anlagen); Energie-Gewichtung/El-Gate ja — aber A2 ist der wirksame Hebel (Gates-Sim: Peak 1.78→1.36–1.46 statt →1.2, Aktivierung +2 h verzögert) |
| F6.3 APPLY_HORIZON 360→120–180 | **Ablehnen als Sofortmaßnahme** | kappt symmetrisch echte Korrekturen (19./21.07.); ggf. später 240 → C5 |
| F6 „kein harter Clamp" | **Zustimmen** | 21.07. brauchte >1.6; nach A2 werden Peaks strukturell <1.3 |
| F7.1 Ursache day-0-Kollaps | **Erledigt** | BM-Verdacht (Mitternachts-/Refresh-Verlust) code-seitig widerlegt; Ursache Bin-Cold-Start + Bootstrap-Lücke → A6 |
| F7.2 Scalar auf Bänder anwenden | **Ablehnen** | No-Op, bereits Ist-Zustand; Abnahmekriterium darüber nicht erreichbar |
| F7.3 Abend-Bias / breitere p90 | **Mean-Shift ablehnen; p90 modifiziert** | via A6 (Seeding) + A4 (Re-Bootstrap), kein Hand-Aufschlag |
| Abnahmekriterium | **Modifiziert** | Teil 1 an A6-Release koppeln (sonst Frist ~6 Wochen); Teil 2 zu lasch — struktureller Overshoot passiert UNTER 1.6; ersetzen durch: Wochenmittel served/Ist 07–10 lokal ±20 % UND Scalar 08–09 lokal ≤~1.25 an Tagen mit \|Tagesfehler\| <10 % |

**BM-Faktencheck**: Kernzahlen korrekt (04Z-Watt, Scalar 2.355@08:30, 06:00-Bias −0.8/MAE 1.1,
day-0-Kollaps, Jojo). Korrekturbedarf: (1) „Bänder werden verworfen" ist falsch; (2) die
−29 % am 21.07. — BMs 8.22 ist der AC-Headline-Wert um 00:13 lokal; in konsistenter AC-Sicht
stimmt die Größenordnung (−28.8 %), Welle-1s „Attributionsfehler"-Einwand war selbst ein
DC/AC-Artefakt.

## F. Widerlegtes / Positives

- **REFUTED**: „Day-part-Stufenartefakt weiterhin aktiv" (FOR-5) — v0.19-Solar-Blend
  interpoliert korrekt (±0.75 h Rampe nachgemessen); der Knick ist Zellen-ANKER-Misfit (→ A4),
  kein Anwendungs-Artefakt.
- **Spec-konform**: Entities, Einheiten, bewusst fehlende state_class auf Forecast-Sensoren,
  Bias-Blend, Scalar-Clamp, AC-Re-Clamp der p90, Issued-Ring (8/8, 90 Tage), Services, η 0.9211
  (n=251) im Band, kill_gate=null korrekt bei 11<14 scored_days, degraded/fresh-Logik,
  0 Open-Meteo-Ausfälle.
- Nachmittags-PHYSIK nach 16.07.-Fixes weitgehend gut (raw/Ist 12–18Z 0.82–1.23) — die
  Nachmittags-Unterprognose kommt aus der Bias-Zelle (→ A4), nicht mehr aus der Physik.

## Update 24.07. abends — Ergebnisse der Zusatz-Untersuchungen

**Morgen-Physik (A1-Untersuchung, abgeschlossen):** Die operator-gemessene el10-Ostkante ist real
(Kronen-Oberkante, ab el ~9.5 voll transparent), aber `tau: 0` darunter ist widerlegt — die
5-min-Rampen klarer Tage zeigen eine semi-transparente Krone (tau ~0 bei el<4.5, 0.25 bei el5–6,
0.43 bei el6–7, ~0.9 ab el8–9; keine Stufe um 04:44Z, 83–90 % des Nach-Kanten-Niveaus fließen
schon bei 04:40Z). Empfehlung: in allen 8 Planes die zwei Zeilen az52/az89 el10 tau0 durch eine
tau-Rampe ersetzen (az63→78: 0.05/0.15/0.35/0.55/0.85/0.85, dann az89 tau0; el unangetastet)
+ Site-Feld `bifacial_beam_gain: 1.25` (Fit 1.28–1.30 auf den zwei sauberen Tagen; nach 1 Woche
ggf. 1.30). Erwartung: klare Tage von −20…−28 % raw auf ~−4…−8 %. Übergang: Morgen-Bias-Zelle
(am 1.5-Clamp) lässt served 04–06Z für ~3–7 Tage überschießen — nicht zurückrollen.
NEUER Nebenbefund: M4/M8 messen morgens ~10× den modellierten Diffus (helle Wand + bifacialer
Rückseiten-Pickup, ~0.3–0.5 kWh/Tag) — per Config-tau nicht adressierbar, Kandidat für spätere
Diffus-Floor/Wand-SVF-Modellierung. Saisonaler Caveat: die az-kodierte tau-Rampe driftet
~0.3°/Tag — monatlich nachankern oder langfristig elevationsabhängiges tau (Design-Thema).
Details: Scratchpad `analysis7d/morgen-physik/REPORT.md`.

**Reload-Forensik (abgeschlossen):** Kein fremder Auslöser — alle ≥8 „Reloads" waren komplette
HA-Restarts, jeweils 6–25 s nach einem HACS-Install (7× BSF 0.20.0–0.20.6, 6× BM 0.13.0–0.16.0,
2× Core-Update in 9 Tagen; alle exit code 0). Dazu exakt drei echte Entry-Reloads durch
Options-Änderungen (17.07., 2× 19.07.). Ausgeschlossen: Automationen, Scheduler, Node-RED, HACS-
Hintergrund, battery_manager. Konsequenz: nichts abstellen — **T7 (restart-fester Sample-Ring)
ist bei dieser Release-Frequenz der eigentliche Fix** und in seiner Priorität bestätigt.
Korrektur an SCT-2: die dort genannten Blackout-Zeitfenster waren UTC (nicht lokal) und die
Zuordnung Restart↔Entry-Reload war invertiert; Dauern und Wirkung (19.07. ~8 h blind, +29.5 %-Tag)
bleiben korrekt. Details: Scratchpad `analysis7d/reload-forensik/`.

## Detailquellen

- Agenten-Skripte/Notizen: Scratchpad `analysis7d/` (forensiker/, spec-auditor/, quantile-rca/,
  scout/, gutachter/, verifier*/) — Session 1451a6e6.
- Vollständiges strukturiertes Ergebnis: `analysis7d/final_result.json` (44 Findings + 43 Verdicts).
- Workflow-Journal: `subagents/workflows/wf_c3d5b74f-576/journal.jsonl`.
