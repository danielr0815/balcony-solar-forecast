# Projekt-Instruktionen & Index

**Stand: `main` @ v0.23.0 (2026-07-25).** Diese Datei ist der Einstieg in die
Wissensbasis zum Repository **`danielr0815/balcony-solar-forecast`**
(<https://github.com/danielr0815/balcony-solar-forecast>). Sie erklärt, was das
Projekt ist, welches der sieben Wissensdokumente wofür zuständig ist, über welchen
Pfad du in eine konkrete Aufgabe einsteigst — und sie enthält einen kopierfertigen
Block mit den Projekt-Instruktionen für claude.ai.

---

## 1. Was ist balcony-solar-forecast

Eine eigenständige **Home-Assistant-Custom-Integration** (HACS-installierbar,
Domain `balcony_solar_forecast`), die eine **15-Minuten-PV-Prognose für
Balkonkraftwerke mit mehreren Fassaden-Ebenen** rechnet und sich anhand der
gemessenen Ist-Werte selbst korrigiert.

Der Ansatz unterscheidet sie von den üblichen Prognose-Integrationen: Statt die
Ausgabe eines fremden Prognosedienstes nachzukorrigieren, holt sie in **einem**
Open-Meteo-Call nur die **Rohstrahlungskomponenten** (GHI/DNI/DHI), transponiert
sie **lokal** auf jede Modulebene (Hay-Davies, eigenes Horizont- und
Sky-View-Modell) und **lernt dort, wo die Information liegt**: pro Messkanal
(ein MPPT-Port = ein Modul = ein DC-Leistungssensor) und pro Sonnenstand.
Für den Referenzstandort heißt das: 8 Module, 3 Fassadenrichtungen, sehr steile
Neigungen (70°/80°), starke Verschattung — eine Geometrie, für die generische
Dienste strukturell falsch liegen.

Technische Eckdaten (alle am Code geprüft):

| Punkt | Wert |
|---|---|
| Aktueller Stand | `main` @ **v0.23.0**, `manifest.json` / `pyproject.toml` / `const.INTEGRATION_VERSION` synchron |
| Laufzeit-Abhängigkeiten | **keine** (`manifest.json` → `requirements: []`) — reine stdlib, kein numpy/pandas/pvlib |
| Architekturgrenze | `custom_components/balcony_solar_forecast/core/` importiert **nichts** aus `homeassistant`; genau eine dokumentierte Netz-Ausnahme (`core/openmeteo_backfill.py`, lazy `aiohttp`, injizierte Session) |
| Vertrag | `docs/SPEC.md` (deutsch, Ist-Stand), Herleitung/Historie in `docs/HISTORIE.md`, Designentscheide in `docs/adr/`, Release-Chronik in `CHANGELOG.md` |
| Plattformen in HA | `sensor`, `binary_sensor`, `select`, `date` — plus 10 Actions, Diagnostics, Energy-Dashboard-Hook, zwei mitgelieferte Lovelace-Karten |
| Lernschichten | Intraday-Skalar (transient), Day-Ahead-RLS-Bias, Shademap (geometrisch), Quantil-Bänder, Inverter-η — dazu Scoreboard und Drift-Monitor als Wächter |

**Wofür diese Wissensbasis da ist:** Sie soll einen Assistenten (oder einen
Menschen) ohne Vorwissen und ohne Chatverlauf in die Lage versetzen, am Projekt
zu arbeiten: Code ändern, ein Prognoseproblem diagnostizieren, die Anlage
umkonfigurieren, ein Release bauen. Sie ist **destillierte Analyse**, kein Ersatz
für den Code: Jede Verhaltensaussage ist gegen `main` @ v0.23.0 geprüft, aber
Code altert schneller als Prosa. Im Zweifel gilt der Code.

---

## 2. Index der Wissensdokumente

Alle Dateien liegen in `docs/project-knowledge/`.

### `01-architektur-und-datenfluss.md`
Modul-Landkarte (jede Datei mit Verantwortung und Einstiegsfunktion), die
Kernbegriffe **RAW / CORRECTED / SERVED**, **issued**, **slot vs. hour**,
**DC vs. AC**, die zwei DC-Clamps, die Felder des `ForecastResult`, der
15-Minuten-Live-Loop, der Nightly-Job, der Bootstrap-Pfad und das Store-Layout.
*Brauchst du:* zuerst, wenn du im Repo etwas suchst oder wissen musst, wo ein
Sensorwert herkommt.

### `02-physik-und-horizontmodell.md`
Die HA-freie Prognosephysik exakt wie implementiert: Sonnenstand (NOAA/Meeus),
Haurwitz-Clear-Sky, Hay-Davies-Transposition mit IAM und bifazialem Beam-Gain,
Ross-Zelltemperatur/DC-Modell, der zweistufige AC-Clamp und — am ausführlichsten
— das Horizontmodell inkl. der 0.22-Felder `tau_points` und `diffuse_tau`,
Sky-View-Faktor, Validierungsregeln und die ehrlich benannten Modellgrenzen.
*Brauchst du:* wenn du Prognosewerte nachrechnest, eine Horizontzeile schreibst
oder entscheiden musst, ob eine Abweichung Physik, Lerner oder Config ist.

### `03-lernschichten-und-korrekturen.md`
Was jede adaptive Schicht lernt, **wogegen** sie trainiert, wo sie auf die
servierte Kurve wirkt, welche Gates/Clamps/Konstanten sie begrenzen und wie man
sie zurücksetzt. Dazu die Wächter (Drift-Monitor, Collapse-Detektor,
Rollback-Ring, Scoreboard), der Config-Fingerprint und die
Doppelkorrektur-Fallen.
*Brauchst du:* wenn die Prognose systematisch danebenliegt oder du eine
Config-Änderung planst, die die RAW-Kurve verschiebt.

### `04-ha-integration-entities-services.md`
Die Außenschnittstelle: alle Entities mit Key **und** Entity-ID, die
Kurven-Attribute und der Recorder-Ausschluss, alle 10 Actions mit Feldern und
Antwortstruktur, der Diagnostics-Dump blockweise, sowie Config-Flow:
Setup vs. Reconfigure vs. Options.
*Brauchst du:* beim Anbinden von Automationen/Karten, beim Aufrufen einer Action
und beim Lesen eines Bugreport-Dumps.

### `05-anlage-und-betrieb-runbook.md`
Die reale Anlage des Betreibers (Module, Azimute, Wechselrichter, Mess-Entitäten,
Horizont-Realität) und die Handgriffe: Update, Reconfigure, Re-Bootstrap,
Validierungslauf `scripts/validation/validate.py` (Checks C1–C8), Rollback — plus
ein Diagnose-Kochbuch für die fünf häufigsten Symptome.
*Brauchst du:* wenn du am laufenden System etwas änderst oder ein beobachtetes
Verhalten einordnen musst.

### `06-forensik-juli-2026-und-offene-punkte.md`
Die Wissens-Essenz der 7-Tage-Forensik (17.–24.07.2026): der Actuals-Epoch-Bug,
die bestätigte Ursachenkette mit Zahlen, die gemessene Wirkung nach dem
Deployment, die offenen Punkte O1–O9 mit Status und die Methodik-Lehren.
*Brauchst du:* wenn du eine Regression einordnest, einen offenen Punkt
weiterführst oder eine neue Analyse aufsetzt, ohne die Herleitung zu wiederholen.

### `07-entwicklung-tests-release.md`
Das Handwerk: Repo-Layout, die HA-Freiheits-Regel für `core/` samt Prüfkommandos,
Testsuite und Kommandos (`-p no:homeassistant`), Test-Konventionen (neue Tests
müssen den alten Code durchfallen lassen), die Contracts (SPEC, Store-Schema,
optionale Config-Felder, Fingerprint), der exakte Release-Prozess und die
praktischen Fallen (Windows/PowerShell, Floats, DC/AC in Diagnostics).
*Brauchst du:* bevor du die erste Zeile änderst, und noch einmal bevor du taggst.

---

## 3. Einstiegs-Pfade

| Ich will … | Lies zuerst | Dann |
|---|---|---|
| **Code ändern** | `07-entwicklung-tests-release.md` (Regeln, Tests, Contracts) | `01-…` für die Modul-Landkarte; das Fachdokument des betroffenen Bereichs (`02`/`03`/`04`); `docs/SPEC.md` für den Vertrag |
| **Ein Prognoseproblem diagnostizieren** | `05-anlage-und-betrieb-runbook.md` §5 (Diagnose-Kochbuch) | `03-…` (welche Schicht kann das verursachen), `02-…` (ist schon RAW schief?), `04-…` (welches Attribut/welche Action zeigt es), `06-…` (ist das ein bekannter offener Punkt) |
| **Die Anlage umkonfigurieren** | `05-…` §4.2–§4.4 (Reconfigure, Re-Bootstrap, Übergangsphase) | `02-…` §6/§8 (Horizontzeilen korrekt schreiben und validieren), `03-…` §10/§12 (Fingerprint, Reihenfolge Physik → Reset → Bootstrap) |
| **Ein Release bauen** | `07-…` §6 (Versionsnummer an drei Stellen, Reihenfolge, CI-Guards) | `CHANGELOG.md` + `docs/SPEC.md` (Versionsstempel + betroffene Abschnitte), `06-…` §4 für den Status offener Punkte |
| **Die Physik verstehen** | `02-physik-und-horizontmodell.md` komplett | `01-…` §3 (Begriffe RAW/CORRECTED/DC/AC), `docs/adr/ADR-0022-*` für die Herleitung des Horizont-/Diffus-Modells |
| **Den Stand der offenen Punkte kennen** | `06-…` §4 (Tabelle O1–O9) | `05-…` §6 (bewusst nicht modelliert), `CHANGELOG.md` für das, was seither gelandet ist |

**Wenn du nur fünf Minuten hast:** `01-…` §3 (Begriffe) + `03-…` §1 (Tabelle der
Lernschichten) + `05-…` §1 (die reale Anlage) reichen, um 80 % der Fragen
richtig einzuordnen.

---

## 4. Projekt-Instruktionen (in claude.ai einfügen)

Der folgende Block ist als **Projekt-Instruktion** gedacht und in sich
verständlich — er funktioniert auch ohne die übrigen Dateien, verweist aber auf
sie.

```text
ROLLE UND KONTEXT

Du arbeitest am Repository danielr0815/balcony-solar-forecast: eine
Home-Assistant-Custom-Integration (Domain balcony_solar_forecast, aktuell
v0.23.0), die eine 15-Minuten-PV-Prognose für ein Balkonkraftwerk mit mehreren
Fassaden-Ebenen rechnet und sich anhand gemessener Ist-Werte selbst korrigiert.
Sie holt bei Open-Meteo nur die Rohstrahlung (GHI/DNI/DHI), transponiert lokal
auf jede Modulebene (Hay-Davies + eigenes Horizont-/Sky-View-Modell) und lernt
pro Messkanal (ein MPPT-Port = ein Modul = ein DC-Leistungssensor) und pro
Sonnenstand. Reine Standardbibliothek, keine Laufzeit-Abhängigkeiten
(manifest.json: requirements: []), kein numpy/pandas/pvlib.

Der Nutzer ist der Betreiber und Autor. Antworte auf Deutsch, präzise und dicht,
ohne Marketing. Detailwissen steht in docs/project-knowledge/ (01 Architektur,
02 Physik/Horizont, 03 Lernschichten, 04 HA-Entities/Services, 05 Anlage/Runbook,
06 Forensik/offene Punkte, 07 Entwicklung/Tests/Release).

ARBEITSREGELN (hart)

1. WAHRHEITSQUELLE IST DER CODE. Aussagen über Verhalten, Konstanten,
   Feldnamen, Services oder Sensoren gehören am aktuellen Repo-Stand geprüft.
   Analyse-Artefakte, ältere Chats und Notizen sind Hinweise, keine Belege.
2. KEINE ZEILENNUMMERN als Beleg — nenne Datei plus Funktions- oder
   Konstantenname. Zeilennummern veralten sofort.
3. core/ IST HA-FREI. custom_components/balcony_solar_forecast/core/ importiert
   nichts aus homeassistant, und const.py muss ebenfalls HA-frei bleiben (core
   importiert daraus). Einzige dokumentierte Ausnahme: core/openmeteo_backfill.py
   fasst Netzwerk an — mit lazy aiohttp-Import und injizierter Session, weiterhin
   ohne HA-Import. Neue Mathematik gehört in core/, neuer HA-Glue eine Ebene
   darüber.
4. docs/SPEC.md IST DER VERTRAG (deutsch) und eine reine IST-Spezifikation:
   sie beschreibt nur das Verhalten der aktuellen Version, ihr Kopf nennt
   "Gilt für Version: <X>". Jede Verhaltensänderung wird im selben PR in der
   SPEC nachgezogen, neues Verhalten thematisch einsortiert. Historie und
   Herleitung stehen in docs/HISTORIE.md (nicht normativ, inkl.
   Übergangstabelle alt→neu). Verschiebst du Abschnitte, korrigiere die
   "SPEC §…"-Zitate im Code — sie sind tragend.
5. DIE VERSION STEHT AN DREI STELLEN und muss synchron sein:
   custom_components/balcony_solar_forecast/manifest.json (version),
   pyproject.toml ([project] version),
   custom_components/balcony_solar_forecast/const.py (INTEGRATION_VERSION).
   Der Release-Guard vergleicht zusätzlich den Git-Tag; HACS liefert den Zipball
   des Tags aus, ein Bump nach dem Tag wirkt also nicht.
6. TESTS LAUFEN MIT -p no:homeassistant:
   uv run pytest tests -p no:homeassistant
   (Setup: `uv sync --group dev`; vor dem Dev-Setup-2026 vom 2026-07-30 —
   siehe Update-Kasten in 07 — hieß das `.\.venv\Scripts\python.exe -m pytest …`.)
   Das pytest-Plugin von pytest-homeassistant-custom-component zieht POSIX-only
   fcntl (auf Windows nicht importierbar) und autouse-Fixtures, die unter
   Python >= 3.12 werfen; kein Test benutzt es. pyproject.toml enthält bereits
   addopts = "-q" — hänge auf der Kommandozeile KEIN weiteres -q an, sonst
   verschluckt pytest die Ergebniszeile. Lint ist "ruff check ."; "ruff format"
   ist verboten (der Code ist absichtlich handformatiert, E501 ist aus).
7. NEUE TESTS MÜSSEN DEN ALTEN CODE DURCHFALLEN LASSEN. Beweise das: Worktree
   auf den Parent-Commit legen, nur die neuen Testdateien hineinkopieren, Suite
   laufen lassen — sie müssen fehlschlagen (ein semantischer Fehlschlag zählt
   mehr als ein TypeError auf eine neue Signatur). Für verhaltensneutrale
   Refactorings gilt umgekehrt: Bit-Identität wird bewiesen (eingefrorene Kopie
   der alten Funktion im Testmodul, Vergleich mit == über viele geseedete
   Zufallseingaben), nicht behauptet.
8. ABWÄRTSKOMPATIBILITÄT OPTIONALER CONFIG-FELDER. Neue optionale Felder werden
   in to_dict() NUR SERIALISIERT, WENN GESETZT — eine Alt-Config muss nach dem
   Upgrade byte-identisch dasselbe Dict ergeben (kein neuer Schlüssel, kein
   null). Sonst kippt der Config-Fingerprint und setzt ohne fachlichen Grund
   Lernzustand zurück. Spiegelbildlich im Fingerprint: nur-wenn-gesetzt anhängen,
   Werte runden, Sentinels kollisionsfrei wählen. Ein Feld, das die RAW-Kurve
   verändert, MUSS in den Fingerprint. Store-Migrationen sind additiv: der
   äußere Store-Envelope bleibt für immer Version 1, migriert wird die innere
   schema_version, alte Schlüssel gehen byte-treu durch. Eine Migration, die
   Lernzustand verwirft, ist ein kritischer Fehler.
9. RAW IST DIE LERN-WAHRHEIT. Jede Lernschicht trainiert gegen genau die Kurve,
   auf die sie angewandt wird: Shademap gegen die ungegatete, unclamped
   Physik-Referenz; Day-Ahead-Bias gegen slow_only (Shademap ohne Bias);
   Intraday-Skalar gegen raw x theta; Quantile gegen die issued-corrected Kurve.
   Wer diese Schichtung bricht, baut eine Doppelkorrektur — historisch die
   häufigste Fehlerklasse dieses Projekts, sichtbar als Übertreibung am Morgen.
   Und: ein besserer statischer Prior schlägt immer einen kompensierenden
   Lerner, denn alle Lerner sind geclamped und sättigen am Rand.
10. DC/AC-BASIS IMMER EXPLIZIT MACHEN. Gemessen ist nur die DC-Leistung je Port;
    DC ist die Lern- und Scoreboard-Wahrheit, AC ist der operatorseitige
    Standard der Haupt-Sensoren. Zwischen beiden liegen rund 8 % — jeder
    Vergleich ohne genannte Basis produziert ein Einheiten-Artefakt. Die
    ac_power- und *_dc_total_energy-Felder der Wechselrichter sind
    Firmware-Ableitungen aus DC und tragen KEINE unabhängige Information.
11. AZIMUT-KONVENTION: 0 = NORD, IM UHRZEIGERSINN (90 = Ost, 180 = Süd,
    270 = West) — durchgängig für Sonnenazimut, Ebenen und Horizontzeilen. Es
    gibt im Kern keine Umrechnung; Fremdquellen mit 0 = Süd (Open-Meteo-GTI,
    PVGIS) müssen von Hand gedreht werden.
12. KEINE ERFINDUNGEN. Lieber "nicht modelliert / offen / unsicher" schreiben
    als plausibel klingen. Unsicheres explizit als unsicher kennzeichnen.

TYPISCHE FALLSTRICKE

- energy_today_kwh ist absichtlich eine stabile Day-Ahead-Erwartung: der
  transiente Intraday-Faktor wird für die Headline herausdividiert, bleibt aber
  in der ausgelieferten Kurve. Am laufenden Tag gilt deshalb gewollt
  energy_today_kwh != Summe der heutigen wh_period; für morgen/übermorgen
  stimmen sie überein.
- Entity-Key != Entity-ID: HA bildet die ID aus dem englischen Anzeigenamen
  (translations/en.json). Stabil ist die unique_id ({entry_id}_{key}),
  autoritativ die Entity-Registry.
- Strukturelle Einstellungen (Site-Objekt, Geometrie, Horizonte, Albedo,
  Beam-Gain, AC-Zähler, Intervalle) gehören in Setup/Reconfigure nach
  entry.data. In den Options stehen nur Laufzeit-Schalter und die
  Vergleichsliste — jeder Leser merged {**data, **options}, ein strukturelles
  Feld in den Options verschattet entry.data für immer.
- Der Config-Fingerprint kippt schon bei einer reinen Umbenennung einer Ebene
  oder Wechselrichter-Gruppe und löst dann einen Bias-Reseed samt Repair-Issue
  aus, obwohl sich die modellierte Kurve nicht ändert.
- Recorder-Epoch: die In-Process-Statistik-API liefert Sekunden, die
  WebSocket-API Millisekunden. Diese Verwechslung hat monatelang jedes Lernen
  still lahmgelegt.
- Kalt-Start ist kein Defekt: learner_status "cold_start", neutrale Bänder
  (p10 == p50 == p90) und ein gelerntes tau exakt
  gleich dem statischen Prior sind korrekte Zustände. Sie als Bug zu lesen führt
  zu Resets, die den Kalt-Start nur verlängern.
- Bin-Abwesenheit heißt nicht "freie Sicht": eine fehlende Shademap-Zelle heißt
  zuerst "kein zulässiges Sample". Härtester Test sind 5-Minuten-Rohkurven
  klarer Referenztage, nicht Rückrechnungen durch das eigene Modell.
- Slot-Werte sind Intervallmittel über 15 Minuten, die Sonnenposition wird am
  Slot-Mittelpunkt ausgewertet. Stundenschlüssel sind ISO-UTC, Tagesschlüssel
  lokale Kalendertage.
- Beim Erzeugen von Config-YAML/JSON Floats NIE runden (Pythons float-repr ist
  round-trip-exakt) — sonst verschiebst du still Geometrie und kippst den
  Fingerprint.
- Windows/PowerShell: kein && / ||-Verketten (A; if ($?) { B }), Kommandos
  immer als `uv run …` aufrufen (uv gehört seit 2026-07-30 zum Setup, siehe
  Update-Kasten in 07).
- Nach einer prognoserelevanten Config-Änderung sind 3-7 Tage Einschwingen
  normal (die Bias-Zellen lernen gegen die verschobene RAW-Kurve zurück). Das
  ist keine Regression und kein Grund zurückzurollen. Reihenfolge immer:
  Physik korrigieren, dann Reset/Reseed, dann Re-Bootstrap, dann eine Woche
  messen — und nur eine Stellschraube pro Woche.

LIVE-ZUSTÄNDE VERIFIZIEREN

Alles, was den aktuellen Zustand der laufenden Anlage betrifft — gelernte
theta-Werte, Shademap-Bins, Quantil-Füllstände, Scoreboard, die
tatsächlich konfigurierte Site-Geometrie, Albedo, Beam-Gain, ross_coeff, aktive
Kill-Switches, Degradationsstatus — ist NICHT aus dem Repo und nicht aus dieser
Wissensbasis belegbar. Verbindlich ist der Diagnostics-Download des
Config-Entries (Einstellungen > Geräte & Dienste > Integration > Diagnose
herunterladen); ergänzend die Actions dump_shademap, get_issued_forecast und
get_forecast sowie die Attribute des Day-Ahead-Bias-Sensors. Wenn du eine
Aussage über den Live-Zustand triffst, nenne die Quelle und ihr Datum — und
behandle jede Zahl aus einer älteren Analyse als Momentaufnahme, nicht als
heutigen Zustand.
```

---

## 5. Aktualität & Pflege

**Stand dieser Wissensbasis:** `main` @ **v0.23.0**, erstellt am **2026-07-25**.
Alle Code-Aussagen sind gegen diesen Stand geprüft; Betriebs- und Messzahlen
stammen aus Live-Abzügen vom 16.–25.07.2026 und sind **Momentaufnahmen**.

### Was schnell veraltet

| Kategorie | Warum | Woran prüfen |
|---|---|---|
| **Live-Zustände** (θ-Werte, Shademap-Bins, Quantil-Füllstände, Scoreboard, aktive Schalter, Degradationsstatus) | ändern sich täglich durch den Nightly-Job | Diagnostics-Dump; Actions `dump_shademap`, `get_issued_forecast` |
| **Die real konfigurierte Site** (Horizontzeilen, `tau_points`, `diffuse_tau`, Albedo, Beam-Gain, `ross_coeff`) | Betreiber-Edits laufen am Repo vorbei; `const.DEFAULT_SITE` ist nur die Auslieferungs-Referenz | Reconfigure-Formular bzw. Diagnostics (Koordinaten dort redigiert) |
| **Offene Punkte O1–O9** (`06-…` §4) | genau die Liste, die abgearbeitet werden soll | `CHANGELOG.md`, `git log`, `docs/orders/` |
| **Versionsnummern und Zählwerte** (v0.23.0, Testanzahl 2480, Konstantenwerte) | jeder Release verschiebt sie | die drei Versionsdateien; `pytest --collect-only` |
| **Entity-IDs** | abgeleitet aus `translations/en.json`, durch Umbenennung oder Kollision verschiebbar | Entity-Registry, `_dashboard.collect_entity_map` |

Stabil bleiben dagegen: die Architekturgrenze `core/` ↔ HA-Glue, die Begriffe
(RAW/CORRECTED/SERVED, DC/AC, slot/hour/issued), die Azimut-Konvention, die
Schichtung „jede Lernschicht trainiert gegen die Kurve, auf die sie wirkt", die
Contracts (SPEC, Store-Envelope 1, nur-wenn-gesetzt-Serialisierung) und die
Methodik-Lehren aus `06-…` §5.

### Wie man die Basis aktualisiert

1. **Anlass:** nach jedem Release, nach jeder Config-Kampagne des Betreibers und
   nach jeder größeren Analyse.
2. **Reihenfolge:** erst das betroffene Fachdokument (01–07), dann diese Datei
   (Stand-Datum in Kopfzeile, Index-Zeilen, Versionsangaben in §1 und im
   Instruktionsblock).
3. **Regeln beim Schreiben:** Belege als *Datei + Funktions-/Konstantenname*,
   niemals Zeilennummern; jede Verhaltensaussage vorher am Code prüfen; Unsicheres
   ausdrücklich markieren; keine Tokens/Zugangsdaten (HA-URL, Entity-IDs und
   Koordinaten der eigenen Anlage sind zulässig); Nachbardokumente nicht
   duplizieren, sondern per Dateiname verweisen.
4. **Nach dem Edit prüfen:** Stimmen die Versionsangaben in allen Dokumenten
   überein? Sind Konstantenwerte, die an mehreren Stellen stehen (z. B. Clamps,
   Ringtiefen, Gates), noch identisch? Widerspricht eine Statusaussage („erledigt"
   / „offen") einer anderen Datei?
5. **Bekannte Mehrfachabdeckung** (bewusst, weil unterschiedliche Perspektiven,
   aber beim Ändern **gemeinsam** anzufassen): der Config-Fingerprint steht in
   `02-…` §8, `03-…` §10, `05-…` §4.3 und `07-…` §5.4; der zweistufige DC-Clamp in
   `01-…` §3.5 und `02-…` §5.1; die Validierungs-Checks C1–C8 in `05-…` §4.5
   (PASS-Kriterien) und `06-…` §4.7 (Vor-Fix-Werte); der Intraday-Re-Arm in
   `03-…` §5, `05-…` §5.3 und `06-…` §2.7.

---

## 6. Kurz-Glossar (Details in den Fachdokumenten)

| Begriff | Bedeutung in diesem Projekt |
|---|---|
| **RAW** | reine Physik-Kurve, Lerner aus (statischer Horizont-τ). Referenz für jede Fehlerdiagnose |
| **CORRECTED** | Kurve mit Lernschichten; ohne Hooks bit-identisch zu RAW |
| **SERVED** | was tatsächlich ausgeliefert wird — DC-seitig CORRECTED nach dem 2. Clamp, operatorseitig die AC-Kurve |
| **slow_only** | Shademap ∘ Physik, ohne Day-Ahead-Bias — die Trainingsreferenz des Bias |
| **issued** | die eingefrorene Prognose *wie an jenem Tag ausgegeben* (90-Tage-Ring); Grundlage jeder leckfreien Bewertung |
| **Ebene / Plane** | ein Modul an einem MPPT-Port = ein Messkanal (`actual_entity`) |
| **Gruppe / InverterGroup** | ein Wechselrichter mit AC-Limit über seine Port-Ebenen |
| **k_c** | Clear-Sky-Index (GHI / Haurwitz-Referenz) — nur Lern-Gate und Normierung, nie Prognosequelle |
| **θ (theta)** | multiplikativer Day-Ahead-Biasfaktor einer RLS-Zelle (Wolkenklasse × Tagesteil) |
| **Shademap** | gelernte Beam-Transmittanz je Messkanal × (Sonnenazimut × Elevation × Halbjahr) |
| **SVF** | Sky-View-Faktor: relative Reduktion des isotropen Diffus durch den Horizont |
| **Kill-Gate** | *(entfernt in v0.25.0)* Urteil des Scoreboards gegen Vergleichsprognosen — war rein informativ, schaltete nichts ab |
| **Fingerprint** | Hash über alle kurvenformenden Config-Felder; Änderung ⇒ Re-Seed der Bias-Zellen |
