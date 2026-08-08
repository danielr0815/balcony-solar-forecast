# Antwort: ORDER_balcony-solar-forecast_wh-period-ac.md (2026-08-07)

Umsetzungsbericht zur Order aus dem 7-Tage-Live-Audit (31.07.–07.08.2026).
Code-Belege nennen Datei + Funktions-/Konstantenname (Stand: dieser PR).

---

## Punkt 1 — `wh_period_ac*`-Attribute: umgesetzt

**Was ausgeliefert wird:** die drei Tages-Entities
(`energy_production_today` / `_tomorrow` / `_day_after_tomorrow`) tragen neu
die Attribute `wh_period_ac` / `wh_period_ac_p10` / `wh_period_ac_p90` —
15-min-Buckets, identische ISO-Keys wie `wh_period` (gleiche Slots, gleiches
Format). Die Werte sind die **servierte AC-Kurve** (`result.ac_watts` bzw.
`ac_p10_watts` / `ac_p90_watts`, `coordinator._build_data`,
`sensor.EnergyProductionSensor.extra_state_attributes`): DC × η je
Wechselrichtergruppe **plus AC-Clamp am Gruppenlimit** — physikalisch
korrekter als das von der Order vereinfacht geforderte „DC-Bucket × η"
(ein geklampfter Mittags-Slot liegt unter η × DC, und genau das will die
Simulation wissen). `wh_period*` / `watts` bleiben unverändert DC
(Abwärtskompatibilität); alles ist per `_unrecorded_attributes` +
`recorder.exclude_attributes` wie bisher aus dem Recorder ausgeschlossen.

**Korrektur einer Order-Prämisse:** der Faktor 0,9215 zwischen State und
Σ(`wh_period`) ist nicht das Intraday-Skalar, sondern das gelernte
Inverter-η (`core/inverter_cal.py`, EMA am AC/DC-Verhältnis; der Beleg wurde
um ~20:15 Uhr erhoben, da ist das Intraday-Faktorfenster
`INTRADAY_APPLY_HORIZON_MINUTES` = 360 min produktionslos). Der Befund und
die Lösung bleiben unverändert richtig: die Kurven-Attribute waren DC, der
State ist AC, und die AC-Kurve fehlte als Attribut.

**Invariante (festgelegt):** Σ(`wh_period_ac` eines Tages) == AC-State gilt
**exakt für `_tomorrow` und `_day_after_tomorrow`** (State und Kurve rollen
aus derselben `ac_watts`-Reihe; Rundung ≤ ±1 Wh — Test
`test_energy_sensor_ac_curve_sum_matches_state`). Für `_today` gilt sie bei
aktivem Intraday-Skalar **bewusst nicht**: die Heute-Headline ist die
day-ahead-stabile Erwartung (Skalar herausgerechnet,
`coordinator._dayahead_today_kwh_ac`), die servierte Kurve behält den Skalar
als Live-Nowcast — dasselbe Design wie bei der DC-Kurve (SPEC §14.1). Eine
„je Entity"-Gleichheit auch für heute gäbe den Nowcast-Charakter der Kurve
auf; das war in der Order so nicht gemeint (die 24.07.-Antwort wollte die
Kurve als Nowcast behalten). Der BM-seitige η-Fallback
(State ÷ Σ, Clamp [0.5, 1.0]) entartet für `_tomorrow`/`_d2` zum No-op (η ≈ 1),
für `_today` normalisiert er weiterhin auf den day-ahead-Stand — wie bisher.

**Lernphase (festgelegt):** die AC-Attribute werden **immer** ausgegeben,
sobald Kurvendaten existieren. Bis die gelernte η-Kalibrierung vertraut ist
(`INVERTER_CAL_MIN_SAMPLES`), steht das konfigurierte η
(`inverter_efficiency_source: "config"` am `power_production_now`-Sensor) —
kein Weglassen, kein Stillschweigen. `wh_period_ac_p10/p90` folgen den
DC-Bändern: leere Dicts, solange keine Bänder ausgegeben wurden (Cold Start).

## Punkt 2 — Intraday-Instabilität: Ursachen eingegrenzt, Glättung geprüft

**Ursachen, gewichtet (Code-Belege):**

1. **Wetter-Refresh ersetzt das Payload ungedämpft** (`coordinator._async_try_fetch`:
   `set_last_payload` wholesale; danach rechnet der nächste 15-min-Tick
   `_build_data` mit dem neuen Modelllauf). Die Nacht-Zeitstempel des Audits
   (01:46/03:46 CEST) fallen in die Ankunftsfenster frischer ICON-Läufe; d2
   liegt am Horizont-Rand (60–72 h Modellunsicherheit) und trägt zusätzlich
   das Null-Tail-Risiko (`fetcher.parse_weather` mappt fehlende Radiation auf
   0.0; `radiation_coverage` validiert nur das 24-h-Nahfenster) — Mechanismus,
   der den −7,15-kWh-d2-Sprung am 04.08. erklären kann, ohne die Rohdaten
   nicht beweisbar.
2. **Nächtliches RLS-θ-Retrain 01:30 + Startup-Catch-up** (`_nightly.train_day_ahead`,
   `__init__.async_startup_catchup`): in früher Lernphase
   (`RLS_INIT_COVARIANCE` groß, wenige Samples) verschiebt ein Retrain θ um
   Zehntel — auf einer 10–12-kWh-Prognose 1–3 kWh in einem Schritt, auf alle
   Horizont-Slots (erklärt 01:01–03:31 und alternativ 03:46, falls die Nacht
   ein HA-Restart/Update hatte).
3. **Drift-Auto-Disable / η-Trust-Crossing** (`_nightly._update_drift`,
   `inverter_cal.effective_eta`): selten, aber sprunggewaltig (Korrektur
   entfällt bzw. AC-Kurve schaltet von Config- auf gelerntes η).
4. **Intraday-Skalar:** für tomorrow/d2 strukturell ausgeschlossen (Faktor 1.0
   jenseits des 360-min-Horizonts); für today seit v0.25.1 gedoppelt gezügelt
   (Headline-Strip `_dayahead_today_kwh_ac`, Rate-Limit
   `INTRADAY_MAX_STEP_PER_TICK`, Elevations-Gate). **Das Audit-Fenster beginnt
   vor v0.25.1 (Release 01.08.)** — die frühen Beobachtungen (31.07.,
   inkl. Scalar-Verletzung 1,42 um 09:00) liefen noch ohne diese Zügelung.
5. **Mitternachts-Bucket-Rollover:** erwartbare Diskontinuität, die ein naives
   Audit als Revision mitzählt (kein Bug).

**Glättung geprüft — Entscheidung: keine Publikations-Glättung (Stand dieses
PRs).** Begründung: die 24.07.-Forensik
(`docs/orders/7-tage-forensik-2026-07-24-fixliste.md`) hat ein
Tagessummen-Slew-Limit/Hysterese bereits begründet abgelehnt („versteckt
echte Weather-Refresh-Sprünge"). Ein Pauschal-Rate-Limit auf die Headlines
hätte in der Audit-Woche die echten Modell-Revisionen (Ursache 1) genauso
gebremst wie das Lerner-Rauschen (Ursache 2) — bei einem Planer, der im
5-Minuten-Raster replant, ist ein verzögertes *echtes* Update der
schlimmere Fehler. Die seitdem gelandeten Fixes (v0.25.1) adressieren die
Skalar-Beiträge direkt an der Ursache statt an der Publikation. Was fehlte,
war die **Messbarkeit** — die liefert Punkt 3: künftige Audits können
Revisionen > 0,5 kWh direkt am `curve_audit`-Sensor ablesen, inkl.
Provenienz (`payload_refreshed` trennt Modell-Revisionen von Lerner-Rauschen)
und getrennt nach Tages-Offset. Ein Wieder-Audit nach einer vollständigen
v0.25.1+-Woche liefert die Evidenz, ob und wo nachgeschärft werden muss
(Kandidaten, falls ja: Horizont-Tail-Coverage im Fetcher;
θ-Schritt-Begrenzung im Day-ahead-Retrain).

## Punkt 3 — Auditierbarkeit: umgesetzt

Neuer Diagnose-Sensor **`sensor.balcony_solar_forecast_curve_audit`**
(`sensor.CurveAuditSensor`, SPEC §14.4), kompakt und **aufgezeichnet**:

- **State:** Anzahl signifikanter Revisionen (> 0,5 kWh,
  `HEADLINE_REVISION_THRESHOLD_KWH`) der drei publizierten AC-Tageswerte am
  laufenden Tag (Summe über die Offsets).
- **Attribute:** `days` — je lokalem Tag d0/d1/d2 die Tages-Skalare
  (`dc_wh` = Σ `wh_period`, `ac_wh` = Σ `wh_period_ac`, `state_kwh` =
  publizierter AC-State); `eta_effective` + `eta_source` (`learned`/`config`);
  `revisions_today` / `revisions_yesterday` je Offset; `last_revision` mit
  Provenienz (`at`, `date`, `day_offset`, `old_kwh`, `new_kwh`,
  `payload_refreshed`).
- Zählung keyed per **Ziel-Kalenderdatum** (`coordinator._track_headline_revisions`)
  — der Mitternachts-Rollover zählt nicht als Revision. Zählerstand wird
  restart-sicher persistiert (additive Store-Sektion `curve_audit`, kein
  Schema-Bump; die Vergleichs-Baseline bleibt bewusst RAM-only, damit ein
  Restart keine Revision fabriziert). Der Diagnostics-Download trägt den
  persistierten Stand unter `store.curve_audit`.
