# ADR-0023: Onboarding & Standortkonfiguration für die allgemeine Veröffentlichung

| | |
|---|---|
| **Status** | Proposed |
| **Datum** | 2026-07-25 |
| **Ziel-Release** | gestuft: MVP ~0.24/0.25, v1 zur ersten breiten Veröffentlichung, Ausbaustufen danach |
| **Autoren** | Design-Agent (Fable) |
| **Evidenz** | Code-Analyse `main` @ v0.23.0 (`config_flow.py`, `_site_validation.py`, `const.py::DEFAULT_SITE`, `core/types.py`, `core/shademap.py`, `_services.py`, `manifest.json`); 7-Tage-Forensik Juli 2026 (`docs/project-knowledge/06-…`); ADR-0022; Wissensbasis `docs/project-knowledge/` (02 Physik, 03 Lernschichten, 05 Runbook); Kampagnen-Site-YAML des Betreibers (Session-Artefakt, 333 Zeilen) |
| **Scope** | NUR Analyse + Design. Kein Code, keine bestehende Doku geändert. Frage: Wie entsteht und ändert sich die STANDORT-KONFIGURATION, wenn die Integration von der Einzel-Betreiber-HACS-Installation zur allgemein veröffentlichten Integration wird? |

---

## 0. Executive Summary

**Der eine Blocker:** `const.DEFAULT_SITE` — der Startpunkt jeder Neuinstallation —
ist die komplette Referenzanlage DIESES Betreibers: 8 Module in Landshut,
3 Fassaden, ein durch Messdaten **widerlegter** Verschattungs-Screen (az 135–175
sitzt auf M4/M8, verschattet real M2/M3), eine überholte Wandkante (az 212 statt
live 195) — und **acht hartkodierte Hoymiles-Entity-IDs**. Letzteres ist der
schwerste Defekt: ein konfigurierter Kanal ohne brauchbare LTS-Zeile verwirft in
`_actuals` den **ganzen Trainingstag** — ein Fremdnutzer, der die Default-Site
übernimmt, bekommt eine geometrisch falsche Prognose UND ein System, das **nie
einen einzigen Tag lernt**. Empfehlung MVP-0: neutraler Minimal-Default (1 Ebene,
offener Himmel, keine Entities), Betreiber-Site wird benanntes Preset/Fixture.
Aufwand **S**, nicht verhandelbar vor Veröffentlichung.

**Empfehlung je Option** (Details §2, gestufter Plan §3):

| # | Option | Verdikt | Aufwand |
|---|---|---|---|
| O1 | Geführter mehrstufiger Config-Flow (Ebenen als Subentries, Fassaden-Horizont) | **v1** — aber strikt als zweite Erzeugungsart desselben `site`-Dicts; Horizont bleibt auch dort YAML/Preset | **L** |
| O2 | Presets + Import/Export (Menü im User-Step, `export_site`-Service) | **MVP** — billigster großer Hebel; ersetzt zusammen mit MVP-0 den falschen Default | **S–M** |
| O3 | Start ohne Horizont, Verschattung lernen | **MVP als Default-Haltung** — offener Himmel irrt in die *lernbare* Richtung; aber Wände MÜSSEN abgefragt werden (Diffus-Pfad lernt nie, Tages-Gate 0,8 sperrt stark verschattete Sites strukturell aus) | **S** (+ dokumentierte Grenzen) |
| O4 | Datengetriebener Horizont-Vorschlag aus Shademap (`suggest_horizon`) | **Ausbaustufe** — der designte Lebenszyklus „lernen → ernten → Config"; Bausteine (`run_bootstrap`, `dump_shademap`, Shade-Profil-Karte) existieren | **M–L** |
| O5 | PVGIS-Fernhorizont als optionaler Wizard-Schritt | **v1** — einmaliger Fetch, keine neue Dependency, ehrlich als „Fernhorizont, keine Bäume/Wände" gelabelt | **S–M** |
| O6 | Migrations-/Versionierungs-UX (Site-Snapshots, geführter Re-Bootstrap, Export ohne Redaktion) | **MVP-Kern + v1-Ausbau** — die Mechanik (Fingerprint-Reseed, `run_bootstrap`, Rollback-Ring) existiert; was fehlt, ist die Orchestrierung für Nicht-Experten | **M** |

**Die drei wichtigsten Trade-offs:**

1. **Falscher Prior vs. fehlender Prior.** Die RAW-Kurve ist die Lern-Wahrheit;
   alle Lerner sind geclamped (θ ≤ 1,5, Shademap-τ ≤ 1,1). Ein zu *dunkler*
   Prior (der ausgelieferte Screen bei einem Nutzer ohne diesen Baum) zwingt die
   Lerner in die Sättigung nach oben — exakt der Juli-2026-Fehlermodus. Ein
   *offener* Prior irrt nach oben, und Abdunkeln ist die Richtung, die die
   Shademap bis τ = 0 ausdrücken kann. Deshalb: kein Horizont schlägt einen
   falschen Horizont — aber eine Hauswand ist billig zu **fragen** und teuer zu
   **lernen** (Diffus-/SVF-Pfad hat gar keinen Lerner), also gehört sie in den
   Wizard.
2. **UI-Aufwand vs. Datenmodell-Stabilität.** Jede Onboarding-Verbesserung muss
   dasselbe kanonische `site`-Dict erzeugen, das `validate_site` heute prüft und
   `SiteConfig.from_dict` lädt — Engine, Store, Fingerprint, Backfill bleiben
   unberührt. Ein zweites „Wizard-Schema" wäre die Neuauflage der in ADR-0022
   verworfenen H-B-Indirektion auf UI-Ebene.
3. **Automatik vs. Ehrlichkeit.** Daten- und PVGIS-Vorschläge nur als
   *Vorschlag mit Evidenz* (welche Referenztage, wie viele Samples), nie
   Auto-Apply — die Forensik-Lehre „Bin-Abwesenheit ≠ freie Sicht" und „Rohkurven
   klarer Referenztage sind der härteste Test" gilt für jede automatische
   Ableitung genauso wie für Handarbeit.

**Aufwand gesamt:** MVP **M** (~4–6 PT), v1 zusätzlich **L** (~8–12 PT),
Ausbaustufen (O4 + Feinschliff) weitere **M–L**.

---

## 1. Kontext

### 1.1 Ist-Zustand (code-verifiziert, v0.23.0)

**Ein Feld für alles.** Der Config-Flow (`config_flow.py`) zeigt im User- und im
Reconfigure-Step neben Name/Koordinaten/Intervallen und vier optionalen
Site-Skalaren (AC-Zähler + Invert, Albedo, Beam-Gain) genau EIN strukturelles
Feld: `vol.Required(CONF_SITE, default=site): _site_selector()` mit
`_site_selector() = selector.ObjectSelector(ObjectSelectorConfig())` — in der UI
ein freier YAML/JSON-Editor über das komplette Site-Objekt (Ebenen, Horizonte,
Gruppen). Für die Betreiber-Anlage sind das **~330 Zeilen YAML** (verifiziert am
Kampagnen-YAML: 333 Zeilen, 8 Ebenen mit je 13–23 Horizontzeilen).

**Der Default ist die falsche Anlage.** `_current_values` fällt ohne bestehende
Config auf `copy.deepcopy(DEFAULT_SITE)` zurück. `DEFAULT_SITE` (`const.py`)
enthält:

- lat/lon Landshut (unkritisch — die sichtbaren lat/lon-Felder defaulten aus
  `hass.config` und werden in `_structural_data` **in** das Site-Dict gemerged;
  der Koordinator liest nur die site-eingebetteten Koordinaten);
- 8 Ebenen M1–M8 (az 25/115/205, tilt 70/80, 370–430 Wp) + 4 WR-Gruppen à 800 W;
- je Ebene den `_FARFIELD_SLOPE` (Ost-Hang el 13–16°, τ 0) — die Geländelinie
  DIESES Standorts;
- auf M4/M8 den saisonalen Screen az 135–175 (el 40/30, τ 0,45/0,8) aus
  `_south_horizon()` — durch die Bootstrap-Shademap **widerlegt**: der reale
  Verschatter wirkt primär auf M2/M3 (Nahfeld-Objekt), M4/M8 zeigten im
  konfigurierten Keil τ 1,00–1,10; die Live-Config des Betreibers trägt längst
  modulindividuelle Fenster (`05-anlage-und-betrieb-runbook.md` §3d);
- die Hauswand ab az 212 — live auf az 195 korrigiert, im Default veraltet;
- **acht `actual_entity`-Werte** (`sensor.inverter_port_1_dc_power` …
  `_dc_power_4`) — die DTU-Sensoren des Betreibers.

**Konsequenz für Fremdnutzer (der eigentliche Skandal):** Ein konfigurierter
Messkanal ganz ohne brauchbare LTS-Zeile verwirft in `_actuals` den **gesamten
Tag für alle Lerner** („Messkanal-Dropout ⇒ ganzen Tag verwerfen", Runbook
§5.5). Acht tote Default-Entities heißen: **kein einziger Trainingstag, je** —
Day-ahead-Bias, Shademap, Quantile und Scoreboard bleiben dauerhaft
`cold_start`, ohne dass irgendetwas als Fehler sichtbar würde. Dazu prognostiziert
die falsche Geometrie (fremde Fassaden, fremder Hang, widerlegter Screen)
systematisch daneben — und weil die Lerner verhungern, korrigiert es auch nichts.

**Kein geführter Weg, Validierung erst am Ende.** Es gibt keinen Schritt „Ebene
anlegen", keine Sensor-Zuordnung per `EntitySelector` (Entity-IDs werden als
rohe Strings ins YAML getippt), keinen Horizont-Editor. `validate_site`
(`_site_validation.py`) läuft erst beim Speichern und liefert den **ersten**
Fehler als einen Übersetzungscode am Feld `site` (`bad_tau_points`,
`tau_points_above_edge`, …) — bei 330 Zeilen ohne Zeilenangabe.

**Änderungen sind teuer und orchestrierungsbedürftig.** Jede prognoserelevante
Site-Änderung verschiebt die RAW-Kurve: der Config-Fingerprint
(`coordinator._config_fingerprint`) re-seedet automatisch die
Day-ahead-Bias-Zellen (Kovarianz auf, n gedeckelt) und setzt ein Repair-Issue
(`ISSUE_CONFIG_CHANGED_BIAS_RESEED`); Shademap und Quantile bleiben semantisch
veraltet, bis der Betreiber `run_bootstrap` (`dry_run` default `true`) ausführt;
die Übergangswoche (3–7 Tage Überschießen) muss man kennen, um nicht
zurückzurollen. Diese Kette steht im Runbook — für den Betreiber. Ein
allgemeiner Nutzer kennt weder Reihenfolge noch Erwartung. Zusätzlich: der
Diagnostics-Download redigiert lat/lon **auch innerhalb** des Site-Objekts und
taugt darum nicht als Backup; das einzige saubere Backup ist Copy-Paste aus dem
Reconfigure-Formular.

**Was schon trägt (wiederverwendbar):** getrennter Reconfigure-Flow
(strukturell → `entry.data`, HA-Quality-Scale-Muster) vs. Options-Flow (nur
Runtime-Tunables); `validate_site` als HA-freie, einzeln testbare
Validierungsgrenze mit kanonischer Normalisierung (stabile Azimut-Sortierung);
Übersetzungen de/en inkl. aller Fehlercodes; Repair-Issues; `run_bootstrap`
in-process mit Lock/Nightly-Docking und Dry-Run-Default; `rollback_learners`
(Ring 10); `suggest_shade_groups` (Complete-Linkage über Shademap-Bins);
`get_shade_profile` + Shade-Profil-Karte (Sonnenbahn vs. gelerntes τ vs.
statische Horizontlinie); `dump_shademap` (Polar-Tabelle je Kanal);
Only-when-set-Serialisierung als etabliertes Kompatibilitätsmuster.

### 1.2 Zielgruppe und Zielbild

Balkonkraftwerk-Betreiber: typisch **1–4 Module, 1 Wechselrichter, 1–2
Fassadenrichtungen**, HA-affin, aber ohne PV-Fachwissen — „Azimut" und „Neigung"
sind vermittelbar, „Horizontzeile mit τ-Elevationsprofil" nicht. Die
8-Ebenen-Anlage des Betreibers ist das obere Ende, nicht der Auslegungsfall.
Messsensorik: oft vorhanden (OpenDTU/AhoyDTU/Hoymiles-Integrationen liefern
per-Port-DC), aber nicht immer; ohne `actual_entity` muss die Integration als
ehrliche reine Physik-Prognose laufen (tut sie: Lerner bleiben `cold_start`,
Kurve wird serviert).

Zielbild Onboarding: **In < 10 Minuten von „Integration hinzufügen" zu einer
laufenden, ehrlichen Prognose** — Standort aus `hass.config`, Ebenen mit vier
Zahlen + einem Sensor-Picker, offener Himmel als Start, Verschattung optional
sofort (Wand/Preset) oder später (lernen, Vorschlag bestätigen). Und: **jede
spätere Änderung erklärt selbst, was mit den gelernten Daten passiert.**

### 1.3 Randbedingungen für jede Lösung

- **RAW-Kurve ist Lern-Wahrheit; Lerner sind geclamped.** θ ∈ [0,5; 1,5],
  Shademap-τ ∈ [0; 1,1], Intraday ∈ [0,25; 2,5]. Je schlechter der statische
  Prior, desto mehr Hub verbrauchen die Lerner — Clamp-Sättigung war der
  Kernbefund der Juli-Forensik („Geometrie gehört in die Config, nicht in den
  Lerner"; falsche statische Zeilen dominieren jahrelang, ein Shademap-Bin
  bekommt live nur ~2–3 akzeptierte Samples/Jahr).
- **Ein Datenmodell.** `SiteConfig.from_dict`/`to_dict` mit
  Only-when-set-Serialisierung ist der einzige Vertrag; Engine, Backfill,
  Fingerprint, Diagnostics hängen daran. Onboarding-Optionen dürfen nur
  *Erzeugungswege* dieses Dicts sein.
- **Abhängigkeitsfrei.** `manifest.json` hat `requirements: []`; HTTP geht über
  die integrationseigene aiohttp-Session (wie der Open-Meteo-Fetcher). Keine
  neuen Python-Dependencies für Onboarding-Features.
- **HA-Qualitätsstandards.** Übersetzbarkeit (de/en, `data_description`),
  Reconfigure-Flow, Repair-Issues statt Log-Prosa, kein blockierendes I/O im
  Event-Loop (Flow-Steps dürfen awaiten; lange Läufe wie Bootstrap gehören in
  Executor + Progress), `unique_id`-Disziplin. Für wiederholbare Objekte
  (Ebenen) existiert seit HA 2025.3 der **Config-Subentry**-Mechanismus.
- **Azimut-Konvention.** Intern durchgängig 0 = Nord im Uhrzeigersinn; PVGIS
  und Open-Meteo-GTI nutzen 0 = Süd — jede externe Quelle braucht die Rotation
  an der Importgrenze (im Kern existiert bewusst keine Umrechnung).

---

## 2. Optionen

### 2.1 Option O1 — Geführter mehrstufiger Config-Flow

**Design.** Der User-Step wird ein Menü (`async_step_menu`): *„Geführt
einrichten"* / *„Vorlage verwenden"* (O2) / *„Experte: YAML"* (heutiger
ObjectSelector, bleibt vollwertig erhalten). Der geführte Pfad:

1. **Basis:** Name, Standort (Default `hass.config`), optional Gesamt-AC-Zähler
   (`EntitySelector`, wie heute).
2. **Ebenen als Config-Subentries** (HA ≥ 2025.3): je Ebene ein kurzes Formular —
   Azimut (NumberSelector 0–360 mit Kompass-Hilfetext), Neigung (Slider 0–90,
   Default 70 „Balkongeländer steil"), Wp, optional DC-Leistungssensor
   (`EntitySelector` domain `sensor`, device_class `power`), optional
   Shade-Group. Subentries geben „Ebene hinzufügen/bearbeiten/löschen" nach der
   Ersteinrichtung gratis in der Integrations-Kachel.
3. **Wechselrichter:** ein Formular je Gruppe (Name, Mitglieds-Ebenen als
   Multi-Select aus den angelegten Ebenen, AC-Limit Default 800 W, Ableitung
   „alle Ebenen an einem WR" als Ein-Klick-Vorschlag für den 1-WR-Normalfall).
4. **Horizont separat und optional:** pro *Fassade* (= Gruppe gleicher
   Azimut±10°), nicht pro Ebene: Auswahl „offener Himmel" (Default, O3) /
   „Hauswand links/rechts ab Azimut X" (erzeugt die zwei Wandzeilen `el 90, τ 0`
   inkl. az-360-Wrap-Terminator — die dokumentierte Falle einer einzelnen
   Wandzeile wird damit strukturell unmöglich) / „Preset" (O2) / „PVGIS" (O5) /
   „YAML". Der Wizard **kopiert** die Fassaden-Zeilen in jede Mitglieds-Ebene —
   das kanonische Dict bleibt flach; keine Schema-Indirektion (dieselbe
   Begründung, mit der ADR-0022 H-B verworfen hat).

**Wiederholung erträglich machen:** (a) Zielgruppe hat 1–4 Ebenen — der 8×-Fall
ist der Betreiber, der YAML ohnehin beherrscht; (b) „Ebene duplizieren": das
Formular der nächsten Ebene startet mit den Werten der letzten (nur Name/Sensor
ändern — deckt „zwei gleiche Module nebeneinander", den häufigsten Fall);
(c) Fassaden-Horizont statt 8× Horizont-Edit; (d) Experten-YAML bleibt für
Masse-Edits.

**Validierung:** feldnah pro Step (Selector-Ranges decken `bad_azimuth`,
`bad_tilt`, `bad_wp` ab), `validate_site` bleibt die finale, einzige
Wahrheitsgrenze vor `async_create_entry` — der Wizard erzeugt, die Validierung
entscheidet. Fehlercodes existieren bereits übersetzt.

| Kriterium | Bewertung |
|---|---|
| Nutzwert Zielgruppe | **Hoch** — der Unterschied zwischen „installierbar" und „veröffentlichbar" |
| Datenmodell-Risiko | Gering, WENN strikt nur Dict-Erzeugung; Subentry↔`entry.data`-Sync ist der heikelste Teil (Subentries müssen beim Speichern ins eine `site`-Dict zurückgeschrieben werden, sonst zwei Wahrheiten) |
| Aufwand | **L** (~5–8 PT): Menü + 3–4 Steps + Subentry-Flow + Übersetzungen ×2 + Flow-Tests; kein Engine-/Store-/Validierungs-Umbau |
| Risiken | Subentry-Mechanik ist jung (API-Drift zwischen HA-Versionen); Fingerprint-Flip pro Ebenen-Edit (jeder Subentry-Save = Reseed + Repair-Issue — braucht O6-Orchestrierung, sonst Issue-Spam); Übersetzungspflege |
| Alternative im Kleinen | Ohne Subentries: wiederholter `async_step_plane` mit „weitere Ebene? ja/nein" — Aufwand M, aber kein nachträgliches Ebenen-Management in der UI |

### 2.2 Option O2 — Import/Export und Presets

**Design.**

- **Presets** als mitgelieferte Daten (JSON im Integrationspaket, Namen
  übersetzt): *„Balkon, eine Fassade"* (1–2 Ebenen, 1 WR 800 W, offener Himmel),
  *„Zwei Fassaden über Eck"* (2×1–2 Ebenen, 90° versetzt), *„Reihenhaus mit
  Hauswand"* (eine Fassade + Wandsektor-Fragment). Presets enthalten **keine**
  Koordinaten (kommen aus `hass.config`), **keine** Entity-IDs (Platzhalter →
  Wizard/YAML) und **keinen** Fernhorizont. Der Betreiber-Standort wird ein
  benanntes Preset *„Referenzanlage (Entwicklung)"* oder wandert ganz in
  `tests/fixtures` — er verschwindet jedenfalls als stiller Default.
- **Import:** Menüpunkt im User-/Reconfigure-Step „Site-YAML/JSON einfügen"
  (der heutige ObjectSelector, nur ehrlich benannt) plus optional Datei-Pfad
  (Muster von `import_bootstrap`: `payload` ODER `path` in HA-erlaubtem
  Verzeichnis). Jeder Import läuft durch `validate_site` — unverändert.
- **Export:** neuer Service `export_site` (`SupportsResponse.ONLY`): liefert das
  kanonische `site.to_dict()` **ohne Redaktion** (im Gegensatz zu Diagnostics,
  die lat/lon auch im Site-Objekt redigieren und darum als Backup ausfallen).
  Damit werden Backup vor Kampagnen, Community-Sharing („zeig mal deine Site")
  und Bug-Reports trivial. Horizont-Fragmente (nur die Zeilen einer Fassade)
  als dokumentiertes Teil-Format für den Austausch.

| Kriterium | Bewertung |
|---|---|
| Nutzwert | Hoch pro Aufwandseinheit; Presets beantworten „wie sieht eine korrekte Site überhaupt aus" besser als jede Doku |
| Kompatibilität | Voll — Presets sind gewöhnliche Site-Dicts; Export ist read-only |
| Aufwand | **S–M** (~1–2 PT): Preset-Daten + Menü-Step + Service + Tests |
| Risiken | Preset-Pflegepflicht bei Schema-Erweiterungen (Test: jedes Preset muss `validate_site` passieren — trivial automatisierbar); Datei-Import braucht Pfad-Whitelist wie `import_bootstrap` |

### 2.3 Option O3 — Start ohne Horizont: Verschattung ausschließlich lernen

**Was die Shademap wirklich kann (code-verifiziert):** Sie lernt **absolute**
beam-referenzierte Transmittanz je (5°-az × 2,5°-el × Halbjahr)-Bin, Clamp
[0; 1,1], EMA α 0,15, Shrinkage `w = n/(n+20)` gegen den statischen Prior. Mit
offenem Prior (τ = 1) ist **Abdunkeln bis τ = 0 voll ausdrückbar** — die
richtige Richtung. Ein zu dunkler Prior kann dagegen nur bis 1,1 aufgehellt
werden: Der ausgelieferte falsche Screen ist darum schlimmer als gar kein
Horizont. Zusätzlich entlastet die Schichtung: θ trainiert seit 0.21 gegen
`slow_only` (Shademap ∘ Physik), lernt eine gelernte Verschattung also nicht
doppelt.

**Wie lange dauert es:**

- *Ohne Historie:* Ein Bin braucht n = 20 für w = 0,5; akzeptiert werden nur
  quasi-klare Stunden (k_c-Band, Beam-Anteil > 5 % Wp, Nachbar-Stabilität), und
  eine gegebene Sonnenposition wird nur in einem schmalen Saisonfenster
  durchlaufen — praktisch **~2–3 akzeptierte Samples pro Bin und Jahr**
  (Runbook §4.4). Ein spezifisches Verschattungsobjekt wird ohne Bootstrap erst
  über **Monate bis Jahreszeiten** hinweg nennenswert abgebildet (n = 3 ⇒
  w ≈ 13 %).
- *Mit Historie:* Wer die Modul-DC-Sensoren schon vor der Installation im
  Recorder hatte, bekommt per `run_bootstrap` (bis 400 Tage LTS) eine
  Tag-1-Shademap — bewusst schwach gewichtet (`BOOTSTRAP_MAX_BIN_N` = 5 ⇒
  w ≈ 0,2), aber flächig. Das ist der realistische Königsweg für die Zielgruppe
  (DTU-Sensoren existieren oft länger als die Integration).

**Was strukturell offen bleibt (die ehrlichen Grenzen):**

1. **Der Diffus-Pfad lernt nie.** Shademap ersetzt nur Beam-τ; SVF/`diffuse_tau`
   sind rein statisch. Eine nicht konfigurierte Hauswand überschätzt den
   Diffus-Sockel dauerhaft (SVF 1 statt real ~0,3–0,6) — kein Lerner korrigiert
   das je gezielt, nur der grobe Tagesbias.
2. **Das Tages-Gate sperrt stark verschattete Sites aus.**
   `SHADEMAP_MEASURED_CLEAR_MIN_FRAC` = 0,8 vergleicht die gemessene
   Site-Energie mit der modellierten **RAW**-Tagessumme. Mit offenem Prior auf
   einer Site, die real > ~20 % Tagesenergie durch Verschattung verliert, fällt
   jeder klare Tag unter das Gate — **der Lerner, der die Verschattung lernen
   soll, wird von genau dieser Verschattung vom Training ausgesperrt.**
   (Design-Folge-Punkt, nicht Teil dieses ADRs: Gate auf `slow_only` statt raw
   beziehen oder pro Kanal statt pro Site prüfen.)
3. **θ-Clamp 0,5:** liegt die reale Tagesenergie unter 50 % der Open-Sky-RAW,
   sättigt der Tagesbias — wieder Juli-Muster, nur mit umgekehrtem Vorzeichen.
4. **RAW bleibt falsch.** Servierte Kurve und Bänder werden gut (corrected),
   aber `raw_hourly_wh`, Scoreboard-Referenz und jeder künftige Physik-Fit
   arbeiten gegen einen wissentlich falschen Prior. Das Projektprinzip bleibt:
   Geometrie gehört am Ende in die Config — O3 ist Startzustand, kein Endzustand
   (die Ernte übernimmt O4).

**Verdikt:** Als **Default-Haltung des Onboardings richtig** (offener Himmel +
ehrlicher Hinweis „erste Wochen ohne Verschattungsmodell"), aber zwingend
kombiniert mit der billigen Wand-Abfrage (O1 Schritt 4) und den Grenzen 1–3 in
der Doku. Aufwand **S** (es ist vor allem das Weglassen des falschen Defaults
plus Doku), Risiko: Erwartungsmanagement.

### 2.4 Option O4 — Datengetriebene Vorschläge: Horizont aus Messdaten

**Wiederverwendbar (existiert):** `run_bootstrap` (in-process LTS-Rekonstruktion
+ Previous-Runs-Wetter), die Shademap selbst als az×el×Halbjahr-Polartabelle
(`dump_shademap`), `suggest_shade_groups` als Muster für „Analyse als Service
mit Antwort-Objekt" (Complete-Linkage, `SHADE_SIM_MAX_MEAN_DIFF` 0,06,
`SHADE_SIM_MIN_COMMON_BINS` 30), `get_shade_profile` + Karte als fertiges
Anzeige-Vehikel (Sonnenbahn, gelerntes τ, statische Horizontlinie überlagert).
Der Machbarkeitsbeweis ist erbracht: **genau diese Pipeline hat die
Screen-Fehlzuordnung des Betreibers aufgedeckt** (Bootstrap-Shademap ab Jul
2025 → M2/M3 statt M4/M8).

**Design `suggest_horizon`** (neuer Service, `SupportsResponse.ONLY`, Kern
HA-frei neben `suggest_shade_groups`):

1. Voraussetzung: gefüllte Shademap (live oder via `run_bootstrap`).
2. Je Kanal und az-Spalte (5°): niedrigstes el-Bin, ab dem gepooltes τ ≥ ~0,85
   und n ausreichend → **Kanten-Vorschlag** `elevation_deg`; Median-τ unterhalb
   → **τ-Vorschlag**; signifikante Differenz der Halbjahre → `seasonal`-Flag;
   deutlicher τ(el)-Gradient unter der Kante → Hinweis „Kandidat für
   `tau_points`" (kein Auto-Fit — ADR-0022 hat die automatische
   tau_points-Ableitung im Core bewusst zum Nicht-Ziel erklärt).
3. Antwort: paste-fertige Horizontzeilen je Ebene **plus Evidenz**: n je Bin,
   Zahl und Daten der beitragenden quasi-klaren Tage, Konfidenz je Sektor,
   explizite `insufficient`-Sektoren. Anzeige als Overlay in der
   Shade-Profil-Karte (Vorschlag vs. aktuell), Übernahme via Reconfigure —
   später optional als Ein-Klick-Apply mit O6-Orchestrierung.

**Grenzen (aus den Methodik-Lehren, hart einzuhalten):** Bin-Abwesenheit ≠
freie Sicht (Gates, Stunden-Aliasing und ein dunkler Alt-Prior löschen genau
die gesuchten Bins — der Vorschlag darf aus fehlenden Bins nie „offen"
schließen, nur „unbekannt"); Bootstrap-Bins sind stundengeschmiert (die weiche
Baumkante wurde dadurch als harte el-10-Kante aliast — Kanten-Vorschläge aus
Bootstrap-Daten sind ±2–3° unscharf); der härteste Test bleiben **Rohkurven
klarer Referenztage** — die Vorschlags-UX soll die 3–5 besten Referenztage
benennen, gegen die der Nutzer per Power-History-Karte gegenprüfen kann.
Diffus-Seite (`diffuse_tau`) ist aus Shademap-Daten nicht ableitbar
(beam-referenziert); höchstens Heuristik „el-90-Zeile vorhanden ⇒ schlage
`diffuse_tau 0.5` als Startwert vor" mit Overcast-Tag-Verweis.

| Kriterium | Bewertung |
|---|---|
| Nutzwert | Der Lebenszyklus-Schlussstein: O3 lernt, O4 erntet in die Config — löst „RAW bleibt falsch" strukturell |
| Kompatibilität | Voll additiv (read-only Service + Karte) |
| Aufwand | **M** Service/Kern (~2–3 PT, viel Wiederverwendung) + **M** Bestätigungs-UX/Karte (~2 PT) |
| Risiken | Vertrauensrisiko bei Überversprechen — deshalb Evidenzpflicht und `insufficient`-Ehrlichkeit; Kanten-Bias durch Stunden-Aliasing dokumentieren |

### 2.5 Option O5 — Externe Horizont-Quellen (PVGIS, Geländemodelle, Apps)

**PVGIS `printhorizon`:** EU-JRC-API, kostenlos, kein Key, JSON; liefert den
**Geländehorizont** (SRTM-basiert, ~90-m-Raster). Machbar über die vorhandene
aiohttp-Session (keine neue Dependency, `requirements: []` bleibt), als
**einmaliger** Fetch in einem optionalen Wizard-Schritt; das Ergebnis wird zu
gewöhnlichen Horizontzeilen (τ 0) konvertiert und normal persistiert — **keine
Laufzeit-Abhängigkeit**, kein Offline-Problem (Schritt ist überspringbar,
Fehler degradieren zu „offener Himmel"). Zwei Pflicht-Konvertierungen an der
Importgrenze: **Azimut-Rotation 0=S → 0=N** (SPEC §20.1; im Kern existiert
bewusst kein Remap) und Ausdünnen auf ≤ ~24 Stützstellen mit
Terminator-Disziplin (geschlossenes 360°-Profil).

**Genauigkeits-Ehrlichkeit:** SRTM sieht Gelände ab ~Kilometer-Skala
zuverlässig; der 80–250-m-Hang des Betreibers ist grenzwertig, **Bäume und
Hauswände — die dominante Balkon-Verschattung — sieht es nie.** Der Schritt
muss so gelabelt sein: „Fernhorizont (Berge/Gelände). Nahes — Bäume, Wände,
Nachbargebäude — ergänzt du per Wand-Abfrage/Preset oder lässt es lernen."
Lizenz: PVGIS-Daten sind frei nutzbar mit Quellenangabe (Hinweis in Doku und
Attribut). Kompass-/AR-Apps (PeakFinder u. ä.): nicht automatisierbar; die
Integration bietet dafür nur das dokumentierte Paste-Format `[[az, el], …]`
(O2-Import) plus eine Doku-Seite „Horizont mit dem Handy messen".

| Kriterium | Bewertung |
|---|---|
| Nutzwert | Mittel — relevant für Tal-/Hanglagen; für die typische Innenstadt-Zielgruppe klein (dort dominiert Nahfeld) |
| Kompatibilität | Voll (erzeugt normale Zeilen); Fingerprint ändert sich wie bei jedem Horizont-Edit |
| Aufwand | **S–M** (~1–2 PT): Fetch + Konvertierung + Step + Tests (Konvertierung pur testbar) |
| Risiken | API-Verfügbarkeit beim Setup (degradieren, nie blocken); Konventionsfehler (durch pure-Function-Tests mit Goldwerten absichern); Overtrust („PVGIS hat doch gesagt…") — Labeling |

### 2.6 Option O6 — Konfigurations-Migration, Versionierung, Lern-UX

**Was existiert:** Fingerprint → automatischer Bias-Reseed + Repair-Issue;
`run_bootstrap` (Dry-Run-Default, Lock, Summary); `rollback_learners` (Ring 10,
Bias+Shademap+Quantile konsistent); Entry `VERSION 1 / MINOR_VERSION 1` als
Migrationsanker; Only-when-set-Serialisierung als Kompatibilitätsregel
(0.22-Felder bewiesen: Alt-Configs byte-identisch, Fingerprint stabil).

**Was fehlt für die Veröffentlichung (Design):**

1. **Post-Save-Orchestrierung.** Nach einem strukturellen Save mit
   Fingerprint-Flip führt das bestehende Repair-Issue heute nur zu Prosa. Neu:
   ein **Repair-Flow** (fixable issue) mit der Kernfrage in Nutzersprache —
   *„Deine Standort-Änderung verschiebt die Prognosebasis. Was soll mit dem
   Gelernten passieren?"* — Optionen: **(a)** „Neu ausrichten (empfohlen)" →
   `run_bootstrap` dry-run, Summary anzeigen, bestätigen → Import; **(b)**
   „Einfach weiterlernen" (Reseed reicht, 3–7 Tage Einschwingen wird angesagt);
   **(c)** „Änderung zurücknehmen" → Site-Snapshot zurückspielen (s. 2.).
   Wichtig: Die Übergangswochen-Erwartung („Überschießen ist Einschwingen,
   nicht Regression — nicht zurückrollen") wird GENAU HIER kommuniziert, nicht
   nur im Runbook.
2. **Site-Snapshot-Ring.** Bei jedem strukturellen Save legt der Coordinator
   `{timestamp, fingerprint, site_dict}` in einen kleinen Store-Ring (z. B. 5).
   Damit: Ein-Klick-Config-Rollback (heute: „hoffentlich hast du das YAML
   vorher rauskopiert"), Diff-Anzeige „was hat sich geändert" im Repair-Flow,
   und `export_site` (O2) bekommt einen `snapshot`-Parameter. Bewusst getrennt
   vom Lern-Rollback-Ring — Config- und Lern-Rollback bleiben einzeln
   auslösbar, der Repair-Flow koppelt sie bei Bedarf (Config zurück ⇒
   Fingerprint flippt erneut ⇒ ordentlicher Reseed statt stiller Drift).
3. **Schema-Versionierung: additiv bleiben.** Die Regel der letzten Releases
   wird Policy: neue Site-Felder sind optional + only-when-set + Default =
   Altverhalten + Fingerprint nur-wenn-gesetzt; `async_migrate_entry` (Entry
   VERSION/MINOR) ist die Reserve für den Fall, der sich additiv nicht
   ausdrücken lässt. Ein explizites `site_schema_version`-Feld wird **nicht**
   eingeführt (das wäre eine zweite Wahrheit neben dem Entry-Versionspaar).
4. **Fingerprint-Hygiene** (Begleitfix): Gruppen-**Umbenennung** flippt heute
   den Fingerprint und re-seedet, obwohl die Kurve identisch bleibt
   (dokumentierte Falle in `03-…` §10) — vor der Veröffentlichung den Hash auf
   kurvenrelevante Gruppenfelder (`ac_limit_w`, η) beschränken, sonst erzeugt
   der Ebenen-/Gruppen-Editor aus O1 Reseed-Spam.

| Kriterium | Bewertung |
|---|---|
| Nutzwert | Hoch — ohne O6 wird jede Onboarding-Verbesserung von der ersten Änderungs-Erfahrung entwertet („warum ist die Prognose seit meinem Edit schlechter?") |
| Kompatibilität | Voll additiv (Ring neu im Store, Repair-Flow neu; bestehende Pfade unverändert) |
| Aufwand | **M** (~2–3 PT): Repair-Flow + Ring + Diff + Fingerprint-Fix + Tests |
| Risiken | Repair-Flow-UX in zwei Sprachen; Bootstrap-Dauer (Minuten) braucht Progress-Kommunikation; Ring-Größe vs. eMMC-Budget (5 × ~15 kB — unkritisch) |

---

## 3. Entscheidungsempfehlung: gestufter Plan

Die Optionen konkurrieren nicht, sie schichten. Empfohlene Stufen:

### Stufe 0+1 — MVP (Mindestvoraussetzung für JEDE Veröffentlichung)

| # | Maßnahme | Aus | Aufwand |
|---|---|---|---|
| M0 | **Neutraler Default statt `DEFAULT_SITE`**: 1 Ebene (az 180, tilt 70, 800 Wp, offener Himmel, keine Entity), Betreiber-Site → Preset „Referenzanlage"/Fixture. Beseitigt widerlegte Geometrie UND die Lern-Totalblockade durch tote Entity-IDs | O2/O3 | **S** |
| M1 | Preset-Menü im User-Step + `export_site`-Service (unredigiert) + dokumentiertes Import-Format | O2 | **S–M** |
| M2 | Open-Sky-Default mit Wand-Kurzabfrage (ein Formular: „Hauswand? ab welchem Azimut?" ⇒ generierte Wandzeilen inkl. Wrap-Terminator) + ehrliche Doku der Lerngrenzen (Diffus lernt nie; Tages-Gate; Zeitachsen) | O3 | **S** |
| M3 | Post-Save-Repair-Flow (Reseed erklären, `run_bootstrap` anbieten, Übergangswoche ansagen) + Fingerprint-Hygiene (Gruppen-Rename) | O6 | **M** |

MVP gesamt: **M** (~4–6 PT). Ergebnis: Ein Fremdnutzer kann die Integration
installieren, bekommt eine ehrliche Prognose, zerstört nichts durch Änderungen —
auch wenn die Ersteinrichtung noch YAML-nah ist.

### Stufe 2 — v1 (erste breite Version)

| # | Maßnahme | Aus | Aufwand |
|---|---|---|---|
| V1 | Geführter Flow: Menü, Ebenen-Subentries mit EntitySelector, WR-Schritt, Fassaden-Horizont-Schritt | O1 | **L** |
| V2 | PVGIS-Fernhorizont als optionaler Schritt (Rotation 0=S→0=N, Ausdünnung, Labeling) | O5 | **S–M** |
| V3 | Site-Snapshot-Ring + Ein-Klick-Config-Rollback + Diff im Repair-Flow | O6 | **M** |

### Stufe 3 — Ausbaustufen (nach Feld-Feedback)

| # | Maßnahme | Aus | Aufwand |
|---|---|---|---|
| A1 | `suggest_horizon` (Vorschlag + Evidenz + Karten-Overlay, Übernahme via Reconfigure) | O4 | **M** |
| A2 | Ein-Klick-Apply des Vorschlags mit O6-Orchestrierung | O4/O6 | **M** |
| A3 | Shademap-Tages-Gate auf `slow_only`/per-Kanal umstellen (Voraussetzung für O3 auf stark verschatteten Sites — eigener kleiner ADR/Design-Punkt) | O3-Folge | **S–M** |

**Begründung der Schnitte:** M0 ist der einzige echte Blocker — auslieferbar
falsche Geometrie plus garantiert verhungernde Lerner sind keine
Qualitätsfrage, sondern ein Defekt. M1–M3 machen den Ist-Mechanismus (YAML +
Reconfigure + Bootstrap) für Dritte *benutzbar*, ohne UI-Großbaustelle. O1 ist
der teuerste Posten und darf deshalb hinter den MVP fallen: Ein YAML-Editor mit
guten Presets und funktionierender Änderungs-Orchestrierung ist veröffentlichbar;
ein hübscher Wizard über einem falschen Default wäre es nicht gewesen. O4 ist
strategisch der wertvollste Baustein (er schließt den Kreis Prior ↔ Lerner),
braucht aber gefüllte Shademaps aus dem Feld — also erst nach v1 sinnvoll.

---

## 4. Konsequenzen

1. **Leichter wird:** Neuinstallation für Fremde (Minuten statt
   YAML-Archäologie); Backup/Sharing/Support („schick mir deinen
   `export_site`-Dump"); Änderungen ohne Wissensbasis-Studium (Repair-Flow
   erklärt Reseed/Bootstrap/Übergangswoche); langfristig die Konvergenz
   Prior ↔ Realität (O3-lernen → O4-ernten).
2. **Schwerer wird:** Pflege — jede Schema-Erweiterung berührt künftig Presets,
   Wizard-Steps, zwei Übersetzungen und die Doku (Gegenmittel: Preset-Tests
   gegen `validate_site`, ein gemeinsamer Erzeugungspfad); Subentry-Sync ist
   dauerhafte Komplexität im Flow-Code.
3. **Bewusste Nicht-Ziele:** kein grafischer Horizont-Editor (Zeichnen von
   az/el-Linien — Karten-Overlay aus O4 ist die billigere 80-%-Lösung); keine
   automatische `tau_points`-/`diffuse_tau`-Ableitung im Core (ADR-0022-Linie);
   kein zweites Konfigurations-Schema; keine neuen Python-Dependencies; kein
   Auto-Apply von Vorschlägen ohne Bestätigung.
4. **Risiko benannt:** Der Open-Sky-Start verschiebt die Fehlerlage von
   „systematisch falsch modelliert" (heute, Fremdnutzer mit DEFAULT_SITE) zu
   „anfangs zu optimistisch, dann lernend" — das ist die richtige Richtung,
   aber die ersten Wochen brauchen Erwartungsmanagement (Onboarding-Text,
   `cold_start`-Status ist bereits ehrlich).
5. **Betreiber-Instanz unberührt:** Alle Maßnahmen sind additiv; die
   bestehende Live-Config (LIVE/KAMPAGNE-Zustände) round-trippt byte-identisch
   weiter; der Fingerprint der Bestandsanlage darf durch M0 (reiner
   const-Default-Tausch) **nicht** flippen — `DEFAULT_SITE` wird bei
   bestehenden Entries nie mehr gelesen (nur `_current_values`-Fallback ohne
   existing).

---

## 5. Migrations- und Testplan

### 5.1 Migration

1. **M0 ist migrationsfrei für Bestandsnutzer:** `DEFAULT_SITE` wird nur beim
   allerersten Rendern eines neuen Entries gelesen; bestehende Entries tragen
   ihre Site in `entry.data`. Kein Store-Touch, kein Fingerprint-Flip.
2. **Presets/Wizard erzeugen kanonische Dicts** → `validate_site` →
   `_structural_data` (lat/lon-Merge bleibt der einzige Pfad). Keine
   Entry-VERSION-Erhöhung nötig.
3. **O6-Ring** ist ein neuer Store-Key (additiv, Migrationsmuster v2→v3:
   fehlender Key ⇒ leerer Ring).
4. **Fingerprint-Hygiene (Gruppen-Rename)** ändert den Hash-Aufbau ⇒ einmaliger
   Reseed bei allen Bestandsinstallationen beim Update — akzeptabel (Reseed ist
   die sanfte Variante), im Release-Text ankündigen; alternativ Hash-Segment
   nur bei künftigen Saves umstellen (mehr Code, wenig Nutzen — nicht
   empfohlen).
5. **Rollout-Reihenfolge** je Release wie gehabt: Code → (optional)
   Config-Edit → automatischer Reseed → angebotener Re-Bootstrap →
   Übergangswoche.

### 5.2 Testplan

Unit (pytest, HA-frei wo möglich):

- Jedes Preset und der neue Minimal-Default passieren `validate_site`;
  Round-Trip `from_dict∘to_dict` byte-identisch (bestehendes Muster).
- Wand-Generator: erzeugte Zeilen enthalten die Wrap-Terminatoren; Property:
  eine generierte Ein-Wand-Site hat SVF < 1 nur im Wandsektor (gegen die
  dokumentierte Einzel-Wandzeilen-Falle).
- PVGIS-Konvertierung: Goldwerte für Rotation 0=S→0=N (Nord-/Süd-/Wrap-Fälle),
  Ausdünnung erhält Kantengeometrie ±1°; Fetch-Fehler ⇒ leerer Horizont, kein
  Abbruch.
- `export_site`: unredigierte Koordinaten, kanonische Form; Snapshot-Parameter.
- `suggest_horizon` (Stufe 3): synthetische Shademap (bekannte Wand + Baum) ⇒
  Kanten-/τ-Vorschlag trifft ±1 Bin; `insufficient`-Sektor bei n unter
  Schwelle; NIE „offen" aus leeren Bins.
- Fingerprint: Gruppen-Rename flippt nicht mehr; `ac_limit_w`-Änderung weiter
  schon (Regressionspaar).

Flow-Tests (HA-Testharness):

- User-Step-Menü: alle drei Pfade erzeugen valide Entries; Fehler-Re-Render
  behält Eingaben (bestehende `_current_values`-Semantik).
- Subentry-Lifecycle: Ebene anlegen/ändern/löschen ⇒ `entry.data['site']`
  konsistent, genau ein Reload, genau ein Fingerprint-Ereignis.
- Repair-Flow: Fingerprint-Flip ⇒ Issue mit drei Optionen; Pfad (a) ruft
  `run_bootstrap`-Dry-Run und zeigt Summary; Pfad (c) stellt Snapshot wieder
  her und erzeugt den Folge-Reseed.

Live-Validierung (1–2 Wochen, zwei Profile):

- *Frische Test-Instanz* (1 Ebene, offener Himmel, echter DC-Sensor):
  Prognose läuft ab Tag 1, Status ehrlich `cold_start`, nach der ersten klaren
  Woche erste Shademap-Bins; kein Repair-Spam.
- *Betreiber-Instanz:* Update ohne Fingerprint-Flip, Bestands-YAML unverändert,
  `export_site` == Reconfigure-Inhalt.

---

## 6. Aufwandsschätzung (Zusammenfassung)

| Stufe | Inhalt | Aufwand |
|---|---|---|
| MVP (M0–M3) | Default-Entkopplung, Presets + Export, Open-Sky + Wand-Abfrage, Repair-Orchestrierung | **M** (~4–6 PT) |
| v1 (V1–V3) | Geführter Flow/Subentries, PVGIS, Site-Snapshots | **L** (~8–12 PT) |
| Ausbau (A1–A3) | suggest_horizon + Apply, Gate-Umbau | **M–L** (~4–7 PT) |

Größte Einzelrisiken: Subentry↔site-Dict-Sync (V1), Übersetzungs-/Doku-Breite,
Erwartungsmanagement der Open-Sky-Anfangsphase.

---

## 7. Anhang: Betroffene Dateien (Konzept-Ebene)

| Datei | MVP | v1 | Ausbau |
|---|---|---|---|
| `custom_components/balcony_solar_forecast/const.py` | Minimal-Default; `DEFAULT_SITE` → Preset-Datenmodul | — | — |
| `config_flow.py` | Menü-Step, Preset-Auswahl, Wand-Kurzabfrage | Subentry-Flows, WR-/Fassaden-Steps, PVGIS-Step | Apply-Pfad für Vorschläge |
| `_site_validation.py` | unverändert (bleibt einzige Wahrheitsgrenze) | unverändert | unverändert |
| `_services.py` | `export_site` | — | `suggest_horizon` |
| `coordinator.py` | Repair-Flow-Anbindung, Fingerprint-Hygiene | Site-Snapshot-Ring | — |
| `store.py` | — | Snapshot-Ring-Key (additiv) | — |
| neu: `presets.py` / `pvgis_import.py` (HA-frei) | Presets | PVGIS-Fetch+Konvertierung | Vorschlags-Kern |
| `translations/{de,en}.json` | neue Steps/Repair-Texte | dito | dito |
| `docs/SPEC.md`, `docs/project-knowledge/05-…` | Onboarding-Abschnitt, Lerngrenzen O3 | Wizard-Doku | Vorschlags-Doku |

*Nicht* betroffen: `core/engine.py`, `core/horizon.py`, `core/types.py`
(Schema unverändert), Sensor-Contracts, Lern-Store-Schema (außer additivem
Ring), `scripts/backfill.py`.
