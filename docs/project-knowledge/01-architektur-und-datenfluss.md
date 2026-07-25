# Architektur & Datenfluss

**Worum es geht:** Dieses Dokument ist der Code-Wegweiser für `balcony_solar_forecast`.
Es beantwortet: Was macht die Integration, welches Modul ist wofür zuständig, wie
fließen Daten vom Open-Meteo-Abruf bis zum Sensor-Attribut, und welche Begriffe
(RAW / CORRECTED / SERVED, DC / AC, slot / hour, issued) bedeuten im Code exakt was.
**Wann du es brauchst:** Als Erstes, wenn du im Repo arbeiten sollst und in fünf
Minuten wissen musst, wo etwas liegt. Physik-Details stehen in
`02-physik-und-horizontmodell.md`, die Lernschichten in
`03-lernschichten-und-korrekturen.md`, Entities/Services in
`04-ha-integration-entities-services.md`.

Stand: `main` @ **v0.23.0** (2026-07-25). Alle Aussagen unten sind am Code geprüft;
Belege werden als *Datei + Funktions-/Konstantenname* genannt (keine Zeilennummern,
die veralten sofort).

---

## 1. Was ist balcony_solar_forecast

Eine **eigenständige Home-Assistant-Custom-Integration** (HACS-installierbar), die
eine **15-Minuten-PV-Prognose für Balkonkraftwerke mit mehreren Fassaden-Ebenen**
rechnet und sich anhand der gemessenen Ist-Werte selbst korrigiert.

Fakten aus `custom_components/balcony_solar_forecast/manifest.json`:

| Feld | Wert | Bedeutung |
|---|---|---|
| `domain` | `balcony_solar_forecast` | Service-Namespace, Entity-Präfix |
| `version` | `0.23.0` | HACS-Release |
| `iot_class` | `cloud_polling` | holt aktiv von einer Cloud-API (Open-Meteo) |
| `integration_type` | `service` | kein Hub / keine Hardware-Bridge, sondern ein Dienst. Trotzdem gruppiert die Integration **alle** Entities unter EINEM Geräte-Eintrag je Config-Entry (`DeviceInfo` in `sensor.BalconyForecastEntity`, `docs/SPEC.md` §15.1) |
| `config_flow` | `true` | Einrichtung nur über die UI, kein YAML |
| `dependencies` | `http`, `lovelace` | für die mitgelieferten Frontend-Karten |
| `after_dependencies` | `recorder` | Ist-Werte kommen aus der Langzeitstatistik |
| `requirements` | **leer** | reine stdlib + HA-eigenes aiohttp; kein numpy/pandas/pvlib |

Kernidee (Begründung in `docs/SPEC.md` §3): Nicht die *Ausgaben* einer bestehenden
Prognose-Integration nachkorrigieren, sondern die **Rohstrahlungskomponenten**
(GHI/DNI/DHI) aus einem einzigen Open-Meteo-Call holen, die **Transposition auf jede
Ebene lokal** rechnen (mit Horizont- und Diffus-Behandlung) und **dort lernen, wo die
Information liegt**: pro Messkanal (MPPT-Port), pro Sonnenstand.

Die harte Architekturgrenze: `core/` importiert **nichts** aus `homeassistant` — reine
Funktionen über frozen dataclasses, mit bloßem pytest testbar. Der HA-Glue
(Coordinator, Config-Flow, Plattformen) liegt eine Ebene darüber. Eine einzige,
dokumentierte Ausnahme: `core/openmeteo_backfill.py` macht Netzwerk-IO (siehe unten).

---

## 2. Modul-Landkarte

### 2.1 HA-Glue: `custom_components/balcony_solar_forecast/`

| Modul | Verantwortung | Wichtigste Einstiegsfunktion |
|---|---|---|
| `__init__.py` | Reines Plumbing: aiohttp-Session, Store und Coordinator verdrahten, Plattformen forwarden, sauber entladen. Registriert Services + Frontend **entry-unabhängig**. | `async_setup`, `async_setup_entry` |
| `coordinator.py` | Das Herz (größte Datei). Fetch-Timer, 15-min-Recompute, Degradationsleiter, Lerner-Hooks binden, Coordinator-`data` bauen, Nightly-Job planen. Delegiert schwere Teilaufgaben an die `_*`-Module. | `BalconySolarCoordinator._async_update_data`, `_build_data`, `_build_learner_hooks` |
| `fetcher.py` | Open-Meteo-Client + Payload-Parsing. Pure Teile (URL-Bau, Shape-Validierung, Parse) sind ohne aiohttp importierbar; aiohttp wird erst in der Netz-Coroutine geladen. Auch der Ensemble-Endpunkt. | `build_params`, `validate_payload`, `parse_weather`, `OpenMeteoFetcher` |
| `store.py` | Versionierter Persistenzspeicher (ein HA-`Store` je Config-Entry). Migration v1→v2→v3, **validate-and-clamp beim Laden** (korrupt ⇒ neutral, nie Setup-Crash), gebündelte Schreibvorgänge. | `ForecastStore`, `validate_state`, `_migrate_*` |
| `_nightly.py` | Nächtlicher Trainings- und Guard-Lauf (~01:30 lokal, idempotent, datumsschlüsselig): Snapshot, Actuals, Rollback-Punkt, Collapse-Detektor, Training, Drift-Monitor. | `async_nightly_job`, `train_and_guard` |
| `_actuals.py` | Liest die gemessene DC-Energie eines abgeschlossenen Tages je Modul aus der Recorder-Langzeitstatistik und wendet die Label-Gates an (Messkanal-Dropout ⇒ ganzer Tag verworfen). | `async_read_actuals`, `async_read_ac_actuals` |
| `_scoreboard_glue.py` | Leak-freies IO rund um die pure Scoreboard-Mathematik: Engine-Wert **as issued**, Messwert, Vergleichs-Entities *wie sie damals standen*. | (Modulfunktionen, aufgerufen über `coordinator._score_scoreboard_day`) |
| `_bootstrap.py` | Re-Bootstrap der Lernzustände **in-process** aus ~2 Jahren Historie (die `run_bootstrap`-Action). Kein Token, keine `site.json`, kein Dev-Rechner. | `async_run_bootstrap` |
| `_services.py` | Registrierung aller Integrations-Actions: `get_forecast`, `import_bootstrap`, `run_bootstrap`, `rollback_learners`, `reset_day_ahead_bias`, `dump_shademap`, `install_dashboard`, `suggest_shade_groups`, `get_shade_profile`, `get_issued_forecast`. | `async_register_services` |
| `_glue_util.py` | Geteilte kleine Helfer für Coordinator + die `_*`-Module: ISO-/Stunden-Keys, 15-min-Slot-Walk, lokale Kalendertag-Rollups, Ist-Wert-Zustandsprüfung. | `_power_now`, `_local_daily_kwh`, `_usable_power` |
| `_dashboard.py` | Pure Builder für das generierte Observability-Lovelace-Dashboard (mappt die **echten** Entity-IDs der Installation). | `collect_entity_map`, `build_dashboard_config` |
| `_frontend.py` | Liefert die zwei mitgelieferten Lovelace-Karten (`frontend/shade_profile_card.js`, `frontend/power_history_card.js`) als Static Path aus und registriert sie im Storage-Mode automatisch als Resource. | `async_register_frontend` |
| `_site_validation.py` | HA-freie Validierung des Config-Flow-`site`-Objekts (Azimut 0..360, Neigung 0..90, wp>0, tau 0..1). Horizontzeilen werden **nicht abgelehnt**, sondern stabil nach aufsteigendem Azimut sortiert und in dieser kanonischen Form zurückgegeben (Voraussetzung für die lineare Interpolation in `core/horizon.py`; der Config-Flow persistiert die sortierte Form). Nur echte Wertverletzungen werfen `SiteValidationError` mit Übersetzungs-`code`. | `validate_site` |
| `config_flow.py` | Config- und Options-Flow. Ein Config-Entry je benanntem Standort; das gesamte `site` ist ein editierbares Objekt mit `const.DEFAULT_SITE` als Vorgabe. | `async_step_user`, Options-Flow-Schritte |
| `sensor.py` | Sensor-Plattform: Tages-kWh (heute/morgen/d2), Momentanleistung, DC-Diagnose-Pendants, Degradations- und Lerner-Diagnose, Scoreboard-MAE je Vergleich. Baut auch die `get_forecast`-Antwort. | `async_setup_entry`, `_build_forecast_response` |
| `binary_sensor.py` | Vier Entities: ein `degraded`-Problem-Sensor (Degradationsleiter sichtbar), zwei `*_learner_active`-Diagnosen (fast/slow) und der `kill_gate_passed`-Sensor (`KillGatePassedSensor`, Scoreboard-Urteil). Alle bleiben verfügbar, auch wenn die Prognose es nicht ist. | `async_setup_entry` |
| `select.py` | Auswahl des Moduls für das Verschattungs-Diagramm (über Neustart hinweg gemerkt). | `async_setup_entry` |
| `date.py` | Auswahl des Datums für das Verschattungs-Diagramm (**nicht** persistiert, öffnet immer auf heute). | `async_setup_entry` |
| `diagnostics.py` | Diagnose-Download für Bug-Reports: Entry, Degradationszustand, Prognose-Zusammenfassung, Lerner-/Scoreboard-/Quantil-/Ensemble-/Store-Summary. Koordinaten werden redigiert. | `async_get_config_entry_diagnostics` |
| `energy.py` | Hook fürs HA-Energie-Dashboard: liefert `{"wh_hours": …}` aus dem **AC**-Stunden-Rollup (`hourly_wh_ac`). | `async_get_solar_forecast` |
| `recorder.py` | Hält die dicken Kurven-Attribute (`watts`, `wh_period`, …) aus der Recorder-Historie heraus; Live-State bleibt unberührt. | `exclude_attributes` |
| `const.py` | Single Source of Truth für Domain, Config-Keys, Defaults, Store-Keys, Tunables und das mitgelieferte Betreiber-Referenz-Site. Azimut-Konvention **0 = Nord, im Uhrzeigersinn**. | (nur Konstanten) |

### 2.2 Reiner Kern: `custom_components/balcony_solar_forecast/core/`

| Modul | Verantwortung | Wichtigste Einstiegsfunktion |
|---|---|---|
| `types.py` | Alle unveränderlichen Datenverträge (frozen dataclasses): `SiteConfig`, `PlaneConfig`, `InverterGroup`, `HorizonRow`, `WeatherSlot`/`WeatherSeries`, `PlaneResult`, `ForecastResult`, sowie die Lern-/Scoreboard-/Quantil-Zustände. Jeder `from_dict` ist validate-and-clamp. | `ForecastResult`, `SiteConfig.from_dict` |
| `engine.py` | Die Pipeline: solpos → transpose → horizon → electrical über jeden Slot und jede Ebene, danach Gruppen-Clamp und Rollups. Rechnet **beide** Kurven (RAW und CORRECTED) pro Zyklus. | `compute_forecast`, `LearnerHooks` |
| `solpos.py` | Sonnenstand (NOAA-Closed-Form nach Meeus, nur `math`), inkl. Refraktionskorrektur; Genauigkeitsziel < 0,3°. | `sun_position`, `hours_from_solar_noon` |
| `transpose.py` | Hay-Davies-Transposition auf die Ebene (Beam + Zirkumsolar + isotroper Rest + Bodenreflex). Liefert die *rohen* geometrischen Komponenten; Skalierung/Maskierung macht die Engine. | `hay_davies_poa` |
| `horizon.py` | Pro Ebene: Beam-Maskierung an der Horizontlinie und **Sky-View-Faktor** für den isotropen Diffusanteil; saisonale Laub-Rampe über eine Kosinus-Blende. | `interp_elevation`, `transmittance_at`, `sky_view_factor` |
| `clearsky.py` | Haurwitz-Clear-Sky-GHI und Clear-Sky-Index `k_c` — ausschließlich als Lern-Gate und Normalisierer, **nie** als Prognosequelle. | `haurwitz_ghi`, `clear_sky_index` |
| `electrical.py` | DC-Leistung (Ross-Zelltemperatur, Temperaturkoeffizient) und der Wechselrichter-Clamp je Gruppe — DC-seitig und DC→AC. | `dc_power`, `clamp_groups`, `clamp_groups_ac` |
| `bias.py` | **Schneller Lerner**: (1) transienter Intraday-Skalar im Clear-Sky-Index-Raum (nie persistiert), (2) persistierter Day-Ahead-RLS-Bias je (Wolkenklasse × Tagesabschnitt). | Live-Pfad: `compute_intraday_scalar` + `intraday_factor_at`; `day_ahead_factor_solar`, `classify_cloud`. `apply_intraday_scalar` / `apply_day_ahead_bias` sind Hilfsfunktionen auf fertigen Stunden-Wh-Dicts, **nicht** der Engine-Pfad |
| `shademap.py` | **Langsamer Lerner**: geometrisches Beam-Transmissionsfeld je Messkanal × (Sonnenazimut-Bin × Elevations-Bin × Halbjahr), EMA über `T = (P_gemessen − P_diffus_modelliert) / P_beam_modelliert`. Gespeichert wird je Modul-Kanal, gepoolt wird ausschließlich beim **Lesen** über die `shade_group`-Geschwister (`docs/SPEC.md` §5). | `effective_tau_pooled` (Live-Lesepfad), `effective_tau` (Einzelkanal), `update_bin` / `beam_referenced_t` (Training) |
| `shadeprofile.py` | Daten hinter dem Verschattungs-Diagramm: Sonnenbahn vs. gelernte Verschattung für eine Ebene an einem Datum. Repliziert das Engine-Gate exakt. | `compute_shade_profile` |
| `quantiles.py` | P10/P50/P90-Bänder per nichtparametrischer historischer Simulation: 90-Tage-Ring relativer Fehler je (Wetterklasse × Tagesabschnitt). | `bands_for_bin` |
| `ensembleband.py` | Wandelt Open-Meteo-Ensemble-Member-GHI in eine **relative** Stundenspreizung und verschmilzt sie per Hüllkurven-Maximum mit den gelernten Bändern (nie multiplizieren, nie verengen). | `ensemble_band_factors`, `fuse_bands` |
| `scoreboard.py` | Das **Kill-Gate**: Tagesfehler-Scoring, rollierendes Fenster, Urteil gegen Vergleichsprognosen. Reine Fehlermathematik — das leak-freie IO macht der Glue. | Scoring-/Aggregationsfunktionen (siehe `_scoreboard_glue.py`) |
| `inverter_cal.py` | Lernt **einen** standortweiten Skalar `eta_inv` gegen den AC-Gesamtzähler. Nie tragend: unter `INVERTER_CAL_MIN_SAMPLES` liefert `effective_eta` `None` und die Engine nimmt die Konfig-/Default-Effizienz. | `effective_eta` |
| `bootstrap_build.py` | HA-freier Kern des Lerner-Bootstraps: macht aus ~2 Jahren Historie einen Warmstart für Bias, Shademap und Quantil-Ring. Keine Netz-IO — die Datenbeschaffung liegt in den Aufrufern. | Rekonstruktions-/Akkumulator-Funktionen |
| `openmeteo_backfill.py` | **Die einzige `core/`-Datei mit Netzwerk-IO** (bewusste, dokumentierte Ausnahme): Open-Meteo *Previous-Runs* / Historical-Forecast-Abruf mit injizierter aiohttp-Session, damit CLI und In-Process-Action nicht auseinanderdriften. Bleibt HA-frei. | Fetch-Funktion mit `session`-Parameter |

Außerhalb des Komponenten-Ordners: `scripts/backfill.py` (Dev-CLI-Wrapper um
`bootstrap_build`; verpflichtend sind nur `--ha-url`/`--token` — ein Long-Lived-Token —
sowie `--start`/`--end`. `--site` ist **optional** und fällt sonst auf das mitgelieferte
`const.DEFAULT_SITE` zurück; wer die Site im Config-Flow geändert hat, MUSS sie
exportieren und `--site` übergeben, sonst rekonstruiert das Skript still gegen die
falsche Geometrie), `scripts/validation/` (Validierungslauf gegen Live-Daten), `tests/core/` (pure
pytest-Golden-Tests) und `tests/` (HA-Glue-Tests). Details in
`07-entwicklung-tests-release.md`.

---

## 3. Kernbegriffe

### 3.1 RAW vs. CORRECTED vs. SERVED

Die Engine rechnet **jeden Zyklus beide Kurven** (`core/engine.py`, Docstring von
`compute_forecast`):

- **RAW** — reine Physik, Lerner aus. Die statische Horizont-Transmittanz gated den
  Beam. Felder: `raw_total_watts`, `raw_hourly_wh`, `raw_daily_kwh`, je Ebene
  `PlaneResult.raw_watts`.
- **CORRECTED** — Lerner an. Zwei Einhak-Punkte (siehe `LearnerHooks`):
  1. `beam_tau` ersetzt an der **Transpositions**-Stufe die statische Transmittanz
     (langsamer Lerner / Shademap),
  2. `slot_factor` skaliert an der **Aggregations**-Stufe die bereits geclampte
     Slot-Leistung (schneller Lerner: Intraday-Skalar ∘ Day-Ahead-Bias).
  Felder: `total_watts`, `hourly_wh`, `daily_kwh`, je Ebene `PlaneResult.watts`.
- **SERVED** — kein eigenes Feld, sondern die Sprechweise für *das, was die
  Integration tatsächlich ausliefert*. Auf DC-Seite ist SERVED == CORRECTED nach dem
  **zweiten** Clamp; auf AC-Seite ist SERVED die `ac_*`-Kurve. Wichtig: die
  Haupt-Power-/Energie-Sensoren lesen die **AC**-Keys, während Lernen und Scoreboard
  auf der **DC**-Wahrheit arbeiten.

Ohne Hooks (`hooks=None` oder alle Callables `None`) ist CORRECTED **bit-genau** gleich
RAW — ein lernerfreier Build ist unverändert.

### 3.2 issued / as-issued-Snapshot

**Issued** = die Prognose, *wie sie an jenem Tag ausgegeben wurde*. Der Nightly-Job
schreibt einmal pro Kalendertag einen `IssuedSnapshot` (`core/types.py`) in den
Issued-Ring des Stores. Er trägt beide Stundenkurven (`raw_hourly_wh`,
`corrected_hourly_wh`), beide Tages-Rollups, die pro Ebene modellierten Komponenten
(`per_plane`, Typ `PlaneHourlyModeled`: `beam_wh`, `diffuse_wh`, `kc` — das Feld `ghi`
existiert im Datentyp, wird vom Coordinator aber **nie befüllt** (`_nightly.per_plane_modeled`
setzt `ghi={}`) und deshalb von `PlaneHourlyModeled.to_dict` gar nicht erst serialisiert;
wer im Issued-Ring nach GHI sucht, findet nichts), die Wolkenklasse je Stunde, optional die
Slow-only-Kurve für die Drift-Attribution und das zur Ausgabezeit gültige `eta`.
`version = 2`.

Der Sinn ist **Leckagefreiheit**: Scoreboard und Drift-Monitor bewerten *nie* eine mit
dem heutigen Lernstand neu gerechnete Kurve, sondern genau den damals ausgegebenen
Snapshot. Deshalb steht in `core/scoreboard.py` ausdrücklich: hier wird die Prognose
**nicht** neu berechnet.

### 3.3 slot vs. hour

- **Slot** = 15-Minuten-Intervall (`const.SLOT_MINUTES = 15`). Ein `WeatherSlot`-Wert
  ist ein **Intervall-Mittel** über `[start, start+15min)`; die Sonnenposition wird am
  **Mittelpunkt** gerechnet (`WeatherSlot.midpoint`, +7:30 min). Alle Leistungs-Tupel
  im `ForecastResult` sind an `slot_starts` (tz-aware UTC) ausgerichtet.
- **Hour** = ISO-8601-UTC-Stundenanfang als String-Key in den `*_hourly_wh`-Dicts.
  Energie-Rollup, nicht Leistung.
- **Tag** = ISO-Datum in der **lokalen** Kalenderzeitzone (`hass.config.time_zone` wird
  als `tz` an `compute_forecast` gereicht), damit „heute/morgen/d2" der Mitternacht des
  Betreibers folgen.

### 3.4 DC vs. AC — welche Kurve trägt was

| | DC-Pfad | AC-Pfad |
|---|---|---|
| Felder | `total_watts`, `hourly_wh`, `daily_kwh`, `plane_watts` | `ac_watts`, `ac_hourly_wh`, `ac_daily_kwh` |
| Clamp | `electrical.clamp_groups` (zweimal, s. u.) | `electrical.clamp_groups_ac` |
| eta greift | **nirgends** — DC ist eta-frei | einmal, in `clamp_groups_ac`: `AC = min(eta_inv · Σ DC, ac_limit_w)`; DC-Clip bei `ac_limit_w / eta_inv` |
| eta-Quelle | – | `LearnerHooks.inverter_efficiency` (gelernt, nur wenn vertrauenswürdig) überschreibt sonst `InverterGroup.inverter_efficiency`, sonst `const.DEFAULT_INVERTER_EFFICIENCY = 0.965` |
| Rolle | **Lern- und Scoreboard-Wahrheit** (Kill-Gate, Shademap, Bias, Drift) | **operatorseitiger Standard**: Haupt-Sensoren, Energie-Dashboard |

Der DC-Pfad ist absichtlich byte-identisch zum Vor-AC-Stand geblieben; AC ist eine
*zusätzliche*, physikalisch korrekte Kurve, gespeist aus dem korrigierten
**unclamped** DC.

Achtung — `PlaneConfig.efficiency` (Modul-/DC-Wirkungsgrad) und
`InverterGroup.inverter_efficiency` (DC→AC-Wandlung) sind zwei verschiedene Dinge.

### 3.5 Die zwei DC-Clamps

Reihenfolge pro Slot in `compute_forecast`:

```
clamp_groups(raw_unclamped)  -> raw_clamped
clamp_groups(cor_unclamped)  -> cor_clamped          (1. Clamp)
cor_factored = cor_clamped * slot_factor(start)      (schneller Lerner)
clamp_groups(cor_factored)   -> cor_final            (2. Clamp / "Re-Clamp")
```

Der Faktor sitzt **zwischen** den Clamps, damit eine Aufwärtskorrektur (Faktor > 1) die
ausgelieferte Kurve nie über das AC-Limit heben kann. Ein Faktor ≤ 1 lässt die Werte
im Limit ⇒ der Re-Clamp ist ein mathematisches No-Op (bit-genau). Ebenen ohne
Wechselrichtergruppe haben keine Decke und passieren beide Clamps unverändert.

### 3.6 ForecastResult — die wichtigsten Felder

Definiert in `core/types.py`. Alle additiven Felder haben leere Defaults, damit ältere
gecachte Ergebnisse weiter funktionieren.

| Feld | Inhalt |
|---|---|
| `slot_starts` | tz-aware UTC-Slotanfänge; **alle** Leistungs-Tupel sind daran ausgerichtet |
| `total_watts` | CORRECTED/SERVED DC-Standortsumme je Slot |
| `raw_total_watts` | reine Physik, DC-Standortsumme |
| `plane_results` | Tupel `PlaneResult` (siehe unten) |
| `hourly_wh` / `daily_kwh` | CORRECTED-Rollups (ISO-UTC-Stunde bzw. ISO-Datum in `tz`) |
| `raw_hourly_wh` / `raw_daily_kwh` | RAW-Rollups |
| `corrected_unclamped_watts` | Standortsumme **vor** dem 2. Clamp. `corrected_unclamped[i] − total_watts[i] > 0` ⇒ der Re-Clamp hat gegriffen |
| `slot_ceilings` | physikalische DC-Clamp-Decke je Slot (Summe der Gruppenlimits + korrigierte Watt der gruppenlosen Ebenen) |
| `ac_watts` / `ac_hourly_wh` / `ac_daily_kwh` | ausgelieferte AC-Kurve und ihre Rollups |
| `ac_corrected_unclamped_watts` / `ac_slot_ceilings` | AC-Pendants zu den beiden Feldern oben |
| `p10_watts` / `p50_watts` / `p90_watts` | Bandkurven je Slot, gleicher Rahmen wie `total_watts`. Leer ⇒ „kein Band" (Konsumenten lesen Band == corrected, **keine** erfundene Spreizung). `p50_watts` muss **nicht** `total_watts` entsprechen |
| `p10_hourly_wh` / `p50_hourly_wh` / `p90_hourly_wh` | Stunden-Rollups der Bänder |
| `ac_p10_watts`, `ac_p10_hourly_wh`, `ac_p90_hourly_wh` | AC-Bänder (P50 == `ac_watts`, deshalb kein eigenes AC-P50-Feld) |
| `correction_source` | welche Lernschicht(en) die Kurve geformt haben (`const.CORRECTION_SOURCE_NONE/INTRADAY/SHADEMAP/BOTH`) — rein informativ |

`PlaneResult` je Ebene: `name`, `watts` (CORRECTED), `raw_watts`, `beam_watts` /
`diffuse_watts` (modellierte DC-Anteile, **nach** proportionaler Rückverteilung des
Clamps), `kc` (Clear-Sky-Index als Gate) sowie `beam_ref_watts` / `diffuse_ref_watts`
— die **ungegatete, unclamped, faktorfreie** Referenz, gegen die der Shademap trainiert
(sonst wäre das gelernte T selbstreferenziell).

Bewusst **nicht** im `ForecastResult`: die stündliche Pro-Ebene-Aggregation. Die baut
der Coordinator (`_per_plane_modeled`) für den Issued-Snapshot, damit der eingefrorene
Ergebnis-Vertrag minimal bleibt.

---

## 4. Datenfluss end-to-end

### 4.1 Der Live-Loop (alle 15 min)

Der Coordinator läuft mit `update_interval = recompute_interval_seconds`
(`const.RECOMPUTE_INTERVAL_SECONDS = 900`, konfigurierbar). Der Fetch hat einen
eigenen, langsameren Takt: `const.FETCH_INTERVAL_SECONDS = 1800`.

1. **Fetch (bedingt)** — `_due_for_fetch` prüft den 30-min-Takt; falls fällig
   `_async_try_fetch` → `fetcher.OpenMeteoFetcher`. Ein Call an
   `https://api.open-meteo.com/v1/forecast`, Modell fix `icon_seamless`, `timezone=UTC`,
   `FORECAST_DAYS = 4`. Geholt werden `minutely_15`: `shortwave_radiation` (GHI),
   `direct_normal_irradiance` (DNI), `diffuse_radiation` (DHI), `temperature_2m`; plus
   `hourly`: Wolken tief/mittel/hoch, Sichtweite, Schneefall, Schneehöhe. **Kein**
   serverseitiges GTI — transponiert wird lokal. Erfolgreiche Payloads landen als
   „last-good" im Store (schreibratenbegrenzt über
   `PAYLOAD_MIN_SAVE_INTERVAL_SECONDS = 6h`).
2. **WeatherSlot-Serie** — `fetcher.parse_weather` erzeugt eine `WeatherSeries` aus
   15-min-`WeatherSlot`s; die Stundenfelder werden auf die Slots vorwärtsgetragen. Der
   Parse wird gecacht, solange dasselbe Payload-Objekt bedient wird
   (`coordinator._cached_weather`).
3. **Degradationsleiter** — `_status_for_age`:
   `fresh` (letzter Fetch ok und jünger als das Fetch-Intervall) → `cached`
   (≤ `MAX_PAYLOAD_AGE_HOURS = 24`) → `physics_fallback`
   (≤ `MAX_PHYSICS_FALLBACK_AGE_HOURS = 72`) → `unavailable` (darüber; `UpdateFailed`).
4. **Ensemble (optional)** — separat und eigenständig abgesichert
   (`ENSEMBLE_URL`, `ENSEMBLE_FETCH_INTERVAL_S = 3h`). Jeder Fehler fällt auf die
   gelernten Bänder zurück; die Hauptleiter aus Schritt 3 wird davon **nie** berührt.
5. **Schneller Lerner (Intraday)** — `_update_intraday_scalar` liest die konfigurierten
   `actual_entity`-Zustände (mit Guards gegen unknown/unavailable/stale) und
   aktualisiert den transienten Skalar. Nach einem Neustart gibt es einen einmaligen
   Re-Arm des Sample-Rings aus der Recorder-Statistik. Ein Fehler hier ist **nie**
   fatal.
6. **Hooks binden** — `_build_learner_hooks` prüft die Kill-Switches, Drift-Flags und
   den Collapse-Freeze und bindet über `coordinator._bind_beam_tau` eine Closure auf
   `shademap.effective_tau_pooled` in `beam_tau`: die Shademap wird je **Modul-Kanal**
   gespeichert, beim **Lesen** aber über die `shade_group`-Geschwister gepoolt
   (Pool-Map aus `coordinator._build_shade_pool_map`, `docs/SPEC.md` §5) — eine Ebene
   ohne Gruppe liest nur ihren eigenen Kanal und verhält sich bit-identisch zum
   Vor-Gruppen-Stand. (Der Docstring von `engine.LearnerHooks` nennt noch das alte
   `effective_tau`; maßgeblich ist der Code in `_bind_beam_tau`.) Dieselbe Closure
   nutzt der nächtliche Slow-only-Attributionslauf (`_slow_only_hourly`), damit die
   Bindung nicht auseinanderlaufen kann. Danach komponiert der Hook-Bau Intraday-Skalar
   und Day-Ahead-RLS-Bias in **einen** `slot_factor` je Slot, baut die
   `band_by_slot`-Map und setzt ggf. die gelernte `inverter_efficiency`.
7. **Engine-Recompute** — `_compute` → `core.engine.compute_forecast(site, weather, now,
   tz, hooks=…)`. Pro Slot und Ebene: Sonnenstand am Mittelpunkt → schneeabhängige
   Bodenalbedo (`_slot_albedo`, Schwelle `SNOW_DEPTH_THRESHOLD_M`, sonst
   `SiteConfig.albedo`) — sie ist **keine** nachgelagerte Stufe, sondern geht als
   Parameter `albedo=` in `hay_davies_poa` und speist dort den Bodenreflex-Term →
   `hay_davies_poa` → IAM (`transpose.ashrae_iam`) auf Beam+Zirkumsolar → optionaler
   `bifacial_beam_gain` → Horizont-Gate auf Beam+Zirkumsolar (statisch = RAW /
   `beam_tau` = CORRECTED) → Sky-View-Faktor auf den isotropen Diffus (Bodenreflex
   bleibt ungegatet) → `dc_power` (Ross-Zelltemperatur, Temperaturderating)
   → 1. Gruppen-Clamp → `slot_factor` → 2. Gruppen-Clamp. Anschließend AC-Transform
   (`clamp_groups_ac`) und die Rollups auf Stunden-Wh und Tages-kWh.
8. **`_build_data`** — formt das Coordinator-Payload (siehe 4.2).
9. **Plattformen** — Sensoren/Binary-Sensoren lesen ausschließlich diese Keys; die
   `get_forecast`-Action baut ihre Antwort aus demselben Payload
   (`sensor._build_forecast_response`).

### 4.2 Coordinator-`data`: die wichtigsten Keys

Aufgebaut in `coordinator._build_data`. v0.1-Keys sind bewusst unverändert; alles
Spätere ist additiv und meist über `const.DATA_KEY_*` benannt.

| Key | Inhalt |
|---|---|
| `status`, `degraded`, `weather_age_seconds`, `last_error` | Degradationsleiter |
| `power_now_w` | DC-Momentanleistung (gerundet) |
| `energy_today_kwh` / `_tomorrow` / `_d2` | Tages-Headline **DC** |
| `watts`, `wh_period` | 15-min-DC-Kurven, `{iso_slot: W}` bzw. `{iso_slot: Wh}` |
| `hourly_wh`, `daily_kwh`, `slot_starts`, `plane_watts`, `computed_at` | DC-Rollups, Achse, Kurve je Ebene |
| `power_now_w_ac`, `energy_today_kwh_ac` / `_tomorrow_ac` / `_d2_ac`, `watts_ac`, `wh_period_ac`, `hourly_wh_ac`, `daily_kwh_ac` | die **AC**-Geschwister; hiervon leben die Haupt-Sensoren und `energy.async_get_solar_forecast` |
| `raw_hourly_wh`, `corrected_hourly_wh` | RAW- vs. CORRECTED-Stundenkurve |
| `intraday_scalar`, `correction_source` | aktueller Skalar, wirksame Lernschicht |
| `learner_status`, `bias_cells`, `drift_mae` | Lerner-Diagnose |
| `scoreboard`, `kill_gate_passed` | rollierendes Fenster + Kill-Gate-Urteil (`None` = Fenster noch zu kurz) |
| `quantile_curves`, `quantile_curves_ac`, `band_source_by_day` | die Bandkurven (DC/AC) und die Herkunft je Tag — **nur vorhanden**, wenn dieser Zyklus wirklich Bänder ausgegeben hat |
| `band_source` | **immer** vorhanden (`learned` / `ensemble` / `envelope`), Default `learned`. Achtung: `learned` heißt auch „Ensemble aus oder ohne Beitrag" — der Key sagt also **nichts** darüber aus, ob überhaupt eine Spreizung existiert; dafür ist `quantile_curves` der Indikator |
| `energy_today_kwh_ac_p10` | AC-P10-Tages-Headline mit asymmetrisch herausgerechnetem Intraday-Faktor |

**Fallstrick `energy_today_kwh`.** Das ist bewusst eine **stabile Day-Ahead-Erwartung**,
kein Nowcast: der transiente Intraday-Faktor wird für die Headline wieder
herausdividiert (`_dayahead_today_kwh`), während er in der ausgelieferten
`watts`/`wh_period`-Kurve **bleibt**. Am laufenden Tag gilt also absichtlich
`energy_today_kwh != Σ heutiger wh_period`; für morgen/d2 stimmen sie exakt überein.
Auf einem Slot, an dem der Re-Clamp gegriffen hat, wird nicht dividiert, sondern
`corrected_unclamped/factor` bei `slot_ceilings[i]` gedeckelt — sonst würde die
Headline unter einem großen Skalar aufgeblasen bzw. unterschätzt.

### 4.3 Der Nightly-Job (~01:30 lokal)

Geplant über `coordinator.async_start_nightly_job`; `async_startup_catchup` holt beim
Start bis zu `NIGHTLY_CATCHUP_MAX_DAYS = 3` verpasste Tage nach. Alles idempotent und
datumsschlüsselig (`store.is_day_trained` / `mark_day_trained`). Ablauf in
`_nightly.async_nightly_job`:

1. **Issued-Snapshot** für heute schreiben (v2, beide Kurven + `per_plane`).
2. Für jeden abgeschlossenen Tag der Catch-up-Liste: **Actuals lesen**
   (`_actuals.async_read_actuals` aus der Recorder-Langzeitstatistik, im
   Recorder-Executor) und in Tages- und Stundenring ablegen. Ein Tag, der das
   Frozen-Channel-Gate reißt, wird **nicht** aufgezeichnet, damit ein späterer Lauf ihn
   nachholen kann.
3. `train_and_guard(day)`: Rollback-Snapshot ziehen → **Collapse-Detektor** (alle Kanäle
   ≈ 0 bei hoher Prognose ⇒ Schnee/Totalausfall: beide geometrischen Lerner für den
   **Folgetag** einfrieren, den Kollapstag selbst nicht trainieren) → Day-Ahead-RLS und
   Shademap unter den Label-Gates trainieren → Quantil-Ring bestücken → **Drift-Monitor**
   (rollierender MAE, Verlust-Streak, Auto-Disable einer Schicht).
4. **Scoreboard** für den Tag (`_score_scoreboard_day`, leak-frei aus dem Issued-Ring).
5. **Wechselrichter-Kalibrierung** (`_train_inverter_cal`), falls ein AC-Zähler
   konfiguriert ist.

Die Schritte 3–5 sind einzeln abgesichert: ein Fehler in einem bricht die anderen nicht
ab und lässt HA nie abstürzen. Als *trainiert* markiert wird ein Tag nur, wenn Issued
**und** Actuals vorlagen.

### 4.4 Bootstrap-Pfad (Warmstart aus Historie)

Zwei Aufrufer, **ein** gemeinsamer Kern:

- `scripts/backfill.py` (Dev-CLI): eigene aiohttp-Session, Long-Lived-Token
  (`--ha-url`/`--token`, dazu `--start`/`--end` — alle vier Pflicht), Actuals über die
  HA-WebSocket-API. `--site` ist **optional**: ohne Angabe rechnet das Skript gegen das
  mitgelieferte `const.DEFAULT_SITE`. Wer die Site im Config-Flow verändert hat, muss sie
  exportieren und übergeben, sonst rekonstruiert der Lauf gegen die falsche Geometrie
  (Optionstabelle in `docs/BACKFILL.md`).
- `_bootstrap.async_run_bootstrap` (die `run_bootstrap`-Action, seit v0.23.0): nutzt die
  Live-Konfiguration, die integrationseigene aiohttp-Session und einen In-Process-
  Recorder-Read. `dry_run` ist **standardmäßig `true`**; erst ein expliziter
  `dry_run: false` importiert. Serialisiert gegen den Nightly-Job über einen
  Bootstrap-Lock.

Beide beschaffen `HourlyWeather` über `core/openmeteo_backfill.py` und rekonstruieren
über `core/bootstrap_build.py`. Achtung auf die Epoch-Konvention: der In-Process-
Recorder-Read liefert `start` in **Sekunden**, der WebSocket-Weg in Millisekunden —
eine historisch fehlerträchtige Stelle (siehe `06-forensik-juli-2026-und-offene-punkte.md`).
Bedienung: `docs/BACKFILL.md`.

---

## 5. Store & Persistenz

Ein HA-`Store` je Config-Entry, Key `const.STORAGE_KEY = "balcony_solar_forecast.data"`.

- **Envelope-Version** `const.STORAGE_VERSION = 1` — *für immer gepinnt*.
- **Inneres Schema** `schema_version`, aktuell `STORAGE_DATA_VERSION_V3 = 3`.
  Migration v1→v2→v3 ist **rein additiv**: alte Abschnitte werden byte-treu
  durchgereicht, neue mit neutralen Defaults injiziert. Eine Migration, die
  vorhandenen Lernzustand verwirft oder zurücksetzt, gilt als kritischer Fehler.
- **Laden ist validate-and-clamp**: ein korruptes oder unbekannt versioniertes Blob
  degradiert zu neutralen Faktoren (1.0 / leere Bins) mit Warnung — es wirft **nie** in
  das Setup hinein.
- **Schreiben** gebündelt über `async_delay_save`
  (`STORAGE_SAVE_DELAY_SECONDS = 300`, eMMC-schonend), mit explizitem Flush beim
  Entladen und beim HA-Stop.

| Store-Key (`const.STORE_KEY_*`) | Inhalt | Ring |
|---|---|---|
| `last_payload` | `{"fetched_at": iso, "payload": {…}}` — letzter guter Open-Meteo-Body | 1 |
| `forecast_issued_log` | `{iso_date: IssuedSnapshot-Dict (v1 oder v2)}` | 90 Tage |
| `daily_actuals_log` | `{iso_date: {modul: Wh}}` gemessene DC-Energie | 90 Tage |
| `hourly_actuals_log` | `{iso_date: {kanal: {iso_hour: Wh}}}` | `HOURLY_ACTUALS_RING_DAYS = 14` |
| `bias_state` | Day-Ahead-RLS-Zellen — **der Intraday-Skalar wird nie persistiert** | – |
| `shademap_state` | Bins je Kanal | – |
| `drift_state` | rollierender MAE, Verlust-Streaks, Auto-Disable-Flags, `collapse_frozen_date` | – |
| `learner_snapshots` | Rollback-Ring, neuester zuletzt | `LEARNER_SNAPSHOT_RING = 10` |
| `trained_days` | sortierte ISO-Daten (Trainings-Idempotenz) | `TRAINED_DAYS_RING = 120` |
| `quantile_state` | `{bin_key: [relerr, …]}` | `QUANTILE_RING_DAYS = 90` |
| `scoreboard_state` | `{iso_date: DayScore}` | rollierendes Fenster |
| `comparison_ring` | `{iso_date: {vergleichsname: kWh}}` (Recorder-Lese-Cache) | – |
| `inverter_cal_state` | gelerntes `eta_inv` (neutral, wenn nicht vorhanden) | – |
| `config_fingerprint` | Fingerabdruck der Site-Konfiguration; eine relevante Geometrieänderung entwertet gelernten Zustand (`_reconcile_config_fingerprint`) | – |

**Rollback.** `_maybe_push_rollback_snapshot` legt einmal pro Nacht (datumsschlüsselig)
einen Vor-Trainings-`LearnerSnapshot` an. Die Ringtiefe von 10 liegt bewusst über
`DRIFT_LOSS_STREAK_DAYS = 7`, damit nach einem Auto-Disable noch ein sauberer
Zustand vor dem Streak erreichbar ist. Auslesen/Zurückrollen über die Action
`rollback_learners` (`coordinator.async_rollback_learners`).

Das **Bootstrap-Format** ist ein eigenes, vom Store getrenntes Schema
(`BOOTSTRAP_SCHEMA_VERSION = 1`) mit `bias_state`, `shademap_state`, `quantile_state`
und einer `site_signature` als Plausibilitätsprüfung.

---

## 6. Wo fange ich an? (Kurz-Rezepte)

| Frage | Erster Griff |
|---|---|
| Warum ist die Kurve zu hoch/niedrig? | `core/engine.py::compute_forecast` (Pipeline), dann `core/transpose.py` / `core/horizon.py` — siehe `02-physik-und-horizontmodell.md` |
| Warum korrigiert der Lerner so? | `coordinator._build_learner_hooks`, dann `core/bias.py` / `core/shademap.py` — siehe `03-lernschichten-und-korrekturen.md` |
| Woher kommt ein Sensorwert? | `coordinator._build_data` (Key) → `sensor.py` (Entity) |
| Warum ist die Prognose „degraded"? | `coordinator._status_for_age` + `binary_sensor.py` |
| Warum wurde ein Tag nicht trainiert? | `_nightly.train_and_guard` (Idempotenz-Gate) + `_actuals.async_read_actuals` (Label-Gates) |
| Was stand gestern in der Prognose? | Issued-Ring im Store, Action `get_issued_forecast` |
| Wie setze ich die Lerner neu auf? | Action `run_bootstrap` (`dry_run` zuerst) — `docs/BACKFILL.md` |
| Was ist der verbindliche Vertrag? | `docs/SPEC.md`; Änderungshistorie in `CHANGELOG.md`; Designentscheide in `docs/adr/` |
