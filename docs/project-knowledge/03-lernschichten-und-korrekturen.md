# Lernschichten & Korrekturen

Dieses Dokument beschreibt alle adaptiven Schichten von `balcony-solar-forecast`
(Stand `main` @ v0.23.0): was jede Schicht lernt, **wogegen** sie trainiert, wo
sie auf die servierte Kurve wirkt, welche Gates/Clamps/Konstanten sie begrenzen
und wie man sie zurücksetzt. Du brauchst es, wenn die Prognose systematisch
daneben liegt und du entscheiden musst, ob das Physik-, Lern- oder Reset-Arbeit
ist — und wenn du eine Config-Änderung planst, die die RAW-Kurve verschiebt.
Nachbardokumente: `01-architektur-und-datenfluss.md`,
`02-physik-und-horizontmodell.md`, `04-ha-integration-entities-services.md`.

## 1. Überblick: fünf adaptive Schichten plus zwei Nebenlerner

Begriffe vorab. **RAW** = reine Physik-Kurve (statischer Horizont-τ, keine
gelernte Korrektur). **slow_only** = Shademap ∘ Physik, ohne Tagesbias.
**corrected** = die servierte Kurve (slow_only × θ × Intraday-Skalar,
AC-geclamped). **k_c** (Clear-Sky-Index) = Globalstrahlung geteilt durch die
Haurwitz-Klarhimmel-Referenz derselben Sonnenhöhe — dimensionslos, Geometrie und
Jahreszeit kürzen sich heraus. **θ** = multiplikativer Tagesbias-Faktor einer
RLS-Zelle. **RLS** = Recursive Least Squares, hier ein Ein-Parameter-Schätzer
mit exponentiellem Vergessen.

| Schicht | lernt | trainiert gegen | wirkt auf | persistiert | Reset |
|---|---|---|---|---|---|
| Intraday-Skalar (`core/bias`) | ein transienter Wetterfehler-Faktor | Live-Messung vs. **raw × θ** (k_c-Raum) | Slot-Faktor, ~6 h voraus | **nie** | Neustart / Ring leer ⇒ 1.0 |
| Day-ahead-Bias θ (`core/bias`) | ein θ je (Wolkenklasse × Tagesteil) | **slow_only** vs. gemessene Stundenenergie | Slot-Faktor, ganze Kurve | ja (`bias_state`) | `reset_day_ahead_bias`, Fingerprint-Reseed, Rollback |
| Shademap (`core/shademap`) | absolute Beam-Transmittanz T je Bin | Messung vs. **ungegatete** Beam-/Diffus-Referenz | Beam-Gate im Motor (ersetzt statisches τ) | ja (`shademap_state`) | Rollback, Re-Bootstrap |
| Quantile (`core/quantiles`) | empirische P10/P50/P90-Multiplikatoren | Messung vs. **issued-corrected** Stunde | P10/P50/P90-Bänder (nicht P50-Kurve!) | ja (`quantile_state`) | Rollback, Re-Bootstrap |
| Inverter-η (`core/inverter_cal`) | ein Site-Skalar η_inv | AC-Zähler vs. Summe DC-Stunden | nur die AC-Kurve | ja (`inverter_cal_state`) | kein Service; nie tragend |
| Scoreboard (`core/scoreboard`) | nichts — **bewertet** nur | issued vs. Messung vs. Baselines | Diagnose, Kill-Gate-Verdikt | ja (`scoreboard_state`) | — |
| Drift-Monitor (`_nightly`) | rollierende Tages-MAE + Streaks | corrected/slow_only vs. raw | schaltet Layer ab, rollt zurück | ja (`drift_state`) | Options-Toggle OFF→ON |

Scoreboard und Drift-Monitor sind bewusst **keine** Lerner: sie messen und
schalten, korrigieren aber nichts.

## 2. Serving-Pfad

`core/engine.compute_forecast` kennt **vier** wertetragende Lern-Hooks
(`LearnerHooks`; ein fünftes Feld `correction_source` trägt nur Provenienz und
ändert die Rechnung nicht) und wendet sie so an: **`beam_tau`** ersetzt das
statische Horizont-τ im Beam-Gate (Beam + Zirkumsolar; die isotrope
Diffusstrahlung behält den statischen Sky-View-Factor) → `clamp_groups`
(AC-Grenze je Gruppe) → **`slot_factor`** (Produkt aus θ und Intraday-Faktor,
gebaut in `coordinator._build_learner_hooks`) → **erneutes** `clamp_groups`,
damit ein Faktor > 1 die Kurve nie über die AC-Grenze hebt → **`band_by_slot`**
multipliziert die Quantil-Bänder auf die servierte Slot-Leistung. Auf der
AC-Seite ersetzt **`inverter_efficiency`** (gelerntes η_inv, §11) vor
`electrical.clamp_groups_ac` das je Gruppe konfigurierte η; die DC-Kurve bleibt
unberührt. Der Motor rechnet RAW und CORRECTED parallel; ohne Hooks sind beide
bit-identisch. Merksatz: Shademap wirkt **innerhalb** der Physik, θ und Intraday **außerhalb**,
Quantile nur auf die Bänder.

## 3. Day-ahead-Bias (RLS) — `core/bias.py`

### Was gelernt wird

Ein Skalar θ pro Zelle `"{cloud_class}|{day_part}"` (4 Klassen × 3 Tagesteile =
max. 12 Zellen), Modell `measured_wh ≈ θ · modeled_wh`, geschätzt per
Ein-Parameter-RLS mit exponentiellem Vergessen (`_rls_step`):
`K = P·x/(λ + x·P·x)`, `θ += K·(y − θ·x)`, `P = (P − K·x·P)/λ` mit
`x = modeled_wh`, `y = measured_wh`. Degenerierte Schritte (nicht-finit,
`x <= INTRADAY_MIN_MODELED_WH`, `y < 0`, kollabierender Nenner) werden
**übersprungen** — `n` wächst dann nicht, eine dunkle oder kaputte Nacht kann
eine Zelle also nicht altern lassen.

### Wogegen trainiert wird (wichtig, seit 0.21/B2)

`_nightly.train_day_ahead` nimmt als Modell-Seite **`snap.slow_only_hourly_wh`**
— die Shademap-korrigierte, aber bias-freie Kurve — mit Fallback auf `raw` und
zuletzt `corrected`. Grund: θ wird *auf* die Shademap-Kurve angewandt; träfe man
gegen reines RAW, korrigierten Shademap und θ denselben Verschattungsfehler
doppelt, sobald die Shademap lernt. `slow_only` ist `{}`, wenn die Slow-Schicht
gerade inaktiv ist (dann gilt slow_only == raw), und entsteht pro Nacht in
`coordinator._slow_only_hourly` in einem zusätzlichen Motorlauf. Die Mess-Seite
ist bevorzugt die **echte Stundenenergie** je Zelle (`hourly_actuals_log`, Summe
über alle Kanäle); ohne Stundenring wird die Tagessumme nach modelliertem
Anteil auf die Zellen verteilt (grober Fallback).

### Anwendung

`coordinator._build_learner_hooks` baut je Wetter-Slot: Wolkenklasse via
`classify_cloud`, Sonnenzeit via `solpos.hours_from_solar_noon`, dann
`bias.day_ahead_factor_solar`. Dieser blendet an den beiden inneren
Tagesteil-Grenzen linear zwischen den Nachbarzellen (Halbbreite
`DAY_PART_SOLAR_BLEND_HALFWIDTH_H`), damit die Korrektur nicht springt. Nur
Faktoren ≠ 1.0 landen im `day_factor`-Dict, das zusätzlich als
`self._day_factor` gecacht wird (Referenz für den Intraday-Sampler, §5).
Tagesteile bildet seit v0.19 `day_part_for_solar` nach **Sonnenzeit**:
midday = |Stunden von Sonnenmittag| < `MIDDAY_SOLAR_HALFWIDTH_H`. Die
Wanduhr-Variante (`day_part_for_hour`, Grenzen 10:00/14:00, Blend 45 min) ist
nur noch Fallback ohne Längengrad-Info; historische Analysen mit dem
„10-Uhr-Sprung" meinen diese alte Binnung ohne Blend.

### Konstanten (`const.py`)

| Name | Wert | Bedeutung |
|---|---|---|
| `DAY_AHEAD_BIAS_MIN` / `_MAX` | 0.5 / 1.5 | Clamp auf θ bei jedem Schreiben |
| `DAY_AHEAD_BIAS_NEUTRAL` | 1.0 | Rückfall bei fehlender/kalter Zelle |
| `RLS_FORGETTING_FACTOR` | 0.98 | λ, diskontiert alte Tage |
| `RLS_INIT_COVARIANCE` | 1000.0 | P₀, große Anfangs-Lernrate |
| `RLS_MIN_SAMPLES` | 3 | `BiasState.get_bias` liefert unter n<3 neutral |
| `DAY_AHEAD_BIAS_RESEED_N` | 20 | n-Deckel beim Fingerprint-Reseed |
| `MIDDAY_SOLAR_HALFWIDTH_H` | 2.0 | Mittagsfenster um Sonnenmittag |
| `DAY_PART_SOLAR_BLEND_HALFWIDTH_H` | 0.75 | Blendbreite in Sonnenstunden |

### Reset-Pfade

* **`reset_day_ahead_bias`** (Aktion): löscht **alle** Zellen
  (`coordinator.async_reset_day_ahead_bias`); Shademap, Drift-State und
  Rollback-Ring bleiben unberührt. Layer meldet danach `cold_start`.
* **Fingerprint-Reseed** (§10): behält θ, öffnet nur die Kovarianz und deckelt n.
* **`rollback_learners`**: setzt Bias + Shademap + Quantile gemeinsam auf einen
  Ring-Snapshot zurück.
* **Drift-Auto-Disable** rollt gezielt nur die Fast-Schicht zurück (§8).

## 4. Wolken-Klassifikation — `bias.classify_cloud`

1. **Nebel zuerst** (überschreibt alles): `visibility_m < FOG_VISIBILITY_M`
   (1000 m) **oder** (`cloud_low > FOG_CLOUD_LOW_PCT` = 85 % **und** Monat in
   `FOG_MONTHS` = Okt–Feb).
2. **k_c-Split** (seit 0.21, „A5"), wenn GHI und Sonnenhöhe vorliegen und
   `elevation_deg >= CLOUD_KC_MIN_ELEVATION_DEG` (5°):
   `k_c >= CLOUD_KC_CLEAR_MIN` (0.65) ⇒ clear, `k_c <= CLOUD_KC_OVERCAST_MAX`
   (0.30) ⇒ overcast, dazwischen mixed.
3. **Fallback** (Dämmerung / kein GHI): Random-Overlap-Gesamtbedeckung
   `100·(1−(1−low)(1−mid)(1−high))`, `< CLOUD_CLEAR_MAX_PCT` (25 %) ⇒ clear,
   `> CLOUD_OVERCAST_MIN_PCT` (75 %) ⇒ overcast.

Warum die alte Logik schadete: die reine Schichtbedeckung zählte mittlere und
hohe Wolkendecks mit vollem Random-Overlap-Gewicht. Cirrus über blauem Himmel
ergab damit „overcast" — im Forensik-Fenster wurden so sonnige Nachmittage in die
Overcast-Zelle geroutet und θ, Quantil-Bins und Scoreboard-Strata gleichzeitig
vergiftet. k_c misst dagegen, was tatsächlich unten ankommt.

Die Taxonomie ist **geteilt**: dieselbe Klasse keyt θ-Zelle, Quantil-Bin und
Scoreboard-Stratum. Deshalb existiert `CLASSIFIER_VERSION` (aktuell **2**), das
in den Config-Fingerprint eingeht: ändert sich die *Bedeutung* der Labels,
werden die θ-Zellen re-seeded. Automatisch passiert bei Quantilen und Scoreboard
nichts: für die Quantil-Bins hilft ein Re-Bootstrap (er ersetzt den Ring); der
Scoreboard-Ring wird von **keinem** Reset-, Rollback- oder Bootstrap-Pfad
angefasst und rollt via `scoreboard.trim_window` binnen des Fensters (Default
14 Tage) von selbst aus.

## 5. Intraday-Skalar — `bias.compute_intraday_scalar`

**Transient, nie persistiert.** Über ein Trailing-Fenster:
`s = Σ wᵢ·measured_kcᵢ / Σ wᵢ·modeled_kcᵢ` mit `wᵢ = exp(−alterᵢ/τ)`, beide
Seiten vorher durch dieselbe Haurwitz-Referenz geteilt (k_c-Raum). Ein
Verhältnis von Summen, kein Mittel von Verhältnissen — Slots mit winzigem
Nenner können den Wert dadurch nicht dominieren; der Ebenen-Mix kürzt sich,
weil beide Seiten dieselbe Referenz benutzen.

**Sample-Bildung (seit 0.21/A2 der entscheidende Fix):**
`coordinator._build_intraday_sample` liest alle `actual_entity`-Sensoren,
merkt sich die Menge der **nutzbaren** Ebenen und normiert die Modellseite auf
genau diese Teilmenge (Kanal-Dropout darf nicht als Produktionsdefizit
erscheinen). Die Modellseite ist `pr.raw_watts` **× θ** des Slots
(`_modeled_power_for_planes`) — also die servierte Kurve *ohne* den Intraday-
Faktor. Vorher wurde gegen reines RAW gesampelt, wodurch θ (morgens 1.36–1.49)
und der Skalar denselben Fehler zweimal korrigierten; das Verhältnis
serviert/gemessen erreichte ×1.9 um 07–09Z. θ ist über den Tag eingefroren, es
gibt also keine Rückkopplung.

**Gates und Konstanten:**

| Name | Wert | Wirkung |
|---|---|---|
| `INTRADAY_TAU_MINUTES` | 90.0 | Zeitkonstante der exponentiellen Gewichtung |
| `INTRADAY_TRAILING_WINDOW_MINUTES` | 240.0 | Fenster, ältere Samples fallen raus |
| `INTRADAY_MIN_TRAILING_MINUTES` | 120.0 | Spanne ältestes↔jüngstes Sample; darunter neutral |
| `INTRADAY_MIN_MODELED_WH` | 5.0 | Slot-Gate gegen Division nahe Null |
| `INTRADAY_APPLY_HORIZON_MINUTES` | 360.0 | Vorwärtsfenster |
| `INTRADAY_SCALAR_MIN` / `_MAX` | 0.25 / 2.5 | Clamp |
| `INTRADAY_NEUTRAL` | 1.0 | Rückfallwert |

**Anwendung mit Decay:** `intraday_factor_at` rampt linear von `s` (jetzt) auf
1.0 am Horizont; jenseits davon exakt 1.0. Im `slot_factor` wird nur auf Slots
angewandt, die nicht vollständig vergangen sind (`age_min > −15.0`).

**Re-Arm nach Neustart (0.21/T7 bzw. A7/SCT-2):** Der Skalar darf nie
persistiert werden, die *Samples* sind aber reproduzierbare Messwerte. Beim
ersten Tick mit Vorgängerkurve und Wetterstatus `fresh`/`cached` rekonstruiert
`_async_rearm_intraday_ring` den Ring aus den 5-Minuten-Statistiken des
Site-Summen-DC-Sensors plus der θ-korrigierten Kurve; die Modellseite wird dabei
auf die **bemessenen** Ebenen (mit `actual_entity`) eingeschränkt — sonst
halbierte ein teilbemessener Standort den Skalar nach jedem Reload bis an den
Clamp-Boden. Der One-Shot wird erst verbraucht, wenn ein echter Versuch möglich
ist; scheitert er, bleibt der Ring leer = neutral.

## 6. Shademap (Slow Learner) — `core/shademap.py`

### Was gelernt wird

Je Messkanal (Ebenenname) und Bin die **beam-referenzierte Transmittanz**
`T = (P_gemessen − P_diffus_modelliert) / P_beam_modelliert` als EMA.
Bewusst *nicht* das Gesamtverhältnis: im Schatten enthält die Messung
weiter den Diffus-Sockel, ein Gesamt-Ratio auf den Beam angewandt überschätzt
die Verschattung und schreibt diffus-unabhängige Verluste (Soiling, η-Fehler)
dem Beam zu.

**Bin-Key** (`shademap_bin_key`): `"{az_idx}:{el_idx}:{half}"` mit
`SHADEMAP_AZ_BIN_DEG` 5.0, `SHADEMAP_EL_BIN_DEG` 2.5 und dem Halbjahr-Index
(`half_year_index`: doy < `SUMMER_SOLSTICE_DOY` = 172 ⇒ 0, sonst 1). Das
Halbjahr verhindert, dass April (laublos) und August (belaubt) im selben
Sonnenstands-Bin aliasen. **T ist absolut**, nicht relativ zum Prior — ein
besserer statischer Prior verkleinert also nur das Residuum, es entsteht keine
Doppelmodellierung.

### Trainingsziel und Referenz

`_nightly.train_channel` rechnet gegen `snap.per_plane[channel]`, also die
**ungegatete, unclamped** Beam-/Diffus-Referenz des Motors
(`beam_ref_watts`/`diffuse_ref_watts`, „FIX-3"). Gegen die bereits gegatete
Serie würde T sich selbst referenzieren (Richtung √T_wahr), und ein Wand-Bin mit
statischem τ=0 hätte ~0 modellierten Beam — untrainierbar.

### Gates

* **Tages-Gate:** gemessene Site-Energie ≥ `SHADEMAP_MEASURED_CLEAR_MIN_FRAC`
  (0.8) der modellierten RAW-Tagessumme, sonst trainiert der Tag **nichts**.
* **Collapse-Freeze** (§8) und Kill-Switch `slow_enabled`.
* **Quasi-klar je Stunde** (`is_quasi_clear`): k_c innerhalb
  `[lo(el), SHADEMAP_KC_HI=1.35]`, wobei lo linear von
  `SHADEMAP_KC_LO_LOW_SUN` (0.65) bei 0° auf `SHADEMAP_KC_LO_HIGH_SUN` (0.85)
  ab `SHADEMAP_KC_PIVOT_ELEV_DEG` (20°) ramp;  modellierter Beam-Anteil >
  `SHADEMAP_MIN_BEAM_SHARE` (0.05 · Wp); Nachbar-Stabilität: relative Änderung
  des **gemessenen** Verhältnisses zum Vorstunden-Wert <
  `SHADEMAP_NEIGHBOUR_STABILITY` (0.15). Die Stabilität keyt bewusst auf die
  Messreihe, nicht auf das glatte Prognose-k_c — nur so fällt eine echte
  Wolkenlücke auf.
* **Kanal-Gates in `_actuals._actuals_from_stats` — wirken auf ALLE Lerner, nicht
  nur die Shademap:** ein *frozen* Kanal (gleicher Nicht-Null-Stundenmittelwert
  über `LABEL_FROZEN_MIN_REPEATS` = 4 Stunden, `_is_frozen_channel`), ein
  *fehlender* Kanal (konfiguriertes Modul ohne eine einzige brauchbare LTS-Zeile
  — toter DTU-Port) oder ein Modul unter `DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE`
  (0.75) der Tageslichtstunden verwerfen jeweils den **ganzen Tag** für alle
  Kanäle (`return {}, {}`) — eine Teil-Site-Messung darf nie Ground Truth gegen
  die Voll-Site-Modellenergie werden, sie läse sich als Produktionsdefizit. Damit
  fallen Shademap, Day-ahead-RLS, Quantil-Ring **und** Scoreboard für den Tag aus
  (alle brauchen `actuals`); er wird nicht als trainiert markiert und von einem
  Catch-up erneut versucht.

### Update, Clamp, Shrinkage

`update_bin`: neues Bin startet beim geclampten Sample, sonst
`τ = (1−a)·τ_alt + a·sample` mit **adaptivem** `a = max(SHADEMAP_EMA_ALPHA,
1/(n_alt+1))` — ein junges Bin ist damit exakt das arithmetische Mittel seiner
Samples, bis der feste α (0.15) übernimmt. Clamp `[SHADEMAP_TAU_MIN=0.0,
SHADEMAP_TAU_MAX=1.1]`: volle Okklusion (Hauswand) muss darstellbar sein, 1.1
lässt Kopfraum für Reflexionsgewinne.

**Anwendung** (`effective_tau` / `effective_tau_pooled`): Shrinkage-Blend gegen
den statischen Prior mit `w = n / (n + SHADEMAP_SHRINKAGE_K)`, K = 20 — ein
unbesuchtes Bin liefert damit **exakt** den Prior, kein harter
Min-Sample-Schalter. Die Speicherung ist immer pro Ebene; **Pooling passiert nur
beim Lesen** (`coordinator._build_shade_pool_map`) als n-gewichtetes Mittel über
die Pool-Kanäle plus dasselbe Shrinkage mit `n_pool` — Gruppieren und Auflösen
ist dadurch verlustfrei reversibel. `ingest_bootstrap_shademap` deckelt jedes
Bin-`n` auf `BOOTSTRAP_MAX_BIN_N` (5), weil stundengeschmierte Backfill-Bins
weniger vertrauenswürdig sind als 15-Minuten-Livedaten.

## 7. Quantile P10/P50/P90 — `core/quantiles.py`

**Was gelernt wird:** je Bin `"{cloud_class}|{day_part}"` (dieselbe Taxonomie
wie θ) ein datierter Ring relativer Fehler `relerr = measured_wh /
corrected_wh`, geclamped auf `[QUANTILE_REL_ERR_MIN=0.0,
QUANTILE_REL_ERR_MAX=5.0]`; nur Stunden mit `corrected_wh >
QUANTILE_MIN_FORECAST_WH` (5.0). Trainiert wird gegen die **issued-corrected**
Kurve — dieselbe, die das Scoreboard bewertet und auf die die Bänder später
multipliziert werden (kein Leck, konsistenter Bezugsrahmen).

**Ring-Fenster:** primär datumsbasiert (`QUANTILE_RING_DAYS` = 90 Tage relativ
zum Trainingstag), als harter Backstop ein Count-Cap von
`QUANTILE_RING_DAYS × QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN` (90 × 8 = 720); bei
Überlauf fliegen zuerst undatierte Legacy-Samples, dann die ältesten.

**Gates (`bands_for_bin`):** ein Bin liefert nur dann echten Spread, wenn
`n >= QUANTILE_MIN_SAMPLES` (20) **und** `effective_days >= QUANTILE_MIN_DAYS`
(5). `effective_days` = Anzahl verschiedener Datumsstempel plus
`ceil(undatiert / 8)` als beweisbare Untergrenze. Begründung: Stunden desselben
Tages sind stark korreliert; drei „bursty" Tage à 8 Stunden sind ~3 unabhängige
Beobachtungen, nicht 24.

**Neutral-Band:** ein zu dünnes oder tagesarmes Bin gibt `QuantileBands.neutral()`
zurück, also `p10 == p50 == p90 == 1.0`. Kein Fake-Spread **und** kein
Fake-Shift — auch der empirische Median wird unterdrückt, weil er bei n<20 von
einem einzelnen geclampten Ausreißer dominiert wäre; ein Slot ohne Band läuft im
Motor unverändert durch. Perzentile per Typ-7-Interpolation
(`empirical_percentile`), Monotonie p10 ≤ p50 ≤ p90 zusätzlich abgesichert.

**Seeding aus dem Bootstrap (0.21/A6):** `core/bootstrap_build.py` bildet je
Tageslichtstunde `corrected = clamp(θ_Zelle) × modelled_site` (θ *nach* dem
RLS-Schritt desselben Tages) und füttert die Samples durch die **live**
`quantiles.train_quantiles` — identische Taxonomie, Clamps, Datumsfenster und
Caps, plus ein Per-Bin-Per-Tag-Deckel von 8. Ohne dieses Seeding war am Tag 0
nur die Overcast-Bin trainiert und jedes andere Band kollabierte wochenlang auf
P50. `store.import_bootstrap` übernimmt den Abschnitt **additiv**: fehlt der
Schlüssel (alte Backfill-Datei), bleibt der Live-Ring unangetastet.

**Ensemble-Fusion (optional, Standard AUS):** ist das Ensemble aktiv, wird pro
Slot das gelernte Band mit dem Ensemble-Spread per Hüllkurven-Maximum kombiniert
(`core/ensembleband.fuse_bands`) — das breitere gewinnt, nie multiplikativ, nie
verengend; Provenienz in `DATA_KEY_BAND_SOURCE` / `..._BY_DAY`.

## 8. Guards: Drift-Monitor, Collapse-Detektor, Rollback-Ring

**Drift-Monitor** (`_nightly.update_drift`) zerlegt `corrected = slow ∘ fast`
und schreibt die Schuld pro Schicht zu: SLOW verliert, wenn `MAE(slow_only)`
gegen `MAE(raw)` verliert; FAST, wenn `MAE(corrected)` gegen `MAE(slow_only)`
verliert. „Verlieren" heißt: schlechter um mehr als `DRIFT_LOSS_MARGIN` (2 %, relativ)
**und** mehr als `DRIFT_LOSS_MIN_ABS_WH` (50 Wh, absolut) — die absolute
Schwelle verhindert, dass Rundungsrauschen an klaren Tagen sieben Münzwürfe
hintereinander als Verlustserie zählt. Als MAE-Proxy dient der absolute
**Tagesenergie-Fehler in Wh**: je Kurve (raw / slow_only / corrected) die auf den
lokalen Trainingstag geschnittene Stundensumme minus der gemessenen Tagesenergie,
als Betrag — dieselbe Einheit wie die Schwelle `DRIFT_LOSS_MIN_ABS_WH` und wie der
Sensor `drift_mae_corrected` (Einheit Wh, `MEASUREMENT`, kein `device_class`, weil
ein Fehlermaß keine Energiemenge ist). „Tages-kWh" meint im Code-Kommentar nur die
*Metrik-Familie* (Tagesenergie statt Stunden-MAE, SPEC §15.1), nicht die Einheit; der
Ring `daily_mae` speichert Wh, auf 2 Nachkommastellen gerundet. Das Fenster ist
`DRIFT_WINDOW_DAYS` (7). Fehlt die
slow_only-Kurve (Legacy-Snapshot oder Slow inaktiv), treibt das alte gemeinsame
Signal corrected-vs-raw beide Streaks.

`DRIFT_LOSS_STREAK_DAYS` (7) aufeinanderfolgende Verlusttage ⇒ Layer
**auto-disable** + Repair-Issue + Rollback dieser einen Schicht auf den Snapshot
von vor der Serie (`restore_layer_snapshot`, Index `len(ring) − 7`; der Ring
`LEARNER_SNAPSHOT_RING` = 10 ist absichtlich länger als der Streak). Das Flag
bleibt, bis der Betreiber die Option im Options-Flow **OFF→ON** schaltet
(`rebuild_learner_config` erkennt genau diesen Übergang — ein bloßer Neustart
löscht nichts).

**Collapse-Detektor** (`is_collapse_day`): gemessene Tagesenergie <
`COLLAPSE_MEASURED_MAX_FRAC` (5 %) der Prognose, und nur wenn die Prognose >
`COLLAPSE_FORECAST_MIN_WH` (500 Wh) war. Das friert **beide geometrischen
Schichten für den Folgetag** ein (Schnee liegt morgens noch) und überspringt das
Training des Kollapstages. Nur der geclampte Intraday-Skalar reagiert dann noch.

**Idempotenz:** `trained_days` (Ring 120) markiert verarbeitete Tage — RLS-Schritte
und Streak-Zähler sind *nicht* selbst idempotent, ohne diese Marke würde jeder
Neustart via Catch-up (`NIGHTLY_CATCHUP_MAX_DAYS` = 3) denselben Tag doppelt
trainieren. Markiert wird erst, wenn *beide* Seiten (issued + actuals) vorlagen.

## 9. Scoreboard & Kill-Gate — `core/scoreboard.py` + `_scoreboard_glue.py`

Das Scoreboard **lernt nichts**; es vergleicht nächtlich pro geschlossenem Tag:
die eigene Prognose **as issued**, jede konfigurierte Vergleichs-Entity **wie
sie damals stand**, gegen die gemessene Tagesenergie.

* **Leckfreiheit:** der Engine-Wert kommt aus dem issued-Ring (nie neu
  gerechnet); Vergleichswerte werden aus der Recorder-Historie am **gleichen
  Day-ahead-Horizont** gelesen (erster brauchbarer Zustand ab 01:30 lokal,
  Fenster 8 h). Ein Snapshot, der erst nach 06:00 lokal geschrieben wurde, wird
  gar nicht gewertet (Nowcast-Schutz).
* **Fenster:** `DEFAULT_SCOREBOARD_WINDOW_DAYS` = 14, konfigurierbar.
* **Strata:** dominante Wolkenklasse des Tages, **energiegewichtet** über die
  issued-Stunden (eine Nebelmorgen-Nacht kann den Tag nicht als „clear" wählen).
  Unter `SCOREBOARD_STRATUM_MIN_N` (3) Tagen wird der Prozentwert unterdrückt
  und `low_n: true` gesetzt.
* **Baselines:** matched-pair — Engine- und Vergleichs-MAE nur über die
  gemeinsamen Tage; gewertet wird der *kleinste* Vorsprung (die stärkste
  Baseline). `SCOREBOARD_MIN_PAIRED_DAYS` = 1.
* **Verdikt** (`kill_gate_passed`): `True` nur bei vollem Fenster, frischem Ring
  (`SCOREBOARD_MAX_STALENESS_DAYS` = 3) und Vorsprung ≥ `gate_margin·100`
  (Default 10 %). `None` heißt „unentschieden" — zu wenige Tage, veralteter Ring
  oder **keine** Baseline; eine fehlende Baseline ist kein Verlust.

Das Verdikt ist rein informativ (Binary-Sensor + Dashboard + Diagnostics); es
schaltet nichts ab. Der einzige Automatismus, der Schichten deaktiviert, ist der
Drift-Monitor.

## 10. Config-Fingerprint — `coordinator._config_fingerprint`

Ein SHA-256-Kurzhash (16 hex) über genau die Felder, auf die die θ-Zellen
konditioniert sind: je Ebene Name, `azimuth_deg`, `tilt_deg`, `wp`,
`efficiency`, `ross_coeff` und **jede Horizontzeile** (`azimuth_deg`,
`elevation_deg`, `tau`, `seasonal`, `tau_leafed`, `tau_bare` sowie — 0.22,
nur-wenn-gesetzt — `tau_points`, `tau_points_bare`, `diffuse_tau`); dazu
`albedo`, `bifacial_beam_gain`, je Wechselrichtergruppe **Name und** `ac_limit_w`
(Segment `grp:{name}:ac{limit}`) und `CLASSIFIER_VERSION`. Achtung: schon das
Umbenennen einer Gruppe flippt den Fingerprint und löst einen Bias-Reseed samt
Repair-Issue aus, obwohl es die modellierte Kurve nicht verändert. Alle Zahlen
gerundet, damit eine Float-Reserialisierung
den Hash nicht verschiebt; die 0.22-Felder erscheinen nur, wenn gesetzt — eine
Legacy-Config behält nach dem Upgrade ihren Fingerprint und wird **nicht**
re-seeded. Bewusst **draußen**: Entity-IDs, `shade_group`, Zählervorzeichen —
alles, was die modellierte Kurve nicht verändert.

**Bei Änderung** (`_reconcile_config_fingerprint`, läuft bei jedem Setup und
jedem Options-Reload): ist kein Fingerprint gespeichert (Erstinstallation), wird
der aktuelle nur **notiert** — kein Reseed. Unterscheidet er sich, läuft
`bias.reseed_day_ahead_bias`: jede Zelle behält ihr θ, bekommt aber
`covariance = RLS_INIT_COVARIANCE` zurück und `n = min(n,
DAY_AHEAD_BIAS_RESEED_N)`. Die RLS-Verstärkung hängt an **P**, nicht an n — bei
λ=0.98 und n≈100 bewegt sich θ sonst nur ~0.001/Tag, die Neuanpassung dauerte
Monate. Anschließend wird der neue Fingerprint persistiert, ein INFO-Log
geschrieben und das Repair-Issue `ISSUE_CONFIG_CHANGED_BIAS_RESEED` gesetzt.

Nicht automatisch angefasst werden Shademap, Quantil-Ring und Scoreboard — deren
Inhalt ist nach einer Geometrieänderung ebenfalls semantisch veraltet. Für
Shademap und Quantile ist der Re-Bootstrap gedacht (`run_bootstrap`, `dry_run`
standardmäßig `true`; `store.import_bootstrap` schreibt genau `bias_state`,
`shademap_state`, `quantile_state` — letzteres additiv, siehe §7). Der
**Scoreboard-Ring ist von allen diesen Pfaden ausgenommen** (auch der
Rollback-Snapshot `LearnerSnapshot` führt nur bias/shademap/quantile) und altert
nur über sein Fenster aus. Siehe `05-anlage-und-betrieb-runbook.md`,
`docs/BACKFILL.md`.

## 11. Nebenlerner: Inverter-Wirkungsgrad — `core/inverter_cal.py`

Lernt **einen** Site-Skalar η_inv aus dem Gesamt-AC-Zähler (`ac_actual_entity`)
gegen die Summe der DC-Stundenwerte. Nur Stunden mit DC ≥
`INVERTER_CAL_MIN_LOAD_W` (100 W) und ohne Clipping-Verdacht
(`INVERTER_CAL_CLIP_HEADROOM_FRAC` 0.90 der Gruppen-AC-Decke) zählen; Ratios
außerhalb `[INVERTER_CAL_MIN, INVERTER_CAL_MAX]` = [0.90, 0.99] werden
**verworfen** (nicht geclamped) und erhöhen n nicht. EMA mit adaptivem Warm-up
wie bei der Shademap (`INVERTER_CAL_EMA_ALPHA` 0.10); `effective_eta` liefert
erst ab `INVERTER_CAL_MIN_SAMPLES` (20) einen Wert, sonst `None` ⇒ Motor nutzt
die konfigurierte/Default-Effizienz. **Nie tragend**, formt nur die AC-Kurve.
Ein Roh-Median der Ratios (inkl. verworfener) bleibt transient als Diagnose:
skalieren die DC-Sensoren falsch, liegt die wahre Ratio außerhalb des Bandes
und n bleibt 0.

## 12. Wechselwirkungen und Fallen

**RAW ist die Lern-Wahrheit.** Jede Schicht muss gegen genau die Kurve
trainieren, auf die sie *angewandt* wird — sonst korrigieren zwei Schichten
denselben Fehler. Der Stapel ist deshalb sauber geschichtet: Shademap gegen die
**ungegatete** Physik-Referenz, θ gegen **slow_only**, Intraday gegen
**raw × θ**, Quantile gegen **issued-corrected**. Alle drei historischen
Doppelkorrektur-Bugs (Shademap-Selbstreferenz „FIX-3", θ-gegen-raw „B2",
Intraday-gegen-raw „A2") hatten dieselbe Signatur: ein Faktor, der einen bereits
korrigierten Fehler noch einmal korrigiert, sichtbar als Übertreibung am Morgen.

**Ein besserer Prior schlägt einen kompensierenden Lerner.** Alle Lerner sind
geclamped: θ ∈ [0.5, 1.5], Shademap-τ ∈ [0.0, 1.1], Intraday ∈ [0.25, 2.5].
Schiebt man einen statischen Modellfehler in einen Lerner, klemmt der irgendwann
am Rand (**Clamp-Sättigung**) — dann wächst der Fehler weiter, aber die
Korrektur nicht mehr, und der Layer ist zusätzlich blind für echte
Wetterabweichungen, weil er seinen ganzen Hub verbraucht hat. Genau das war der
Befund hinter ADR-0022: eine morgendliche `clear|morning`-Zelle stand dauerhaft
am 1.5-Deckel (belegt, ADR-0022). Die M4/M8-Shademap-Morgenbins standen ADR-0022
zufolge **mutmaßlich** am 1.1-Deckel, weil ein fehlender Diffus-Sockel als
Phantom-Beam gelernt werden musste — eine plausible, aber nicht am Bin-Dump
verifizierte Vermutung. Die Reparatur
war **Physik** (`tau_points`, `diffuse_tau`), nicht mehr Lernhub. Diagnose-Signal:
das Feld `clamped: true` je Zelle in `day_ahead_bias_status`.

**Reihenfolge bei Config-Änderungen.** Immer: (1) Physik korrigieren
(Horizontzeilen, τ-Profile, Albedo, Beam-Gain) — die Config lügt sonst weiter;
(2) Reset/Reseed der davon abhängigen Lerner; (3) Re-Bootstrap, damit Shademap
und Quantile nicht monatelang gegen den alten Prior konvergieren müssen; (4) eine
Übergangswoche einplanen — 3–7 Tage Überschießen, während die Bias-Zellen sich
auf die verschobene RAW-Kurve neu einstellen, sind *erwartet* und kein
Rückschritt. Der Fingerprint erledigt Schritt 2 automatisch, aber **nur für den
Day-ahead-Bias**.

**Was ein Reset NICHT heilt.**

* Einen falschen Prior: nach `reset_day_ahead_bias` lernt die Zelle exakt
  denselben Fehler wieder — nur langsamer (n<`RLS_MIN_SAMPLES` = 3 Tage neutral).
* Eine falsch zugeordnete Verschattung: ein Screen, der real M2/M3 verschattet,
  aber bei M4/M8 konfiguriert ist, erzeugt in beiden Kanälen dauerhaft falsche
  Bins — die Shademap lernt die Realität pro Kanal, kann aber die statische
  Fehlzuordnung nicht sichtbar machen. Das findet nur der Vergleich
  Shademap-Polartabelle vs. Config (siehe `06-forensik-juli-2026-...md`).
* Fehlende oder kaputte Trainingsdaten: ohne `hourly_actuals` trainiert die
  Shademap gar nicht und θ fällt auf die grobe Tagesverteilung zurück; bei
  eingefrorenen DTU-Sensoren oder Recorder-Lücken verwerfen die Gates den Tag
  stillschweigend — die Schicht wirkt „tot", ist aber korrekt vorsichtig.
* Semantisch veraltete Quantil-Bins/Scoreboard-Strata nach einem
  `CLASSIFIER_VERSION`-Bump: dafür gibt es keinen automatischen Reset.

**Kalt-Start ist ehrlich, nicht kaputt.** `cold_start` (Bias ohne Zellen),
neutrale Bänder (Bin unter dem Doppel-Gate), `kill_gate_passed = None` (Fenster
noch nicht voll) und ein τ exakt gleich dem statischen Prior (n=0) sind korrekte
Zustände. Sie als Fehler zu lesen führt zu unnötigen Resets — die den Kalt-Start
nur verlängern.
