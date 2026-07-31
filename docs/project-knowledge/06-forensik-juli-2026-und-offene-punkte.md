# Forensik Juli 2026 & offene Punkte

**Worum es geht:** Zwischen dem 16. und 25. Juli 2026 wurde `balcony_solar_forecast` von einer
Integration, deren Lernschichten seit Monaten hungerten, zu einer, deren Fehlerkette vollständig
seziert und in drei Releases (0.21.0 / 0.22.0 / 0.23.0) abgearbeitet ist. Dieses Dokument hält die
**Wissens-Essenz** dieser Analyse fest: die bestätigte Ursachenkette mit Zahlen, die gemessene
Wirkung nach dem Deployment, die noch offenen Punkte mit Status — und die Methodik-Lehren.
**Du brauchst es**, wenn du eine Regression einordnen, einen offenen Punkt weiterführen oder eine
neue Analyse aufsetzen willst, ohne die Herleitung noch einmal zu machen.

Alle Code-Aussagen sind gegen `main` @ v0.23.0 geprüft (Datei + Funktions-/Konstantenname, keine
Zeilennummern). Mess-/Betriebszahlen stammen aus dem Live-Abzug der Betreiber-Instanz und sind
**Momentaufnahmen** — sie belegen das Verhalten am jeweiligen Datum, nicht den heutigen Zustand.
Nachbardokumente: `02-physik-und-horizontmodell.md` (Physik/Horizont), `03-lernschichten-und-korrekturen.md`
(Lerner), `04-ha-integration-entities-services.md` (Services/Diagnostics), `05-anlage-und-betrieb-runbook.md`
(Anlage/Betrieb), `07-entwicklung-tests-release.md` (Tests/Release).

---

## 1. Chronik kompakt

### 1.1 Ausgangslage — der Actuals-Epoch-Bug (bis 16.07.2026)

Der nächtliche Actuals-Reader normalisiert die `start`-Spalte der Recorder-Langzeitstatistiken
(Long Term Statistics, LTS) zu einem Stunden-Schlüssel. Die **In-Process**-Recorder-API
(`statistics_during_period`) liefert `start` als Float in **Epoch-Sekunden**; die WebSocket-Schicht
multipliziert auf **Millisekunden**. Der Code nahm Millisekunden an — mit der Folge, dass alle
24 Stundenzeilen eines Tages auf **einen** Schlüssel im Jahr 1970 kollabierten. Das
Tages-Vollständigkeits-Gate meldete daraufhin „covers only 1 of ~16 daylight hours" und
**verwarf jeden Tag**.

Konsequenz im Feld: Day-ahead-Bias, Shademap-Training, Scoreboard/Kill-Gate, Quantil-Bänder und
Drift-Monitoring erhielten **seit dem Landen des Gates keinen einzigen Live-Lerntag** — der einzige
gelernte Zustand war der aus dem Offline-Bootstrap importierte. Behoben in **0.19.2** (16.07.):
`_actuals._stat_row_datetime` unterscheidet numerische Werte jetzt nach Größenordnung
(`> _EPOCH_MS_THRESHOLD` ⇒ ms, sonst Sekunden); dieselbe Weiche nutzt auch `scripts/backfill.py`.
Am selben Tag liefen die Config-Korrekturen (Ost-Horizont, Screen-Neuzuordnung) und ein
311-Tage-Re-Bootstrap.

**Damit ist der 17.07.2026 der erste Tag mit real funktionierendem Nightly-Learning** — die
Analysewoche ist die erste bewertbare Woche der Projektgeschichte.

### 1.2 Die 7-Tage-Forensik 17.–24.07.2026

Datenbasis: vollständiger Live-Abzug der HA-Instanz (32 Entities, Recorder-Stunden- und
5-Minuten-Statistiken der 8 Modul-DC-Sensoren + unabhängiges Victron-AC-Meter,
`get_issued_forecast` für alle 8 Tage, kompletter Diagnostics-Dump) plus Repo-Stand v0.20.6.
Ergebnis: **44 Findings**; 43 davon adversarial nachgeprüft (**39 CONFIRMED, 1 REFUTED,
2 DUPLICATE, 1 PLAUSIBLE**), eines blieb ohne Verdikt. Die priorisierte Fixliste liegt im Repo:
`docs/orders/7-tage-forensik-2026-07-24-fixliste.md`.

Wochenbilanz (Vorabend-Prognose „issued", auf AC umgerechnet, gegen Victron-AC-Ist):

| Datum | issued → AC | Ist AC | Fehler |
|---|---|---|---|
| 17.07. | 4,35 kWh | 5,69 kWh | −23,6 % |
| 18.07. | 9,57 | 10,74 | −10,9 % |
| 19.07. | 5,93 | 4,84 | **+22,5 %** |
| 20.07. | 11,39 | 11,43 | −0,4 % |
| 21.07. | 8,30 | 11,65 | −28,8 % |
| 22.07. | 10,21 | 10,67 | −4,3 % |
| 23.07. | 7,41 | 8,88 | −16,6 % |
| **Woche** | **57,15** | **63,90** | **−10,6 %** (6/7 Tage unter Ist) |

Wichtig für jede Nachrechnung: Das Scoreboard rechnete gleichzeitig **DC/DC** und wies über
11 Tage einen Tages-MAE von 1,44 kWh aus, +5,5 % besser als die beste Baseline. Kein Widerspruch —
**andere Basis** (siehe §5, „DC/AC-Basis").

### 1.3 Die drei Releases

| Release | Datum | Inhalt (Kurz) |
|---|---|---|
| **0.21.0** | 25.07. | Fix-Tranche T1–T9 aus der Forensik: `bifacial_beam_gain`, kc-Cloud-Klassifikation, Quantile-Seeding aus dem Bootstrap, bias-referenziertes Intraday-Sampling, Keep-Ceiling-Fix der Headline, Scalar-Ring-Re-Arm nach Reload, Config-Fingerprint + Re-Seed, Observability (AC-Kurve, `cloud_class_by_hour`, `clamped`-Flag, Diagnostics) |
| **0.22.0** | 25.07. | ADR-0022 umgesetzt: `tau_points` (elevationsabhängiges Horizont-τ) und `diffuse_tau` (Diffus-Radianz blockierter Sektoren) je Horizont-Zeile; beide ändern die RAW-Kurve ⇒ **ein** Release, **ein** Lern-Reset |
| **0.23.0** | 25.07. | Action `run_bootstrap` (`SupportsResponse.ONLY`): 320-/400-Tage-Re-Bootstrap **in-process**, ohne Token und ohne `site.json`; `dry_run` defaultet auf **true**; HA-freie Kernmodule `core/bootstrap_build.py` + `core/openmeteo_backfill.py`, `scripts/backfill.py` bleibt dünner CLI-Wrapper |

---

## 2. Die bestätigte Ursachenkette

Sieben Glieder, jedes mit Symptom → Ursache → Fix → gemessenem Effekt. Sie greifen ineinander:
Das Kernmuster ist **Doppelkorrektur** — day-ahead-Bias (θ), Intraday-Scalar und die ehrliche
Physiklücke zogen alle am selben Fehler.

### 2.1 Morgen-Rohphysik-Defizit (IRC-1, GUT-F6.1)

**Symptom:** raw/Ist um 04:00Z ≈ 0,44; 05–08Z rund −20 bis −28 % an klaren Tagen; served 04Z
59–244 W gegen Ist 407–570 W an 7 von 8 Tagen.

**Ursache — zwei getrennte Anteile.** (a) Die operator-gemessene Ost-Baumkante bei el 10° ist real,
aber `tau: 0` darunter ist falsch: 5-Minuten-Rampen klarer Tage zeigen eine **semi-transparente
Krone**. **Gemessen** (Median-τ_eff je Elevationsband, vier klare Julitage gepoolt): τ_eff ≈ 0
bei el < 4,5; 0,25 bei el 5–6; 0,43 bei el 6–7; ~0,9 ab el 8–9; ~1 ab el 9,5. Das sind **nicht**
die Knoten der Ziel-Config — die sind daraus gerundet
(`[[4.5,0],[5.5,0.25],[6.5,0.45],[8.0,0.85],[9.5,1.0]]`, §4.1), mit dem 8,0°-Knoten bewusst
konservativ unter dem gemessenen ~0,9.
Es gibt keine Stufe an der Modellkante (04:44Z): schon um 04:40Z fließen 83–90 % des Niveaus von
04:50Z. (b) Ein **multiplikativer, elevationsunabhängiger Beam-Fehler**: am wolkenlosen 24.07. ist
act/raw von 06–10Z fast konstant 1,23–1,30 (Fit 1,276 / 1,296 an den zwei sauberen Tagen).
Zerlegung des 03–08Z-Defizits (1918–3235 Wh/Tag): Horizontanteil 129–193 Wh, multiplikativer
Beam-Anteil 0,9–1,4 kWh, Rest 365–648 Wh (Diffus-Floor, §2.1c).

**Fix:** 0.21 führt das optionale Site-Feld `bifacial_beam_gain` ein (`const.SITE_BEAM_GAIN_MIN/MAX`
= [1,0; 1,6], Default 1,0 = Identität). Es multipliziert in `core/engine._plane_poa_components`
**nur** den direkten Anteil (Beam + Zirkumsolar), nach dem IAM und **vor** dem Horizont-Gate und
der ungegateten Lerner-Referenz — RAW- und korrigierte Kurve sehen denselben Wert, die Lerner
bekommen Headroom statt die Lücke absorbieren zu müssen. 0.22 ersetzt die Interim-az-Rampe
strukturell durch `tau_points` (Details: `02-physik-und-horizontmodell.md`).

**Effekt:** Betreiberwert 1,25 gesetzt; am 25.07. liegt raw im Peakfenster 08:45–09:45 bei
**1,00–1,08 × Ist** (vorher deutlich darunter). Die Frühstunde bleibt offen (§3).

### 2.2 Bias-Zellen gegen die alte Config gefittet (FOR-4, IRC-6, SCT-1)

**Symptom:** issued 12–18Z **−38 % (AC-Sicht)** bzw. −40 % (DC/DC, 6,59 vs. 11,05 kWh über
7 Tage), obwohl die **Rohphysik** im selben Fenster nur −16 % danebenlag — die Bias-Zelle
verschlechterte den Nachmittag aktiv. Morgens klemmte `clear|morning` am oberen Clamp 1,5.

**Ursache:** Der 311-Tage-Bootstrap lief am 16.07. **vor** den späteren Config-Edits. `clear|afternoon`
θ = 0,70 kodierte damit eine Physik, die es nicht mehr gab. Die RLS-Zellen (λ = 0,98, n > 100) lernten
aus dem Steady State nur ~0,001/Tag zurück ⇒ Monate bis zur Konvergenz. Die Zellen bewegten sich
über 8 Nächte praktisch nicht (0,7006 → 0,709 bei n 111 → 115).

**Fix:** (a) `config_fingerprint` — ein Hash über Azimut/Tilt/Wp/Wirkungsgrad/Ross/Horizont **inklusive
aller τ-Felder jeder Horizontzeile** (`tau`, `seasonal`, `tau_leafed`, `tau_bare`, ab 0.22 zusätzlich
`tau_points`, `tau_points_bare`, `diffuse_tau`), Albedo, `bifacial_beam_gain`, Gruppen-AC-Limit und
`CLASSIFIER_VERSION` — wird neben dem Bias-Zustand persistiert; bei Änderung re-seedet
`bias.reseed_day_ahead_bias` jede Zelle (Kovarianz zurück auf `RLS_INIT_COVARIANCE` = 1000, `n` gedeckelt
auf `DAY_AHEAD_BIAS_RESEED_N` = 20). (b) Operator-seitig: `reset_day_ahead_bias` + Re-Bootstrap gegen
die **neuen** Actuals. (c) Zusätzlich trainiert die Day-ahead-RLS jetzt auf `snap.slow_only_hourly_wh`
(Shademap ∘ Physik) statt auf reines raw, damit sie den Schattenfehler nicht doppelt korrigiert,
sobald die Shademap lernt.

**Effekt:** Nach dem Re-Bootstrap vom 25.07. (04:24Z generiert, 12 Zellen) steht `clear|morning`
bei **1,094** statt am Clamp; `clear|midday` 0,908; `clear|afternoon` 0,812; nur `overcast|morning`
liegt noch bei 1,5 (geclamped).

### 2.3 Intraday-Doppelkorrektur (IRC-2, A2)

**Symptom:** Scalar-Peaks 1,33–2,36, **immer** zwischen 08:10 und 09:20 lokal, prozyklisch;
served/Ist im Fenster 07–09Z bis **×1,9**.

**Ursache:** Der Sampler maß `Ist / RAW`, serviert wird aber `raw × θ × scalar`. Da θ morgens bei
1,36–1,49 stand, korrigierten θ und Scalar **denselben** Fehler zweimal.

**Fix:** Die modellierte Seite wird jetzt mit dem nächtlich eingefrorenen θ skaliert (Cache
`_day_factor` im Coordinator); der Intraday-Anteil selbst geht nie ein (θ ist eingefroren ⇒ keine
Zirkularität). Bewusst **kein** harter Scalar-Clamp: der echte Unterprognose-Tag 21.07. braucht
legitim 1,49.

**Effekt (Simulation vor dem Release):** Peaks 1,78 → 1,23; 1,45 → 1,12; 1,36 → 1,14 — der 21.07.
bleibt bei 1,49. Live siehe §3.

### 2.4 Headline-Jojo über den Keep-Ceiling-Pfad (IRC-4, GUT-EXTRA-1)

**Symptom:** 20.07.: `energy_production_today` 9,684 → **12,958** → 9,257 kWh innerhalb weniger
Stunden (+3,274 kWh bei Scalar 2,355).

**Ursache:** Der Headline-Pfad zieht den transienten Scalar bewusst wieder heraus, damit die
Tages-Headline day-ahead-stabil bleibt. Auf einem **re-geclampten** Slot behielt er aber den
servierten (scalar-inflationierten) Ceiling-Wert — die volle Scalar-Headroom schlug durch.

**Fix:** `coordinator._dayahead_slot_strips` verwendet im geclampten Zweig
`min(prereclamp[i] / factor, ceilings[i])` — der exakte scalar-freie Wert, gekappt am physikalischen
Ceiling. Fällt eine der beiden Serien (ältere gecachte Ergebnisse), greift der alte Pfad.
Abgesichert mit einem Unit-Test über einen synthetischen Scalar von 2,35.

**Effekt:** Am 25.07. maximale Stundenänderung der Headline **1,04 kWh/h** gegen eine
Vor-Fix-Baseline von **2,36 kWh/h** (größter 60-Minuten-Swing, Prüfkatalog C6, ebenfalls am
20.07. bei Scalar 2,35); der oben genannte Gesamtausschlag von 3,27 kWh verteilte sich über
mehrere Stunden.

### 2.5 Cloud-Fehlklassifikation (SCT-1, A5)

**Symptom:** 11 von 20 Nachmittagsstunden sonniger Tage landeten in der `overcast`-Zelle;
6 von 11 gewerteten Sonnentagen wurden als „overcast" stratifiziert. Das vergiftete θ-Routing,
die Quantil-Bins und die Scoreboard-Strata gleichzeitig.

**Ursache:** `classify_cloud` bildete die Klasse aus der **Random-Overlap-Gesamtbedeckung** —
mittelhohe und hohe Bewölkung zählten mit vollem Gewicht.

**Fix:** Die Klasse kommt jetzt aus dem **Clear-Sky-Index** `kc = ghi / haurwitz(elevation)`
(`core/bias.classify_cloud`): `kc ≥ CLOUD_KC_CLEAR_MIN` (0,65) ⇒ clear, `kc ≤ CLOUD_KC_OVERCAST_MAX`
(0,30) ⇒ overcast, dazwischen mixed. Unterhalb `CLOUD_KC_MIN_ELEVATION_DEG` (5,0°) oder ohne
brauchbares GHI greift der alte Layer-Cover-Pfad; die Fog-Regel bleibt unverändert und zuerst.
`CLASSIFIER_VERSION` (= 2) fließt in den Config-Fingerprint, damit die Taxonomie-Umstellung die
Bias-Zellen re-seedet. Flankierend (C1): `core/scoreboard.stratified_breakdown` unterdrückt
`engine_vs_best_baseline_pct` und setzt `low_n: true` unterhalb `SCOREBOARD_STRATUM_MIN_N` (3) —
vorher zeigte ein Stratum mit n = 2 absurde −480 %.

### 2.6 Quantile-Cold-Start (QRC-2, SPEC-1, FOR-1)

**Symptom:** Am Tag 0 trugen **4 von 62** Tageslicht-Slots ein echtes Band; alle übrigen hatten
p10 == p90. Nur die drei `overcast`-Bins waren trainiert; `clear|morning` (n = 17) und
`clear|midday` (n = 4) nicht.

**Ursache:** SPEC §12.6 versprach, der Offline-Bootstrap fülle „Bias- **und Quantilspeicher**" —
implementiert war nur Bias + Shademap. Der Quantil-Ring startete leer, und `bands_for_bin` liefert
ohne `QUANTILE_MIN_SAMPLES` (20) **und** `QUANTILE_MIN_DAYS` (5) ein neutrales Band, das der
Coordinator weglässt ⇒ p10 = p50 = p90. Kein day-0-Codepfad-Bug (die Bänder werden jeden Tick für
alle Slots gebaut — das wurde eigens widerlegt), sondern Bin-Cold-Start.

**Fix:** Der Bootstrap faltet jetzt pro Tageslichtstunde den relativen Fehler
`measured / θ-korrigiert` durch die **live** `quantiles.train_quantiles` (identische Taxonomie,
Clamps, Datumsfenster, Caps) und emittiert eine `quantile_state`-Sektion.
`store.import_bootstrap` nimmt sie **additiv** auf: ein Payload ohne den Schlüssel
`BOOTSTRAP_KEY_QUANTILE` lässt den Live-Ring unangetastet. Der Rollback-Snapshot trägt den
Quantil-Zustand mit, so dass alle drei Lerner gemeinsam zurückrollen.

**Effekt:** Der Bootstrap vom 25.07. enthält 12 gefüllte Bins (z. B. `clear|afternoon` 379,
`clear|midday` 275 Samples). Live-Wirkung siehe §3.

### 2.7 Scalar-Blackouts nach Reloads (SCT-2, A7)

**Symptom:** Jeder Neustart/Reload ließ `compute_intraday_scalar` für das gesamte nachlaufende
Fenster neutral. Am 19.07. (einem Tag mit +29,5 % Überprognose) war die Korrektur 8¼ Stunden blind.

**Ursache:** Der Sample-Ring lebt ausschließlich im Speicher, und
`INTRADAY_MIN_TRAILING_MINUTES` (120) verlangt zwei Stunden Historie, bevor der Scalar überhaupt
wirkt. **Reload-Forensik:** Kein fremder Auslöser — die ≥ 8 „Reloads" in 8 Tagen waren komplette
HA-Neustarts, jeweils 6–25 s nach einem HACS-Install (7× diese Integration, 6× ein zweites Repo,
2× Core-Update), plus exakt drei echte Entry-Reloads durch Options-Änderungen. Konsequenz: nichts
abstellen, sondern den Ring neustartfest machen.

**Fix:** Beim ersten frischen Tick nach dem Setup rekonstruiert der Coordinator den Ring einmalig
aus den **5-Minuten**-Recorder-Statistiken des Site-Total-DC-Sensors (Sekunden-Epoch, dieselbe
Weiche wie `_actuals`) plus der letzten θ-korrigierten Kurve. Die modellierte Seite ist dabei auf
die **gemeterten** Planes beschränkt (nur die mit `actual_entity` — exakt die Teilmenge, die der
Site-Total-Sensor summiert); ohne diese Einschränkung halbierte eine teilbemessene Anlage den
rekonstruierten Scalar bis auf den Clamp-Boden. Fehlen Statistiken oder Cache, degradiert der
Pfad sauber auf neutral. Persistiert wird nach wie vor **nichts davon**: SPEC §9.4 verbietet allein
die Persistenz des **Skalars** als Zustand; die Trailing-Samples sind neu bewertbare Rohdaten und
werden nach einem Neustart einmalig rekonstruiert statt gespeichert.

---

## 3. Verifizierte Wirkung nach dem Deployment (25.07.2026, Vormittag)

Datenstand 05:00–09:50 lokal, 5-Minuten-Statistiken + Headline-Historie. Deployment v0.21.0 ~06:00,
Reconfigure-Reload 06:10, `import_bootstrap` ~06:30. Wetter belastbar: überwiegend klar
(Median-5-min-Änderung 3,3 %), also **kein „Freispruch mangels Sonne"**.

| Größe | Baseline 17.–24.07. | 25.07. Vormittag | Urteil |
|---|---|---|---|
| Peak-Ratio served/Ist | **1,50–1,53** (2841/1863, 2793/1854 W) | **1,15** (2000/1744 W @ 09:00); raw/Ist im Peakfenster **1,00–1,08** | greift |
| Intraday-Scalar | Peaks 1,33–**2,36**, prozyklisch, immer 08:10–09:20 | **max 1,237 @ 07:55**, antizyklisch, monotone Decay-Treppe, 0 min > 1,25 | greift |
| Headline-Jojo | größter 60-min-Swing **2,36 kWh/h** (C6) | max **1,04 kWh/h** (plus ein 2-Minuten-Restart-Transient 14,19 aus dem restaurierten State) | greift |
| day-0-Quantilbänder | **4/62** Tageslicht-Slots mit p10 ≠ p90 | **56/56** (today) bzw. 55/55 (tomorrow) | greift |
| Bias-Zellen | `clear\|morning` am Clamp 1,5; `clear\|afternoon` 0,70 | 1,094 / 0,908 / 0,812 (12 Zellen), nur `overcast\|morning` noch geclamped | greift |
| Scalar-Re-Arm nach Restart | ≥ 2 h tot | erste Abweichung 07:55 = 85 min nach dem Import | teilweise |
| Frühfenster 04Z / erste Tagesstunde | served 59–244 W vs. Ist 407–570 W | Stunde 06 lokal Ratio **0,41** (220 vs. 540 W), Stunde 07 **0,79**, ab 08:00 innerhalb ±20 % | **offen** |

Der servierte Überhang 08:45–09:45 (1,15–1,20) ist **Scalar-Carryover**, kein Physik-Overshoot —
raw liegt dort bei 1,00–1,08 und der Scalar baut sich planmäßig ab. Die Rest-Unterdeckung der
ersten Tagesstunde ist strukturell (glatte Klarhimmelkurve, keine Wolken als Ursache) und der
Hauptgrund für die 0.22-Config-Kampagne.

---

## 4. Offene Punkte

| # | Thema | Status | Nächster Schritt |
|---|---|---|---|
| O1 | 0.22-Config-Kampagne (`tau_points` + `diffuse_tau`) | Code released, **Config-Edit noch nicht angewandt** (Zustand KAMPAGNE in `05-…` §3) | Config editieren, dann `run_bootstrap` mit `dry_run: false` |
| O2 | D3 `rear_beam_fraction` | designt, **nicht implementiert** | Post-D2-Fit mit `d3_fit.py`, dann Entscheidung |
| O3 | Diffus-Rest-Gap M4/M8 (beam-gebunden) | dokumentiert offen | nach D2-Woche neu vermessen |
| O4 | Screen-Zuordnung az135–175 | in der **Live-Config erledigt** (im Repo-Default weiter alt), Lernzustände älter | Re-Bootstrap deckt es ab |
| O5 | Horizontsektoren Sep–Apr | unvermessen/extrapoliert | Emergenzpunkte im nächsten Winter |
| O6 | Saisonale Nachprüfung `tau_points` | Profil nur auf Julitagen gefittet | Herbstfenster nachmessen |
| O7 | Reviewer-Suggestions 0.21–0.23 | 3 offen, 1 erledigt, 1 als Doku-Falle abgehakt | siehe §4.6 |
| O8 | Kill-Gate / Scoreboard-Fenster | **erledigt** — die Phase-1-Abnahme ist gelaufen; das Kill-Gate samt externen Vergleichsprognosen wurde in v0.25.0 entfernt, das Engine-Scoreboard läuft weiter | — |
| O9 | Validierungslauf | **überfällig, Ergebnis offen** — Lauf war für ~01.08. terminiert; Paket liegt im Repo, Ergebnis wurde hier nicht nachgetragen (Stand 2026-07-31) | ausführen, Ergebnis hier nachtragen |

### 4.1 O1 — die Config-Kampagne ist der eigentliche nächste Schritt

Drei Config-Zustände sauber trennen (Tabelle in `05-anlage-und-betrieb-runbook.md` §3):
**REPO** = das ausgelieferte `const.DEFAULT_SITE`, **LIVE** = was auf der Anlage läuft,
**KAMPAGNE** = der vorbereitete, noch nicht angewandte 0.22-Edit. O1 betrifft ausschließlich den
Schritt LIVE → KAMPAGNE.

Der Live-Config-Abzug vom 25.07. (06:21 lokal) trägt **weder `tau_points` noch `diffuse_tau`** —
er enthält noch die Interim-az-Rampe (`az63…78`, τ 0,05 → 0,85, verankert auf den Sonnenpfad um
den 1. August) und `bifacial_beam_gain: 1.25`. Alles andere aus den Juli-Korrekturen ist dort
bereits drin: modulindividuelle Baumfenster, Wandkante M4/M8 bei az 195, Ost-Kante el 10 und
(laut demselben Abzug) `albedo` 0,15. Der 0.22-Code ist ein **Migrations-Nullschritt**: Bestandsconfigs bleiben
byte-identisch. Erst der Config-Edit ändert die Physik.

Die Interim-az-Rampe ist ausdrücklich **deprecated**: Sie kodiert τ als Funktion des Azimuts
entlang des Sonnenpfads **eines Ankertags** und driftet ~0,3°/Tag. Ab Ende August bekäme die
Dämmerung (az 77–86, Sonne dann noch unter el 4) die hohen τ-Werte des Ankertags ⇒ **Phantom-Beam
~+35–100 Wh** an klaren Spätaugust-Morgen. Regel: **einmal migrieren, nicht monatlich nachankern.**

Reihenfolge (ADR-0022 §2.7 / §4): Config-Edit → `reset_day_ahead_bias` (bzw. automatische
Fingerprint-n-Deckelung) → Re-Bootstrap. Ab 0.23 ist der letzte Schritt ein Klick:
`run_bootstrap` mit `dry_run: false`. **Übergangswoche einplanen** — die servierte 04–06Z-Kurve
überschießt 3–7 Tage, während die Bias-Zellen gegen die verschobene raw-Kurve zurücklernen; das
ist die Clamp-Zelle beim Einschwingen, **keine Regression, nicht zurückrollen.**

Eine paste-fertige Ziel-Config (8 Planes, **gerundete** `tau_points`-Knoten
`[[4.5,0],[5.5,0.25],[6.5,0.45],[8.0,0.85],[9.5,1.0]]` — Messwerte siehe §2.1 — auf den beiden
Ost-Zeilen az52/az89, `diffuse_tau: 0.5` auf den Wandzeilen M4/M8 az195–360 und M1/M5 az295–360;
die Wandzeilen selbst **stehen bereits live**, neu ist an ihnen nur das `diffuse_tau`) liegt als
Session-Artefakt unter
`…/scratchpad/backfill/site_campaign_022.yaml` — **Entwurf, nicht Repo-Wahrheit**, vor Anwendung
gegen die dann aktuelle Live-Config prüfen.

### 4.2 O2 — D3 `rear_beam_fraction`

Im Code **nicht vorhanden** (nur in CHANGELOG und ADR-0022 erwähnt). Vorbereitender Fit auf den
Daten 17.–24.07.:

- **f ≈ 0,32** (pre-0.22-Konvention, Overcast-Proxy, CI90 0,30–0,36, n = 12 aus 6 Tagen) bzw.
  **0,348** (post-0.22-Konvention mit Modell-Diffus, simuliert, CI90 0,32–0,41). Tages-Mediane
  streuen ±0,05, kein Ausreißer.
- **Zwei Design-Abweichungen zum ADR-Entwurf.** (i) Der ADR-Clamp `[0; 0,3]` ist zu knapp — der Fit
  klemmt daran; Empfehlung `[0; 0,4]`. (ii) Es muss ein **PER-PLANE**-Feld werden, kein Site-Feld:
  an den OSO-Planes (M2/3/6/7) beträgt das implizierte f −0,004…+0,019, an M1/M5 +0,03…+0,06 —
  ein site-weites f = 0,32–0,35 würde dort **~840–910 Wh/Tag Phantom-Ertrag** fabrizieren, mehr
  als der echte M4/M8-Effekt. Physikalische Erklärung: nur bei M4/M8 blickt der Rückraum in den
  offenen Osten; bei allen anderen Planes steht das Haus dahinter — in keiner Horizonttabelle
  kodiert, weil die Engine bisher keinen Rückseiten-Term hat.
- **Erwarteter Ertrag:** ~155–190 Wh/Tag an klaren Julitagen (ADR-Schwelle: > 50 Wh/Tag), an
  Overcast-Tagen strukturell 0 (DNI-gebunden — die entscheidende Sicherheit gegenüber jedem
  statischen Floor). Im Juli-Mischmonat ~80–90 Wh/Tag.
- **Formgrenze:** Die `max(0, −cosθ_front)`-Form trägt nur im tiefen Rückraum. Das implizierte f
  wächst 04Z ~0,30 → 05Z ~0,50 → 06Z 0,6–1,1 → 07Z ~2,5 (M8): der besonnte **Wand-Reflex** wächst
  weiter, während der D3-Hebel abfällt. D3 deckt damit ~55 % des beam-gebundenen M4/M8-Exzesses
  (~180 von ~330 Wh/Tag klar). Den 06–08Z-Rest **nicht** über ein größeres f kaschieren.

**Nächster Schritt:** ≥ 4 klare Morgen nach der 0.22-Kampagne abwarten, dann `d3_fit.py` im
`post022`-Modus (Session-Artefakt `…/scratchpad/design-adr/d3-fit/`, arbeitet auf einem Snapshot
von `scripts/validation/validate.py`). Entscheidungsregel: CI90 innerhalb [0,2; 0,5] **und**
zurückholbar ≥ 50 Wh/Tag ⇒ implementieren (per-Plane, Clamp 0,4, additiv auf den Diffus-Anteil,
**nicht** in die Shademap-T-Referenz).

### 4.3 O3 — Diffus-Rest-Gap M4/M8

**Nach** angewandtem D2 (`diffuse_tau`, Zustand KAMPAGNE) bleibt der klare Morgen an M4/M8 um
etwa **Faktor 3** unterschätzt — ausgehend von **~×10** ohne `diffuse_tau`, dem heute noch
gemessenen Stand, den die Regressionswache C8b als bekannt und akzeptiert führt (~90–150 Wh/Tag
site-weit laut CHANGELOG 0.22.0, ~270–360 Wh/Tag gemessener beam-gebundener Gesamtexzess laut
D3-Fit — die Spanne hängt an der Abzugskonvention und ist **unsicher**). Das ist bewusst dem
Bias-Lerner bzw. D3 überlassen und wird ausdrücklich **nicht** mit überhöhten `diffuse_tau`-Werten
maskiert; die Validierungsobergrenze `HZ_DIFFUSE_TAU_MAX` = 0,8 ist genau diese Leitplanke.
Erratum beachten: der SVF-Goldwert für M4 lautet **0,288 → 0,576** (nicht die ursprünglich
entworfenen 0,63) — der ADR-Text ist korrigiert.

### 4.4 O4/O5/O6 — Horizont-Geometrie

- **Screen-Reassignment (O4):** Der Live-Abzug vom 25.07. zeigt die Neuzuordnung im Zustand
  **LIVE** bereits umgesetzt — jede Plane trägt ihr **eigenes** Baumfenster (M2 az140–175
  el38,5–45 τ0,55; M3 az124–158 τ0,3; M4 az124–155 el40–44 τ0,25; M6 az140–175 el32–38 τ0,75;
  M7 az138–158 τ0,3; M8 az140–155 τ0,45). Das ist **kein** Bestandteil der 0.22-Kampagne, sondern
  war schon vorher live. Der ADR-Text, der die Fehlzuordnung noch als offenen Begleitbefund führt,
  ist an dieser Stelle **veraltet**. Der **Repo-Default** `const.DEFAULT_SITE` (Zustand REPO) trägt
  dagegen unverändert den einen M4/M8-Screen az135–175 aus `_south_horizon()` — eine
  Neuinstallation startet also mit der widerlegten Zuordnung. Offen bleibt außerdem, dass
  Shademap-Bins und Bias-Zellen aus der Zeit **davor** stammen — der Re-Bootstrap deckt das ab
  (`BOOTSTRAP_MAX_BIN_N` = 5 hält das Risiko klein).
- **Sep–Apr-Sektoren (O5):** Die Fernlinie südlich az 140 und das Segment az 173–192 el16 sowie der
  schmale Spike az168–173 el25 sind teils **extrapoliert** aus Winterbeschreibungen, nicht
  gemessen; die Westflanke az 240–300 ist gar nicht modelliert. Belastbare Messpunkte liefern erst
  die Produktions-Emergenzpunkte des nächsten Winters (Nov–Jan-Morgen der OSO-Module,
  Dez/Jan-Mittage von M4/M8).
- **Saisonalität `tau_points` (O6):** Das Profil ist an vier Julitagen im Bereich el 4,5–9,5
  gefittet. Derselbe Sektor wird Sep–Apr bei anderen Elevationen durchlaufen, und
  `tau_points_bare` (das kahle Winterprofil einer `seasonal`-Zeile) ist nicht gesetzt. Im Herbst
  nachmessen — der Vorteil gegenüber der az-Rampe ist, dass hier nur **Werte** nachzuziehen sind,
  keine Geometrie neu zu verankern.

### 4.5 O8 — Scoreboard-Fenster

Das Kill-Gate brauchte ein **volles** 14-Tage-Fenster gewerteter Tage
(`DEFAULT_SCOREBOARD_WINDOW_DAYS` = 14). Nach dem Epoch-Fix waren nur ~3 Tage Catch-up
nachholbar (`NIGHTLY_CATCHUP_MAX_DAYS` = 3); **06.–12.07.2026 blieben dauerhaft unbewertbar**
(keine archivierten issued-Snapshots). Erstes echtes Verdikt daher um den **27.07.**; bis dahin
war `kill_gate_passed = None` korrekt, kein Fehler.

**Nachtrag (2026-07-31):** Die Phase-1-Abnahme ist gelaufen — das Kill-Gate
wurde samt der externen Vergleichsprognosen in v0.25.0 entfernt (das
Engine-Scoreboard mit `daily_kwh_mae` / `hourly_mae` / Strata bleibt). Damit ist
dieser Punkt geschlossen.

### 4.6 O7 — Reviewer-Suggestions aus den Release-Reviews

Nicht-blockierende Punkte, am aktuellen Code nachgeprüft:

| Punkt | Fundstelle | Status |
|---|---|---|
| Rollback mit einem **Legacy-Snapshot** (ohne `quantile`-Sektion) restauriert einen **leeren** Quantil-Ring und wischt live gelernte Bänder | `core/types.LearnerSnapshot.from_dict` (Default `QuantileState()`) | **offen** — ein None-Sentinel würde den Live-Ring erhalten |
| Strata-Prozentwert kann weiterhin aus **einem einzigen gepaarten Tag** entstehen | `core/scoreboard._vs_best_baseline_pct_for_days` | **erledigt** — mit den Vergleichsprognosen in v0.25.0 entfernt (es gibt keinen Strata-Prozentwert mehr; `low_n` bleibt als Dünn-Basis-Markierung) |
| Backfill-Parität für `bifacial_beam_gain` ungetestet | `tests/core/test_backfill_math.py` | **erledigt** in 0.22 (expliziter Paritätstest Engine ↔ `reconstruct_plane_hour`) |
| `az360`-Wrap bei Wandzeilen: der Horizont ist ein geschlossenes 360°-Profil (`core/horizon.interp_elevation`, `_wrap360`), eine Zeile bei az 360 sortiert auf az 0 — das Wrap-Segment interpoliert zwischen letzter und erster Zeile | `core/horizon` | **Falle, kein Bug** — bei den M4/M8-Wandzeilen (az195 el90 / az360 el90) korrekt; wer aber nur EINE Wandzeile setzt, erzeugt eine Rampe über den Nordsektor |
| `classify_cloud`-Fallback bei fehlendem GHI ist im Livebetrieb praktisch toter Pfad, weil der Fetcher fehlendes GHI zu 0,0 coerct | `fetcher` / `core/bias.classify_cloud` | **offen (dokumentieren)** — bei Provider-Ausfall des GHI-Felds würde tagsüber alles `overcast`, harmlos weil die Physik dann ohnehin 0 prognostiziert |

### 4.7 O9 — der Validierungslauf

`scripts/validation/validate.py` (nur Standardbibliothek) zieht einen kompletten Snapshot und
fährt acht Checks, deren Schwellen **gegen die Vor-Fix-Woche kalibriert** sind: auf jenen Daten
sind C1–C7 FAIL und die Regressionswachen C8 PASS. Nach einer erfolgreichen Fix-Woche müssen
C1–C7 grün werden und C8 grün **bleiben**.

| Check | Frage | Vor-Fix-Wert |
|---|---|---|
| C1 | served/Ist 07–10 lokal | 1,23 Wochenmittel; 3 Tage Peak-Ratio > 1,4 |
| C2 | Scalar-Hygiene 08–09 an guten Tagen | max 2,35 |
| C3 | Morgen-Physik (raw 04Z, raw/Ist 06–10Z, Rampe statt Sprung) | 226 Wh; 0,78; 3/3 Tage mit Sprung |
| C4 | Bias-Konvergenz weg von den Clamps | `clear\|morning` 1,491; `clear\|afternoon` 0,709 |
| C5 | day-0-Bänder + p10 vs. End-Ist | 4/62; 3/5 Tage p10 > Ist |
| C6 | Headline-Stabilität (kein Jojo > 1,5 kWh/h) | 1 Tag |
| C7 | issued-AC-Wochenbias / Nachmittagsblock 12–18Z | −10,2 % / **−38,1 %** |
| C8 | Regressionswachen (Overcast-Scalar, M4/M8-Morgen, Mittagsfenster 11–13Z) | alle PASS |

Auswertungsreihenfolge laut Runbook: **erst C8, dann C3, dann der Rest** — C1/C2/C6 hängen kausal
an der Morgen-Physik, C4/C7 am Bias-Reset. Termin: rund eine Woche nach der Config-Kampagne,
also **~01.08.2026**.

**Nachtrag (2026-07-31):** Der Termin steht unmittelbar bevor bzw. ist je nach
Ausführung bereits verstrichen; ein Ergebnis wurde hier noch nicht nachgetragen
— der Lauf bleibt offen.

---

## 5. Methodik-Lehren

Diese Punkte haben in der Analyse mehr Fehler verhindert als jede einzelne Codestelle.

1. **Bin-Abwesenheit ≠ freie Sicht.** Eine fehlende Shademap-Zelle heißt zuerst „kein zulässiges
   Sample" — Gates, Stunden-Aliasing und ein zu dunkler statischer Prior löschen genau die Bins,
   die man sucht. Der Klassiker: ein konfigurierter Screen drückt den modellierten Beam unter das
   Trainings-Gate, die Bins verschwinden, und die leere Karte „bestätigt" die falsche Config.
2. **Roh-Kurven klarer Referenztage sind der härteste Test.** Erst die 5-Minuten-Kurven klarer
   Tage haben die Frage „Stufe oder Rampe?" entschieden — die Stundenmittel hatten die weiche
   Baumkante zu einer scharfen el-10-Kante aliast. Modellfreie Vergleiche schlagen jede
   Rückrechnung durchs eigene Modell.
3. **Overcast-Tage trennen isotrop von beam-gebunden.** Der ×10-Befund an M4/M8 zerfiel erst am
   Kontrolltag: 8–9 W am bedeckten gegen 25–32 W am klaren Morgen ⇒ der isotrope Anteil ist
   SVF/Reflexion (per `diffuse_tau` modellierbar), der Rest ist an die Existenz von Beam gebunden
   und durch **keine** Diffus-Konstruktion erreichbar, auch nicht mit ρ = 1.
4. **DC/AC-Basis immer explizit machen.** Die issued-Kurven waren DC, das Victron-Meter misst AC —
   das hat jedes Verhältnis um ~8 % geschönt und mehrere Findings in die falsche Richtung
   gedreht (ein „+13,5 %"-Befund war reines Einheiten-Artefakt). Deshalb liefert
   `get_issued_forecast` seit 0.21 zusätzlich `hourly_wh_ac` mit dem zum Issue-Zeitpunkt
   eingefrorenen η. Zweite Falle derselben Familie: Nur die **DC**-Leistung ist real gemessen —
   die `ac_power`- und `*_dc_total_energy`-Felder der Wechselrichter sind DC × 0,9472
   (Firmware-Konstante) und tragen keine unabhängige Information.
5. **UTC vs. lokal explizit annotieren.** Die Blackout-Fenster eines Findings waren in UTC notiert
   und wurden lokal gelesen; zusätzlich war die Zuordnung Restart ↔ Entry-Reload invertiert.
   Dauern und Wirkung stimmten, die Erzählung nicht.
6. **Die Recorder-Epoch-Falle kennen.** In-Process-Statistiken = **Sekunden**, WebSocket =
   **Millisekunden**. Wer beide Quellen mischt (Analyse-Skript vs. Integration), muss die
   Größenordnung prüfen. Genau diese Verwechslung hat monatelang alles Lernen still lahmgelegt —
   und sie ist nicht aufgefallen, weil das System weiterlief und plausible Zahlen zeigte.
7. **Adversariale Verifikation lohnt sich.** 39 von 43 nachgeprüften Findings hielten, aber die Ausnahmen waren
   teuer: (a) **REFUTED** — „Day-Part-Stufenartefakt um 10:00 weiterhin aktiv": die Anwendung
   interpoliert nachweislich (±0,75-h-Rampe nachgemessen), der Knick war ein **Anker-Misfit der
   Zelle**, kein Anwendungsartefakt; ein Fix an der falschen Stelle hätte die korrekte Blend-Logik
   zerstört. (b) Teil-widerlegt — die These „die Summe der Stundenfehler steigt durch den Bias"
   war ein DC/AC-Artefakt; DC-konsistent **verbessert** der Bias sie (2,61 vs. 2,88 kWh/Tag).
   (c) **No-Op-Vorschläge erkennen** — „Scalar auf p10/p90 mitanwenden statt Bänder zu verwerfen"
   beschrieb zwei Dinge, die es nicht gab: Bänder werden nie verworfen, und der Scalar steckt
   bereits drin.
8. **Nicht alles, was hilft, gehört umgesetzt.** Ausdrücklich verworfen wurden: harter
   Scalar-Clamp 1,3–1,5 (der 21.07. brauchte vor dem A2-Fix legitim > 1,6; nach dem Fix bleiben
   1,49 übrig — ein Clamp hätte den echten Wetterfehler gekappt), pauschaler konservativer
   Abendaufschlag (hätte die drei Überprognose-Tage verschlimmert), absolute Anhebung von
   `INTRADAY_MIN_MODELED_WH` (die Konstante ist in `const.py` ein fester Absolutwert von 5 Wh und
   skaliert eben **nicht** mit der Anlagengröße — der für diesen 3,26-kWp-Standort nötige Wert von
   ~100–150 Wh würde 800-W-Anlagen fast ganztägig vom Sampling ausschließen) und ein Slew-Limit
   auf der Tagessumme (versteckt echte Weather-Refresh-Sprünge).
