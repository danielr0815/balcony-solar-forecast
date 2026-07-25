# Physik & Horizontmodell

**Worum es geht:** Dieses Dokument beschreibt die reine, HA-freie Prognosephysik von
`balcony-solar-forecast` (Stand `main` @ v0.23.0) exakt so, wie sie im Code steht:
Sonnenstand, Clear-Sky, Hay-Davies-Transposition, Zelltemperatur/DC-Modell, der
zweistufige AC-Clamp und — am ausführlichsten — das Horizontmodell inklusive der
0.22-Erweiterungen `tau_points` und `diffuse_tau`.
**Wann du es brauchst:** wenn du Prognosewerte nachrechnen, eine Horizontzeile
konfigurieren/validieren oder beurteilen willst, ob eine Abweichung Physik, Lerner
oder Konfiguration ist. Lernschichten stehen in `03-lernschichten-und-korrekturen.md`,
Entities/Services in `04-ha-integration-entities-services.md`.

## 1. Konventionen (die häufigsten Fehlerquellen zuerst)

| Größe | Konvention im Code | Anmerkung |
|---|---|---|
| **Azimut** | **0 = Nord, im Uhrzeigersinn** (90 = Ost, 180 = Süd, 270 = West) | gilt intern **durchgängig**: Sonnenazimut, `PlaneConfig.azimuth_deg`, `HorizonRow.azimuth_deg`. Dokumentiert in `core/types.py` (Modul-Docstring) und `core/horizon.py`. |
| Neigung (`tilt_deg`) | Grad ab Horizontale, 90 = senkrecht | Fassadenmodule liegen typisch bei 70–80°. |
| Elevation | Grad über Horizont, negativ darunter | refraktionskorrigiert (siehe §2). |
| Zeit | **tz-aware UTC**, 15-Minuten-Slots (`SLOT_MINUTES = 15`) | naive datetimes werden in `solpos.sun_position` mit `ValueError` abgelehnt. |
| Slot-Semantik | Slot-Werte sind **Intervallmittel** (Open-Meteo rückwärts gemittelt); der Sonnenstand wird am **Slot-Mittelpunkt** ausgewertet | `WeatherSlot.midpoint = start + 7 min 30 s`. |

*Fehlerquelle Azimut:* Andere Werkzeuge nutzen 0 = Süd (Open-Meteo-GTI-Parameter,
PVGIS `printhorizon`). Im Kern gibt es **keine** Umrechnung — wer eine
PVGIS-Horizontlinie übernimmt, muss sie selbst auf 0 = Nord drehen.
`tests/core/test_golden.py` vergleicht bewusst ohne Remap gegen pvlib (auch dort
0 = Nord ab pvlib 0.11), ein Konventionsfehler fällt sofort auf.

## 2. Sonnenstand — `core/solpos.py`

Geschlossene NOAA-Formulierung (Meeus, „low precision"), nur `math`, keine
Abhängigkeiten.

`sun_position(dt_utc, lat, lon) -> (azimuth_deg, elevation_deg)`. Zeitbasis ist das
Julianische Datum direkt aus dem POSIX-Timestamp (`_JD_UNIX_EPOCH = 2440587.5`),
daher kalender- und DST-unempfindlich. Rechenkette: mittlere Länge/Anomalie →
Mittelpunktsgleichung → scheinbare Länge → Deklination → Zeitgleichung → wahre
Sonnenzeit → Stundenwinkel → Zenit/Elevation → Azimut. Der Azimut entsteht aus
Zenit, Deklination, Breite und dem **Vorzeichen des Stundenwinkels** (vormittags
0–180°, nachmittags 180–360°); bei entartetem Nenner (Pol/Zenit) stabil 180°.
Die **Refraktion** ist die NOAA-Stückfunktion (Bennett-Stil) in Bogensekunden,
addiert auf die wahre Elevation (über 85° = 0). Genauigkeitsziel laut
Modul-Docstring **< 0,3°**, Selbsttest-Anker Betreiberstandort: Juni-Mittag 64,9°,
Dezember 18,0°; der pvlib-Golden-Test toleriert 0,5°.

Zusätzlich liefert das Modul `hours_from_solar_noon(dt_utc, lon)`: vorzeichen-
behaftete Stunden **wahrer Sonnenzeit** ab lokalem Sonnen-Mittag (= Stundenwinkel/15).
Das ist die DST- und saisonrobuste Koordinate für die Tagesteil-Bins des
Day-Ahead-Bias — Details in `03-lernschichten-und-korrekturen.md`.

## 3. Clear-Sky — `core/clearsky.py`

Haurwitz (1945), Einparameter-Modell nur über den Zenitwinkel:
`GHI_clear = 1098 · cos z · exp(−0,059 / cos z)` mit `cos z = sin(Elevation)`
(`_HAURWITZ_A = 1098.0`, `_HAURWITZ_B = 0.059`; unter/auf Horizont = 0).

`clear_sky_index(ghi, elevation_deg)` = k_c = GHI / Haurwitz (0, wenn die Referenz
0 ist). `hourly_kc(samples)` ist **die** gemeinsame Stunden-Reduktion von
Live-Nightly und Offline-Backfill: `Σ ghi / Σ haurwitz` über alle Samples mit
positiver Referenz — clear-sky-energiegewichtet, damit ein Dämmerungs-Sample mit
grober Referenz die Stunde nicht dominiert; ein einzelnes Sample reduziert exakt
auf `clear_sky_index`.

**Wichtig:** k_c ist **nur Lern-Gate und Normierung**, nie Prognosequelle
(SPEC §4.2). Haurwitz kennt weder Trübung noch Höhe; deshalb gaten die
Lerner k_c elevationsabhängig.

## 4. Transposition und DC-Modell

### 4.1 Hay-Davies — `core/transpose.py::hay_davies_poa`

Vier geometrische Komponenten pro Ebene und Slot (alles W/m²):

| Komponente | Formel im Code |
|---|---|
| `beam` | `DNI · cos θ` (cos θ auf ≥ 0 geklemmt) |
| `circumsolar` | `DHI · Ai · Rb` |
| `isotropic` | `DHI · (1 − Ai) · (1 + cos β)/2` |
| `ground` | `albedo · GHI · (1 − cos β)/2` |

mit β = Neigung, `Rb = cos θ / cos z` (auf `RB_CAP = 10.0` gedeckelt),
`cos θ = cos(el)·sin β·cos(az_sun − az_plane) + sin(el)·cos β`.

Der Anisotropie-Index `Ai = DNI / E0n` teilt durch die **extraterrestrische
Normalstrahlung**: `E0n = 1361 · (1 + 0,033 · cos(2π·doy/365))` (Spencer/
Duffie-Beckman, Exzentrizität ±3,3 %, Perihel Anfang Januar). Ohne `doy` fällt der
Aufruf auf die feste Solarkonstante 1361 W/m² zurück (rückwärtskompatibel für reine
Aufrufer); die Engine **und** der Backfill reichen `doy` immer durch. `Ai` wird auf
[0, 1] geklemmt.

Pflicht-Guards für tiefe Sonne: `Rb` ist gedeckelt (s. o.), und unterhalb
`LOW_SUN_CUTOFF_DEG = 3.0` wird **`Ai := 0` gesetzt, bevor** der Diffus gesplittet
wird — der misstraute zirkumsolare Anteil wandert damit in die isotrope Kuppel
statt verloren zu gehen (energieerhaltend; nur die Zirkumsolar-Multiplikation zu
nullen hätte den isotropen Term bei `(1 − Ai)` belassen und einen einseitigen
Dämmerungs-Bias erzeugt). Steht die Sonne unter dem Horizont (`sun_el <= 0`), sind
`beam = circumsolar = 0`, aber isotroper Diffus und Bodenreflex bleiben **voll** —
Dämmerungslicht wird nie stillschweigend abgeschnitten.

Rückgabe ist ein Dict mit `beam`, `circumsolar`, `isotropic`, `ground` **plus
`cos_theta`** (geklemmt) für den IAM-Schritt der Engine.

### 4.2 Einfallswinkel-Modifikator (IAM)

`transpose.ashrae_iam(cos_theta, b0=IAM_B0)` mit `IAM_B0 = 0.05`:
`f = 1 − b0 · (1/cos θ − 1)`, geklemmt auf [0, 1], 0 bei cos θ ≤ 0.

Bewusst **nicht** in `hay_davies_poa` selbst, sondern in
`engine._plane_poa_components` angewandt (wie pvlib: IAM nach der reinen
Transposition), damit die pvlib-Golden-Vektoren vergleichbar bleiben. Er wirkt auf
**beam + circumsolar** und **vor** der ungegateten Trainer-Referenz — sonst lernte die
Shademap den Glasreflexionsverlust (bei AOI > 60° auf 70–80°-Fassaden ein großer
Tagesanteil) als AOI-förmige Phantom-Verschattung.

### 4.3 Bifazialer Beam-Gain

`SiteConfig.bifacial_beam_gain` (Default `BEAM_GAIN_DEFAULT = 1.0`, Lade-Clamp
[`SITE_BEAM_GAIN_MIN = 1.0`, `SITE_BEAM_GAIN_MAX = 1.6`]) multipliziert **nur**
`beam` und `circ`, **nach** dem IAM und **vor** ungegateter Referenz und τ-Gate;
Iso-Diffus und Bodenreflex bleiben unberührt. Wirkung: der an klaren Morgen ehrlich
unterschätzte Direktstrahl landet in der **RAW**-Physik, statt dass gedeckelte Lerner
(τ ≤ 1, Bias-Zellen) eine >1-Korrektur ausdrücken müssten. Für den Referenzstandort
ist ≈ 1,23 validiert (Backtest 16.07.); auf der **Live-Anlage ist 1,25 gesetzt**
(`05-anlage-und-betrieb-runbook.md` §1.2/§3 — im ausgelieferten `DEFAULT_SITE`
fehlt das Feld, dort gilt der Default). Default 1,0 = Identität.

### 4.4 Zelltemperatur und DC-Leistung — `core/electrical.py::dc_power`

```
T_cell   = T_amb + k_ross · POA
P_dc     = wp · (POA / 1000) · (1 + TEMP_COEFF_PER_K · (T_cell − 25 °C)) · efficiency
```

`ROSS_COEFF = 0.0342` (Default) ist pro Ebene über `PlaneConfig.ross_coeff`
überschreibbar (Montagegeometrie: ~0,02 freistehend/gut hinterlüftet … ~0,056
fassadenparallel); `TEMP_COEFF_PER_K = −0.0034` (−0,34 %/K), `TEMP_REF_C = 25.0`.
Unter 25 °C entsteht ein kleiner Gewinn — physikalisch real für Silizium und
bewusst **nicht** bei Wp gedeckelt; die Hardwaregrenze ist der AC-Clamp. POA ≤ 0
→ 0 W, negativer Temperaturfaktor → 0.

**Beam/Diffus-Split (`engine._dc_split`):** Die Ross-Derate hängt an der
**Gesamt**-POA (die Zelle heizt sich am gesamten Strahlungsangebot). Deshalb wird
die Gesamt-DC einmal gerechnet und anteilig nach POA-Anteil in Beam-DC und
Diffus-DC zerlegt — `beam_dc + diffuse_dc == dc_power(total_poa, …)` exakt.

## 5. Ebenen-, Gruppen-Modell und der zweistufige AC-Clamp

**Ebene (`PlaneConfig`)** = ein MPPT/Messkanal: `name`, `azimuth_deg`, `tilt_deg`,
`wp`, `efficiency` (DC-seitig, Default `DEFAULT_EFFICIENCY = 0.96`), `horizon`,
optional `actual_entity`, `shade_group`, `ross_coeff`. **Gruppe (`InverterGroup`)**
= ein Wechselrichter mit gemeinsamem AC-Limit über seine Port-Ebenen:
`plane_names`, `ac_limit_w`, `inverter_efficiency` (Default
`DEFAULT_INVERTER_EFFICIENCY = 0.965`, Lade-Clamp [0,80; 1,0]).

### 5.1 DC-Pfad: zwei Clamp-Durchläufe

`engine.compute_forecast` ruft `electrical.clamp_groups` **zweimal**:
(1) **erster Clamp** auf die ungeclampten Plane-DC-Werte (RAW und CORRECTED
getrennt): pro Gruppe `min(Σ Ports, ac_limit_w)`, beim Greifen proportional auf die
Mitglieder zurückverteilt; (2) **Fast-Learner-Faktor** `hooks.slot_factor(start)`
multipliziert die bereits geclampte CORRECTED-Leistung; (3) **zweiter Clamp** auf
das Produkt — damit kann eine Aufwärtskorrektur (Faktor > 1) die servierte Kurve
**nie** über das AC-Limit heben. Für Faktor ≤ 1 ist der zweite Durchlauf
mathematisch ein No-op (bit-exakt gleiche Zahlen).

Ebenen ohne Gruppe haben keine Decke und passieren beide Clamps unverändert.
Die Differenz `corrected_unclamped_watts[i] − total_watts[i] > 0` ist genau das
Signal „in diesem Slot hat der Re-Clamp gebissen" (vom Day-Ahead-Headline-Strip
ausgewertet, siehe `04-ha-integration-entities-services.md`).

### 5.2 AC-Pfad (additiv): `clamp_groups_ac`

Getrennter, physikalisch korrekter DC→AC-Transform, gespeist mit der
**ungeclampten** korrigierten DC × Slot-Faktor:

```
AC_Gruppe   = min(η_inv · Σ DC_unclamped, ac_limit_w)
DC-Clip     = ac_limit_w / η_inv        (höher als das AC-Limit!)
```

Der DC-Clip-Punkt liegt oberhalb von `ac_limit_w`, weil der Mikro-Wechselrichter
AC-seitig deckelt und den MPP zurückdrückt. Beide Ergebnisse werden proportional
auf die Ports zurückverteilt; Ebenen ohne Gruppe bekommen `DC · η_ungrouped`
(dokumentierte Entscheidung: eine Ebene speist immer *irgendeinen* Wechselrichter).
`η_ungrouped` ist der **gelernte** site-weite η_inv, sofern
`LearnerHooks.inverter_efficiency` gesetzt ist, sonst
`DEFAULT_INVERTER_EFFICIENCY` — Parameter `ungrouped_eta` von
`electrical.clamp_groups_ac`; die Engine setzt ihn identisch zur η-Gewichtung des
Pre-Clamp-AC, sodass Pre-Clamp-AC und servierte AC auf jedem ungeclippten Slot
exakt übereinstimmen.
**Trennung merken:** Lernschichten, Scoreboard und Kill-Gate arbeiten auf der
**DC**-Kurve (gemessen gegen `measured_dc_power_total`); die
Betreiber-Hauptsensoren melden die **AC**-Kurve.

### 5.3 Degradation im Slot

Fehlt eine der Größen GHI/DNI/DHI/Temperatur (`_slot_is_usable`), gilt der Slot als
Nullproduktion (`engine._append_zero_slot`), bleibt aber in der Serie:
`slot_starts` und alle Per-Slot-Serien (`total_watts`, `raw_total_watts`,
Plane-/Beam-/Diffus-Serien, Ceilings, Quantilbänder) bleiben dicht und index-gleich.
Stunden- und Tages-Buckets entstehen für solche Slots dagegen **nicht** — eine
vollständig fehlende Stunde hat gar keinen Key in `hourly_wh`/`raw_hourly_wh`/
`ac_hourly_wh`/`daily_kwh`. Der Code stützt sich darauf: die Quantil-Roll-ups
überspringen fehlende Stunden explizit (`if hkey not in hourly_wh: continue`), damit
der Key-Satz der Bänder identisch zum korrigierten `hourly_wh` bleibt.
Auf Null kurzgeschlossen wird nur, wenn `sun_el <= 0` **und** `dhi <= 0` **und**
`ghi <= 0` — echtes Dämmerungs-Diffus wird nie weggeschnitten.

## 6. Horizontmodell — `core/horizon.py` + `core/types.HorizonRow`

### 6.1 Datenmodell einer Zeile

`HorizonRow` (frozen, hashbar — die `lru_cache`-Memos hängen daran):

| Feld | Bedeutung |
|---|---|
| `azimuth_deg` | Stützstelle (0 = N, im Uhrzeigersinn) |
| `elevation_deg` | Höhe der **Horizontlinie/Kante** an dieser Stützstelle |
| `tau` | Beam-Transmittanz 0…1 (0 = opak, 1 = frei), Pflichtfeld |
| `seasonal`, `tau_leafed`, `tau_bare` | saisonale Auflösung von `tau` |
| `tau_points`, `tau_points_bare` | **v0.22**: τ als Funktion der Sonnen-Elevation unterhalb der Kante |
| `diffuse_tau` | **v0.22**: Diffus-Radianz-Ersatz des blockierten Sektors, **nur im SVF** |

Alle optionalen Felder werden **nur wenn gesetzt** serialisiert (`to_dict`), damit
Altconfigs byte-identisch round-trippen.

### 6.2 Azimut-Interpolation inklusive Wrap

`_interp_rows` interpoliert **linear** zwischen den (defensiv nach Azimut
sortierten) Zeilen und behandelt das Profil als **geschlossenen 360°-Ring**: liegt
der Sonnenazimut vor der ersten oder ab der letzten Stützstelle, wird über das
Wrap-Segment `[last_az, 360) ∪ [0, first_az)` von der letzten zur ersten Zeile
interpoliert — nicht geklemmt. Leere Tabelle → neutraler Default
(`interp_elevation` = 0°, `transmittance_at` = 1,0); eine einzelne Zeile gilt für
den gesamten Kreis. Praktische Konsequenz: Kanten modelliert man mit
**Zwillingsstützstellen** (z. B. `az 51.99 el 0 τ1` direkt vor `az 52.0 el 10 τ0`),
sonst entsteht eine lange Rampe statt einer Kante. `_validate_horizon` sortiert
**stabil**, sodass gleiche Azimute ihre Reihenfolge behalten.

### 6.3 Semantik des Beam-Gates: „unter der Linie → τ, darüber → frei"

In `engine._plane_poa_components`: `static_tau` ist
`horizon.transmittance_at(plane, sun_az, doy, sun_el=sun_el)`, **wenn**
`sun_el <= horizon.interp_elevation(plane, sun_az)`, sonst `1.0` (Ergebnis auf
[0, 1] geklemmt).

`static_tau` gatet **beam + circumsolar** (multiplikativ). Der Diffus wird davon
**nie** berührt: Iso-Diffus × SVF, Bodenreflex ungefiltert. Eine harte Wand
(`elevation_deg 90, tau 0`) tötet also den Beam, lässt aber den Diffus-Floor stehen
(dessen Höhe der SVF regelt, §6.6/§6.7). Oberhalb der Linie ist `static_tau = 1.0`
— die CORRECTED-Kurve fragt ihren Shademap-Hook trotzdem, damit ein gelernter Bin
auch dort verdunkeln kann (Nahfeld, das die statische Tabelle nicht kennt);
Details: `03-…`.

### 6.4 Saisonalität (Foliage-Rampe)

`foliage_fraction(doy)` liefert Laubbedeckung 0…1 als angehobene Cosinus-Rampe
`(1 − cos(π·t))/2` (bzw. gespiegelt) um `FOLIAGE_LEAF_ON_DOY = 105` (~Mitte April,
0→1) und `FOLIAGE_LEAF_OFF_DOY = 315` (~Mitte November, 1→0) mit Halbbreite
`FOLIAGE_RAMP_DAYS = 30`; stetig und periodisch über die Jahresgrenze
(Rampen-Stetigkeits-Invariante).

`_row_tau` blendet für `seasonal`-Zeilen `tau_bare → tau_leafed` mit dieser
Fraktion; fehlende Werte fallen auf `tau` zurück. **Reihenfolge ist „resolve vor
interpolate":** erst wird je Zeile das saisonale τ aufgelöst, dann in Azimut
interpoliert — eine gemischt saisonale/statische Nachbarschaft blendet damit
korrekt.

### 6.5 Neu in 0.22: `tau_points` (elevationsabhängiges Profil)

`tau_points: [[el, τ], …]` beschreibt τ als **stückweise lineare Funktion der
Sonnen-Elevation unterhalb** der Kante `elevation_deg` (`_tau_at_el`):

- unterhalb des ersten Knotens gilt der erste Wert, oberhalb des letzten der letzte
  (die Kante bleibt das Gate); **keine Monotonie erzwungen** — reale Kronen haben
  Lücken;
- `sun_el=None` (Alt-Aufrufer) wertet das Profil am **obersten Knoten** aus; Zeilen
  **ohne** `tau_points` verhalten sich bit-identisch wie vor 0.22;
- saisonal: `tau_points` ist das **belaubte** Profil, `tau_points_bare` (gleiches
  el-Raster) das kahle; geblendet wird **pro Knoten** mit `foliage_fraction`, danach
  in Elevation ausgewertet und erst dann in Azimut interpoliert. Fehlt
  `tau_points_bare`, dient der Skalar `tau_bare` (bzw. `tau`) als kahler Wert an
  jedem Knoten.

Die Engine reicht `sun_el` durch; `core/bootstrap_build.py::reconstruct_plane_hour`
(vom Backfill genutzt) und `core/shadeprofile.py` (Verschattungsdiagramm) tun
dasselbe — Spiegel-Invariante zwischen Live-Engine, Backfill und Diagramm.

**Warum das den Saisondrift strukturell löst.** Die abgelöste Interim-Lösung
kodierte dieselbe Messung als τ(**az**) entlang des Sonnenpfads **eines Ankertags**.
Der Zusammenhang el↔az gilt aber nur an diesem Tag und verschiebt sich um ~0,3°/Tag:
Ab Ende August bekamen Dämmerungs-Azimute (Sonne dort noch el < 4°) die hohen
τ-Werte des Ankertags → **Phantom-Beam ~+35–100 Wh** an klaren Spätsommer-Morgen,
im Frühjahr spiegelbildlich. `tau_points` bindet τ an die Sonnen-Elevation, also an
die Größe, die die optische Weglänge durch die Krone bestimmt — datumsfrei,
driftfrei, je Baumsektor wiederverwendbar, und die Config dokumentiert wieder
Geometrie statt Projektionsartefakt. `tests/core/test_season_regression.py` ist der
Kernbeweis: ein synthetischer Spätaugust-Morgen liefert mit `tau_points` ~0 Beam,
mit der az-Rampe den Phantom-Beam. Die az-Rampe ist deprecated: **einmalig
migrieren, nicht monatlich nachankern.**

### 6.6 Neu in 0.22: `diffuse_tau` (Diffus-Radianz-Ersatz)

`diffuse_tau` (0…0,8) ist die **effektive Radianz des blockierten Sektors relativ
zum offenen Himmel** — für eine helle Putzwand ungefähr ihre Reflektanz ≈ 0,5.

- Es wirkt **ausschließlich** im Sky-View-Integral (`_row_diffuse_tau_at`
  überschreibt dort die Beam-τ); der Beam-Pfad bleibt byte-unberührt. Default
  `None` ⇒ der Diffus nutzt weiter die Beam-τ (`tau`/`tau_points`), Vor-0.22-Verhalten.
- Es ist **keine Transmission.** Wer es als „Durchlässigkeit der Wand" liest,
  missversteht das Feld. Daher die Obergrenze `HZ_DIFFUSE_TAU_MAX = 0.8`: Werte
  nahe 1 („Sektor für Diffus unsichtbar") wären das bequeme Ventil, um den
  beam-gebundenen Restfehler zu kaschieren — was das Feld ausdrücklich nicht soll.
- Unabhängig von `tau`/`tau_points`: eine halbtransparente Baumzeile **darf** es
  zusätzlich tragen (Beam weiter τ(el), Diffus dann `diffuse_tau`).

Gemessener Effekt am Referenzstandort (Release-Erratum ADR-0022 §3.5/§3.8, gegen
Brute-Force-Quadratur bestätigt): `diffuse_tau 0.5` auf den Wandzeilen hebt den SVF
von **0,2879 → 0,5761** (M4) bzw. **0,2944 → 0,5852** (M8) — die ursprüngliche
Designprognose „→ ~0,63/0,64" war ein Anteils-Rechenfehler. Der gepinnte Unit-Test
nutzt eine reine Wand-Synthetik (0,423 → 0,712), weil die geschlossene
Blend-Identität `ρ + (1 − ρ)·SVF₀` nur für einen reinen Wand-Dom gilt.

### 6.7 Sky-View-Faktor (SVF) — das Band-Integral

`sky_view_factor(plane, doy)` liefert die **relative** Reduktion des isotropen
Diffus durch den Horizont, **nicht** den Neigungs-Sichtfaktor (der steckt schon in
`(1 + cos β)/2` der Transposition; ihn erneut anzuwenden wäre Doppelzählung).
Definition im Code: `SVF = F(Horizont) / F(flach 0°)` mit
`F = (1/π) ∫∫ cos θ_i dΩ` über den sichtbaren Himmel.

- Äußeres Integral: Mittelpunkt-Quadratur über den Azimut mit
  `_SVF_AZ_SAMPLES = 360` (1°-Schritte; der Horizont ist stückweise linear).
  Inneres Integral über die Elevation **geschlossen**
  (`_inner_elevation_integral`): `G(az) = A·J2(h) + B·J1(h)` mit
  `A = sin β·cos(az − az_p)`, `B = cos β`, `J1(h) = (1 − sin²h)/2`,
  `J2(h) = (π/2 − h)/2 − sin(2h)/4`; `G` auf ≥ 0 geklemmt (die Ebene sieht die
  Rückhalbkugel nicht).
- **Halbtransparenz:** Der Himmel *unter* der Linie trägt τ seines offenen Werts
  (`_semi_transparent_column`, algebraisch `above + τ·(full − above)`). τ = 0
  reproduziert bit-exakt die alte opake Reduktion, τ = 1 exakt SVF = 1.
- **Band-Integral (0.22):** Trägt *irgendeine* Zeile der Ebene `tau_points`, wird
  der blockierte Keil `[0, h]` an **allen** Profilknoten segmentiert
  (`_profile_knot_elevations`), und jedes Segment trägt seine **Mittelpunkts-τ**
  (`_band_column`), az-interpoliert bei der Segment-Mittenelevation. Segmentbeitrag
  = `τ_mid · (inner(e0) − inner(e1))`. Sonderfälle bleiben bit-exakt: alle
  Segment-τ = 0 → opake Spalte, alle = 1 → offene Spalte (SVF genau 1).
- Der SVF ist damit **doy-abhängig** (Foliage-Rampe). Normalisiert wird mit
  derselben Quadratur über einen flachen 0°-Horizont, sodass eine unverbaute Ebene
  bei **jeder** Neigung exakt 1,0 liefert; Ergebnis in (0, 1], ein vollständig
  zugemauerter Dom endet bei `1e-6`, nie 0.
- **Memoisierung:** `lru_cache(maxsize=512)` auf `(horizon_rows, tilt, azimuth,
  doy)` — die O(360)-Quadratur läuft höchstens einmal pro (Geometrie, Tag) im
  ganzen Prozess und überlebt die 15-min-Recomputes. Invalidierung ist
  **strukturell**: geänderte Config ⇒ anderer Schlüssel.

## 7. Site-Parameter

| Feld | Ort | Default | Band / Clamp | Wirkung |
|---|---|---|---|---|
| `albedo` | `SiteConfig` | `ALBEDO_DEFAULT = 0.2` | [`SITE_ALBEDO_MIN` 0,05; `SITE_ALBEDO_MAX` 0,9] | Bodenreflex `albedo·GHI·(1−cos β)/2`. Schnee überschreibt: `snow_depth_m > SNOW_DEPTH_THRESHOLD_M (0,01 m)` ⇒ `ALBEDO_SNOW = 0.5` (`engine._slot_albedo`). |
| `bifacial_beam_gain` | `SiteConfig` | `BEAM_GAIN_DEFAULT = 1.0` | [1,0; 1,6] | Faktor **nur** auf beam+circumsolar (§4.3). |
| `efficiency` | `PlaneConfig` | `DEFAULT_EFFICIENCY = 0.96` | Validierung [0, 1] | DC-seitiger Systemwirkungsgrad. |
| `wp` | `PlaneConfig` | — | > 0 | STC-Nennleistung; POA/1000 skaliert sie. |
| `ross_coeff` | `PlaneConfig` | `ROSS_COEFF = 0.0342` | Validierung [0,005; 0,12] | Zelltemperatur `T_amb + k·POA`. |
| `inverter_efficiency` | `InverterGroup` | `DEFAULT_INVERTER_EFFICIENCY = 0.965` | [`INVERTER_EFFICIENCY_MIN` 0,80; `MAX` 1,0] | η_inv für AC-Kurve und DC-Clip-Punkt `ac_limit/η`. |
| `ac_limit_w` | `InverterGroup` | — | 0 < x ≤ 100 000 W (`AC_LIMIT_MAX_W`) | AC-Clamp der Gruppe. |

Ein **gelernter** site-weiter η_inv kann die Gruppenwerte auf der AC-Kurve
überschreiben (`LearnerHooks.inverter_efficiency`); der DC-Pfad bleibt unberührt.
Siehe `03-lernschichten-und-korrekturen.md`.

## 8. Validierungsregeln für Horizontzeilen (`_site_validation.py`)

| Regel | Fehlercode |
|---|---|
| `0 ≤ azimuth_deg ≤ 360` | `bad_horizon_azimuth` |
| `0 ≤ elevation_deg ≤ 90` | `bad_horizon_elevation` |
| `0 ≤ tau ≤ 1`; ebenso `tau_leafed`, `tau_bare` und jeder `tau_points`-/`tau_points_bare`-τ | `bad_tau` |
| `seasonal: true` ⇒ `tau_leafed` **und** `tau_bare` gesetzt | `seasonal_missing_tau` |
| `diffuse_tau` endlich und `0 ≤ x ≤ 0.8` (`HZ_DIFFUSE_TAU_MAX`) | `bad_diffuse_tau` |
| `tau_points`: 1–12 Paare; `el` **streng aufsteigend** | `bad_tau_points` |
| `tau_points`: `0 ≤ el ≤ elevation_deg` der Zeile | `tau_points_above_edge` |
| `tau_points_bare` nur bei `seasonal` **mit** `tau_points`, gleiche Länge, identisches el-Raster (Toleranz 1e-9); `tau_points_bare` ohne `tau_points` | `seasonal_points_mismatch` |
| Zeilen werden **stabil nach Azimut sortiert** zurückgegeben (nicht abgelehnt) | — |

Nicht erzwungen, aber empfohlen: letzter Knoten `τ = 1.0` **an** der Kante
(`el == elevation_deg`), damit am Gate-Übergang keine Sprungstelle entsteht.
Ebenen-/Gruppenregeln im selben Modul (Auszug): `bad_azimuth` (0…360),
`bad_tilt` (0…90), `bad_wp` (> 0), `bad_efficiency` (0…1), `bad_ross_coeff`
(endlich in [0,005; 0,12]). Ein Edit an `tau`, `tau_points`, `tau_points_bare` oder
`diffuse_tau` geht in den Config-Fingerprint des Koordinators ein; weicht er beim
Start vom gespeicherten ab, ruft `coordinator._reconcile_config_fingerprint` die
Funktion `bias.reseed_day_ahead_bias` auf und öffnet damit **jede**
Day-Ahead-Bias-Zelle für schnelle Neuanpassung: RLS-Kovarianz zurück auf
`RLS_INIT_COVARIANCE`, Stichprobenzähler `n` gedeckelt auf
`DAY_AHEAD_BIAS_RESEED_N` (= 20), θ bleibt als Startpunkt erhalten (der
Lernraten-Hebel ist die Kovarianz P, nicht n). Zusätzlich wird der neue
Fingerprint persistiert und ein Repair-Issue erzeugt. Das ist **nicht** zu
verwechseln mit dem Wertebereichs-Clamp des Biasfaktors
(`DAY_AHEAD_BIAS_MIN`/`MAX`) — siehe `03-…`.

## 9. Bekannte Modellgrenzen (ehrlich benannt)

1. **Diffus-Floor unter blockierten Sektoren.** Vor 0.22 war ein blockierter
   Sektor diffusseitig faktisch schwarz. `diffuse_tau` schließt den **isotropen**
   Anteil der gemessenen Lücke (Overcast-Kontrolltag: Faktor ~2 vollständig
   erklärt, ρ ≈ 0,5 = Wand-Albedo). Die beiden kursierenden Faktoren gehören zu
   **verschiedenen Zeitpunkten**: **~×10** ist der Ausgangsbefund an den
   SSW-Modulen **ohne** gesetztes `diffuse_tau` (so führt ihn die
   Regressionswache C8b in `scripts/validation/bsf_checks.py`, solange die
   Wandzeilen das Feld nicht tragen — Stand der Live-Config, siehe
   `05-…` §3); **~×3** ist der **erwartete Rest nach** gesetztem `diffuse_tau`
   an klaren Morgen (~90–150 Wh/Tag site-weit).
   Dieser Rest wird **nicht** über überhöhte `diffuse_tau`-Werte kaschiert.
2. **Kein Rückseiten-Beam (D3 offen).** Der Restüberschuss ist beam-**gebunden**
   (er fehlt am Overcast-Tag) und damit von keiner Diffus-/SVF-Konstruktion
   erreichbar — auch ρ = 1 nicht; Mechanismus ist bifazialer Rückseiten-Pickup des
   tiefstehenden Ost-Beams. ADR-0022 Option **D3** (`rear_beam_fraction`) ist
   entworfen, aber **zurückgestellt und im Code nicht vorhanden** (kein
   `rear_beam`-Bezeichner in `custom_components/` oder `scripts/`). Ein
   Offline-Vorabfit (Skripte nicht im Repo, daher **unbestätigt**) nennt f ≈ 0,32
   und empfiehlt ein **per-Plane**-Feld, weil ein site-weiter Wert auf den übrigen
   Ebenen Phantomertrag fabrizieren würde. `bifacial_beam_gain` kann das
   strukturell nicht ausdrücken: es multipliziert den **vorderseitigen** Beam, der
   hier exakt 0 ist.
3. **Kein Raytracing, kein Voll-Bifacial-Modell.** Explizites Nicht-Ziel
   (ADR-0022 Option D0): keine Rückseiten-Transposition, keine Wand-Leuchtdichte,
   keine Sichtfaktoren Modul↔Wand↔Boden — die Parameter sind aus den vorliegenden
   Daten nicht identifizierbar, und die Kosten verdoppelten die Transposition.
4. **Zuordnung von Verschattern ist Config-Sache.** Das Modell rechnet exakt die
   Tabelle, die konfiguriert ist. Die Bootstrap-Shademap deutet z. B. darauf hin,
   dass ein az135–175-Screen real andere Module verschattet als die, auf denen er
   konfiguriert ist — ein **Konfigurationsbefund**, kein Modellfehler; geführt in
   `06-forensik-juli-2026-und-offene-punkte.md`.
5. **Kleinere, bewusste Vereinfachungen:** Bodenreflex `albedo·GHI·(1−cos β)/2`
   wird weder vom Horizont-Gate noch vom SVF reduziert (er kommt „von unten");
   Haurwitz kennt keine Trübung/Höhe (daher k_c nur als Gate); Slotwerte sind
   Intervallmittel, der Sonnenstand ein Momentanwert am Mittelpunkt; unter
   `LOW_SUN_CUTOFF_DEG` weicht die Physik absichtlich von pvlib ab (Rb-Deckel,
   Ai := 0) — der Golden-Test prüft dort nur „keine Explosion".

## 10. Wo die Physik abgesichert ist (`tests/core/`)

`test_golden.py` prüft gegen offline erzeugte pvlib-Referenzvektoren
(`reference_vectors.json`): Sonnenstand ±0,5°, Hay-Davies-POA `max(2 W/m², 0,5 %)`
— Horizont/SVF/Elektrik liegen dort bewusst außerhalb des Vergleichs.
Modulphysik inkl. Azimutkonvention und DST-Fallen: `test_solpos.py`,
`test_clearsky.py`, `test_transpose.py`, `test_electrical.py`. Horizont:
`test_horizon.py`, `test_horizon_tau_points.py`, `test_horizon_diffuse_tau.py`,
`test_horizon_svf_cache.py` (Wrap-Interpolation, Profil-Randfälle, Band-Integral
gegen Brute-Force, Bit-Identität für Zeilen ohne die 0.22-Felder).
`test_season_regression.py` ist der Phantom-Beam-Regressionstest (§6.5);
`tests/core/test_backfill_math.py` hält die Spiegel-Invariante Engine ↔
`reconstruct_plane_hour` (Abschnitt „Engine mirror-invariant", Helfer
`_engine_raw_plane_hour`, u. a. `test_reconstruct_matches_engine_on_tau_points_diffuse`
für `tau_points` + `diffuse_tau` sowie ein `bifacial_beam_gain`-Paritätstest).
Davon **getrennt**: `tests/core/test_backfill_parity.py` prüft die Kern-↔-CLI-Parität
(`core/bootstrap_build.py` und `scripts/backfill.py` erzeugen denselben
Bootstrap-Dict), nicht die Physik-Spiegelung.

## Querverweise

Architektur/Datenfluss `01-…` · Lernschichten (Shademap-τ, Bias, Quantile) `03-…` ·
Entities/Services/Diagramm `04-…` · Anlage & Runbook `05-…` · offene Punkte,
D3-Fit, Screen-Zuordnung `06-…`.
Primärquellen im Repo: `docs/SPEC.md` §4/§5/§20.1,
`docs/adr/ADR-0022-horizont-tau-und-diffus-floor.md` (+ `…-rechnungen-diffus-floor.md`),
`CHANGELOG.md` 0.22.0.
