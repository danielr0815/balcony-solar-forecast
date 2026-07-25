# Anlage & Betriebs-Runbook

Dieses Dokument beschreibt die **konkrete Anlage des Betreibers** (Geometrie,
Wechselrichter, Mess-Entitäten, Horizont-Realität) und die **Handgriffe im
Betrieb**: Update, Rekonfiguration, Lerner neu aufsetzen, Validierung, Rollback
und ein Diagnose-Kochbuch für die fünf häufigsten Symptome — gebraucht, wenn du
am laufenden System etwas änderst oder ein Verhalten einordnen musst. Das *Warum*
der Physik steht in `02-physik-und-horizontmodell.md`, das der Korrekturen in
`03-lernschichten-und-korrekturen.md`, die Entity-/Service-Referenz in
`04-ha-integration-entities-services.md`. Stand: `main` @ **v0.23.0**.

## 1. Die reale Anlage

Ein **Balkonkraftwerk** an einer Wohnung: 8 Module in zwei Reihen übereinander
(unterer/oberer Balkon) am Geländer montiert, also **sehr steil** (`tilt_deg`
70°/80° gegen die Horizontale, 90° = senkrecht). Drei Fassadenrichtungen,
jeweils 90° zueinander. **Standort** 48,547853° N / 12,187272° O (Raum
Landshut), so auch in `const.DEFAULT_SITE`. Eine **Zeitzone** kennt die
Integration nicht als eigenes Feld — sie nutzt durchgängig
`hass.config.time_zone` (beim Betreiber `Europe/Berlin`, UTC+1/+2). Der
**Azimut** ist intern überall **0° = Nord im Uhrzeigersinn** (180° = Süd) — und
zwar durchgängig: die Integration holt bei Open-Meteo nur die rohen
Strahlungskomponenten (GHI/DNI/DHI) und **transponiert lokal**
(`core/transpose.py`); `fetcher.build_params` sendet **keinen** Azimut/Tilt (nur
lat/lon, `minutely_15`, `hourly`, `models`, `forecast_days`, `timezone`). Eine
Umrechnung auf die Open-Meteo-GTI-Konvention (0=S) oder auf PVGIS findet im Code
**nicht** statt; PVGIS wird gar nicht abgefragt. Die abweichenden Konventionen in
SPEC Anhang A betreffen die historische rany2-Baseline bzw. manuell importierte
PVGIS-Horizonte. (Der Modul-Docstring von `config_flow.py` behauptet noch das
Gegenteil — veraltet.)

### 1.1 Module, Ausrichtung, Sensor-Zuordnung

Alle Werte aus `const.DEFAULT_SITE` (Referenz-Site = Anlage des Betreibers).
Port 1 trägt immer das ungerade Modul, die Entity-Suffixe `_2 … _4` die WR 2–4.
Diese Tabelle gilt für **beide** Config-Zustände: Azimut, Tilt, Wp, Gruppen und
Entity-Zuordnung sind im ausgelieferten Default und in der Live-Config gleich —
die Unterschiede liegen ausschließlich in den Horizontzeilen und den
Site-Parametern (§3).

| Modul | Azimut | Tilt | Wp | Balkon | Wechselrichter/Port | Mess-Entity (`actual_entity`) |
|---|---|---|---|---|---|---|
| M1 | 25° (NNO) | 70° | 370 | unten | WR1 / Port 1 | `sensor.inverter_port_1_dc_power` |
| M2 | 115° (OSO) | 70° | 370 | unten | WR1 / Port 2 | `sensor.inverter_port_2_dc_power` |
| M3 | 115° (OSO) | 70° | 370 | unten | WR2 / Port 1 | `sensor.inverter_port_1_dc_power_2` |
| M4 | 205° (SSW) | 70° | 430 | unten | WR2 / Port 2 | `sensor.inverter_port_2_dc_power_2` |
| M5 | 25° (NNO) | 80° | 430 | oben | WR3 / Port 1 | `sensor.inverter_port_1_dc_power_3` |
| M6 | 115° (OSO) | 80° | 430 | oben | WR3 / Port 2 | `sensor.inverter_port_2_dc_power_3` |
| M7 | 115° (OSO) | 80° | 430 | oben | WR4 / Port 1 | `sensor.inverter_port_1_dc_power_4` |
| M8 | 205° (SSW) | 80° | 430 | oben | WR4 / Port 2 | `sensor.inverter_port_2_dc_power_4` |

**Summe 3260 Wp.** Vier Mikro-Wechselrichter **Hoymiles HMS-800W-2T**, je zwei
Module, **je Port ein MPPT** → die Module sind elektrisch unabhängig, weshalb
jede Ebene einen eigenen Messkanal hat (das zentrale Asset dieses Projekts:
modulgenaue Ist-Werte statt einer Summenkurve). Die vier `groups` in der Config
bilden die WR ab, jede mit `ac_limit_w: 800.0` — die **AC-Klemme**: die Summe
beider Ports wird nach DC-Modell und DC→AC-Wandlung auf 800 W begrenzt
(`core/electrical.clamp_groups_ac`). Bei diesen steilen Neigungen greift das
Clipping praktisch nie, ist aber modelliert.

**Verteilung nach Ausrichtung** (wichtig zum Einordnen der Tageskurve): ~1600 Wp
schauen nach **OSO 115°** (M2/M3/M6/M7), nur 860 Wp nach SSW und 800 Wp nach NNO —
deshalb hat die Site-Prognose ihr Maximum **vormittags (~09–10 Uhr lokal)** und
fällt über den Mittag ab. Korrekte Orientierungsphysik, **kein Fehler** (eine
wiederkehrende Fehlinterpretation).

### 1.2 Site-weite Parameter (Betreiber-Werte)

| Feld | Wert live | Default im Code | Bedeutung |
|---|---|---|---|
| `albedo` | 0,15 | `ALBEDO_DEFAULT = 0.2` | Bodenreflexion; wirkt auf steilen Ebenen überproportional. Bei Schnee überschreibt `ALBEDO_SNOW = 0.5`. |
| `bifacial_beam_gain` | **1,25** gesetzt (Fit ≈ 1,23, Backtest 2026-07-16) | `BEAM_GAIN_DEFAULT = 1.0` | Faktor **nur** auf den Direkt-/Zirkumsolaranteil der POA-Einstrahlung. Clamp `[1.0, 1.6]`. |
| `efficiency` je Ebene | 0,96 | `DEFAULT_EFFICIENCY = 0.96` | DC-seitige Systemverluste. |
| `ross_coeff` je Ebene | 0,02 | `ROSS_COEFF = 0.0342` | Zelltemperatur `Tcell = Tamb + coeff × POA`; freistehend/gut hinterlüftet → kleiner. |
| `inverter_efficiency` je Gruppe | unbelegt | `DEFAULT_INVERTER_EFFICIENCY = 0.965` | Datenblatt-η, nur Fallback (siehe §2). |

> Unsicher: `albedo`, `bifacial_beam_gain`, `ross_coeff` stammen aus
> Session-Artefakten (Diagnostics-Abzug 2026-07-24, Kampagnen-YAML 0.22), nicht
> aus dem Repo — verbindlich ist immer der Diagnostics-Dump des Config-Entries.
> Die Spalte „Wert live" meint den Zustand **LIVE** aus §3; im ausgelieferten
> `DEFAULT_SITE` (**REPO**) sind `albedo` und `bifacial_beam_gain` gar nicht
> gesetzt, dort gelten die Code-Defaults.

### 1.3 Der Victron-AC-Referenzzähler

Optional, hier gesetzt: `ac_actual_entity:
sensor.victron_vebus_out_l1_power_228`, `ac_actual_invert: true`. Ein
**unabhängiger** AC-Zähler hinter allen vier Wechselrichtern (bestätigt: es
hängen **nur** die vier WR daran — deshalb eine saubere Referenz); er meldet
Einspeisung **negativ**, daher das Invert-Flag, das
`SiteConfig.ac_actual_invert` beim Lesen anwendet. Er ist das Kalibrierungsziel für den gelernten DC→AC-Wirkungsgrad `eta_inv`
(`core/inverter_cal.py`) und erzeugt den Sensor `…_measured_ac_power`. Der
gelernte η ist **nie load-bearing**: fehlender Zähler, zu wenige gültige Stunden
(`INVERTER_CAL_MIN_SAMPLES = 20`) oder ein Verhältnis außerhalb
`[INVERTER_CAL_MIN 0.90, INVERTER_CAL_MAX 0.99]` führen auf den
Config-/Default-η zurück. Weitere Gates: `INVERTER_CAL_MIN_LOAD_W = 100` (darunter
verzerrt der Eigenverbrauch) und `INVERTER_CAL_CLIP_HEADROOM_FRAC = 0.90` (eine
geclippte Stunde würde η unterschätzen).

## 2. Semantik der Messkette — nur DC ist gemessen

Die wichtigste Betriebsfalle der ganzen Anlage:

```
GEMESSEN:  port_N_dc_voltage × port_N_dc_current  →  port_N_dc_power
ABGELEITET: Σ(DC) × 0,9472                        →  inverter_ac_power
ABGELEITET: dasselbe, integriert                  →  *_dc_total_energy
```

Messung 2026-07-19 an der Live-Anlage: `dc_power` = U×I auf allen 8 Ports
(innerhalb der Rundung) → echt. Das AC/DC-Verhältnis ist über einen
**50-fachen Lastbereich** (5 W … 258 W) konstant 0,9472–0,9504 und **steigt**
bei kleiner Last leicht an — das Gegenteil einer echten Wirkungsgradkurve, also
eine Firmware-Konstante. `*_dc_total_energy` verfolgt **trotz seines Namens die
AC-Ausbeute** (Zähler/AC = 1,0004–1,0046 über einen ganzen Tag).

**Konsequenzen für den Betrieb:**

1. Die **DC-Leistungssensoren sind die einzig korrekte Ground Truth** für die
   Lerner (`actual_entity` zeigt genau dorthin). `*_dc_total_energy` und
   `ac_power` der Wechselrichter sind **keine unabhängige Gegenprobe** — sie sind
   die DC-Sensoren in Verkleidung. Genau dieser Denkfehler führte zum Feld
   `actual_energy_entity`, das in **v0.20.6 zurückgezogen** wurde. Beleg dafür
   ist der Rückverweis **im CHANGELOG-Eintrag 0.20.6** („Withdrawn: the per-plane
   `actual_energy_entity` field from 0.20.5") samt der dort dokumentierten
   Messung — einen eigenen 0.20.5-Abschnitt hat der CHANGELOG **nicht** (nur den
   Git-Tag `v0.20.5`).
2. Die **reale** DC→AC-Effizienz ist **0,9248** (gelernt, n=173, gegen den
   Victron-Zähler); die 2,4 Prozentpunkte Abstand zur Firmware-Konstante sind
   echter Verlust (Verkabelung, Teillast, Messpunkt).
   `scripts/validation/bsf_checks.DEFAULT_ETA = 0.9248` nutzt genau diesen Wert.
3. Tages-Wh aus einem Leistungssensor: **Stundenmittel × 1 h** bzw.
   **Tagesmittel × 24 h** ist exakt (dreifach verifiziert). Ein
   `statistics-graph` mit `stat_types: [sum]` bleibt dagegen **leer** —
   Leistungssensoren haben `has_sum: false` (Fix 0.20.4).

## 3. Horizont-Realität des Standorts

Die Anlage ist stark verschattet; das Horizontmodell trägt hier mehr Genauigkeit
als jede Lernschicht.

**Zuerst die wichtigste Unterscheidung: drei Config-Zustände.** Fast jede
Fehlinterpretation in diesem Kapitel entsteht daraus, dass „die Config" drei
verschiedene Dinge meinen kann:

| Zustand | Was es ist | Inhalt (Kurz) |
|---|---|---|
| **REPO** | `const.DEFAULT_SITE` im ausgelieferten Code — Referenzstandort und Startpunkt jeder **Neu**installation | nur `_FARFIELD_SLOPE` (az 60/100 → el 13°, az 100,01/150 → el 16°, τ 0) auf allen Ebenen; M4/M8 zusätzlich der saisonale Screen az 135–175 (el 40°/30°, τ 0,45 belaubt / 0,8 kahl) und die Wand ab **az 212°**; **kein** `tau_points`, **kein** `diffuse_tau`, und weder `albedo` noch `bifacial_beam_gain` als Schlüssel ⇒ es greifen `ALBEDO_DEFAULT = 0.2`, `BEAM_GAIN_DEFAULT = 1.0`, `ROSS_COEFF = 0.0342` |
| **LIVE** (Stand 25.07.2026) | was auf der Anlage des Betreibers tatsächlich läuft — im Wesentlichen der Config-Edit vom 16.07. | **modulindividuelle** Baumfenster statt des einen M4/M8-Screens · Wandkante M4/M8 bei **az 195°** (nicht 212) · Ost-Kante **el 10° ab az 52** samt der **Interim-az-Rampe** τ(az) über az 63–78 (0,05 → 0,85, verankert auf den Sonnenpfad um den 1. August) · `albedo` 0,15 · `bifacial_beam_gain` 1,25 |
| **KAMPAGNE** (0.22, vorbereitet) | Code ist released, der **Config-Edit ist noch nicht angewandt** (O1 in `06-…`) | ersetzt die Interim-az-Rampe durch `tau_points` auf den Ost-Zeilen und ergänzt `diffuse_tau: 0.5` auf den Wandzeilen (M4/M8 az 195–360, M1/M5 az 295–360) — sonst **nichts** |

Nur **REPO** ist aus dem Repo belegbar. **LIVE** und **KAMPAGNE** stammen aus dem
Live-/Diagnostics-Abzug bzw. dem Kampagnen-YAML (Session-Artefakte); verbindlich
für den Ist-Zustand ist immer der Diagnostics-Dump des Config-Entries. Die vier
realen Objekte unten sind deshalb durchgehend mit **REPO / LIVE / KAMPAGNE**
ausgezeichnet — die Buchstaben **(a)–(d)** bezeichnen dort weiterhin nur die
Objekte, nicht die Zustände.

**(a) Fern-Hang mit Baumkronen im Osten/Südosten.** Ein Hang in 80–250 m
Entfernung, oben bewaldet. Vom Betreiber eingemessen plus sonnenpositionsgenaue
Emergenzpunkte aus den Messdaten: az 89° → el 10,0°, az 98° → el 11,3°,
az 112° → el 15° — und dann **flach 15–16° bis az ~168**. Die vom Betreiber
gepeilten ~25° bei „az 170" sind **kein Plateau**, sondern ein **schmaler Spike
az 168–173 mit el 25° und τ 0,5**; danach fällt die Linie wieder auf 16° bis zur
Wand. Die frühere Interpolation 140°→20°/170°→25° wurde durch Rohkurven klarer
Novembertage widerlegt (M3/M7 lieferten dort volle Leistung) und ist überholt.
Diese fein aufgelöste Linie steht so in **LIVE**; **REPO** trägt an ihrer Stelle
nur den groben `_FARFIELD_SLOPE` (az 60–100 → el 13°, az 100–150 → el 16°, τ 0).

Die **Baumkronen sind halbtransparent**: statt eines harten τ **unterstützt** die
Horizontzeile seit v0.22 ein Inline-Profil `tau_points: [[el, τ], …]`, das die
Transmittanz an die **Sonnenelevation** bindet. Zwei Zahlenreihen, die dabei
regelmäßig verwechselt werden:

- **Gemessen** (5-Minuten-Rampen von vier klaren Julitagen 2026, gepoolt;
  Median-τ_eff je Elevationsband): ~0 unterhalb el 4,5° · 0,25 bei el 5–6° ·
  0,43 bei el 6–7° · ~0,9 bei el 8–9° · ~1,0 ab el 9,5°.
- **Knoten der Ziel-Config (KAMPAGNE)**, daraus gerundet:
  `[[4.5,0],[5.5,0.25],[6.5,0.45],[8.0,0.85],[9.5,1.0]]`. Der 8,0°-Knoten liegt
  mit 0,85 bewusst **unter** dem gemessenen ~0,9 (konservativ: der Rohmorgen soll
  nicht überzeichnet werden), 6,5°→0,45 rundet die gemessenen 0,43 leicht auf.

**Zustand:** `tau_points` ist **nur in KAMPAGNE** gesetzt. Weder **REPO** (dort
der harte Fern-Hang mit τ 0) noch **LIVE** (dort die Interim-az-Rampe) enthalten
das Feld.

Warum an der Elevation statt am Azimut: eine τ(az)-Rampe entlang des Sonnenpfads
eines Ankertags driftet mit ~0,3°/Tag und erzeugt im Spätsommer **Phantom-Beam**
in der Dämmerung. Die alte Interim-Rampe ist **deprecated** und wird einmalig
migriert, nicht monatlich nachverankert.

**(b) Zwei Nahbäume SSE.** Vom Betreiber von M3-Oberkante eingemessen: rechter
Baum Kante ~50° bei 16,5 m Fußabstand, linker ~40° bei 14 m; von der oberen
Reihe (+2,8 m) rechnerisch 45,6°/32,6°. Sektor ~az 124–176, Wirkung
**modulabhängig** (Position entlang des Balkons: M1/M5, M2/M6, M3/M7, Ecke,
M4/M8), obere Reihe immer milder. Aus Rohkurven klarer Referenztage (Sep 2025,
Mär 2026) abgeleitete Fenster: M3 124–158 @el45 τ0,3 · M4 124–155 @el44 τ0,25 ·
M7 138–158 @el39 τ0,3 · M8 140–155 @el36 τ0,45 · M2 140–175 @el45–44 τ0,55 ·
M6 140–175 @el32–38 τ0,75. **Zustand: LIVE** — diese modulindividuellen Fenster
sind seit dem Config-Edit vom 16.07. eingespielt und im Abzug vom 25.07.
bestätigt; sie sind **nicht** Teil der 0.22-Kampagne. **REPO** kennt sie nicht
(dort nur der eine M4/M8-Screen az 135–175, siehe (d)).

**(c) Hauswände.** Harte Kante, τ = 0 für Beam. M4/M8 (SSW): in **REPO** beginnt
die Wand bei **az 212°** (`_wall_row(212.0)` in `const._south_horizon`) — das
entspricht der Beobachtung „Sonne hinterm Haus ab ~14:20". In **LIVE** ist die
Kante seit dem 16.07. auf **az 195°** vorgezogen (aus Dezember-Rohkurven klarer
Tage abgeleitet und dort bestätigt), der Direktstrahl endet entsprechend früher;
das ist **kein** Bestandteil der 0.22-Kampagne. M1/M5 (NNO): Wandzeilen ab
**az ~295°** — ebenfalls **LIVE**, die Kante allerdings **geschätzt, nicht
nachgemessen**; in **REPO** tragen die Nordebenen ausschließlich den Fern-Hang.
Was **KAMPAGNE** an den Wandzeilen ändert, ist **allein** `diffuse_tau: 0.5`
(M4/M8 az 195–360, M1/M5 az 295–360); weder **REPO** noch **LIVE** tragen heute
ein `diffuse_tau` — `_wall_row()` liefert nur `elevation 90 / tau 0` ohne
Diffus-Override. **Achtung:
`diffuse_tau` ist keine Transmission**, sondern die **effektive Radianz des
blockierten Sektors relativ zum offenen Himmel** — bei heller Putzwand etwa ihre
Reflektanz. Sie wirkt **ausschließlich** im Sky-View-Faktor (Diffus); der
Beam-Pfad bleibt byte-unberührt (τ0 bleibt τ0). Validierungsgrenze
`0 ≤ diffuse_tau ≤ 0,8`.

**(d) Der offene Punkt: Screen-Zuordnung az 135–175.** In **REPO** sitzt der
saisonale „Baum-Screen" (az 135–175, el 40°/30°, τ 0,45 belaubt / 0,8 kahl,
`_south_horizon()`) **nur auf M4/M8**. Die gelernte
Shademap (LTS ab Jul 2025) widerlegt das: **M2/M3 sind dort stark verschattet**
(τ ≈ 0,4–0,63 bis el ~45), und zwar in **beiden** Jahreshälften → statisches,
nicht saisonales Objekt; **M4/M8** zeigten im konfigurierten Keil τ 1,00–1,10.
Nahfeld-Beweis: bei identischer Sonnenposition unterscheiden sich die
co-lokierten Ebenen az115/t70 (M2/M3) und az115/t80 (M6/M7) stark — ein
Fernfeldobjekt würde neigungsunabhängig verschatten. Später teil-korrigiert:
Rohkurven klarer Tage zeigten doch eine tiefe Baumkerbe bei M4 (az ~124–155,
τ~0,2) — das alte Fenster hatte die **richtige Startkante, aber das falsche
Ende**; deshalb die modulindividuellen Fenster (siehe (b)).

**Status:** Die Neuzuordnung ist **LIVE** erledigt (die Fenster aus (b));
**REPO** trägt unverändert den alten M4/M8-Screen — eine Neuinstallation startet
also mit der widerlegten Zuordnung. **KAMPAGNE** ändert an der Zuordnung
**nichts** mehr, sie ergänzt nur den Diffus-Floor. Die Regressionswache **C8b**
(`scripts/validation/bsf_checks.py`) nennt das M4/M8-Morgen-Diffus-Defizit von
**~×10** ausdrücklich *bekannt und akzeptiert* — dieser Faktor beschreibt den
Zustand **ohne** `diffuse_tau`, also REPO/LIVE und damit den bis heute gemessenen
Stand („durch die 0.21-Fixes NICHT adressiert", so der Detailtext des Checks).
Erst die Anwendung von KAMPAGNE soll ihn auf den **erwarteten Rest ~×3** drücken
(§5.1). Details: `06-forensik-juli-2026-und-offene-punkte.md`.

**Methodische Lehre:** *Abwesenheit gelernter Bins ≠ freie Sicht* — ein fehlender
Bin kann am Stunden-Aliasing des Backfills oder an einem alten Config-Gate liegen;
härteste Probe sind **Rohkurven klarer Referenztage**.

## 4. Betriebs-Runbook

### 4.1 Integration aktualisieren

HACS → Integrationen → *Balcony Solar Forecast* → **Update**, dann Home
Assistant **neu starten** (`custom_components` werden nicht heiß geladen),
danach prüfen: `sensor.balcony_solar_forecast_source_status` = `fresh` und im
Diagnostics-Download steht die neue Version. Der Lernzustand liegt in einem
versionierten HA-`Store` und wird **additiv** migriert (v2 → v3 byte-treu; ein
Migrationslauf, der Lernzustand verwirft, gilt als kritischer Fehler) — ein
Update kostet keinen Lernfortschritt.

### 4.2 Struktur ändern (Reconfigure): Site-Objekt + Beam gain

**Strukturelle** Änderungen — Ebenen, Horizonte, WR-Gruppen, `shade_group`,
`ross_coeff`, Albedo, Beam gain, AC-Zähler — laufen über **Drei-Punkte-Menü der
Integration → „Neu konfigurieren"**, nicht über „Konfigurieren"/Optionen. Grund
(Docstring `config_flow.py`): Optionen überschatten `entry.data` dauerhaft über
den `{**data, **options}`-Merge, den jeder Leser benutzt. Der Reconfigure-Flow
schreibt direkt in `entry.data` und entfernt dabei alte strukturelle Reste aus
den Optionen. Nach dem Speichern lädt der Entry automatisch neu.

Das Feld **`site`** ist ein Object-Selector — in der UI ein YAML-Editor; geprüft
von `_site_validation.validate_site()` (Azimut 0–360, Tilt 0–90, Wp > 0, τ 0–1,
`tau_points` 1–12 Paare mit **streng aufsteigendem `el`** innerhalb
`[0, elevation_deg]`, `diffuse_tau` 0–0,8). Unsortierte **Horizontzeilen** sind
dagegen **kein Fehler**: `_validate_horizon` sortiert sie stabil nach Azimut und
persistiert diese kanonische Form — normalisiert, nicht abgelehnt (hart
aufsteigend muss nur `el` *innerhalb* `tau_points` sein). Fehler
erscheinen inline am Feld `site` mit sprechendem Code (`bad_tau_points`,
`tau_points_above_edge`, `seasonal_points_mismatch`, `bad_diffuse_tau`).
**Beam gain** (`bifacial_beam_gain`) ist ein eigenes Feld im selben Formular,
Clamp `[1.0, 1.6]`, leer/1.0 = Identität.

> **Regel: eine Stellschraube pro Woche** — Beam gain **oder** τ-Profil **oder**
> Reset; sonst ist die nächste Messwoche nicht attribuierbar.

### 4.3 Lerner neu aufsetzen (Re-Bootstrap)

Jede **prognoserelevante** Config-Änderung (τ, `tau_points`, `diffuse_tau`,
Albedo, Beam gain, Geometrie) verschiebt die **RAW**-Kurve, gegen die alle Lerner
konditioniert sind. Zwei Dinge folgen daraus. **(1) Day-ahead-Bias — automatisch.** Ein `config_fingerprint` über alle
prognoserelevanten Felder (inkl. `tau_points`, `tau_points_bare`, `diffuse_tau`,
`CLASSIFIER_VERSION`) liegt neben dem Bias-State. Ändert er sich, wird **jede
Zelle re-seeded**: RLS-Kovarianz zurück auf `RLS_INIT_COVARIANCE`, `n` gedeckelt
auf `DAY_AHEAD_BIAS_RESEED_N = 20` — θ bleibt, lernt aber wieder schnell. Dazu
ein Repair-Issue (`config_changed_bias_reseed`).

**(2) Shademap + Quantile — Re-Bootstrap empfohlen**, weil gelernte Bins gegen
die **alte** Diffus-Floor-/Beam-Referenz trainiert wurden. **Standardweg (v0.23+):
Aktion `run_bootstrap`** — in-Process, ohne Token, ohne `site.json`, mit der
**live konfigurierten** Site (Entwicklerwerkzeuge → Aktionen):

```yaml
action: balcony_solar_forecast.run_bootstrap
data:
  dry_run: true    # Schritt 1: Default, ändert NICHTS — Zusammenfassung prüfen
  # dry_run: false # Schritt 2: importieren (nimmt vorher einen Rollback-Snapshot)
```

Standardbereich: `start_date` ≈ heute − `BOOTSTRAP_DEFAULT_MAX_DAYS` (400 Tage),
`end_date` = gestern; Tage ohne Messhistorie werden übersprungen. Laufzeit
~2–5 min für ~320 Tage, Fortschrittslog alle 50 Tage, Rechenarbeit außerhalb des
Event-Loops, serialisiert gegen den Nightly-Job über einen Per-Site-Lock (ein
zweiter paralleler Aufruf wird abgelehnt). Die Antwort ist immer eine
Zusammenfassung: `days_used`, `days_skipped`, `date_range`, `weather_source`
(`as_issued` oder `analysis_fallback`), `bias_cells`,
`shademap_channels`/`_bins`/`_samples`, `quantile_bins`/`_samples`, `imported`,
`duration_s`. Im Trockenlauf vor allem prüfen: `weather_source == as_issued`
(sonst ist der Bias-Anteil schwächer) und `days_used` in der erwarteten
Größenordnung.

**Der Offline-Weg** (`scripts/backfill.py` + Aktion `import_bootstrap`) bleibt
gültig, wenn du das `bootstrap.json` als Artefakt brauchst (CI, Review, Archiv)
oder die Developer Tools nicht erreichst — siehe `docs/BACKFILL.md`; beide Pfade
teilen denselben HA-freien Kern (`core/bootstrap_build.py`,
`core/openmeteo_backfill.py`) und liefern byte-identische Bootstraps. Der Import
validiert und clampt jeden Faktor, lehnt unbekannte `schema_version`
ab und prüft die eingebettete `site_signature` (lat/lon + Ebenennamen) gegen die
laufende Site. Backfill-Bins tragen ein gedeckeltes `n`
(`BOOTSTRAP_MAX_BIN_N = 5`); ein Payload **ohne** `quantile_state` lässt den
Live-Quantilring unangetastet — ein Import löscht nie gelernte Bänder.

**Wann zusätzlich `reset_day_ahead_bias`?**

| Situation | `reset_day_ahead_bias` nötig? |
|---|---|
| Prognoserelevante Config-Änderung (τ, `tau_points`, `diffuse_tau`, Albedo, Beam gain, Geometrie, AC-Limit) | **Nein** — der Fingerprint-Reseed erledigt das seit **v0.21** automatisch (v0.22 hat nur `tau_points`/`tau_points_bare`/`diffuse_tau` zusätzlich in den Hash aufgenommen). |
| Einzelne Zelle nachweislich fehltrainiert (θ klebt am Clamp, verzerrt die servierte Kurve) | **Ja** — das ist der Anwendungsfall der Aktion. |
| `CLASSIFIER_VERSION`-Bump | **Nein** — steht als Segment `clsver=` im Fingerprint, der Reseed läuft automatisch; Quantil-Bins/Scoreboard-Strata sind aber semantisch veraltet → Re-Bootstrap. |
| Änderung der **Solar-Tagesteil-Grenzen** (`MIDDAY_SOLAR_HALFWIDTH_H`, `DAY_PART_SOLAR_BLEND_HALFWIDTH_H`) | **Ja** — diese Code-Konstanten stehen **nicht** im Fingerprint (der Hash deckt Ebenensignaturen, Albedo, Beam gain, Gruppen-AC-Limits und `clsver` ab); die gelernten Zellen passen dann zu einer alten Binnung. Genau der in `services.yaml` genannte Anwendungsfall („after the solar day-part binning change"). |
| Nur Shademap/Quantile sollen neu — Bias soll bleiben | **Nein**, nur `run_bootstrap`. |

`reset_day_ahead_bias` löscht die gelernten Zellen (Antwort: Anzahl gelöschter
Zellen); Shademap, Kill-Switches und Rollback-Ring bleiben unberührt. Danach steht
`sensor.balcony_solar_forecast_day_ahead_bias_status` auf **`cold_start`** —
korrekt, nicht kaputt.

### 4.4 Die Übergangsphase richtig lesen

Nach Config-Kampagne oder Reset gilt für **3–7 Tage**: die servierte
04–06Z-Kurve **überschießt** an klaren Morgen, weil die Bias-Zellen gegen die
verschobene RAW-Kurve neu lernen — dokumentiertes **Einschwingen**, kein Regress,
**nicht zurückrollen**. Zellen brauchen `RLS_MIN_SAMPLES = 3` Tage, bevor sie
überhaupt wirken (C4 meldet vorher INFO „cold start"), belastbar sind sie nach
~5 Tagen.

**Zellen lernen zurück, sie rollen nicht zurück.** Ein Reseed hebt die Lernrate,
ein Reset setzt den Wert neutral — keiner stellt „gestern" wieder her; wer nach
zwei Tagen zurückrollt, restauriert genau den Zustand, den er loswerden wollte.
Bootstrap-Shademap-Bins haben `n ≤ 5` bei Shrinkage `w = n/(n+20)`, also Gewicht
~0,2 — bewusst schwach und von Live-Daten schnell überstimmt. Umgekehrt:
**falsche statische Prior-Zeilen in der Config dominieren jahrelang** (ein Bin
bekommt nur ~2–3 akzeptierte Samples/Jahr). Geometrie gehört in die Config, nicht
in den Lerner.

### 4.5 Validierung mit `scripts/validation/validate.py`

Ein stdlib-Skript (Python ≥ 3.11, kein numpy), das ~1 Woche nach einem Deployment
objektiv prüft, ob die Fixes wirken. Die Schwellen sind gegen die **Vor-Fix-Woche
17.–24.07.2026** geeicht: dort müssen C1–C7 FAIL zeigen und C8 PASS bleiben.

**Token erzeugen:** HA-Frontend → Profil (Avatar) → Tab **Sicherheit** →
*Langlebige Zugriffstoken* → erstellen (z. B. `bsf-validation`), einmalig
kopieren, **nach der Validierung wieder löschen**. Admin-Konto nötig für den
Diagnostics-Endpunkt (ohne Admin laufen alle Checks außer den Quantil-/
Versions-Zusatzinfos trotzdem). Das Token gehört nie in ein Dokument oder in die
Shell-History — in PowerShell über `$env:HA_TOKEN`.

```powershell
# Live (aus scripts/validation/ heraus), IP statt Hostname verwenden:
python validate.py --ha-url http://<HA-IP>:8123 --token $env:HA_TOKEN --json report.json
# Offline-Wiederholung auf dem gezogenen Snapshot:
python validate.py --offline --data-dir bsf_pull_<timestamp>
# Kalibrierungsnachweis gegen die archivierte Vor-Fix-Woche.
# Der Snapshot-Ordner liegt NICHT im Repo, nur lokal beim Betreiber:
python validate.py --offline --data-dir <pfad-zum-hadata-snapshot>
# Erwartung dort: C1-C7 FAIL, C8 PASS, Exit 2.
```

Weitere Flags: `--days N` (Default 8), `--fetch-only`, `--eta 0.9248`,
`--entry-id`. **Exit-Code:** 0 = grün, 1 = nur WARN, 2 = mindestens ein FAIL.

| Check | Frage | PASS-Kriterium | Aussagekräftig wenn … |
|---|---|---|---|
| **C1** Morgen-Peak | Ist die servierte AC-Prognose 07–10 lokal ehrlich? | Wochenmittel served/Ist 0,80–1,20 und ≤1 Tag mit Peak-Ratio > 1,4 | ≥5 volle Tage, davon einige klare Morgen |
| **C2** Scalar-Hygiene | Muss der Intraday-Scalar morgens noch Physik kompensieren? | an Tagen mit \|Tagesfehler\| < 10 %: max Scalar 08–09 lokal ≤ 1,25 | es gibt Tage mit kleinem Tagesfehler |
| **C3** Morgen-Physik | Hebt die τ-Rampe den Rohmorgen ohne Sprung? | raw 04Z ≥ 300 Wh an klaren Morgen; raw/Ist 06–10Z 0,90–1,05; keine Sprünge | klare Morgen im Fenster; **immer zuerst lesen** |
| **C4** Bias-Konvergenz | Löst sich der RLS von den Clamps? | `clear\|morning` ≤ 1,25 · `clear\|afternoon` ≥ 0,85 · `mixed\|afternoon` und `overcast\|afternoon` ≥ 0,80; `clamped`-Flags rückläufig | ≥5 Tage seit Reset (vorher INFO „cold start") |
| **C5** day-0-Bänder | Trägt der Tag echte p10/p90? | > 50 % der Tageslicht-Slots mit p10 ≠ p90; kein Tag mit Intraday-p10 > End-Ist | nur Tagesstunden werten (nachts ist p10 == p90 == 0 trivial) |
| **C6** Headline-Stabilität | Kein Korrektur-Jojo? | kein 60-min-Swing > 1,5 kWh, der mit einem Scalar-Spike zusammenfällt | nur relevant, wenn der Swing mit einem Spike koinzidiert |
| **C7** Abend/Vorabend | Taugt die Vorabendprognose als Planungsbasis? | issued-AC-Wochenbias ±10 %; Nachmittag 12–18Z ±15 % | ≥5 volle Tage mit archivierten issued-Snapshots |
| **C8** Regressionswachen | Haben die Fixes nichts beschädigt? | (a) Scalar korrigiert an Überprognosetagen < 0,95, (b) M4/M8-Morgen-Diffus im Band 0,4×–2,0× gegenüber der Baseline `BASELINE_M4M8_MORNING_WH = 2254` Wh — nur **INFO/WARN**, nie FAIL, reine Beobachtungswache, (c) Mittagsfenster 11–13Z raw/Ist ±10 % gegen Baseline 0,90 | immer — **C8a und C8c müssen grün bleiben; C8b ist eine reine Beobachtungsgröße** |

**Lesereihenfolge: erst C8, dann C3, dann der Rest.** C1/C2/C6 hängen kausal an
der Morgen-Physik (C3), C4/C7 am Bias-Reset. C8c rot ⇒ **Stopp**: die τ-Rampe
reicht in zu hohe Elevationen, Horizont-YAML prüfen, bevor irgendetwas anderes
nachjustiert wird. Das Skript behandelt zentral: DC-vs-AC (`hourly_wh_ac` seit v0.21, sonst DC×η),
epoch-**Millisekunden** der Recorder-WS-API (automatisch erkannt), partielle Tage
(aus Tagessummen ausgeschlossen) und fehlende v0.21-Felder (Block
„Feature-Erkennung" statt Absturz).

> Der Prüfkatalog ist auf **v0.21.0** geeicht. Auf 0.23 laufen die Checks
> unverändert, die Baselines beziehen sich aber weiter auf die Juli-2026-Woche —
> als *Regressionsdetektor* gültig, als absolute Note nur mit diesem Kontext.

### 4.6 Rollback

**Lernzustand zurück:** `action: balcony_solar_forecast.rollback_learners` mit
`snapshots_back: 1` (1 = jüngster Snapshot = Stand vor letzter Nacht; max. 10).
Der Ring hält `LEARNER_SNAPSHOT_RING = 10` nächtliche Snapshots — bewusst tiefer
als `DRIFT_LOSS_STREAK_DAYS = 7`, damit beim Auto-Disable mindestens ein Zustand
von **vor** der Verluststrecke überlebt. Ein Snapshot umfasst Bias-, Shademap-
**und** Quantil-State, ein Bootstrap-Import ist also konsistent rücknehmbar.
**Nicht** enthalten: der Inverter-Kalibrier-State (self-gating, nie load-bearing)
und die Kill-Switches — eine per Drift abgeschaltete Schicht musst du in den
Optionen von Hand wieder einschalten.

**Config zurück:** Reconfigure-Flow öffnen und das vorherige Site-Objekt
einsetzen — halte deshalb **vor** jeder Kampagne eine Kopie des Site-YAML vor
(aus dem Reconfigure-Formular herauskopieren). Der Diagnostics-Download eignet
sich dafür nur bedingt: `async_get_config_entry_diagnostics` redigiert
`latitude`/`longitude` auch **innerhalb** des Site-Objekts (`TO_REDACT`), das
Ergebnis ist also nicht ohne Nacharbeit wieder einspielbar. Achtung: die Rücknahme ändert den
Fingerprint erneut → erneuter Bias-Reseed, erneutes Einschwingen.

## 5. Diagnose-Kochbuch

### 5.1 „Prognose morgens zu hoch / zu tief"

**Prüfen (in dieser Reihenfolge):** (1) `get_issued_forecast` für den Tag →
`raw_hourly_wh` (reine Physik) gegen die gemessene DC-Summe 04–10Z halten; ist
schon **raw** schief, ist es Physik/Horizont, kein Lernfehler. (2)
`…_day_ahead_bias_status`, Attribut `bias_cells`: steht `clear|morning` bei ~1,5
(= `DAY_AHEAD_BIAS_MAX`) mit `clamped: true`? (3) `…_intraday_correction_scalar`:
über 1,6 morgens trotz kleinem Tagesfehler?

**Typische Ursachen:** **Doppelkorrektur** — Bias-θ, Intraday-Scalar und ein
echtes Physikdefizit ziehen am selben Fehler (dominantes Muster der
Juli-Forensik, in 0.21 adressiert) · zu spät startende Horizont-τ am Ost-Hang ·
Beam gain zu niedrig (bifaziale Rückseitenausbeute an steiler Ostgeometrie).
**Gegenmittel:** raw zu tief ⇒ `bifacial_beam_gain` **eine** Stufe anheben
(z. B. 1,25 → 1,30) **oder** die `tau_points`-Rampe eine Stufe früher beginnen
lassen, dann eine Woche messen. Raw korrekt, serviert zu hoch ⇒
`reset_day_ahead_bias`, dann 5 Tage. Bekannte Restlücke an klaren Morgen bei
M4/M8, zeitlich sauber getrennt: **ohne** gesetztes `diffuse_tau` (Zustand REPO
und LIVE, §3) liegt der Morgen-Diffus um **~×10** daneben (so führt es die
Regressionswache C8b); **nach** Anwendung der 0.22-Kampagne bleibt
erwartungsgemäß ein Rest von **~×3** (~90–150 Wh/Tag site-weit laut CHANGELOG
0.22.0, der beam-gebundene Rückseitenanteil) — bewusst **nicht** mit
aufgeblasenen `diffuse_tau`-Werten kaschiert.

### 5.2 „Bänder kollabiert (p10 == p90)"

**Prüfen:** Diagnostics → Block `quantiles`: je Bin `n`, `days`, `trained`. Ein
Bin serviert erst ein echtes Band, wenn **beide** Gates fallen:
`n ≥ QUANTILE_MIN_SAMPLES = 20` **und** `effective_days ≥ QUANTILE_MIN_DAYS = 5`;
zusätzlich `get_forecast` → `band_source_by_day`. **Typische Ursachen:** Quantil-Seeding nie gelaufen (dann sind typischerweise
**nur die overcast-Bins** trainiert) · Quantile in den Optionen aus · oder es ist
schlicht Nacht (p10 == p90 == 0 ist dort trivial und **kein** Kollaps).
**Gegenmittel:** `run_bootstrap` mit `dry_run: false` (der Bootstrap faltet je
Tageslichtstunde `measured / θ-corrected` durch dasselbe `train_quantiles` wie
der Live-Nightly); danach müssen mindestens die clear-Bins `trained: true`
zeigen. Ein schmales, aber nicht kollabiertes Band ist gewollt — die
Cold-Start-Regel verbietet **fabrizierte** Spreizung.

### 5.3 „Scalar hängt bei 1.0"

**Prüfen:** `…_intraday_correction_scalar` und `…_fast_learner_status`.
**Typische Ursachen:** Fast-Learner auf `off` (Kill-Switch) oder
`disabled_by_drift` · **zu wenig Historie** — der Scalar braucht
`INTRADAY_MIN_TRAILING_MINUTES = 120` Minuten Samples und akkumuliert nur, wo
die modellierte Energie `INTRADAY_MIN_MODELED_WH = 5` übersteigt (früh morgens
und spät abends ist 1,0 also korrekt) · **nach Neustart/Reload**: der Sample-Ring
liegt rein im Speicher und wird nie persistiert; seit v0.21 wird er beim ersten
frischen Tick aus den 5-Minuten-Recorder-Statistiken rekonstruiert und fällt bei
fehlenden Stats sauber auf neutral zurück. In die Modellseite gehen nur
**metrierte** Ebenen (mit `actual_entity`) ein.
**Gegenmittel:** Kill-Switch prüfen, sonst abwarten (≥2 h Tageslicht mit
nennenswerter Produktion). Dauerhaft neutral bei laufender Produktion und Status
`active` ⇒ Bug-Verdacht, Issue aufmachen.

### 5.4 „`kill_gate` ist unknown"

`binary_sensor.balcony_solar_forecast_kill_gate_passed` ist **absichtlich**
`None`, solange kein Urteil belastbar ist (`core/scoreboard.kill_gate_passed`):
rollendes Fenster `DEFAULT_SCOREBOARD_WINDOW_DAYS = 14` noch nicht gefüllt · zu
wenige **gepaarte** Tage, an denen Engine **und** Vergleichsprognose bewertet
wurden (`SCOREBOARD_MIN_PAIRED_DAYS`) · **Staleness**: jüngster bewerteter Tag
älter als `SCOREBOARD_MAX_STALENESS_DAYS = 3` → Urteil wird ausgesetzt statt ein
altes „bestanden" weiterzusenden · **keine Vergleichssensoren konfiguriert**
(`comparison_sensors` ist per Default **leer**) → es gibt keine Baseline-Latte.
**Prüfen:** Diagnostics → `scoreboard`: `window_days`, `scored_days`,
`comparison_daily_kwh_mae`, `strata` (Zeilen mit `low_n: true` unter
`SCOREBOARD_STRATUM_MIN_N = 3` liefern bewusst `null` statt absurder Prozente).
**Gegenmittel:** Vergleichssensoren in den Optionen eintragen und Fenster füllen
lassen; nicht bewertbare Vergangenheit bleibt es (kein Re-Score-Service).

### 5.5 „Learner-Status `cold_start`"

Betrifft die **Day-ahead-Schicht**: *eingeschaltet, aber ohne gelernten Zustand*
— die Schicht wendet **nichts** an (der Korrekturhaken ist auf `bool(cells)`
gated; „active" zu melden wäre eine Lüge, v0.19.2). Erwartbar nach frischer
Installation, `reset_day_ahead_bias` oder einem Rollback auf einen leeren
Snapshot. Die Attribute bleiben sichtbar als `bias_cells: {}` / `cells_n: 0` —
ein *verschwundenes* Attribut wäre der Bug, ein leeres ist korrekt.
**Gegenmittel:** nichts tun; nach 3–5 verwertbaren Nächten füllen sich die
Zellen. Bleibt es länger, prüfe im Diagnostics-Block `store` die Füllstände
`issued_days`, `actuals_days`, `hourly_actuals_days` — ohne Ist-Daten trainiert
der Nightly nicht. Jedes Gate in `_actuals.py` verwirft dabei den **ganzen Tag**
(„Messkanal-Dropout ⇒ ganzen Tag verwerfen"): ein Tag zählt nur, wenn **jedes**
konfigurierte Modul für ≥ `DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE = 0,75` der
Tageslichtstunden Stundenmittel hat — maßgeblich ist das **schlechtest**
abgedeckte Modul (der Code bildet `covered_hours` je Modul und nimmt das
Minimum), ein gesundes Nachbarmodul darf einen mittags ausgefallenen Kanal
**nicht** maskieren. Ebenso verwerfen ein Kanal ganz ohne brauchbare LTS-Werte
(unavailable gewordener DTU-Port) und ein als eingefroren erkannter Kanal
(`_is_frozen_channel`: gleicher Nicht-Null-Wert über
`LABEL_FROZEN_MIN_REPEATS = 4`+ Stunden) den kompletten Tag. **Betriebsfolge:**
ein dauerhaft toter, aber noch konfigurierter Sensor blockiert jedes nächtliche
Training, bis er aus der Config entfernt wird; die Warnung im Log nennt Modul und
Entity-ID genau dafür. (Ein Kommentar in `const.py` spricht noch vom
„best-covered module" — veraltet.) **Verwandte Status:** `off` (Kill-Switch) · `disabled_by_drift` (Drift-Monitor
nach `DRIFT_LOSS_STREAK_DAYS = 7` Verlusttagen; Wiedereinschalten nur manuell in
den Optionen) · `frozen` (Kollaps-Detektor: alle Kanäle ~0 bei hoher Prognose →
Schnee/Ausfall, Lerner für den Tag eingefroren, nur der geclampte
Intraday-Scalar reagiert).

## 6. Bewusst nicht modelliert (damit niemand danach sucht)

**Balkon-über-Balkon-Verschattung** (Betreiber-Entscheid: „nur leicht/selten,
ignorieren") · **Winternebel als Geometrie** (er ist die Wetterklasse `fog`,
keine Horizontzeile) · **Rückseiten-Beam-Anteil** `rear_beam_fraction`
(ADR-0022-Option D3, zurückgestellt — die M4/M8-Morgenlücke bleibt sichtbar
statt kaschiert) · **saisonales τ für den Fern-Hang** (statisch 0, kein
Laubmodell; die Kronen-Halbtransparenz sitzt in `tau_points`) · **genaue
Westflanke az 240–300 für M1/M5** (Kante ~295° geschätzt, nicht eingemessen).
