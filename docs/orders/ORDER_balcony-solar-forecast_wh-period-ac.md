# Arbeitsauftrag: balcony-solar-forecast — AC-Stundenkurve + Intraday-Stabilität

**An:** externen Coding-Agenten im Repo `balcony-solar-forecast` (Schwester-Repo
von `battery-manager-ha`)
**Von:** 7-Tage-Live-Audit der Referenz-Anlage, 31.07.–07.08.2026 (CEST)
**Datum:** 2026-08-07
**Priorität:** Punkt 1 hoch (systematischer Planerfehler nachgelagert), Punkt 2 mittel

---

## Kontext

Der Battery Manager (BM) plant seine AC-seitige Simulation aus den
15-Minuten-Kurven-Attributen `wh_period` / `wh_period_p10` / `wh_period_p90`
der drei Tages-Entities (`energy_production_today` / `_tomorrow` /
`_day_after_tomorrow`). Das Audit hat diese Kurven gegen die States und die
Realität (Integral von `measured_ac_power`) vermessen.

---

## Punkt 1 (Hauptbefund): `wh_period*` ist die DC-Modellkurve, der State ist AC

**Beleg (live, 07.08. ~20:15 CEST):**

| Entity | State | Σ(wh_period) | Verhältnis |
|---|---|---|---|
| `…_today` | 5,649 kWh (AC) | 6.129 Wh = `…_today_dc`-State | 0,9215 |
| `…_tomorrow` | 11,503 kWh (AC) | 12.483 Wh = `…_tomorrow_dc` | 0,9215 |
| `…_day_after_tomorrow` | 11,229 kWh (AC) | 12.184 Wh = `…_day_after_tomorrow_dc` | 0,9215 |

Der Faktor ist exakt das gelernte Skalar
(`sensor.balcony_solar_forecast_intraday_correction_scalar` = 0,921).
Der Code-Kommentar in v0.25.1 (`sensor.py`) sagt es selbst: „the 15-min
watts/wh_period curve attributes stay the DC model curve".

**Auswirkung nachgelagert:** der BM simulierte mit ~+8,5 % zu viel PV
(~1,1 kWh/Normaltag, ~0,55 kWh/Schwäche-Tag). Mitverursacher eines vermeidbaren
Netzbezugs von 0,7 kWh am 07.08. (Haus-SOC fiel auf 13 %, unter den
20-%-Inverter-Floor). Die Order-Antwort vom 24.07. hatte ein `hourly_wh_ac`
bereits empfohlen — es existiert bis heute nicht.

**Bereits geschehen (BM-seitig, ab v0.25.0):** der BM skaliert die Kurven
notfalls selbst mit η = State ÷ Σ(Kurve) (Clamp [0.5, 1.0]) und **bevorzugt
explizite AC-Attribute**, sobald vorhanden:

- `wh_period_ac` statt `wh_period`
- `wh_period_ac_p10` statt `wh_period_p10`
- `wh_period_ac_p90` statt `wh_period_p90`

### Aufgabe

1. **Neue Attribute `wh_period_ac` / `wh_period_ac_p10` / `wh_period_ac_p90`**
   an denselben drei Tages-Entities ausgeben: identische Bucket-Struktur
   (15-min, ISO-Keys, naive Lokalzeit oder UTC-konvertierbar — Format exakt
   wie `wh_period`), Werte = DC-Bucket × gelerntem η (dasselbe Skalar, das
   auch den AC-State aus dem DC-State ableitet). Invariante, die der BM
   implizit annimmt und die ein Test sichern muss:
   **Σ(`wh_period_ac`) == AC-State (in Wh, Rundung ±1 Wh)** — je Entity.
2. **Bestehende Attribute unverändert lassen** (`wh_period*` bleibt DC —
   Abwärtskompatibilität; Doku-Kommentar, der das sagt, existiert bereits).
3. Dokumentation (`README` bzw. Attribut-Referenz des Repos) ergänzen: welche
   Attribute DC vs. AC sind.
4. Tests: Bucket-für-Bucket η-Skalierung; die Summen-Invariante; p10/p90
   konsistent zum Median (gleiches η); Verhalten bei fehlendem η (Lernphase)
   — dann AC-Attribute weglassen oder = DC, klar festlegen und dokumentieren.

**Akzeptanz:** BM (v0.25.0+) liest ohne weitere Änderung die AC-Kurve; die
η-Ratio-Schätzung entfällt als No-op (η ≈ 1 gegen `wh_period_ac`).

---

## Punkt 2: Intraday-Instabilität der publizierten Tageswerte

**Beleg (Audit-Fenster, 8 Tage):** 68 State-Revisionen ≥ 0,5 kWh
(today 17×, tomorrow 32×, day_after 19×), davon 40 ≥ 1,0 kWh — ~8,5
signifikante Eingangsänderungen pro Tag für einen Planer, der im
5-Minuten-Raster replant. Größte Einzelsprünge:

- 06.08. 03:46: **−3,04 kWh** (tomorrow)
- 07.08. 01:01–03:31: −2,75 kWh, dann 06.46: **+2,15 kWh** (Jojo)
- 04.08.: day_after **−7,15 kWh**

Das Scalar-Kriterium aus der 24.07.-Order (08–09 Uhr ≤ 1,25 bei
<10-%-Prognosetagen) war am 31.07. verletzt (1,42 um 09:00) und am 07.08.
15:16 erneut (1,672). Jeder Sprung erzwingt einen BM-Replan und treibt das
dort gemessene Plan-Flattern (Entfeuchter-Empfehlung 254 on/off-Flips in der
Woche, 04.08. allein 45× in 2,4 h).

### Aufgabe

1. Ursache der Sprünge eingrenzen (Wettermodell-Refresh? Intraday-Korrektur
   überkorrigiert bei Bewölkungswechsel?) — kurz dokumentieren.
2. Publikations-Glättung prüfen: z.B. revisionsbegrenzte Übernahme
   (Rate-Limit oder Mindest-Änderungsschwelle für State-Updates, außer bei
   echter Wetterfront), Hysterese auf das intraday-Skalar, oder getrennte
   „rohe" vs. „geglättete" States. Ziel: keine Sprünge > 1 kWh ohne
   realen Anlass; Scalar-Kriterium 08–09 Uhr ≤ 1,25 wieder einhalten.
3. Messgröße nachrüsten, damit das prüfbar bleibt (siehe Punkt 3).

---

## Punkt 3 (klein): Auditierbarkeit

`wh_period*` ist per `_unrecorded_attributes` aus dem Recorder ausgeschlossen —
sinnvoll wegen der Payload-Größe, aber historische Per-Slot-Analysen sind so
unmöglich. Bitte eine **kompakte, aufgezeichnete** Diagnose ergänzen, z.B.
Tages-Skalare (Σ wh_period je Entity, Σ wh_period_ac, η, Anzahl
State-Revisionen > 0,5 kWh des Vortags) als eigene Sensoren oder Attribute
eines bestehenden Diagnose-Sensors.

---

## Was NICHT kaputt ist (zur Einordnung)

- Tagessummen-Güte solide: MAE 0,92 kWh (~11 %) über die Audit-Woche,
  Vorabend-20:00-Prognose 0,74 kWh, Bias ≈ 0. `daily_kwh_mae` (1,127)
  definitorisch korrekt und konsistent.
- p10/p90-Coverage 8/8 Tage (Tagessummen); Slot-Bänder ausgereift
  (48/49/49 D2-Slots, `band_source=bin`, keine Kollapse) — der F7-Fix der
  Vorwoche ist verifiziert wirksam.
- Der 0,0-Coverage-Wert am Abend ist ein Artefakt („kein Tageslicht-Slot im
  Horizont"), kein Bug.

## Referenzen

- Audit-Protokolle: `battery-manager-ha` → `/tmp/bm_audit/pv-prognose-guete.md`
  (vollständige Messreihen, Skripte, Rohdaten)
- BM-seitige Gegenmaßnahme: `battery-manager-ha` v0.25.0,
  `coordinator.py::_pv_curve_ac_factor` + `_WH_PERIOD_AC_ATTRS`
- Frühere Order-Antwort 24.07.2026 (Empfehlung `hourly_wh_ac`)
