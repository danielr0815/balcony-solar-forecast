# HA-Integration: Entities, Services, Diagnostics

Dies ist die **Außenschnittstelle** von `balcony_solar_forecast` (Stand `main` @ v0.23.0): welche Home-Assistant-Entitäten
die Integration anlegt, welche Attribute die Prognosekurven tragen, welche Aktionen (Services) es gibt und was der
Diagnostics-Download enthält. Du brauchst dieses Dokument, wenn du eine Automation/Karte an die Integration anbindest,
einen Entity-State interpretierst, eine Aktion aufrufst oder einen Bugreport-Dump liest.
Physik → `02-physik-und-horizontmodell.md`, Lernschichten → `03-lernschichten-und-korrekturen.md`,
Betrieb/Runbook → `05-anlage-und-betrieb-runbook.md`.

---

## 1. Grundmuster: Gerät, Entity-IDs, Verfügbarkeit

- **Ein Config-Entry = eine Anlage („Site")**, ein HA-**Gerät** namens `Balcony Solar Forecast`
  (`BalconyForecastEntity.__init__` in `sensor.py`, `DeviceInfo` mit `identifiers={(DOMAIN, entry_id)}`,
  `sw_version = INTEGRATION_VERSION`). Alle Entitäten hängen an diesem Gerät.
- **Plattformen** (`__init__.py`, `PLATFORMS`): `binary_sensor`, `date`, `select`, `sensor`. Mehr nicht.
- **Stabiler Schlüssel ist die `unique_id`**, nicht die Entity-ID: jede Entität setzt
  `unique_id = f"{entry_id}_{key}"` und `translation_key = key` — mit **einer Ausnahme**: die dynamischen
  Vergleichs-Sensoren (`ComparisonDailyKwhMaeSensor.__init__` in `sensor.py`) setzen `translation_key = None`
  und einen eigenen `_attr_name` (ein geteilter translation_key würde sie alle gleich benennen); sie haben
  folglich auch keinen Eintrag in `translations/en.json`, und ihre Entity-ID wird explizit gepinnt (§2.6).
  Die **Entity-ID** wird von HA aus dem
  *englischen Anzeigenamen* (`translations/en.json`) plus Gerätename gebildet und **weicht teils vom Key ab** —
  `_dashboard.py` dokumentiert das ausdrücklich (`learner_status_fast` → `sensor.…_fast_learner_status`).
  In den Tabellen unten steht deshalb beides. Wer die Zuordnung programmatisch braucht: `collect_entity_map()`
  in `_dashboard.py` mappt `key → entity_id` aus der Entity-Registry; die Aktion `install_dashboard` nutzt genau das.
- **Verfügbarkeit (SPEC §13, „nie still"):** Prognose-Entitäten folgen dem Coordinator
  (`CoordinatorEntity.available`, d. h. `last_update_success`). **Diagnose-Entitäten sind immer verfügbar**
  (`_DiagnosticSensor.available` → `True`), damit der Betreiber auch im Ausfall lesen kann, *warum*.
  Die beiden Mess-Sensoren sind **vom Coordinator entkoppelt** und hängen nur an ihren Quell-Entitäten.
- „Degradiert" wird nie durch einen stehengebliebenen Wert signalisiert, sondern durch `source_status` /
  `binary_sensor …_degraded` bzw. durch echtes `unavailable` (Coordinator wirft `UpdateFailed`).

---

## 2. Sensoren

### 2.1 Prognose-Headline (AC) — kein `state_class`

| Key | Entity-ID | Einheit | device_class | Bedeutung |
|---|---|---|---|---|
| `energy_production_today` | `sensor.balcony_solar_forecast_energy_production_today` | kWh | `energy` | Tagesenergie **heute**, servierte **AC**-Summe (`energy_today_kwh_ac`) |
| `energy_production_tomorrow` | `…_energy_production_tomorrow` | kWh | `energy` | morgen (`energy_tomorrow_kwh_ac`) |
| `energy_production_d2` | `sensor.balcony_solar_forecast_energy_production_day_after_tomorrow` | kWh | `energy` | übermorgen (`energy_d2_kwh_ac`); **hier laufen Key und Entity-ID am stärksten auseinander** — `en.json` nennt den Sensor „Energy production day after tomorrow", `d2` steht nur in `unique_id`/`translation_key` |
| `power_production_now` | `…_power_production_now` | W | `power` | AC-Leistung des Slots „jetzt" (`power_now_w_ac`); **`state_class: MEASUREMENT`** |

**Warum die Energie-Sensoren bewusst KEIN `state_class` tragen** (`EnergyProductionSensor` Docstring):
eine *Prognose* darf nie in die HA-Langzeitstatistik (LTS) laufen. Mit `state_class` würde HA für eine sich
ständig ändernde Zukunftszahl Statistik-Reihen anlegen, die später als „Messwert" gelesen werden — dieselbe
Entscheidung trifft die rany2-Integration `open-meteo-solar-forecast`, deren Entity-Muster hier absichtlich
kompatibel nachgebaut ist (Konsumenten wie `battery_manager` stellen nur ihre drei Entity-Picker um).
`power_production_now` trägt dagegen `MEASUREMENT` — ein Momentanwert ohne Summenbildung ist statistisch harmlos.

Attribute von `power_production_now` (`PowerNowSensor.extra_state_attributes`):
`inverter_efficiency` = `{Gruppenname: konfiguriertes η_inv}`; `inverter_efficiency_source` = `"learned"` oder
`"config"` (Ehrlichkeits-Label ab v0.19.2: ohne AC-Zähler ist η ein reines Config-Echo);
`inverter_efficiency_learned` = `{eta, n, effective, ggf. raw}` — nur vorhanden, sobald die AC-Kalibrierung
Samples gefaltet hat (`n > 0`) **oder** ein `raw`-Block existiert (gegatete Rohverhältnisse, Diagnose
mis-skalierter DC-Sensoren).

### 2.2 Modellinternes DC (Diagnose) — ebenfalls ohne `state_class`

| Key | Entity-ID | Einheit | Bedeutung |
|---|---|---|---|
| `power_production_now_dc` | `…_power_production_now_dc` | W | DC-Leistung jetzt (`power_now_w`), `state_class: MEASUREMENT` |
| `energy_production_today_dc` | `…_energy_production_today_dc` | kWh | DC-Tagesrollup heute (`energy_today_kwh`) |
| `energy_production_tomorrow_dc` | `…_energy_production_tomorrow_dc` | kWh | DC morgen |
| `energy_production_d2_dc` | `sensor.balcony_solar_forecast_energy_production_day_after_tomorrow_dc` | kWh | DC übermorgen; **Key ≠ Entity-ID** (Anzeigename „Energy production day after tomorrow (DC)", vgl. §2.1) |

DC bleibt Lern- und Scoreboard-Grundwahrheit; AC ist seit Phase 2 der betreiberseitige Standard auf den
Haupt-Sensoren. Alle vier sind `EntityCategory.DIAGNOSTIC` und immer verfügbar.

### 2.3 Messung (Grundwahrheit) — **mit** `MEASUREMENT`, absichtlich im Recorder

| Key | Entity-ID | Einheit | Bedingung |
|---|---|---|---|
| `measured_dc_power_total` | `…_measured_dc_power_total` | W | nur wenn **mindestens eine** Ebene ein `actual_entity` hat |
| `measured_ac_power` | `…_measured_ac_power` | W | nur wenn `SiteConfig.ac_actual_entity` konfiguriert ist |

Beide: `device_class: power`, `state_class: MEASUREMENT` → HA führt Langzeitstatistik, und sie sind bewusst
**nicht** vom Recorder ausgeschlossen (ihre Historie *ist* der Zweck). Beide abonnieren ihre Quell-Entitäten
direkt (`async_track_state_change_event`) statt `coordinator.data` zu lesen — sie rechnen bei jeder Änderung neu,
unabhängig vom Prognose-Takt, und bleiben verfügbar, solange mindestens eine Quelle numerisch meldet
(Grundwahrheit muss auch bei degradierter Prognose weiterlaufen).
Attribute DC-Summe: `channels_total`, `channels_reporting`, `sources` (Entity-IDs, dedupliziert, Plane-Reihenfolge),
`source_names` (Plane-Namen M1…M8, indexgleich — damit Karten die Kanäle beschriften können).
Attribut AC: `source`. Ein vorzeichenverkehrter Zähler wird über `SiteConfig.ac_actual_invert` **einmal** hier
negiert (`MeasuredAcPowerSensor._recompute`).

### 2.4 Betriebszustand (Diagnose)

| Key | Entity-ID | Einheit / Typ | Bedeutung |
|---|---|---|---|
| `last_fetch_age_min` | `…_last_fetch_age` | min, `MEASUREMENT` | Alter des letzten guten Open-Meteo-Payloads. Liest bevorzugt `coordinator.weather_age_seconds_live`, damit der Wert im Ausfall **weiterklettert** statt einzufrieren |
| `source_status` | `…_source_status` | ENUM | Sprosse der Degradationsleiter: `fresh`, `cached`, `physics_fallback`, `unavailable` |

### 2.5 Lernschichten (Diagnose)

| Key | Entity-ID | Einheit / Typ | Bedeutung |
|---|---|---|---|
| `intraday_scalar` | `…_intraday_correction_scalar` | dimensionslos, `MEASUREMENT` | aktuell angewandter Skalar des FAST-Learners; 1.0 = keine Korrektur. Transient (nicht persistiert) |
| `drift_mae_corrected` | `…_drift_mae_corrected` | Wh, `MEASUREMENT` | rollierender Tageslicht-MAE der **korrigierten** (servierten) Kurve. Attribute: `raw_mae`, `corrected_mae`, `baseline_mae` |
| `learner_status_fast` | `…_fast_learner_status` | ENUM | Status FAST-Layer |
| `learner_status_slow` | `…_shademap_learner_status` | ENUM | Status Shademap-Layer |
| `learner_status_day_ahead` | `…_day_ahead_bias_status` | ENUM | Status Day-ahead-Bias-Layer |

ENUM-Werte (`LEARNER_STATUS_VALUES` in `const.py`): `active`, `off`, `disabled_by_drift`, `frozen`,
`cold_start` (aktiviert, aber **ohne** gelernten Zustand — z. B. direkt nach `reset_day_ahead_bias`; „active" wäre
hier eine Statuslüge, v0.19.2). Unbekannte Werte melden `None`, nie einen erfundenen Status.
`drift_mae_corrected` trägt **absichtlich keine `device_class`**: `ENERGY` + `MEASUREMENT` ist in HA eine
ungültige Kombination (Energie verlangt `total`/`total_increasing`), und ein MAE ist ohnehin keine Energiemenge.

**Nur der Day-ahead-Sensor** trägt Attribute (`LearnerStatusSensor.extra_state_attributes`):
`bias_cells` = `{"<cloud_class>|<day_part>": {cloud_class, day_part, theta, n, applied, clamped}}` und `cells_n`.
`theta` = gelernter (bandgeklammerter) Multiplikator, `applied` = der aktuell **servierte** Faktor der Zelle
(= `theta` erst ab `n >= RLS_MIN_SAMPLES`, sonst 1.0), `clamped: true` wenn `theta` am Bandrand klebt
(`<= DAY_AHEAD_BIAS_MIN` oder `>= DAY_AHEAD_BIAS_MAX`) — das Signal für „die RLS will weiter korrigieren, als das
Band erlaubt". Bei leerem Learner bleiben die Attribute als `bias_cells: {}` / `cells_n: 0` bestehen (ein
verschwindendes Attribut sah aus wie ein Bug). Quelle: `coordinator._bias_cells_summary`.

### 2.6 Scoreboard (Diagnose)

| Key | Entity-ID | Einheit | Bedeutung |
|---|---|---|---|
| `daily_kwh_mae` | `…_daily_kwh_mae` | kWh | Engine-MAE der Tagesenergie über das Rollfenster; Attribute `window_days`, `scored_days` |
| `hourly_mae` | `…_hourly_mae` | Wh | Engine-MAE je Tageslichtstunde |
| `vs_best_baseline_pct` | `…_vs_best_baseline_pct` | % | um wie viel Prozent die Engine die **beste** konfigurierte Vergleichsprognose auf Tages-MAE schlägt (positiv = Engine besser) |
| `comparison_daily_kwh_mae_<slug>` | `sensor.balcony_solar_forecast_comparison_daily_kwh_mae_<slug>` | kWh | MAE **einer** konfigurierten Fremdprognose |

Alle mit `state_class: MEASUREMENT`, ohne `device_class` (MAE ist keine kumulative Energie). Werte sind `None`
statt 0, solange kein bewerteter Tag im Fenster liegt.
Die Vergleichs-Sensoren sind **dynamisch**: einer je Eintrag in `CONF_COMPARISON_SENSORS` (Options-Flow),
Auslieferung mit **leerer** Liste. Ihre Entity-ID wird explizit auf `sensor.{DOMAIN}_comparison_daily_kwh_mae_<slug>`
gepinnt (`ComparisonDailyKwhMaeSensor.__init__` setzt `self.entity_id`, der Slug ist rein ASCII), Attribute
`comparison_name` und `daily_entity`. Beim Umbenennen/Entfernen räumt `_prune_stale_comparison_sensors` die
Registry-Leichen weg, sonst hängen sie für immer auf `unavailable`.

### 2.7 Quantilbänder (P10/P90) — kein `state_class`

| Key | Entity-ID | Einheit | Bedeutung |
|---|---|---|---|
| `energy_production_today_p10` | `…_energy_production_today_p10` | kWh | pessimistisches Tagesband **heute**, AC |
| `energy_production_today_p90` | `…_energy_production_today_p90` | kWh | optimistisches Tagesband heute, AC |

Beides Prognosen → wie die Headline **kein `state_class`**, und sie folgen der Coordinator-Verfügbarkeit
(keine Diagnose-Entitäten). Summiert wird die **stündliche** AC-Bandkurve (`DATA_KEY_QUANTILE_CURVES_AC`) über
die Stunden des lokalen Heute; für P10 ist `energy_today_kwh_ac_p10` autoritativ (dort ist der transiente
Intraday-Skalar asymmetrisch herausgerechnet, damit ein Spike das Tages-P10 nicht über den Tagesist hebt).
Ohne trainiertes Band ist der State `None` — das Band kollabiert ehrlich, statt eine Spanne zu erfinden.
Attribute (nur wenn ein Band existiert): `band_source` (`learned` | `ensemble` | `envelope`) und
`band_source_by_day` (siehe §3).

### 2.8 Verschattungsprofil

`shade_profile` → `sensor.balcony_solar_forecast_shade_profile`, Einheit `%`, Diagnose, immer verfügbar.
State = verschatteter Anteil des Tageslichts (Anteil der Samples mit effektivem τ unter der Schwelle) für das per
`select` gewählte Modul am per `date` gewählten Tag; `None` ohne Samples. Die Kurven liegen als Attribute an
(Details siehe §3.3). Neu berechnet wird bei jedem Coordinator-Update und bei jeder Auswahl-Änderung.

---

## 3. Kurven-Attribute und Recorder

### 3.1 Auf den Energie-Sensoren (heute/morgen/übermorgen)

| Attribut | Inhalt | Auflösung |
|---|---|---|
| `watts` | `{ISO-UTC-Slotstart: W}` — servierte Momentanleistung | 15 min |
| `wh_period` | `{ISO-UTC-Slotstart: Wh}` — Energie je Slot | 15 min |
| `wh_period_p10` | untere Bandkurve | 15 min |
| `wh_period_p90` | obere Bandkurve | 15 min |

Jeder Tagessensor trägt **nur seinen eigenen lokalen Kalendertag**: die site-weite Kurve wird per
`_local_date_of()` auf das Zieldatum geschnitten. Wichtig: die vier Kurven-Attribute sind die **DC**-Modellkurven
(die AC-Bänder existieren nur stündlich), während der *State* AC ist — beim Nachrechnen „Attributsumme vs. State"
ist das der erwartete Versatz (≈ η_inv). `wh_period_p50` ist in `_unrecorded_attributes` deklariert, wird aber vom
Sensor **nicht** emittiert (die servierte Kurve *ist* der P50-Stand).

### 3.2 Band-Provenienz `band_source_by_day`

Ab 0.21 (Forensik B4/SCT-4) auf den P10/P90-Sensoren und in der `get_forecast`-Antwort:
`{"<ISO-Datum>": {"bin": n, "envelope": n, "ensemble": n, "neutral": n}}` — wie viele Slots dieses lokalen Tages
ein trainiertes Bin-Band, ein per Ensemble aufgeweitetes Band, ein reines Ensemble-Band bzw. gar kein Band bekamen.
Damit ist sichtbar, welche Prognosetage überhaupt ein *gelerntes* Band tragen. Der Tages-Aggregatlabel dazu ist
`band_source`. Beide fehlen, wenn Quantile aus sind oder kalt starten (kein Band → kein Quellenlabel).

### 3.3 Shade-Profil-Attribute

Parallel-Arrays je Tageslicht-Sample: `time`, `azimuth`, `sun_elevation`, `transmittance` (gepoolte effektive τ),
`transmittance_individual` (τ des **eigenen** Kanals; leer bei ungruppierter Ebene), `sample_n` (gepoolte
Bin-Evidenz je Sample). Horizontlinien auf Azimutraster: `horizon_azimuth`, `static_horizon`, `shade_horizon`,
plus die jahresstabilen Achsgrenzen `axis_azimuth_min` / `axis_azimuth_max`. Skalare Zusammenfassung:
`module`, `date`, `doy`, `tau_threshold`, `sample_count`, `has_learned_data`, `learned_bins`, `shaded_fraction`,
`mean_transmittance`, `max_elevation`, `sunrise`/`sunset` (je `{time, azimuth}`).

### 3.4 Recorder-Ausschluss — zwei Mechanismen

1. `recorder.exclude_attributes()` (Datei `recorder.py`, integrationsweit): `watts`, `wh_period` und **alle**
   Shade-Profil-Kurvenarrays.
2. `_unrecorded_attributes` je Entität: `EnergyProductionSensor` schließt zusätzlich `wh_period_p10/_p50/_p90`
   aus, `EnergyBandSensor` schließt `band_source_by_day` aus, `ShadeProfileSensor` zusätzlich die beiden
   `axis_azimuth_*`.

Der Live-State und die Live-Attribute bleiben vollständig — beschnitten wird nur, was in die History-DB geschrieben
wird. Wer Kurven historisch braucht, nutzt `get_issued_forecast` (§4.1), nicht den Recorder.

---

## 4. Aktionen (Services)

Alle Aktionen werden **einmal in `async_setup`** registriert (`_services.async_register_services`, Quality-Scale
`action-setup`) und bleiben unabhängig vom Entry-Ladezustand bestehen: ein Aufruf ohne geladenen Entry liefert einen
klaren `ServiceValidationError` statt „Service not found". Jede schreibende Aktion löst ihr Ziel über
`_resolve_single_coordinator()` auf — `entry_id` optional, aber bei mehreren Sites Pflicht.
Alle Feld- und Antwortnamen unten sind aus `_services.py` / `services.yaml` / `_bootstrap.py` verifiziert.

### 4.1 Lesen

**`get_forecast`** (`SupportsResponse.ONLY`), Feld: `entry_id` (optional; ohne Angabe **alle** Sites).
Antwort: `{"entries": {entry_id: {planes, slot_starts, total_15min, total_hourly, issued_at}}}` —
`planes` = `{Plane-Name: [W je Slot]}` ausgerichtet an `slot_starts`. Zusätzlich, **nur wenn Bänder existieren**:
`p10`/`p50`/`p90`, je `{wh_period: {iso: Wh}, hourly: {iso_stunde: Wh}}`, plus `band_source` und
`band_source_by_day`. Ein Coordinator ohne aktuelle Prognose liefert leere Kurven, nie eine alte.

**`get_issued_forecast`** (`ONLY`), Felder: `entry_id` (optional), `date` (**Pflicht**, ISO `YYYY-MM-DD`, lokaler Tag).
Liest die **wie ausgegeben** eingefrorene Day-ahead-Kurve aus dem 90-Tage-Issued-Ring — ohne Hindsight, also
*nicht* mit heutigem Lernstand nachgerechnet. Antwort unter `result`:
- Treffer: `date`, `available: true`, `issued_at`, `oldest_available`, `hourly_wh` (**DC**, serviert/korrigiert),
  `raw_hourly_wh` (**DC**, reine Physik), `hourly_wh_ac`, `eta`, `eta_source`, `cloud_class_by_hour`,
  `applied_factor_by_hour`.
- Fehltreffer: `date`, `available: false`, `oldest_available` — **kein Fehler** (die Karte zeichnet einfach keine
  Linie; `oldest_available` erlaubt den ehrlichen Hinweis „Archiv seit …").

> **DC/AC-Fallstrick der Altversionen:** vor 0.21 lieferte diese Aktion nur `hourly_wh` / `raw_hourly_wh` **ohne**
> Kennzeichnung, dass es DC ist. Jeder daraus gebildete Vergleich gegen gemessene AC-Werte war damit systematisch
> um ~8 % zugunsten der Prognose verzerrt. Seit 0.21 gibt es `hourly_wh_ac` = DC × dem **im Snapshot eingefrorenen**
> η; fehlt das Feld im Alt-Snapshot, wird das aktuelle gelernte η eingesetzt und über `eta_source: "current"`
> (statt `"snapshot"`) gekennzeichnet. `applied_factor_by_hour` = serviert/roh je Stunde (Shademap ∘ Day-ahead),
> Stunden mit roh ≈ 0 fehlen (undefiniertes Verhältnis).

**`get_shade_profile`** (`ONLY`), Felder: `entry_id`, `module` (Default: aktuelle Diagramm-Auswahl),
`date` (ISO, Default: aktuelle Auswahl). Liefert unter `result` dieselbe Struktur wie die Sensor-Attribute (§3.3),
**ohne** die Live-Auswahl zu verändern und ohne den Einzel-Slot-Memo des Diagramms zu verdrängen — gedacht als
Vergleichs-Overlay der Karte. Unbekanntes Modul / kaputtes Datum → `ServiceValidationError` mit Klartext.

**`dump_shademap`** (`ONLY`), Feld: `entry_id` (optional, sonst alle). Antwort:
`{"entries": {entry_id: {"channels": {kanal: {"bins": [{sun_az, sun_el, half, tau, n}, …]}}}}}` —
Polar-Tabelle je Kanal, sortiert nach `(half, sun_az, sun_el)`; `half` 0/1 = vor/nach Sommersonnenwende,
Bin-Mitten aus den Indizes und den `SHADEMAP_*_BIN_DEG`-Konstanten rekonstruiert. Kaputte Bins werden
übersprungen, ein Dump wirft nie.

**`suggest_shade_groups`** (`ONLY`), Felder: `entry_id`, `max_diff` (Default `0.06`, Bereich (0, 0.5]),
`min_common_bins` (Default `30`, ≥ 1). Vergleicht die je Ebene individuell gelernten Shademap-Kanäle binweise und
schlägt eine Gruppierung vor (Complete-Linkage über die n-gewichtete mittlere τ-Differenz). Antwort unter `result`
inkl. `current_groups` = `{Plane-Name: konfigurierter shade_channel}` zum direkten Vergleich.

### 4.2 Schreiben / Wartung

**`import_bootstrap`** (`SupportsResponse.OPTIONAL`), Felder: `entry_id`, **genau eines** von `payload`
(JSON-Objekt oder JSON-String) oder `path` (Datei in einem HA-erlaubten Verzeichnis) — beides oder keines ist ein
Validierungsfehler. Übergibt an `coordinator.async_import_bootstrap` (Schema-/Site-Signatur-Prüfung, Clamping,
n-Credit-Cap, Rollback-Snapshot im Store). Antwort: `{"result": {bias_cells, shademap_channels, shademap_bins,
quantile_bins}}`. Typischer Einsatz: Ergebnis von `scripts/backfill.py` einspielen (siehe `docs/BACKFILL.md`).

**`run_bootstrap`** (`ONLY`, seit 0.23) — der In-App-Weg zum selben Ziel, **ohne** Token, ohne `site.json`,
ohne Desktop-Python.
- Felder: `entry_id` (optional); `start_date` / `end_date` (ISO, Default `end` = gestern lokal,
  `start` = heute − `BOOTSTRAP_DEFAULT_MAX_DAYS` (= 400)); **`dry_run` (Default `true`)**.
- **Der Default schützt vor Versehen:** der erste Aufruf holt Wetter, rekonstruiert und liefert nur die Summary —
  der Store wird **nicht** angefasst. Erst ein explizites `dry_run: false` importiert über denselben Pfad wie
  `import_bootstrap` (inkl. Rollback-Snapshot).
- **Lock/Nightly-Docking:** ein einziger `asyncio.Lock` je Coordinator (`_bootstrap_lock`). Der nächtliche
  Trainingsjob hält denselben Lock und **wartet** auf einen laufenden Bootstrap (keine Trainingsnacht geht
  verloren); ein zweiter gleichzeitiger `run_bootstrap` sieht `locked()` und wird sofort mit
  `ServiceValidationError` abgewiesen.
- Datenquellen: Open-Meteo Previous-Runs über die integrationseigene aiohttp-Session, in
  `BOOTSTRAP_WEATHER_CHUNK_DAYS`-Fenstern (= 90) gechunkt; Actuals über einen In-Process-
  `statistics_during_period`-Read im Recorder-Executor (nicht die WebSocket-API). Die Rekonstruktion läuft im
  Executor mit INFO-Fortschrittslogs alle 50 Tage — Laufzeit Minuten, nicht Sekunden.
- Antwort (immer): `days_used`, `days_skipped`, `date_range {start, end}`,
  `weather_source` (`as_issued` | `analysis_fallback`), `bias_cells`, `shademap_channels`, `shademap_bins`,
  `shademap_samples`, `quantile_bins`, `quantile_samples`, `imported` (bool), `duration_s`; im Dry-Run zusätzlich
  `hint`. Fehlerbilder (kein Recorder, keine Actuals, Open-Meteo-Fehler, invertierter Zeitraum, keine
  `actual_entity`, keine nutzbaren Tage) kommen als `ServiceValidationError` mit Klartext, nie als Traceback.

**`reset_day_ahead_bias`** (`OPTIONAL`), Feld: `entry_id`. Löscht **alle** gelernten
(cloud_class × day_part)-Zellen; die servierte Kurve fällt sofort auf Physik + Shademap zurück, die nächtlichen
RLS-Läufe lernen jede Zelle kalt neu. Shademap, Kill-Switches und Rollback-Ring bleiben unberührt.
Antwort: `{"result": {"cleared_cells": n}}`. Einsatz: eine fehltrainierte Zelle verzerrt den Tag (z. B. nach einer
Binning-Änderung).

**`rollback_learners`** (`OPTIONAL`), Felder: `entry_id`, `snapshots_back` (Default 1, max `LEARNER_SNAPSHOT_RING` = 10).
Stellt Bias + Shademap + Quantile gemeinsam aus dem nächtlichen Rollback-Ring wieder her (1 = jüngster Snapshot).
Antwort: `{"result": {restored_taken_at, snapshots_back, ring_size}}`. **Nur Zustand** — Enable-Schalter und ein
Drift-Auto-Disable bleiben, wo sie sind; das Wieder-Einschalten einer Schicht ist bewusst eine explizite
Betreiber-Aktion im Options-Flow. Leerer Ring → `ServiceValidationError`.

**`install_dashboard`** (`OPTIONAL`), Felder: `entry_id`, `dashboard` (Default `balcony-solar`), `overwrite`
(Default `false`). Schreibt das Observability-Dashboard mit den **echten** Entity-IDs dieser Installation in ein
zuvor in der UI angelegtes, **leeres** Storage-Dashboard (der URL-Pfad muss einen Bindestrich enthalten).
Idempotent: ein erneuter Lauf frischt die Config auf. Sicherheits-Gate: ein Dashboard mit Karten, das **nicht** den
Marker `bsf_managed` trägt, wird ohne `overwrite: true` nicht überschrieben; YAML-Dashboards werden abgelehnt.
Antwort: `{"result": {dashboard, views, cards, missing_entities}}` — `missing_entities` nennt die Keys, deren
Entität (noch) nicht registriert ist und deren Karte deshalb weggelassen wurde.

---

## 5. Binary-Sensoren, Select, Date

### 5.1 Die vier Binary-Sensoren (alle Diagnose, alle immer verfügbar)

| Key | Entity-ID | device_class | „on" bedeutet |
|---|---|---|---|
| `degraded` | `binary_sensor.balcony_solar_forecast_degraded` | `problem` | Prognose läuft auf **weniger als** einem frischen Pull (cached / physics_fallback / unavailable). Attribute: `source_status`, `last_fetch_age_min` |
| `fast_learner_active` | `…_fast_learner_active` | `running` | FAST-Layer formt die servierte Kurve **jetzt** (Status genau `active`). Attribut: `status` |
| `slow_learner_active` | `…_shademap_learner_active` | `running` | Shademap-Layer aktiv. Attribut: `status` |
| `kill_gate_passed` | `…_kill_gate_passed` | — | Engine schlägt über ein **volles** Fenster die beste Baseline um mindestens die Gate-Marge. `None`/unknown, solange das Fenster nicht voll ist — nie ein verfrühtes Urteil. Attribute: `window_days`, `scored_days`, `engine_daily_kwh_mae`, `engine_vs_best_baseline_pct` |

### 5.2 Auswahl-Entitäten des Verschattungsdiagramms (beide `EntityCategory.CONFIG`, immer verfügbar)

- `select.balcony_solar_forecast_shade_profile_module` — Optionen = die konfigurierten Plane-Namen
  (`coordinator.shade_profile_plane_names()`). Default ist die Front-Ebene (die Ausrichtung, die die meisten Ebenen
  teilen). Die manuelle Wahl **wird** über Neustarts gehalten (`RestoreEntity`); ein zwischenzeitlich
  umbenanntes/entferntes Modul fällt auf den Default zurück statt auf eine tote Option.
- `date.balcony_solar_forecast_shade_profile_date` — der lokale Kalendertag, den das Diagramm zeigt.
  **Bewusst NICHT restauriert:** jeder Neustart/Reload öffnet auf *heute*, eine Ad-hoc-Wahl gilt nur in der Sitzung.

Beide Setter rufen `coordinator.set_shade_profile_*`, was über `async_update_listeners` den `shade_profile`-Sensor
neu rechnen lässt.

### 5.3 Update-Entity

**Die Integration liefert keine `update`-Entity.** Es gibt keine `update.py`, `Platform.UPDATE` steht nicht in
`PLATFORMS`, und `manifest.json` deklariert nichts dergleichen. Eine `update.…`-Entität zu dieser Integration im
HA-System stammt daher von **HACS** (HACS legt für verwaltete Custom-Integrationen eigene Update-Entities an) und
nicht aus diesem Repository. Versionsstand ist über `sw_version` am Gerät (`INTEGRATION_VERSION`, aktuell
`0.23.0`) und im Diagnostics-Dump sichtbar.

---

## 6. Diagnostics-Dump lesen

Herunterzuladen über Einstellungen → Geräte & Dienste → Integration → „Diagnose herunterladen"
(`diagnostics.async_get_config_entry_diagnostics`). Koordinaten sind redigiert — **auch die im verschachtelten
`site`-Objekt**; alles andere ist Anlagen-Geometrie, die der Betreiber sehen soll.

**Wie komme ich an einen Dump / an Live-Daten?** Der bequemste Weg ist die HA-UI: Geräte- bzw. Integrationsseite →
„Diagnose herunterladen" liefert eine JSON-Datei, die man unverändert in eine Analyse-Session kippen kann. Dieselbe
Datei liegt hinter dem REST-Pfad `/api/diagnostics/config_entry/<entry_id>` (die `entry_id` findet man über
`/api/config/config_entries/entry?domain=balcony_solar_forecast`) — praktisch für Skripte, die den Dump automatisch
mitziehen. Der Dump ist aber ein **Momentanbild**: Kurven und Historie stehen nicht darin. Live-States samt
Attributen kommen über `/api/states`, zeitliche Verläufe über die Recorder-/History-APIs — `/api/history/period/…`
für State-Historie und der WebSocket-Befehl `recorder/statistics_during_period` für die Langzeitstatistik — die es
per REST **nicht** gibt, weshalb die Power-History-Karte sie per `callWS` zieht (`run_bootstrap` kommt an dieselben
Daten ohne API, per In-Process-Read im Recorder-Executor, §4.2).
Ausgegebene Prognosekurven vergangener Tage holt man nicht aus dem Recorder, sondern
über `get_issued_forecast` (§4.1). Ein **Long-Lived-Token** braucht man nur, wenn ein *externer* Prozess auf diese
APIs zugreift (Validierungslauf `scripts/validation/validate.py`, `scripts/backfill.py`) — innerhalb von HA
(Aktionen, Templates, Karten) ist keins nötig. Anlegen/Widerrufen und der komplette Validierungslauf stehen in
`05-anlage-und-betrieb-runbook.md` §4.5; Token gehören nirgends in dieses Repo oder in eine Chat-Session.

| Block | Inhalt | Woran du beim Lesen zuerst schaust |
|---|---|---|
| `entry` | `title`, `data`, `options` (beide lat/lon-redigiert) | Liegt eine strukturelle Einstellung fälschlich in `options` statt `data`? (siehe §7) |
| `state` | `last_update_success`, `source_status`, `degraded`, `weather_age_seconds`, `last_fetch_age_min`, `last_error`, `computed_at` | Sprosse der Degradationsleiter + Alter des Wetterbilds |
| `forecast` | `slot_count`, `first_slot`, `last_slot`, `plane_names`, **`daily_kwh_dc`**, **`daily_kwh_ac`**, `hourly_count` | DC/AC sind getrennt ausgewiesen (vor 0.21.0 gab es nur ein mehrdeutiges `daily_kwh`, das ~8 % über der AC-Zahl lag). Randnotiz: der Docstring von `_forecast_summary` in `diagnostics.py` nennt dafür fälschlich „v0.20.7" — diese Version existiert weder im CHANGELOG noch als Git-Tag; der Split kam laut CHANGELOG mit 0.21.0 |
| `store` | `issued_days`, `actuals_days`, `hourly_actuals_days`, `snapshot_ring` + `snapshot_ring_capacity`, `schema_version` | Füllstände der Persistenz — 0 issued_days bei laufendem Betrieb ist ein Alarm |
| `learners` | `status` (je Layer), `intraday_scalar`, `drift_mae`, `correction_source`, `state: {bias_cells, quantile_bins, shademap_channels, shademap_bins: {kanal: n}}` | Hat die Schicht überhaupt gelernten Zustand? Steht ein Layer auf `disabled_by_drift`? |
| `scoreboard` | `engine_daily_kwh_mae`, `engine_hourly_mae`, `comparison_daily_kwh_mae`, `engine_vs_best_baseline_pct`, `kill_gate_passed` (+ `kill_gate_passed_flag`), `window_days`, `scored_days`, `strata` | `strata` = Aufschlüsselung je Wetter-Stratum (clear/mixed/overcast/fog) — dort zeigt sich, in welchem Wetter die Engine verliert |
| `quantiles` | `enabled` plus je Bin `{n, days, trained}` | `trained` spiegelt **exakt** das Servier-Gate: `n >= QUANTILE_MIN_SAMPLES` **und** `days >= QUANTILE_MIN_DAYS`. Ein Bin ohne `trained` liefert kein Band |
| `ensemble` | Enable-Flag, Alter des gecachten Payloads, Member-Zahl, abgedeckte Stunden, heutige Bandquelle | nur relevant, wenn der Ensemble-Schalter an ist (Default AUS) |

Fehlende Zubringer degradieren blockweise zu `{"available": false}` bzw. `{"error": …}` — Diagnostics wirft nie.
Ist der Coordinator gar nicht geladen, enthält der Dump nur `entry` und
`state: {available: false, reason: "coordinator_missing"}`.

---

## 7. Config-Flow: Setup vs. Reconfigure vs. Options

Die häufigste Verwechslung: **strukturelle Felder liegen NICHT in den Optionen.** Jeder Leser mischt
`{**entry.data, **entry.options}` (Optionen gewinnen) — eine strukturelle Einstellung, die fälschlich in
`options` landet, würde `entry.data` für immer verschatten. Deshalb:

| Feld | Wo | Konstante |
|---|---|---|
| Site-Name (unveränderlich nach Setup) | **nur Setup** | `CONF_NAME` |
| Breiten-/Längengrad | Setup **+ Reconfigure** → `entry.data` | `CONF_LATITUDE` / `CONF_LONGITUDE` |
| Fetch-Intervall (300–21600 s), Recompute-Intervall (60–3600 s) | Setup + Reconfigure → `data` | `CONF_FETCH_INTERVAL` / `CONF_RECOMPUTE_INTERVAL` |
| AC-Zähler-Entität + „Vorzeichen invertieren" | Setup + Reconfigure → `data` (in das Site-Objekt gemergt) | `CONF_AC_ACTUAL_ENTITY` / `CONF_AC_ACTUAL_INVERT` |
| Bodenalbedo | Setup + Reconfigure → `data` (ins Site-Objekt) | `CONF_SITE_ALBEDO` |
| **Beam-Gain (bifazialer Direktstrahlungs-Gain)** | Setup + Reconfigure → `data` (ins Site-Objekt) | `CONF_SITE_BEAM_GAIN` |
| **Site-Objekt** (alle Ebenen, Horizonttabellen, Wechselrichter-Gruppen) | Setup + Reconfigure → `data` | `CONF_SITE` |
| Kill-Switch FAST-Learner | **nur Optionen** | `CONF_FAST_LEARNER_ENABLED` (Default AN) |
| Kill-Switch Shademap-Learner | nur Optionen | `CONF_SLOW_LEARNER_ENABLED` (Default AN) |
| Kill-Switch Day-ahead-Bias | nur Optionen | `CONF_DAY_AHEAD_BIAS_ENABLED` (Default AN) |
| Kill-Switch Quantilbänder | nur Optionen | `CONF_QUANTILES_ENABLED` (Default AN) |
| Kill-Switch Ensemble-Bänder | nur Optionen | `CONF_ENSEMBLE_ENABLED` (Default **AUS**, Opt-in) |
| Vergleichsprognosen (Liste `{name, daily_entity}`) | nur Optionen | `CONF_COMPARISON_SENSORS` (Default **leer**) |

Merksatz: **Beam-Gain und alles Geometrische liegen im Reconfigure — in den Optionen stehen ausschließlich
Laufzeit-Schalter und die Vergleichsliste.**

Weitere Mechanik, die man kennen sollte:
- **`_structural_data()`** ist für Setup und Reconfigure derselbe Code: die sichtbaren lat/lon-Felder werden **in das
  Site-Dict hineingemergt**, weil der Coordinator ausschließlich die site-eingebetteten Koordinaten liest. Ohne
  diesen Merge würde jeder Nutzer außerhalb der Referenzanlage still für die Referenzkoordinaten rechnen.
- **Autoritative Felder:** AC-Zähler, Albedo und Beam-Gain nutzen `suggested_value` statt `default` — ein geleertes
  Feld **bleibt geleert** und entfernt den Wert aus dem Site-Dict (dann greift wieder der Auslieferungs-Default,
  bei Beam-Gain die Identität 1.0).
- **Reconfigure räumt auf:** in derselben atomaren `async_update_reload_and_abort`-Operation werden stale
  strukturelle Schlüssel (`_STRUCTURAL_OPTION_KEYS`) aus `entry.options` entfernt — Altlasten des früheren
  Options-Flows, die sonst die frisch gespeicherten Daten wieder verschatten würden.
- **Options-Speichern erhält Unbekanntes:** der Options-Flow spreizt zuerst die bestehenden Optionen und schreibt
  nur die sechs Laufzeitwerte darüber. Vergleichszeilen laufen durch `ComparisonConfig.list_from_options` — halb
  ausgefüllte oder kaputte Zeilen werden verworfen statt persistiert.
- **Jede Options- oder Reconfigure-Änderung löst einen Entry-Reload aus** (`_async_reload_entry`); Entitäten
  werden neu aufgebaut, der Intraday-Skalar startet bei 1.0.
- Das Site-Objekt wird beim Speichern validiert (`_site_validation.validate_site`: Azimut 0–360, Neigung 0–90,
  Wp > 0, τ 0–1, Horizontzeilen nach Azimut aufsteigend); Fehler erscheinen inline am Feld `site`.

---

## 8. Weitere Berührungspunkte mit HA

- **Energy-Dashboard:** `energy.async_get_solar_forecast(hass, entry_id)` liefert
  `{"wh_hours": {ISO-Stunde: Wh}}` aus der **servierten AC**-Stundenkurve (`hourly_wh_ac`). Ohne Prognose gibt es
  `None` — lieber kein Overlay als ein altes.
- **Gebündelte Lovelace-Karten:** `_frontend.async_register_frontend` serviert und registriert
  `custom:balcony-shade-profile-card` und `custom:balcony-power-history-card` (Dateien unter
  `custom_components/balcony_solar_forecast/frontend/`) — kein HACS nötig. Das generierte Dashboard nutzt sie
  anstelle des optionalen ApexCharts-Snippets; Details in §8.1.
- **Repair-Issues:** die Integration meldet u. a. `fast_learner_auto_disabled`, `slow_learner_auto_disabled` und
  `config_changed_bias_reseed` (Geometrieänderung → Bias-Zellen neu geseedet) als HA-Reparaturhinweise.

### 8.1 Die zwei mitgelieferten Frontend-Karten (Einstieg)

Beide Karten liegen als fertige, abhängigkeitsfreie JS-Dateien im Repo
(Verzeichnis `custom_components/balcony_solar_forecast/frontend/`) und werden **mit der Integration**
ausgeliefert — HACS wird für sie nicht gebraucht:

| Datei | Card-Type | Was sie zeigt | Woher die Daten kommen |
|---|---|---|---|
| `shade_profile_card.js` | `custom:balcony-shade-profile-card` | Sonnenbahn des gewählten Tages mit eingefärbtem gelerntem τ, gelernte + statische Horizontlinie | Attribute des `shade_profile`-Sensors (§3.3); die kartenlokale Vergleichskurve über die Aktion `get_shade_profile` |
| `power_history_card.js` | `custom:balcony-power-history-card` | gestapelte Stunden-Balken je Modul (gemessenes DC) + gestrichelte Prognoselinie (AC) | `recorder/statistics_during_period` (WebSocket) für die Balken, `wh_period` des Heute-Sensors bzw. `get_issued_forecast` für die Linie vergangener Tage |

**Registrierung** (`_frontend.py`, einmal aus `async_setup` heraus über `async_register_frontend`): beide Dateien
werden in **einem** `async_register_static_paths`-Aufruf unter dem gemeinsamen Präfix
`/balcony_solar_forecast/frontend/<datei>.js` mit `cache_headers=True` serviert. Danach — und nur bei
**Storage-Mode-Lovelace** — legt die Integration je Karte eine Lovelace-Ressource (`res_type: module`) an bzw.
aktualisiert sie; damit tauchen die Karten direkt im „Karte hinzufügen"-Picker auf (die JS-Dateien melden sich
zusätzlich selbst über `window.customCards` an). Läuft Lovelace im YAML-Modus, wird nichts angefasst, sondern die
Ressourcen-URL nur per INFO geloggt. Der ganze Pfad ist als *Enhancement* gebaut: jede Ausnahme wird geschluckt,
eine kaputte Karte darf `async_setup` nie blockieren.

**Cache-Busting läuft ausschließlich über die Versionsnummer.** Die Ressourcen-URL wird als
`…/<datei>.js?v=<INTEGRATION_VERSION>` registriert; die JS-Dateien selbst tragen **keinen** Versionsstring. Beim
Start sucht `_sync_one_resource` eine bestehende Ressource, deren URL mit dem Basispfad beginnt, und schreibt sie
auf die aktuelle Version um, sobald sie abweicht — ein Versionsbump in `const.INTEGRATION_VERSION` ist also die
einzige und zugleich hinreichende Maßnahme, damit Browser die neue Kartenversion ziehen. Wer eine Kartendatei
ändert, ohne die Version zu erhöhen, bekommt beim Betreiber weiter die alte aus dem Browser-Cache.

**Welche Datei fasst man wann an?**

- Verhalten/Aussehen einer Karte → die jeweilige `.js` (plus Versionsbump; für die Power-History-Karte gibt es die
  Node-Harness `tests/harness/power_card_harness.mjs`, gefahren von `tests/test_frontend_harness.py`).
- Layout des **generierten** Dashboards (Aktion `install_dashboard`) → `_dashboard.py`; nur dort sind die beiden
  `custom:`-Karten verdrahtet.
- Das **ausgelieferte Dashboard-YAML** `dashboards/balcony_solar_forecast.yaml` ist der Copy-&-Paste-Weg und
  bewusst **built-in-only** — kein `custom:`-Type darin (`tests/core/test_dashboard_yaml.py` wacht darüber).
  `_dashboard.py` spiegelt dessen View-/Karten-Inventar und ersetzt beim Generieren das optionale
  ApexCharts-Snippet (`dashboards/shade_profile_apexcharts.yaml`) durch die gebündelten Karten. Wer eine Karte
  ergänzt, muss deshalb in der Regel **beide** Stellen anfassen.
- Betreiber-Doku (Installation, Bedienung, Lesehilfe der Karten) → `docs/DASHBOARD.md`.

---

## 9. Unsicheres / bewusst offen

- **Entity-IDs** sind hier aus dem englischen Anzeigenamen (`translations/en.json`) abgeleitet und — **soweit dort
  überhaupt vorhanden** — gegen `dashboards/balcony_solar_forecast.yaml` gegengeprüft. Die beiden
  Übermorgen-Sensoren kommen in diesem YAML nicht vor, ihre IDs stammen rein aus `en.json`.
  Sie gelten für eine Erstinstallation ohne manuelle
  Umbenennung und ohne ID-Kollision (HA hängt sonst `_2` an). Autoritativ ist immer die Entity-Registry bzw.
  `unique_id = {entry_id}_{key}`.
- Die **`update`-Entity** existiert in diesem Repository nicht (§5.3); dass eine solche Entität in einer
  HACS-Installation auftaucht, ist plausibel, aber nicht aus diesem Code belegbar.
- Konkrete **Schwellenwerte** (`RLS_MIN_SAMPLES`, `DAY_AHEAD_BIAS_MIN/MAX`, `QUANTILE_MIN_SAMPLES/DAYS`,
  `SCOREBOARD_GATE_MARGIN`, Drift-Fenster) werden hier nur benannt, nicht beziffert — sie gehören zu
  `03-lernschichten-und-korrekturen.md` und stehen in `const.py`.
