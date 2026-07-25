# ADR-0022: Elevationsabhängiges Horizont-tau + Diffus-Floor/Wand-SVF

| | |
|---|---|
| **Status** | Accepted (0.22.0) |
| **Datum** | 2026-07-24 |
| **Ziel-Release** | 0.22.0 |
| **Autoren** | Design-Agent (Fable), auf Basis der 7-Tage-Forensik 17.–24.07.2026 |
| **Evidenz** | 7-Tage-Morgen-Physik-Forensik (17.–24.07.2026, Offline-Reproduktion der Engine-Physik); Live-HA-Abzug 24.07. (Recorder-Stundenstatistiken); Begleit-Rechnung `ADR-0022-rechnungen-diffus-floor.md` (dieser Ordner) |
| **Scope** | Ursprünglich Analyse + Design; in 0.22.0 umgesetzt (Thema 1 / H-A, Thema 2a / D2; D3 zurückgestellt). Zeilennummern bewusst vermieden; zitiert werden Module/Konzepte. |

---

## 0. Executive Summary

**Thema 1 (Horizont-tau):** Empfohlen wird **Option H-A** — ein optionales, pro
Horizont-Zeile inline definiertes Elevationsprofil `tau_points: [[el, tau], …]`
unterhalb der bestehenden `elevation_deg`-Kante. Bestehende Configs bleiben
byte-identisch gültig (Feld nur-wenn-gesetzt, exakt die Konvention von
`seasonal`/`ross_coeff`/`shade_group`). Die Interim-az-Rampe wird durch einen
reinen Config-Edit abgelöst; der saisonale Drift (~0,3°/Tag, Phantom-Beam ab
Ende August) verschwindet strukturell, weil tau an der physikalischen Größe
(Sonnen-Elevation) hängt statt am Sonnenpfad eines Ankertags. Aufwand **M**.

**Thema 2 (Diffus-Floor):** Der ×10-Befund an M4/M8 zerfällt datenseitig in zwei
Mechanismen: (i) ein **isotroper Anteil** (Overcast-Kontrolltag 19.07.: Faktor
~2 Lücke), den eine per-Zeile getrennte **`diffuse_tau`** auf den Wand-Zeilen
(effektive Reflexions-Radianz ρ ≈ 0,5) vollständig erklärt, und (ii) ein
**klarhimmel-gebundener Überschuss** (+~45 W/m², nur an klaren Morgen), der
physikalisch Rückseiten-/Reflex-Pickup des tiefstehenden Beams ist und von
KEINER Diffus-SVF-Konstruktion (auch ρ=1 nicht) erreichbar ist. Empfehlung:
**`diffuse_tau` jetzt** (Option D2, passt nahtlos in das Zeilen-Schema von
Thema 1, Aufwand **S**), den beam-gebundenen Rest als optionales, klar
begrenztes Site-Feld `rear_beam_fraction` **entwerfen, aber zurückstellen**
(Option D3, Aufwand M, erst nach einer Validierungswoche von D2). Volles
Bifacial-Modell ist explizites Nicht-Ziel.

**Wichtigste Trade-offs:** H-A kauft die saubere Physik mit einer
Signatur-Erweiterung von `transmittance_at` (+ `sun_el`) und einem
Band-Integral im SVF; H-C (alles in die Shademap) wurde verworfen, weil die
statische Config der Prior der **RAW-Kurve** bleiben muss (Bias/Quantile/
Scoreboard trainieren gegen raw). D2 ist bewusst konservativ: sie schließt
~20–30 % der M4/M8-Lücke (den isotropen Teil vollständig), hebt aber auch den
wandverschatteten M4/M8-**Nachmittag** (gemessener Floor 24–31 W je Modul, vom
Modell ebenfalls verfehlt) und die M1/M5-Abende — Nebenwirkungen sind klein und
positiv-gerichtet, brauchen aber die Regressions-Checks aus dem Testplan.
Beide Themen ändern die RAW-Kurve ⇒ **ein** gemeinsames Release mit
Bias-Reset/Fingerprint-Deckelung (A4) und Shademap-Re-Bootstrap.

**Aufwand gesamt:** Thema 1 **M** (~2–3 PT inkl. Tests/SPEC), Thema 2a **S**
(~0,5–1 PT huckepack auf Thema 1), Thema 2b (deferred) **M**.

---

## 1. Kontext

### 1.1 Ist-Zustand des Horizont-Modells

Das Config-Schema (`core/types.py::HorizonRow`) kennt pro Azimut-Stützstelle
genau **eine** Linie `(azimuth_deg, elevation_deg, tau)` plus optionale
Saisonalität (`seasonal`, `tau_leafed`, `tau_bare`, Cosinus-Foliage-Rampe in
`core/horizon.py::foliage_fraction`). Die Semantik in der Engine
(`core/engine.py::_plane_poa_components`):

- Steht die Sonne **unter** der interpolierten Horizontlinie
  (`horizon.interp_elevation`), wird Beam+Zirkumsolar mit
  `horizon.transmittance_at(plane, sun_az, doy)` multipliziert — **tau ist
  dabei eine reine Funktion des Azimuts**, nicht der Elevation.
- Der isotrope Diffus wird immer mit dem Sky-View-Faktor
  (`horizon.sky_view_factor`, semi-transparente Spalten-Integration
  `_semi_transparent_column`: Keil unterhalb der Linie trägt tau seines
  offenen Werts) skaliert; der Bodenreflex bleibt ungefiltert.
- Die Shademap (`core/shademap.py`) lernt pro (Sonnen-az 5° × el 2,5° ×
  Halbjahr)-Bin eine **absolute** beam-referenzierte Transmittanz
  `T = (P_gemessen − P_diffus_modelliert) / P_beam_ungated` und blendet sie per
  Shrinkage `w = n/(n+20)` **gegen den statischen Prior** — sie ersetzt den
  Prior in der CORRECTED-Kurve, die RAW-Kurve bleibt rein statisch.

### 1.2 Befund Thema 1 (Morgen-Physik-Forensik, 4 klare Referenztage)

Die Ost-Baumkronen (az52–89, operatorgemessene Oberkante el10) sind
**semi-transparent mit elevationsabhängiger Transmittanz**:

| el-Bin | 3–4 | 4–5 | 5–6 | 6–7 | 7–8 | 8–9 | ≥9 |
|---|---|---|---|---|---|---|---|
| tau_eff (Median, 4 Tage gepoolt) | ~0 | ~0 | 0,25 | 0,43 | 0,41 | 0,90 | ~1 |

Weil das Schema tau(el) nicht ausdrücken kann, kodiert die Interim-Lösung den
Ramp als **tau(az) entlang des Sonnenpfads** (az63→78: 0,05…0,85, verankert auf
~1. August). Bekannte Defekte der Interim-Lösung:

- **Saisondrift ~0,3°/Tag**: Der el(az)-Zusammenhang gilt nur am Ankertag. Ab
  Ende August bekommt die Dämmerung (az77–86, Sonne dann noch bei el<4) die
  hohen tau-Werte des Ankertags ⇒ **Phantom-Beam ~+35–100 Wh** an klaren
  Spätaugust-Morgen; ab Mitte September ist der Sektor inaktiv, ab Frühjahr
  kehrt der Fehler spiegelbildlich zurück. Monatliches Hand-Nachankern wäre
  Dauerbetriebsaufwand mit Fehlerpotential.
- Die az-Rampe ist **nicht wiederverwendbar** für die anderen Baum-Sektoren
  (az89–98, az112–140), die morgens nur Sep–Apr aktiv sind — dort müsste
  derselbe Trick mit anderem Anker wiederholt werden.
- Die Config dokumentiert nicht mehr die Geometrie (Kronenkante), sondern ein
  Artefakt (Pfadprojektion) — Operator-Wartbarkeit leidet.

### 1.3 Befund Thema 2 (Diffus-Floor / ×10 an M4/M8)

M4/M8 (az205, tilt 70/80, 430 Wp, Wand-Zeilen az195–360 el90 tau0, SVF laut
Diagnostics 0,288/0,294) sind morgens beamfrei bis ~09–10Z und damit die
Diffus-Kontrollgruppe. Messung um 04Z (Stundenmittel, Recorder-Statistiken):

| Tag | Typ | M4 04Z (W) | M8 04Z (W) | M4 14Z (W, wandverschattet) |
|---|---|---|---|---|
| 18.07. | klar | 27,1 | 29,7 | 23,4 |
| 19.07. | **overcast** | **8,1** | **9,3** | 27,7 |
| 21.07. | klar | 24,6 | 26,8 | 30,6 |
| 22.07. | klar | 26,6 | 29,1 | 31,8 |
| 24.07. | wolkenlos | 29,2 | 31,6 | 20,2 |

Das Modell liefert diffus-only **2,8–2,9 W** je Modul (POA ≈ 6,8 W/m² vs.
gemessen ≈ 65 W/m² klar bzw. ≈ 20 W/m² overcast). Zwei getrennte Lücken:

1. **Isotrope Lücke (Overcast, Faktor ~2):** 8–9 W gemessen vs. ~4 W Modell.
   Ursache: Die Engine behandelt Horizont-Objekte diffusseitig als **schwarz**
   (tau=0 ⇒ Sektor trägt 0 Diffus). Eine helle Hauswand hat aber Albedo ~0,5
   und ersetzt die blockierte Himmelsradianz teilweise durch reflektierte.
2. **Klarhimmel-Überschuss (Faktor ~3 zusätzlich, nur klare Tage):** +~20 W je
   Modul, die auf dem Overcast-Tag **fehlen**. Der Mehrertrag ist also an die
   Existenz von Beam gebunden — konsistent mit bifacialem
   Rückseiten-Pickup des tiefstehenden Ost-Beams (Rückseite von az205-Modulen
   zeigt nach az25; cosθ zur Morgensonne ≈ 0,6–0,7) und/oder zirkumsolar
   aufgehellter Umgebung. **Kein** SVF-/Reflexions-Modell (auch ρ=1) kann das
   erklären — siehe Rechnung §4.1 und `ADR-0022-rechnungen-diffus-floor.md`.

Standortweit fehlen über den Tag **~0,3–0,5 kWh** Diffus-Floor (Dämmerungs-
Defizit Faktor 4–9 auf allen Planes um 04:00Z, M4/M8 ganztags, wandverschattete
M4/M8-Nachmittage). Der Befund ist durch kein aktuelles Config-Feld sauber
adressierbar (Site-Albedo-Erhöhung brächte <2 W je Modul und würde mittags
falsch wirken).

### 1.4 Randbedingungen für jede Lösung

- **Abwärtskompatibilität hart:** Bestehende Configs (inkl. der 8-Planes-
  Live-Config und aller Fremdnutzer) müssen unverändert und byte-identisch
  weiterrechnen. Das Repo-Muster dafür existiert: optionale Felder werden
  nur-wenn-gesetzt serialisiert (`shade_group`, `ross_coeff`,
  `bifacial_beam_gain`).
- **Config-UI ist ein ObjectSelector** (`config_flow.py::_site_selector`):
  Das Site-Objekt wird als freies YAML/JSON-Objekt editiert; ein neues
  verschachteltes Feld braucht **keine** neue UI-Komponente, nur Validierung
  (`_site_validation.py`) + Doku (SPEC §13).
- **RAW-Kurve ist Lern-Wahrheit:** Day-ahead-Bias, Quantile-Bins, Scoreboard
  und Kill-Gate referenzieren die statische Physik. Eine Korrektur, die nur in
  der CORRECTED-Kurve lebt (Shademap), repariert die servierte Kurve, aber
  nicht den Prior — SPEC-Grundsatz und explizite Forensik-Aussage („die
  statische Config bleibt der Prior der RAW-Kurve").
- **Shademap lernt ABSOLUT, wirkt residual:** Die gelernte T ist eine absolute
  Transmittanz (Referenz: ungated Beam), die per Shrinkage gegen den statischen
  Prior geblendet den Prior **ersetzt**. Doppelmodellierung droht daher nicht
  auf der tau-Achse selbst, sondern (a) über die Bias-Zellen (trainiert auf
  raw; ändern wir raw, passen sie nicht mehr — Forensik A4) und (b) über den
  Diffus-Term in der T-Formel: ein zu kleiner modellierter Diffus-Floor bläht
  gelernte T auf (Zähler zu groß) — Thema 2 verändert also rückwirkend die
  Bedeutung bereits gelernter Shademap-Bins.
- **15-min-Loop-Budget:** `transmittance_at` läuft pro Plane × Slot
  (8 × ~288 × Recompute alle 15 min); `sky_view_factor` ist pro
  (Geometrie, doy) prozessweit memoisiert (O(360)-Quadratur). Neue Semantik
  muss in diesen Kostenrahmen passen.
- **Backfill/Bootstrap-Konsistenz:** `scripts/backfill.py` rekonstruiert
  Plane-Stunden mit denselben `horizon.*`-Funktionen wie die Engine
  (Spiegel-Invariante der SLOW-Referenzserien). Jede Semantik-Änderung muss
  beide Pfade gleichzeitig treffen (gleiches Modul ⇒ automatisch erfüllt,
  aber Bootstrap-Blobs sind gegen die alte Physik gelernt).

---

## 2. Thema 1 — Elevationsabhängiges Horizont-tau

### 2.1 Optionen

#### Option H-A — Inline-Elevationsprofil pro Horizont-Zeile (EMPFOHLEN)

Jede `HorizonRow` erhält ein optionales Feld `tau_points`: eine Liste von
`[elevation_deg, tau]`-Stützpunkten, die tau **unterhalb** der bestehenden
`elevation_deg`-Kante als stückweise lineare Funktion der Sonnen-Elevation
definieren. `elevation_deg` bleibt unverändert die Gate-Kante (darüber tau=1
bzw. Shademap-Blend wie bisher); das skalare `tau` bleibt Pflichtfeld und ist
der Fallback (Zeilen ohne `tau_points` verhalten sich exakt wie heute).

Auswertung `tau(az, el)`: für die beiden az-bracketing Zeilen wird jeweils
`tau_row(el)` bestimmt (Profil-Interpolation; unterhalb des ersten Knotens =
erster Wert, oberhalb des letzten Knotens = letzter Wert; ohne Profil =
skalares `tau`), danach wie bisher linear in az interpoliert. Das ist eine
minimale Verallgemeinerung des bestehenden `_interp_rows`-Closures (Closure
bekommt `el` und `doy` mit).

Saisonalität: `seasonal: true` + `tau_points` definieren das **belaubte**
Profil; optional `tau_points_bare` (gleiches el-Raster, per Validierung
erzwungen) für den Winter; fehlt es, gilt kahl = `tau_bare` (skalar) bzw.
degradiert zur bisherigen Semantik. Foliage-Blend pro Knoten, dann
Interpolation — identisch zur bestehenden „resolve vor interpolate"-Regel.

SVF: Die Spalten-Integration erhält statt eines einzigen tau-Blends ein
**Band-Integral**: der blockierte Keil `[0, h]` wird an den Profilknoten
segmentiert, pro Segment gilt der Mittelpunkts-tau und die geschlossene Form
über die bestehenden `J1/J2`-Stammfunktionen (Differenzen zweier
Elevationsgrenzen statt `h..90`). Fehler der Mittelpunktsnäherung < 1 % bei
den vorliegenden Profilbreiten; die Quadratur bleibt O(360) und memoisiert.

#### Option H-B — Benannte tau(el)-Profile auf Site-Ebene, Referenz per Zeile

Site-Feld `tau_profiles: {ost_baeume: [[4.5, 0], [5.5, 0.25], …]}`; Zeilen
referenzieren `tau_profile: ost_baeume`. Identische Physik wie H-A, aber
DRY über die 8 Planes (das Live-Setup wiederholt die Baumreihe 8×).

#### Option H-C — Elevationsabhängigkeit ausschließlich der Shademap überlassen

Config bleibt wie heute (Kante el10, tau 0 oder ein konservativer Skalar);
die az×el×Halbjahr-Bins der Shademap lernen die reale tau(el)-Struktur als
absolute Werte, ggf. beschleunigt durch einen erneuten LTS-Bootstrap.

### 2.2 Trade-off-Tabelle Thema 1

| Kriterium | H-A inline `tau_points` | H-B benannte Profile | H-C Shademap-only |
|---|---|---|---|
| Abwärtskompatibilität | **Voll** — Feld optional, nur-wenn-gesetzt-Serialisierung, Alt-Configs byte-identisch | Voll, aber neues Site-Level-Feld + Referenz-Auflösung | **Trivial voll** (keine Schema-Änderung) |
| RAW-Kurve korrekt | **Ja** — Prior selbst wird richtig | Ja | **Nein** — raw bleibt falsch; Bias/Quantile/Scoreboard trainieren weiter gegen falschen Prior; issued raw_hourly_wh bleibt −20 % morgens |
| UI/Config-Flow | Keine UI-Änderung (ObjectSelector); Validierung +~40 Zeilen | Keine UI-Änderung; Validierung komplexer (dangling refs, Namensraum) | Keine |
| Saisondrift-Problem | **Gelöst** (tau hängt an el; Halbjahres-/Foliage-Logik unverändert) | Gelöst | Teilweise — Bins sind Sonnenpositions-nativ (driftfrei), aber Lerntempo: EMA α 0,15, Shrinkage n/(n+20), nur quasi-klare Samples ⇒ Wochen–Monate pro Bin, und die el<4-Dämmerbins scheitern oft am beam_share-Gate (>5 % Wp) |
| Doppelmodellierung Shademap | Keine — gelernte T ist absolut; besserer Prior verkleinert nur das Residuum (Shrinkage-Blend konvergiert schneller) | identisch | Konzept-inhärent: die Config lügt, der Lerner kompensiert — genau die Konstellation, die die Forensik als Ursache der Clamp-Sättigung identifiziert hat |
| Rechenkosten 15-min-Loop | +O(Knoten) pro `transmittance_at`-Aufruf (~5 Vergleiche); SVF-Band-Integral memoisiert pro (Geometrie, doy) — **vernachlässigbar** | identisch + eine Map-Lookup-Indirektion | 0 |
| Backfill/Bootstrap | Gleiche `horizon.*`-Funktionen ⇒ Engine/Backfill automatisch konsistent; Bootstrap-Neulauf empfohlen (raw-Referenz ändert sich) | identisch | Bootstrap müsste die tau(el)-Struktur selbst hergeben — der 311-Tage-Bootstrap lief gegen die VOR-Juli-Config, Bins tragen Misch-Semantik |
| Config-Lesbarkeit / Wartung | Profil steht bei der Geometrie, die es beschreibt; 8× dupliziert (~5 Knoten × 2 Zeilen × 8 Planes) | **Beste** (einmal definiert) | Config dokumentiert die Realität nicht |
| Implementierungsrisiko | Interp-Randfälle (Knoten außerhalb [0, Kante], Wrap-Segment), SVF-Integral | H-A-Risiken + Referenzvalidierung | Kein Code, aber Erwartungs-Risiko: Morgenfehler bleibt Monate sichtbar |

### 2.3 Entscheidung(sempfehlung) Thema 1

**H-A.** Begründung: H-C scheidet aus, weil die RAW-Kurve die Lern- und
Bewertungs-Wahrheit ist und bleiben soll — die Forensik hat gerade
nachgewiesen, was passiert, wenn statische Fehler in die (geclampten) Lerner
verschoben werden (Morning-θ am 1,5-Deckel, Intraday-Doppelkorrektur). H-B ist
H-A plus Indirektion; die DRY-Ersparnis (~70 YAML-Zeilen beim Referenz-Setup)
rechtfertigt die zusätzliche Validierungs- und Doku-Komplexität nicht. Sollte
sich Profil-Wiederverwendung später als häufiges Bedürfnis zeigen, ist H-B als
**additive Syntax-Ergänzung** auf H-A nachrüstbar (Profil-Referenz expandiert
beim Laden zu Inline-Knoten — kein Bruch).

### 2.4 Schema-Beispiel (YAML, Ziel-Config der Ost-Baumreihe)

Ersetzt die Interim-az-Rampe (8 Zeilen az52…89) in allen 8 Planes:

```yaml
horizon:
  - azimuth_deg: 51.99
    elevation_deg: 0
    tau: 1
  - azimuth_deg: 52
    elevation_deg: 10          # Kronen-Oberkante (unverändert, operatorgemessen)
    tau: 0                     # Fallback/Legacy-Wert (Pflichtfeld bleibt)
    tau_points:                # NEU: tau(Sonnen-el) unterhalb der Kante
      - [4.5, 0.00]
      - [5.5, 0.25]
      - [6.5, 0.45]
      - [8.0, 0.85]
      - [9.5, 1.00]
  - azimuth_deg: 89
    elevation_deg: 10
    tau: 0
    tau_points:
      - [4.5, 0.00]
      - [5.5, 0.25]
      - [6.5, 0.45]
      - [8.0, 0.85]
      - [9.5, 1.00]
  - azimuth_deg: 98            # ab hier unverändert (Sep–Apr-Sektoren,
    elevation_deg: 11.3        # in der Übergangsjahreszeit neu vermessen,
    tau: 0                     # dann ggf. ebenfalls tau_points)
  # … Rest der Tabelle unverändert (Screens, Wände)
```

Die Knotenwerte sind die gemessenen tau_eff-Mediane, konservativ gerundet wie
im Forensik-Report (Mitte ±0,15 Unsicherheit); der oberste Knoten endet bewusst
auf 1,0 **an** der Kante, damit am Gate-Übergang (unter/über der Linie) keine
Sprungstelle entsteht.

### 2.5 Validierungsregeln (`_site_validation._validate_horizon`, additiv)

1. `tau_points` optional; wenn vorhanden: Liste von 1–12 Paaren `[el, tau]`.
2. `el` streng aufsteigend; `0 ≤ el ≤ elevation_deg` der Zeile (Knoten oberhalb
   der Kante ⇒ Fehler `tau_points_above_edge` — oberhalb gilt per Definition 1).
3. `0 ≤ tau ≤ 1` je Knoten (Fehlercode `bad_tau`, bestehender Key).
4. `seasonal: true` + `tau_points` ⇒ `tau_points_bare` erlaubt; wenn gesetzt:
   gleiche Länge und identisches el-Raster (Fehler `seasonal_points_mismatch`);
   fehlt es, gilt kahl = skalares `tau_bare` (bestehende Pflicht bei seasonal).
5. Empfohlene (nicht erzwungene) Konvention: letzter Knoten `tau = 1.0` bei
   `el == elevation_deg` — Verstoß erzeugt kein Fehler, aber die Doku (SPEC §13)
   erklärt die resultierende Sprungstelle. Kein Monotonie-Zwang (reale Kronen
   können Lücken haben; der Messbefund 7–8 = 0,41 < 6–7 = 0,43 zeigt das).
6. Serialisierung: `to_dict` emittiert `tau_points` nur wenn gesetzt
   (Round-Trip-Garantie für Alt-Configs, bestehendes Muster).

### 2.6 Betroffene Konzepte/Module (Thema 1)

- `core/types.py::HorizonRow`: + `tau_points`, `tau_points_bare`
  (frozen/slots-kompatibel als Tupel von Tupeln ⇒ hashbar, die
  `lru_cache`-Memos in `horizon.py` funktionieren unverändert strukturell).
- `core/horizon.py`: `transmittance_at(plane, sun_az, doy)` →
  `transmittance_at(plane, sun_az, doy, sun_el=None)`; `sun_el=None` reproduziert
  exakt das heutige Verhalten (Profil wird dann am obersten Knoten ausgewertet
  bzw. Skalar-Fallback) — alle Alt-Aufrufer bleiben korrekt. Engine übergibt
  `sun_el` (liegt dort vor). SVF-Spaltenintegral → Band-Integral (§2.1).
- `core/engine.py`: nur Aufruf-Signatur (sun_el durchreichen); `static_tau`-
  Logik (unter Linie: profil-tau; über Linie: 1,0 + Shademap-Blend) unverändert.
- `scripts/backfill.py` und Shade-Profile-Diagnostik: nutzen dieselben
  `horizon.*`-Funktionen; das Schattenprofil-Diagramm (az×el-Scan) wird durch
  die el-Abhängigkeit erstmals **korrekt** statt konstant je az-Spalte.
- `docs/SPEC.md` §13 (+ §4 Schritt 5 ein Satz).

### 2.7 Migrationspfad von der Interim-az-Rampe

1. Release 0.22 ausrollen (Code versteht `tau_points`; Verhalten aller
   Bestands-Configs unverändert — Migrations-Nullschritt, kein Store-Touch).
2. Operator-Config-Edit (Options-Flow, ObjectSelector): in allen 8 Planes die
   8 Interim-Rampen-Zeilen (az52…az89 mit tau 0→0,85→0) durch die 2 Zeilen aus
   §2.4 ersetzen. Reiner Datenedit, jederzeit rückrollbar.
3. **Gleichzeitig** `reset_day_ahead_bias` ausführen (A4: raw ändert sich um
   +50–150 Wh/Tag morgens; die Morgenzelle klemmt ohnehin am 1,5-Clamp).
   Sobald der Config-Fingerprint (A4-Code-Teil) existiert, passiert die
   n-Deckelung automatisch.
4. Shademap: kein Zwangs-Reset nötig (gelernte T absolut), aber ein erneuter
   LTS-Bootstrap wird empfohlen, damit Bins, deren Samples unter tau=0-Prior
   admittiert wurden, gegen die neue Diffus-/Beam-Referenz neu entstehen
   (BOOTSTRAP_MAX_BIN_N-Deckel macht das risikoarm).
5. Erwarteter Übergangseffekt (identisch zum Interim-Fix dokumentiert):
   served 04–06Z überschießt ~3–7 Tage, bis die Bias-Zellen zurücklernen —
   nicht zurückrollen. Voraussetzung: Actuals-Epoch-Bugfix deployed.
6. Interim-Rampe im Repo/Doku als deprecated markieren (Betriebsanleitung:
   „nicht nachankern, migrieren").

### 2.8 Testplan Thema 1

Unit (pytest, pure Core):
- `transmittance_at`-Goldwerte auf einem synthetischen Profil: unter erstem
  Knoten, zwischen Knoten, exakt auf Knoten, über letztem Knoten aber unter
  Kante, über Kante (⇒ 1,0); az-Interpolation zwischen Profil-Zeile und
  Skalar-Zeile; Wrap-Segment mit Profil.
- Property-Test Abwärtskompatibilität: für Zeilen OHNE `tau_points` ist
  jedes (az, el, doy)-Ergebnis von `transmittance_at` und `sky_view_factor`
  **bit-identisch** zur alten Implementierung (Referenz eingefroren).
- SVF-Band-Integral vs. numerische Brute-Force-Quadratur (feines el-Raster)
  < 0,5 % Abweichung; Extremfälle tau_points konstant 0 (== opak) und
  konstant 1 (== SVF 1) bit-exakt (bestehende Invarianten).
- Seasonal: Foliage-Blend pro Knoten; Ramp-Kontinuität über Jahresgrenze.
- Validierung: jede Regel aus §2.5 positiv + negativ.

Integration/Regression:
- Engine-Snapshot Referenz-Site mit 24.07.-Wetter: Migration Interim-Rampe →
  `tau_points` ändert die Juli-raw-Kurve 04:00–04:45Z um < ±40 Wh (beide
  kodieren dasselbe Messprofil), Mittag/Nachmittag byte-identisch (Gate greift
  nie über el10 — bestehende Wirkkontrolle).
- Saisontest: synthetischer 25.08.-Lauf — Dämmerungs-Slots (Sonne el<4,
  az77–86) liefern mit `tau_points` ~0 Beam, mit der Interim-Rampe +35–100 Wh
  (der Phantom-Beam-Regressionstest, DER Beweis des Designs).
- Backfill-Spiegel: `reconstruct_plane_hour` == Engine-Referenzserien auf
  einem Profil-Setup.

Live-Validierung (1 Woche, Operator; identisch zum Forensik-Protokoll):
raw 04Z ≥ 300 Wh an klaren Tagen, raw06–10Z/act 0,90–1,05, Morgenzelle löst
sich vom 1,5-Clamp, Scoreboard-Stratum clear fällt.

### 2.9 Aufwand Thema 1: **M**

~2–3 PT: 4 Module + Validierung + ~15 neue Tests + SPEC-Absatz. Kein
UI-Aufwand, kein Store-Schema, keine Migrationstools. Das „M" (statt S) kommt
aus dem SVF-Band-Integral und der Property-Test-Absicherung der
Bit-Identität; das Risiko ist auf `horizon.py` lokalisiert.

---

## 3. Thema 2 — Diffus-Floor / Wand-SVF

### 3.1 Quantifizierung: Was erklärt welchen Teil des ×10?

Alle Zahlen: Begleit-Rechnung `ADR-0022-rechnungen-diffus-floor.md`; Kernkette hier.

**Messbasis (04Z-Stundenmittel):** M4 klar 22,5–29,2 W ⇒ POA ≈ 55–71 W/m²
(P/(Wp·η_mod) = 27/(430·0,96) ⇒ ~65 W/m², Report-konsistent). Overcast 19.07.:
8,1 W ⇒ POA ≈ 20 W/m². Modell diffus-only: 2,8–2,9 W ⇒ POA ≈ 6,8 W/m²
(= iso·SVF + Bodenreflex; SVF 0,288, Albedo 0,15).

**Schritt 1 — Was kann eine Diffus-Reflektanz ρ maximal?** Ersetzt man den
blockierten SVF-Anteil durch ρ-fach mittlere Himmelsradianz
(`SVF_eff = SVF + ρ·(1−SVF)`), skaliert nur der iso-Term. Selbst ρ = 1
(blockierter Sektor strahlt wie offener Himmel) hebt M4 von 6,8 auf nur
~8–18 W/m² (iso_unobstructed ≈ 5–13 W/m² an klaren 04Z-Morgen, weil Hay-Davies
den klaren Dämmerungs-DHI überwiegend als Zirkumsolar führt, das für az205 mit
cosθ=0 wegfällt). **⇒ Die klare 65-W/m²-Beobachtung ist mit keiner
physikalisch beschränkten Diffus-Reflexion erreichbar; ρ müsste ~6 sein.**

**Schritt 2 — Der Overcast-Tag trennt die Mechanismen.** Am 19.07. (kein Beam,
DHI ≈ GHI) messen M4/M8 8–9 W ≈ 20 W/m²; das isotrope Modell liefert dort
~9–10 W/m² unobstructed ⇒ benötigt `SVF_eff ≈ 0,65` ⇒
**ρ ≈ (0,65 − 0,29)/(1 − 0,29) ≈ 0,5** — exakt die Albedo einer hellen
Putzwand. Der isotrope Anteil der Lücke ist damit VOLLSTÄNDIG durch eine
Wand-Reflektanz ~0,5 erklärt.

**Schritt 3 — Der Rest ist beam-gebunden.** Klar minus overcast: ~+45 W/m²
POA, die nur existieren, wenn die (für die Vorderseite unsichtbare) Morgensonne
scheint. Geometrie: Rückseite von M4/M8 zeigt nach az25, tilt-bedingt leicht
abwärts; cosθ zur 04Z-Sonne (az60–75, el5–12) ≈ 0,6–0,7. Mit effektivem
Rückseiten-Koeffizienten ~0,2 (Modul-Bifazialität × Wand-/Spalt-Sichtfaktor)
und DNI 300–600 durch die Baumkronen (tau_eff 0,25–0,9) ergeben sich
~15–50 W/m² — Größenordnung passt. Das bestehende Site-Feld
`bifacial_beam_gain` kann das strukturell NICHT ausdrücken: es multipliziert
den **vorderseitigen** Beam, der hier exakt 0 ist.

**Konsequenz:** Ein Fix in zwei Schichten — (a) Diffus-Reflektanz jetzt
(deckt Overcast-Anteil ganz, klaren Anteil ~20–30 %), (b) Rückseiten-Beam als
separates, optionales, begrenztes Feld später. Der Rest bleibt bewusst dem
Bias-Lerner (der nach A4-Reset Headroom hat).

### 3.2 Optionen

#### Option D1 — Globales Site-Feld `obstacle_reflectance` (ρ)

`SiteConfig.obstacle_reflectance: float | None` (Default None ⇒ 0 ⇒ heutiges
Verhalten; Clamp [0, 0,8]). Engine/SVF: `SVF_eff = SVF + ρ·(1−SVF)` — eine
Zeile in der Spaltenintegration oder nachgelagert auf den SVF-Skalar.

#### Option D2 — Getrennte `diffuse_tau` pro Horizont-Zeile (EMPFOHLEN)

`HorizonRow.diffuse_tau: float | None` (Default None ⇒ Diffus nutzt wie heute
das Beam-tau — bestehende semi-transparente Semantik bleibt der Normalfall).
Nur in der SVF-Spaltenintegration: der blockierte Keil trägt
`diffuse_tau` (statt Beam-tau) seines offenen Werts. Beam-Gate unverändert.
Interpretation dokumentieren: „effektive Radianz des blockierten Sektors
relativ zum offenen Himmel" — für eine helle Wand ist das ihre Reflektanz
(~0,5), für Bäume bleibt es die Transmission (Default = Beam-tau korrekt).
**Passt exakt in das Thema-1-Zeilenschema** (gleiche Zeile, gleiche
Validierung, gleiches nur-wenn-gesetzt-Muster); bei Bedarf später
`diffuse_tau_points` analog — jetzt nicht nötig (Wände sind el-konstant).

#### Option D3 — Rückseiten-Beam-Pickup `rear_beam_fraction` (entwerfen, zurückstellen)

Site-Feld (Clamp [0, 0,3], Default 0 ⇒ no-op):
`rear_beam_poa = rear_beam_fraction × DNI × max(0, −cosθ_front) × tau_static(sun_az, sun_el, doy)`
— additiv auf `diffuse_poa` (er ist diffus-artig für Clamp/Shademap-Zwecke:
er darf NICHT in die beam-referenzierte T-Trainingsreferenz einfließen, sonst
lernt die Shademap ihn doppelt). Nutzt vorhandene Größen (`cos_theta` aus der
Transposition, tau aus Thema 1 — dieselben Baumkronen filtern den
Rückseiten-Beam). Kein neuer Integrations- oder Raytracing-Schritt.
Zurückgestellt, weil (i) der Koeffizient aus den vorhandenen Daten nur bis auf
Faktor ~2 bestimmbar ist, (ii) erst D2 + Thema 1 die Referenz-Physik
stabilisieren, gegen die man ihn sauber fitten kann (eine Woche klare-Morgen-
Residuen M4/M8 nach 0.22 liefert den Fit frei Haus).

> **Nachtrag 25.07. (D3-Vorab-Fit, Offline-Fit-Skripte, nicht im Repo):** Der
> Erst-Fit aus den 17.–24.07.-Daten ergibt **f ≈ 0,32 (CI90 0,30–0,36)**, stabil
> über den Kronen-tau-Ramp (tau_static-Gate empirisch bestätigt) und
> **~155–190 Wh/Tag** erklärbar ⇒ D3 ist LOHNEND. Zwei Korrekturen am Entwurf:
> (1) **per-Plane-Feld statt Site-Feld** — die Konsistenzprüfung zeigt den Effekt
> ausschließlich an M4/M8 (OSO-Abende implied f ≈ 0, M1/M5 ≈ 0,03–0,06); ein
> Site-weites f=0,32 würde ~840–910 Wh/Tag Phantom auf den übrigen 6 Planes
> fabrizieren. (2) **Clamp [0, 0,4] statt [0, 0,3]** — der Fit klemmt sonst am
> Deckel. Gültigkeitsgrenze: die −cosθ-Form trägt nur bei |cosθ_front| ≥ 0,35
> (04–05Z); der ab 06Z weiter wachsende Exzess ist besonnter Wand-Reflex und
> bleibt bewusst dem Bias-Lerner. Bestätigung durch die D2-Validierungswoche
> bleibt Voraussetzung (`d3_fit.py`, Modus post022).

#### Option D0 (verworfen) — Volles Bifacial-/Reflexionsmodell

Rückseiten-Hay-Davies je Plane, Wand-Leuchtdichte aus Sonnenstand,
Sichtfaktoren Modul↔Wand↔Boden. Nicht-Ziel: Datenlage kann die Parameter
nicht identifizieren, Rechenkosten ×2 im Loop, und der Nutzen über D2+D3
hinaus liegt unter der Wetter-Rauschgrenze des Standorts.

### 3.3 Trade-off-Tabelle Thema 2

| Kriterium | D1 global ρ | D2 per-Zeile `diffuse_tau` | D3 `rear_beam_fraction` | D0 Bifacial voll |
|---|---|---|---|---|
| Erklärt Overcast-Lücke (×2) | Ja, aber unselektiv | **Ja, gezielt** (nur Wand-Zeilen) | Nein (beamlos) | Ja |
| Erklärt klaren ×10-Rest | Nein (ρ≤1 ⇒ ~20 %) | Nein (~20–30 %) | **Ja (Mechanismus passt)** | Ja |
| Nebenwirkungen andere Planes | **Hebt AUCH Baum-Sektoren** aller 8 Planes (Bäume „reflektieren" wie Wände — physikalisch falsch; Mittags-iso +5–15 W/m² je Plane) | Nur dort, wo der Operator es setzt; Bäume behalten Transmissions-Semantik | Wirkt nur bei Sonne HINTER der Plane (M4/M8 morgens, M2/3/6/7 abends — dort real existent); Mittag exakt 0 | breit, schwer prüfbar |
| Abwärtskompatibilität | Voll (Default 0) | **Voll** (Default None ⇒ Beam-tau, heutige Semantik) | Voll (Default 0) | Voll, aber Schema-Explosion |
| Schema-Kohärenz mit Thema 1 | getrenntes Site-Feld | **Gleiche Zeile, gleiches Muster** — ein Review, eine Doku-Sektion | Site-Feld analog `bifacial_beam_gain` | neue Objektfamilie |
| Rechenkosten | ~0 (memoisierter SVF) | ~0 (memoisierter SVF) | +1 Multiplikation/Plane/Slot | O(2×) Transposition |
| Lerner-Wechselwirkung | Diffus-Floor ↑ ⇒ beam-referenzierte T-Samples ↓ (Zähler kleiner): M4/M8-Bins am 1,1-Clamp normalisieren; Bias-Zellen brauchen Reset (A4) | identisch, aber nur betroffene Planes | Muss aus der T-Referenz herausgehalten werden (sonst Doppellernen); Bias-Reset | massiv |
| Aufwand | S | **S** (huckepack Thema 1) | M | L |

### 3.4 Entscheidung(sempfehlung) Thema 2

**D2 jetzt, D3 als vorbereiteter Folgeschritt, D0 Nicht-Ziel.** D1 wird
verworfen, weil ein globales ρ die Baum-Sektoren fälschlich aufhellt — genau
die Sektoren, deren Diffus-Verhalten Thema 1 gerade korrekt (transmissiv)
modelliert; die Wand ist eine Eigenschaft einzelner Zeilen, nicht der Site.
D2 ist zudem die einzige Option, die mit dem Thema-1-Schema eine gemeinsame,
in sich konsistente Horizont-Zeilen-Semantik ergibt: **`tau`/`tau_points` =
Beam-Transmission; `diffuse_tau` = Diffus-Radianz-Ersatz des Sektors; Default
diffus = beam** (heutiges Verhalten, dokumentiert seit v0.5.x).

Bewusste Ehrlichkeit: D2 schließt die M4/M8-Lücke NICHT vollständig (klare
Morgen bleiben ~×3 unterschätzt, ~90–150 Wh/Tag site-weit) — das wird im
Release vermerkt und NICHT über überhöhte `diffuse_tau`-Werte kaschiert
(Werte > 0,8 lehnt die Validierung ab, s. u.), exakt wie der Forensik-Report
den Diffus-Rest nicht über Beam-tau kaschiert hat.

### 3.5 Schema-Beispiel (YAML, M4/M8-Wand + M1/M5-Wand)

```yaml
# M4 / M8 (az205): Hauswand hinter/neben den Planes
  - azimuth_deg: 195
    elevation_deg: 90
    tau: 0                 # Beam: Wand bleibt opak (unverändert)
    diffuse_tau: 0.5       # NEU: helle Putzwand ersetzt ~50 % der blockierten Himmelsradianz
  - azimuth_deg: 360
    elevation_deg: 90
    tau: 0
    diffuse_tau: 0.5

# M1 / M5 (az25): Wand az295–360 — gleicher Edit, eigener Wert
  - azimuth_deg: 295
    elevation_deg: 90
    tau: 0
    diffuse_tau: 0.5
  - azimuth_deg: 360
    elevation_deg: 90
    tau: 0
    diffuse_tau: 0.5
```

Erwartete Wirkung (Rechnung §3.1 / Begleitdatei): M4-SVF 0,288 → ~0,64
(Wandanteil ~0,7 des blockierten Doms; konsistent mit der Report-Schätzung
„tau_wand 0,3 ⇒ SVF 0,29→0,5"). Modell-Diffus M4 04Z ~2,9 → ~7–9 W (Overcast-
Beobachtung getroffen); wandverschatteter Nachmittag (12–17Z, gemessen
24–31 W je Modul) steigt entsprechend; site-weit ~+0,1–0,2 kWh/Tag.

> **Erratum / Implementierungsnotiz (0.22.0, korrigiert 25.07.):** Die obige
> Wirkprognose ist zu optimistisch (derselbe Anteils-Rechenfehler wie §3.8 /
> `rechnungen` §4). Real hebt `diffuse_tau 0.5` M4 von **0,2879 auf 0,5761**
> (nicht ~0,64), der Wandanteil ist ~0,81 des *blockierten* Doms. Der iso-Diffus-
> Anteil skaliert damit ~×2,0 statt ~×2,2 ⇒ **Modell-Diffus M4 04Z ~2,9 → ~5–7 W**
> (nicht 7–9 W). Für die D2-Validierungswoche gilt entsprechend die revidierte
> Operator-Erwartung: Overcast-Morgen (19.07., gemessen ~8 W) real **~5–7 W**;
> das ±30 %-Kriterium aus §3.8 wird am Overcast-Tag damit **grenzwertig** erreicht
> (nicht komfortabel). Richtung und Größenordnung des Fixes bleiben korrekt, der
> Rest-Gap M4/M8 (~×3, beam-gebunden, D3) bleibt unverändert dokumentiert-offen.

### 3.6 Nebenwirkungs-Analyse (explizit gefordert)

- **M1/M5 (Wand az295–360, tilt 70/80, az25):** SVF steigt ⇒ mehr iso-Diffus
  ganztägig, insbesondere abends, wenn die Sonne hinter der Wand steht.
  Risiko Überprognose abends: klein — der iso-Anteil an M1/M5-Abenden ist
  ~5–15 W/m²·ΔSVF; Testplan prüft 16–19Z-Residuen. Die Wand ist real hell ⇒
  Korrektur physikalisch gerichtet richtig.
- **M2/3/6/7 mittags/abends:** keine Wand-Zeilen ⇒ nur über Thema 1
  (tau_points wirken auch im SVF-Band-Integral: Baumsektoren tragen künftig
  tau(el)-gewichtetes Diffus statt 0) — hebt die Dämmerungs-Floors aller
  Planes und adressiert einen Teil des „Faktor 4–9"-Dämmerungsdefizits.
- **Abendstunden generell:** Der Beam-Pfad ist von D2 unberührt; die
  bekannte Screen-Fehlzuordnung (az135–175-Screen wirkt real auf M2/M3, ist
  auf M4/M8 konfiguriert) bleibt ein SEPARATER Befund — dieselbe
  Konfig-Kampagne sollte ihn mit erledigen, aber er ist nicht Teil dieses ADR.
- **Gelernte Zustände:**
  - *Shademap:* T-Samples = (meas − diffus_modell)/beam_ungated. Diffus-Modell
    ↑ ⇒ künftige T-Samples ↓. Bereits gelernte Bins (u. a. M4/M8-Morgenbins,
    mutmaßlich am 1,1-Clamp, weil der fehlende Diffus-Floor als Phantom-Beam-
    Gain gelernt wurde) tragen die alte, zu helle Semantik ⇒ **Re-Bootstrap
    nach dem Release** (deckt via BOOTSTRAP_MAX_BIN_N schnell über) oder
    gezielter Channel-Reset M4/M8/M1/M5.
  - *Day-ahead-Bias:* raw ändert sich (Diffus + Thema 1) ⇒ `reset_day_ahead_bias`
    im selben Wartungsfenster (ein Reset für beide Themen — Hauptgrund, sie in
    EINEM Release zu bündeln).
  - *Quantile-Ring:* relerr-Verteilungen verschieben sich leicht; kein Reset
    nötig (90-Tage-Fenster wäscht aus), aber im Scoreboard eine
    Übergangswoche einplanen.
  - *Intraday-Scalar:* profitiert (Morgen-Peaks sinken strukturell weiter,
    komplementär zu Forensik A2).

### 3.7 Validierungsregeln (additiv)

1. `diffuse_tau` optional; wenn gesetzt: `0 ≤ diffuse_tau ≤ 0.8`
   (Fehlercode `bad_diffuse_tau`). Obergrenze 0,8 statt 1,0: Werte nahe 1
   wären „Sektor unsichtbar fürs Diffus" — physikalisch nur für Spiegel
   plausibel und das bevorzugte Missbrauchs-Ventil, um den beam-gebundenen
   Rest zu kaschieren (Design-Absicht: NICHT kaschieren).
2. `diffuse_tau` ist unabhängig von `tau`/`tau_points` gültig (eine
   semi-transparente Baumzeile DARF zusätzlich `diffuse_tau` tragen; dann gilt
   fürs Diffus `diffuse_tau`, fürs Beam weiter tau(el)).
3. Serialisierung nur-wenn-gesetzt (Round-Trip-Garantie).
4. D3 (wenn aktiviert): `0 ≤ rear_beam_fraction ≤ 0.3`, Clamp beim Laden wie
   `bifacial_beam_gain` (Site-Feld-Muster inkl. getattr-Toleranz für Fakes).

### 3.8 Testplan Thema 2

Unit:
- SVF-Quadratur: Wand-Sektor mit `diffuse_tau 0,5` ⇒ M4-Geometrie-SVF
  0,288 → 0,576±0,01 (Goldwert gegen Brute-Force); `diffuse_tau` None ⇒
  bit-identisch heute; `diffuse_tau 0` ⇒ bit-identisch heute (Wand-Fall).

> **Erratum / Implementierungsnotiz (0.22.0, korrigiert 25.07.):** Der ursprüngliche
> Designwert „0,288 → 0,63±0,02" war zu hoch. Mit dem Release-Code auf der realen
> 17-Zeilen-M4-Tabelle (az195/az360-Wandzeilen, `diffuse_tau 0.5`) ergibt sich
> **0,2879 → 0,5761** (M8: **0,2944 → 0,5852**), unabhängig per Brute-Force-
> Quadratur bestätigt und deckungsgleich mit dem Live-Diagnostics-Baseline 0,288.
> Ursache ist der Anteils-Rechenfehler in `ADR-0022-rechnungen-diffus-floor.md` §4
> (siehe dortige Korrektur): der Wandanteil zählt zum *blockierten* Dom, nicht zum
> Gesamtdom (real ~0,81 des blockierten Doms). Der **realer** Goldwert ist damit
> **0,288 → 0,576±0,01**. Der pytest-Goldwert wird bewusst auf einer *wall-only*-
> Synthetik gepinnt (`tests/core/test_horizon_diffuse_tau.py`: 0,423 → 0,712,
> geschlossene Blend-Identität `ρ + (1−ρ)·SVF₀`, unabhängig verifiziert), weil die
> lineare Identität nur für einen reinen Wand-Dom gilt — die reale Tabelle trägt
> zusätzlich Baum-/Screen-Sektoren, auf die `diffuse_tau` nicht wirkt.
- Engine: Beam-Serien byte-identisch mit/ohne `diffuse_tau` (Diffus-only-Fix);
  `diffuse_ref_watts` (SLOW-Referenz) trägt den neuen Floor (Trainings-Label
  konsistent), `beam_ref_watts` unverändert.
- Validierung §3.7 positiv/negativ.

Daten-Regression (offline, vorhandene Abzüge):
- Overcast-Morgen 19.07.: Modell-M4/M8 04–05Z innerhalb ±30 % der Messung
  (heute −65 %).
- Wolkenloser 24.07.: 11–17Z site-raw ändert sich um < +2 % (Mittags-Guard);
  M4/M8 12–17Z-Floor-Residuum halbiert.
- M1/M5 16–19Z: kein neuer Overshoot > +10 %.
- Klare Morgen: dokumentierter Rest-Gap M4/M8 ~×3 bleibt (Erwartungswert für
  den späteren D3-Fit; NICHT als Regression werten).

Live (1 Woche): M4/M8-Vergleich port_2_2/port_2_4 vs. Modell je Tagesklasse;
Shademap-Diagnose: M4/M8-Morgenbins verlassen den 1,1-Clamp; day_ahead-Zellen
nach Reset im Band 0,9–1,15.

### 3.9 Aufwand Thema 2

- **D2: S** (~0,5–1 PT): 1 Feld in `HorizonRow`, ~10 Zeilen in der
  SVF-Spaltenintegration, Validierung, ~8 Tests, SPEC-Absatz — huckepack auf
  das Thema-1-Release (gleiche Dateien, gleiche Review).
- **D3 (zurückgestellt): M** (~1–2 PT): Site-Feld, Engine-Additiv im
  Diffus-Kanal, Abgrenzung von der T-Referenz, Fit-Skript für den
  Koeffizienten, Tests. Entscheidung nach einer D2-Validierungswoche anhand
  der klaren-Morgen-Residuen.

---

## 4. Konsequenzen (beide Themen gemeinsam)

1. **Ein Release (~0.22), eine Config-Kampagne, ein Lern-Reset.** Beide Themen
   ändern die RAW-Kurve; getrennte Releases hießen zwei Bias-Einschwingphasen.
   Reihenfolge im Release: Schema+Physik (H-A, D2) → Operator-Edit
   (tau_points-Migration + diffuse_tau-Wände + sinnvollerweise der separate
   Screen-Reassign) → `reset_day_ahead_bias` → LTS-Re-Bootstrap.
   Voraussetzungen: Actuals-Epoch-Bugfix deployed, idealerweise
   Config-Fingerprint (A4) im selben Release.
2. **Erwarteter Netto-Effekt** (klare Tage, zusätzlich zu den 16./24.07.-Fixes):
   Dämmerung/Morgen-raw realistischer (Phantom-Beam-Zukunftsrisiko eliminiert),
   +~0,1–0,2 kWh/Tag Diffus site-weit, M4/M8-Modellierung von „Faktor 10" auf
   „Faktor ~3, dokumentiert, beam-gebunden". Der Rest ist bewusst dem
   Bias-Lerner bzw. D3 zugewiesen.
3. **Schema-Schuld getilgt statt vermehrt:** Die Horizont-Zeile bekommt eine
   vollständige, orthogonale Semantik (Beam-Transmission el-abhängig;
   Diffus-Radianz-Ersatz) — die nächste Erweiterung (saisonale Wand? Sep–Apr-
   Sektoren neu vermessen) braucht keine neue Mechanik mehr.
4. **Risiken:** (i) SVF-Integral-Fehler wären flächig sichtbar — durch
   Bit-Identitäts-Property-Tests für Alt-Configs abgesichert; (ii) die
   Übergangswoche mit Überschießen 04–06Z ist unvermeidlich (Clamp-Zelle) und
   kommuniziert; (iii) Doku-Pflicht: `diffuse_tau` ist ein EFFEKTIV-Wert —
   Operatoren könnten ihn als Transmission missverstehen (SPEC-§13-Absatz +
   Validierungs-Obergrenze 0,8 als Leitplanke).
5. **Nicht-Ziele festgeschrieben:** kein volles Bifacial-/Reflexionsmodell
   (D0), keine automatische Ableitung von tau_points aus Messdaten im Core
   (bleibt Offline-Analyse/Shademap), kein UI-Editor für Horizontprofile.

---

## 5. Anhang: Betroffene Dateien (Konzept-Ebene, Stand 24.07. abends)

| Datei | Thema 1 | Thema 2 (D2) |
|---|---|---|
| `custom_components/balcony_solar_forecast/core/types.py` | HorizonRow + `tau_points`(+`_bare`), from/to_dict nur-wenn-gesetzt | HorizonRow + `diffuse_tau` |
| `custom_components/balcony_solar_forecast/core/horizon.py` | `transmittance_at(+sun_el)`, `_interp_rows`-Closure el-fähig, SVF-Band-Integral | `_interp_diffuse_tau` bevorzugt `diffuse_tau` |
| `custom_components/balcony_solar_forecast/core/engine.py` | sun_el an `transmittance_at` durchreichen | — (SVF-only) |
| `custom_components/balcony_solar_forecast/_site_validation.py` | Regeln §2.5 | Regel §3.7 |
| `scripts/backfill.py` | folgt automatisch (gleiche horizon-Funktionen); Re-Bootstrap-Empfehlung | dito |
| `docs/SPEC.md` §4/§13 (+§5 Hinweis T-Referenz) | ja | ja |
| `config_flow.py` | keine Änderung (ObjectSelector) | keine Änderung |

*Nicht* betroffen: Store-Schema, Sensor-Contracts, Services, Shademap-Keying.
