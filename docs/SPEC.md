# Spezifikation: Balcony Solar Forecast — Mehrebenen-PV-Prognose mit Selbstlernen

> **Gilt für Version: 0.23.3** · Zuletzt aktualisiert: 2026-07-30
>
> Diese Spezifikation beschreibt **ausschließlich den Ist-Stand dieser Version**:
> was die Integration `balcony_solar_forecast` heute tut und tun muss. Sie
> enthält keine Historie, keine Entscheidungsprotokolle, keinen Lieferplan und
> keine Herkunftsvermerke. Die Herleitung — Gründungskontext, Messdatenanalysen,
> Entscheidungspunkte, Phasenplan, abgelöste Regeln und die Zuordnung der alten
> Abschnittsnummern auf diese Gliederung — liegt in `docs/HISTORIE.md`
> (nicht normativ). Veröffentlichte Änderungen je Release stehen in
> `CHANGELOG.md`, architektonische Einzelentscheidungen unter `docs/adr/`.

## §1 Vertrag, Geltung und Änderungsregeln

### §1.1 Was dieses Dokument ist

`docs/SPEC.md` ist der **Vertrag** dieses Projekts: es beschreibt normativ, was
die Integration tut — Physik, Lernschichten, Gates, Schnittstellen, Persistenz.
Es ist eine **Ist-Spezifikation**: jeder Satz darin behauptet, dass sich die
ausgelieferte Software heute so verhält. Überholtes Verhalten wird **ersetzt**,
nicht markiert.

Der **Versionsstempel** im Kopf nennt die Version, für die dieses Dokument
zuletzt durchgesehen wurde. Er ist maschinell gegen `const.INTEGRATION_VERSION`
(gleichlautend `manifest.json`) geprüft (§1.4): eine Release-Version, die den
Stempel nicht mitzieht, lässt die Testsuite rot laufen. Der Stempel ist damit
keine Kosmetik, sondern die Zusage „hier hat jemand hingesehen".

**Wahrheitsquelle bleibt der Code.** Widerspricht die SPEC dem implementierten
Verhalten, ist genau eines von beiden ein Bug, und der Widerspruch wird
aufgelöst statt stehengelassen.

**Zitierform.** Code, Tests, Dashboards und Doku verweisen als `SPEC §x.y` auf
die Abschnitte. Diese Verweise sind tragend: sie sind der Weg von einer Zeile
Physik zu dem Absatz, der sie begründet. Jede Zitierung wird maschinell gegen
die Überschriften dieses Dokuments geprüft (§1.4).

### §1.2 Wegweiser: Thema → maßgeblicher Abschnitt

| Thema | Maßgeblich | Ergänzend |
|---|---|---|
| Vertrag, Änderungsregeln, Wächter | **§1** | §21 |
| Architektur, Modulschnitt, Coordinator-Takte, Generik | **§2** | §7 (Konfiguration), §16 (Store) |
| Wetterbezug (Open-Meteo, Fetch-Kadenz, Cache, Budget) | **§3** | §11.3 (Ensemble-API), §13 |
| Physik: Sonnenstand, Clear-Sky, Transposition, IAM, Bifazial | **§4** | §5 (Horizont), §20.1 (Azimut) |
| Horizont, Verschattungsgeometrie, `tau_points`, `diffuse_tau`, SVF | **§5** | §7.4 (Feldtabelle), §9.1 (gelernte Karte) |
| Elektrik: Zelltemperatur, η, DC→AC-Kette, AC-Clamps, Re-Clamp | **§6** | §14.1 (AC-Sensorik), §9 |
| Konfiguration: `site`-Schema, Validierung, Fingerprint, Default | **§7** | §5.1 (Horizont-Semantik), §20 (Konventionen) |
| Wetterklassifikation und Zeitbinnung (gemeinsame Taxonomie) | **§8** | §9.5, §11.1, §15.2 |
| Lernschichten 1–4, Gates, Drift, Rollback | **§9** | §8 (Bins), §6.4 (Re-Clamp), §7.7 (Fingerprint) |
| Lern-Sichtbarkeit: Messkanäle, Verwurfssträhne, Repair-Issues | **§10** | §9.8 (die Gates selbst), §14.6 (Dump), §16.1 |
| Unsicherheit: Quantilbänder und Ensemble-Hüllkurve | **§11** | §8 (Bins), §12.6 (Seeding) |
| Bootstrap und Re-Bootstrap aus Historie | **§12** | §7.8 (Default-Site), §9.7 (Zeitstempel) |
| Degradationsleiter | **§13** | §14 (Sensorik), §3 (Last-Good-Cache) |
| Konsumenten-Schnittstellen, Entitäten, Attribute, Diagnostics | **§14** | §6.5 (DC/AC), §11.2 (Bänder) |
| Metriken, Skill-Scoreboard, Kill-Gate | **§15** | §8 (Strata), §16.2 (Issued-Ring) |
| Persistenz: Store-Schema, Ringe, Schreibsemantik | **§16** | §9.8 (Rollback-Ring), §12.5 (Import) |
| Verschattungsprofil-Diagramm | **§17** | §9.1/§9.2 (Datenquelle), §18.3 (Karte) |
| Dashboard und mitgelieferte Lovelace-Karten | **§18** | §14 (Entitäten), §17 (Diagramm) |
| Aktionen (Services): Inventar und Grenzen | **§19** | je definierender Fachabschnitt |
| Konventionen (Azimut, Neigung), Inbetriebnahme-Checkliste | **§20** | §7 (Schema) |
| Qualitätssicherung, Referenzvektoren, maschinelle Wächter | **§21** | §1.4 |

### §1.3 Änderungsregeln (verbindlich)

1. **Thematisch einsortieren, nicht chronologisch anhängen.** Neues Verhalten
   kommt als Unterabschnitt an das **Ende des thematisch zuständigen §** oder,
   wenn es kein passendes Thema gibt, als **neuer Top-Level-§ mit thematischem
   Titel**. Ein Unterpunkt unter einer fremden Versionsüberschrift ist verboten.
2. **Gleicher PR.** Jede Verhaltensänderung zieht die SPEC im **selben** PR nach;
   eine Konstante wird mit **Namen** genannt (nie mit Zeilennummer), damit die
   Aussage nicht mit dem nächsten Edit veraltet.
3. **Nummern sind ab dieser Fassung append-only.** Bestehende Abschnittsnummern
   werden nicht umnummeriert, nicht gelöscht und nicht neu belegt — der Code
   zitiert sie. Titel*text* darf präzisiert werden.
4. **Kein Versionsbezug im Text.** Ein „seit v0.x" gehört nicht in die
   Ist-Spec. Ausnahme sind **Kompatibilitätsregeln für Altzustände** (etwa:
   Legacy-Gruppenkanäle werden mitgelesen, ein Snapshot ohne Schattenkarten-
   Kurve fällt auf das gemeinsame Signal zurück) — die beschreiben laufendes
   Verhalten und sind normativ.
5. **Überholtes wird ersetzt, nicht markiert.** Was nicht mehr gilt, verlässt
   dieses Dokument. Ist die Herleitung erhaltenswert, wandert sie nach
   `docs/HISTORIE.md`; die dortige Übergangstabelle hält ältere Verweise aus
   Issues, PRs und `CHANGELOG.md` auflösbar.
6. **Versionsstempel mitziehen.** Ein Release, das den Vertrag berührt, aktualisiert
   den Kopfstempel auf die neue `INTEGRATION_VERSION`.

### §1.4 Maschinelle Wächter

`tests/test_spec_integrity.py` prüft diesen Vertrag mechanisch; die Guards sind
in §21 im Detail beschrieben. Kurz: jede `SPEC §x.y`-Zitierung löst auf, jede
Aktion aus `services.yaml`, jedes öffentliche `site`-Feld und jede `ISSUE_*`-Id
ist hier benannt, jeder Top-Level-Abschnitt steht im Wegweiser (§1.2), jeder
`docs/…`-Pfad zeigt auf eine getrackte Datei, der `async_setup`-Docstring nennt
Zahl und Namen aller Aktionen, der Versionsstempel stimmt mit
`INTEGRATION_VERSION` überein, und die SPEC führt keine als historisch
markierten Abschnitte mehr.

## §2 Systemüberblick: Paketierung, Modulschnitt, Takte

`balcony_solar_forecast` ist eine **eigenständige HA-Custom-Integration**
(HACS-Struktur). Konsumenten koppeln **ausschließlich über
Standard-HA-Schnittstellen** (§14) — Sensoren, Attribute, Service-with-Response,
Energy-Platform-Hook. Es gibt keine Code-Kopplung an ein Konsumentenprojekt.

**HA-freier Kern (harte Invariante).**
`custom_components/balcony_solar_forecast/core/` enthält ausschließlich reine
Funktionen über einfache Datentypen (`core/types.py`) und importiert **nichts**
aus Home Assistant. Er ist mit bare pytest testbar (`tests/core/`); der HA-Glue
(Coordinator, Config Flow, Plattformen, Services) liegt darüber. Diese Trennung
ist testbar formuliert und wird geprüft (§21).

**Laufzeit stdlib-only.** `requirements` bleibt **leer**: aiohttp kommt aus
HA-Core, alles Übrige ist stdlib (`math`). Kein numpy, kein pandas, kein pvlib
zur Laufzeit — musl- und update-sicher. pvlib/pandas erzeugen offline die
Referenzvektoren der Golden-Tests und sind **niemals** Laufzeitabhängigkeiten
(§21).

**Generik statt Hardcoding.** Ebenen (Azimut/Neigung/Wp/η), Horizonttabellen,
Wechselrichter-Gruppen, Ist-Mess-Entitäten und die Liste der Vergleichsprognosen
sind **Konfiguration** (§7). Nichts ist an einen Wechselrichter-Hersteller oder
einen Standort gebunden; die Vergleichsliste wird **leer** ausgeliefert (§15.3),
und der mitgelieferte `const.DEFAULT_SITE` ist ein Struktur-/Formatbeispiel
(§7.8), kein Pflichtverhalten.

**Marken-Icon lokal.** Die PNGs unter `custom_components/<domain>/brand/`
(`icon.png` / `icon@2x.png` / `logo.png` / `logo@2x.png`) werden vom lokalen
Brands-Proxy von HA ausgeliefert — **bewusst ohne** Einreichung ins
`home-assistant/brands`-Repo, sodass die Custom-Integration ihr Icon ohne
Upstream-PR mitbringt.

**Modulkarte.** `fetcher.py` (Wetterbezug, §3) → `core/solpos.py` (Sonnenstand,
§4.1) → `core/clearsky.py` (Haurwitz + k_c, §4.2) → `core/transpose.py`
(Hay-Davies, §4.3) → `core/horizon.py` (Horizont, SVF, §5) →
`core/shademap.py` (gelernte Verschattung, §9.1) → `core/electrical.py`
(Zelltemperatur, η, AC-Clamp, §6) → `core/bias.py` / `core/quantiles.py`
(Korrekturen und Bänder, §9/§11). `core/engine.py` orchestriert die Kette über
15-min-Slots × N Ebenen.

**Takte.** Ein `DataUpdateCoordinator` je Config-Entry: Wetter-Fetch alle
**30 min** (`FETCH_INTERVAL_SECONDS`), Neuberechnung alle **15 min**
(`RECOMPUTE_INTERVAL_SECONDS`), nächtliches Training gegen **~01:30 lokal** im
Executor (§9.7). Ein Rechenlauf bleibt unter **50 ms**. Beide Intervalle sind je
Entry konfigurierbar (§7.1).

**Eine Config-Entry je Standort**, eindeutig über den Anlagennamen. Mehrere
Anlagen sind zulässig; die Aktionen lösen ihr Ziel entsprechend auf (§19).

## §3 Wetterbezug: Open-Meteo-Fetch, Validierung und Cache

**Ein Call je Zyklus** gegen `OPEN_METEO_URL`:
`minutely_15=shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m`
plus `hourly=cloud_cover_low,cloud_cover_mid,cloud_cover_high,visibility,snowfall,snow_depth`,
`models=icon_seamless`, `forecast_days=4`. ICON-D2 liefert für Mitteleuropa
native 15-min-Werte; die stündlichen Felder tragen den Wolken-/Sicht-/Schnee-
Kontext für Klassifikation (§8) und Schnee-Albedo (§4.6).

**Warum vier Prognosetage für einen Drei-Tage-Horizont.** Der Fetch läuft mit
`timezone=UTC`, und `forecast_days` zählt **UTC-Tage ab dem aktuellen
UTC-Datum**. Am lokalen Abend (UTC+2) läge der lokale d2 sonst teilweise
jenseits eines Drei-UTC-Tage-Fensters — der Puffertag deckt den lokalen
Drei-Tage-Horizont immer ab.

**Schema-Validierung statt HTTP-Status.** `fetcher.validate_payload` prüft die
**Form** der Antwort, nicht nur den Statuscode. Insbesondere die
HTTP-200-mit-Nulls-Falle: Open-Meteo antwortet unter Umständen mit 200 und
durchgehend `null`-Werten; ein solcher Payload wird verworfen wie ein
Fehlschlag, damit die Degradationsleiter (§13) greift statt still eine
Nullkurve auszuliefern.

**Last-Good-Cache im Store.** Der zuletzt gültige Payload liegt im `Store`
(§16.2) und **übersteht einen Neustart**. Sein Alter wird ehrlich fortgeschrieben
(§13) und nie auf ~0 zurückgesetzt, auch nicht, wenn ein ärmerer neuer Payload
zugunsten des reicheren alten verworfen wird.

**Retry.** Begrenzte Versuche (`MAX_TRIES`) mit **Backoff und Jitter**. Ein
Serverhinweis `Retry-After` über `_RETRY_AFTER_MAX_INLINE_SECONDS` wird **nicht**
inline abgewartet: der Coordinator serviert weiter aus dem Last-Good-Cache und
versucht es in seiner eigenen Kadenz erneut, statt den Tick zu blockieren.

**Budget.** Das Open-Meteo-Free-Tier erlaubt 10 000 Calls/Tag; ein Call je
30 min sind ~48/Tag und lassen Raum für Ensemble (§11.3) und Bootstrap (§12).

**Warum keine serverseitige GTI.** Open-Meteos GTI-Parameter rechnet
**isotrop**, mit **fixem Albedo 0,20** und **einer Ebene je Call**. Auf den
steilen Balkonebenen ist das nachweislich zu niedrig, Schnee-Albedo ist nicht
abbildbar, und N Ebenen kosteten N Calls. Deshalb holt die Integration die
Rohkomponenten und transponiert lokal (§4.3).

## §4 Physikkern: Sonnenstand, Clear-Sky, Transposition

### §4.1 Sonnenstand

`core/solpos.py` rechnet den Sonnenstand in **geschlossener NOAA-Form** (stdlib
`math`). **Genauigkeitsziel < 0,3°** gegen PVGIS-verifizierte Referenzwerte;
die Prüfanker liegen in `tests/core/test_solpos.py` (§21). Unter dem Horizont
und nachts liefert die Funktion definierte Werte (negative Elevation), sodass
nachgelagerte Stufen ohne Sonderfälle auf „Sonne unten" prüfen können.
`solpos.hours_from_solar_noon` liefert die scheinbare Sonnenzeit für die
Tagesabschnitts-Binnung (§8).

### §4.2 Clear-Sky und Clear-Sky-Index

`core/clearsky.py` liefert den **Haurwitz**-Klarhimmel-GHI und daraus den
**Clear-Sky-Index** `k_c = GHI / Haurwitz(Elevation)`. k_c ist **ausschließlich
Lern-Gate und Normierung** — nie Prognosequelle. Haurwitz ist bei Tiefstand
grob; Aufrufer gaten k_c deshalb **elevationsabhängig** (§9.1) bzw. fallen
unterhalb `CLOUD_KC_MIN_ELEVATION_DEG` auf eine andere Klassifikationsquelle
zurück (§8).

### §4.3 Transposition (Hay-Davies) je Ebene

`core/transpose.py` rechnet je Ebene **Hay-Davies**:

- **Beam** aus DNI über den Einfallswinkel,
- **zirkumsolarer Diffusanteil** über den Anisotropie-Index
  `Ai = DNI / E0n` mit der extraterrestrischen Normalstrahlung
  `E0n = 1361 · (1 + 0,033 · cos(2π · doy / 365))` (Sonnendistanz-Exzentrizität;
  der Erd-Sonne-Abstand schwankt ±3,3 % übers Jahr). Die Engine reicht den
  Slot-`doy` durch, sodass Live- und Backfill-Pfad identisch rechnen;
  `doy=None` bedeutet die feste Solarkonstante und bleibt für reine Aufrufer
  zulässig.
- **isotroper Rest × Sky-View-Faktor** (§5.3),
- **Bodenreflex** (§4.6).

**Pflicht-Deckel gegen Tiefstands-Artefakte:** der Verhältnisfaktor R_b ist auf
**≤ 10** gedeckelt, und unterhalb **3° Sonnenhöhe** wird der zirkumsolare Anteil
auf 0 gesetzt (`ai = 0` **vor** dem Diffus-Split, sodass der misstrauische
Anteil in die isotrope Kuppel fällt statt zu verschwinden).

### §4.4 Einfallswinkel-Modifikator (IAM)

ASHRAE-IAM `f = 1 − IAM_B0 · (1/cos θ − 1)` auf **Beam + Zirkumsolar**,
angewandt in der **Engine** nach der reinen Transposition (pvlib-analog, damit
die Golden-Vektoren vergleichbar bleiben) und **vor** der ungegateten
Trainer-Referenz (§9.1). Ohne diese Reihenfolge absorbierte die Shademap den
Glasreflexionsverlust (5–15 % bei AOI > 60°, auf steilen Fassaden großer
Tagesanteil) als AOI-förmige Phantom-Verschattung.

### §4.5 Bifazialer Beam-Gain

Der optionale Site-Faktor `bifacial_beam_gain` (§7.2) wirkt auf **nur**
Beam + Zirkumsolar, in der Engine **nach** dem IAM und **vor** der ungegateten
Trainer-Referenz und dem τ-Gate. Er wirkt damit identisch auf die RAW- und die
korrigierte Kurve und hebt einen ehrlich unterschätzten Direktstrahl (bifaziale
Rückseite, steile Geometrie) in die **Rohphysik**, statt gedeckelte Lerner
(Transmittanz ≤ 1, Bias-Zellen) das Defizit als >1-Korrektur ausdrücken zu
lassen. Iso-Diffus und Bodenreflex bleiben unberührt. Default
`BEAM_GAIN_DEFAULT` = 1,0 ist die **Identität**: eine Konfiguration ohne das
Feld verhält sich unverändert.

### §4.6 Bodenreflex und Albedo

Bodenreflex `albedo · GHI · (1 − cos β) / 2` — bei 70–80° Neigung immerhin
~7–8 % der GHI. Ohne Konfiguration gilt `ALBEDO_DEFAULT`; ein site-weites
`albedo` (§7.2) überschreibt es geklemmt. Meldet das Wetterbild eine
**Schneedecke**, gilt für den Slot `ALBEDO_SNOW` (`engine._slot_albedo`).

### §4.7 Intervallmittel-vs.-Instant-Semantik

Die 15-min-Werte des Anbieters sind **Intervallmittel**; die Engine behandelt
sie konsistent als Mittelwert über den Slot (nicht als Momentanwert am
Slot-Rand). Diese Semantik ist empirisch verifiziert und mit einem klaren Morgen
als Unit-Test festgeschrieben. Der Offline-/In-Process-Backfill (§12) rechnet
**dieselben** Faktoren in derselben Reihenfolge.

## §5 Horizont, Verschattungsgeometrie und Sky-View-Faktor

### §5.1 Horizontzeile: Feldsemantik und Validierung

Eine Horizontzeile (`planes[].horizon[]`, Feldtabelle in §7.4) beschreibt einen
Azimutstützpunkt der Horizontlinie einer Ebene und die Transmittanz dahinter.
Die Zeilen werden an der Config-Grenze stabil nach Azimut sortiert.

- `azimuth_deg` — Stützpunkt-Azimut, 0…360, Konvention 0 = Nord (§20.1);
  Fehlercode `bad_horizon_azimuth`.
- `elevation_deg` — **die Horizontkante** dieses Azimuts, 0…90;
  `bad_horizon_elevation`. **Über** der Kante gilt immer τ = 1.
- `tau` — statische Transmittanz unterhalb der Kante, 0…1, `bad_tau`. Bei
  saisonalen Zeilen zugleich der belaubte Rückfallwert.
- `seasonal` — Bool. Ist es gesetzt, sind `tau_leafed` **und** `tau_bare`
  Pflicht (`seasonal_missing_tau`); beide 0…1 (`bad_tau`).
- `tau_points` — optionales **Elevationsprofil** τ(Sonnen-Elevation) als
  1…12 Paare `[el, τ]`, `el` **streng steigend** und **≤ `elevation_deg`**
  (`bad_tau_points`, `tau_points_above_edge`); τ ∈ [0,1], **kein Monotoniezwang**
  (reale Kronen sind nicht monoton). Der oberste Knoten wird per Konvention auf
  τ = 1 an der Kante gelegt, damit am Gate-Übergang keine Sprungstelle entsteht.
- `tau_points_bare` — optional, nur zusammen mit `seasonal` **und**
  `tau_points`, gleiche Länge und **identisches el-Raster**
  (`seasonal_points_mismatch`); der kahle Gegenpart, gegen den knotenweise
  geblendet wird.
- `diffuse_tau` — optional, `0 ≤ diffuse_tau ≤ HZ_DIFFUSE_TAU_MAX`
  (`bad_diffuse_tau`). **Achtung: `diffuse_tau` ist KEINE Transmission.** Es ist
  die **effektive Radianz des blockierten Sektors relativ zum offenen Himmel**
  — für eine helle Putzwand ungefähr deren Reflektanz. Wer es als
  „Durchlässigkeit der Wand" liest, missversteht das Feld. Die Obergrenze
  `HZ_DIFFUSE_TAU_MAX` (0,8) ist eine Kaschier-Leitplanke: Werte nahe 1
  („Sektor für Diffus unsichtbar") versteckten den beam-gebundenen Rest, den das
  Feld bewusst nicht abdecken soll.

**Serialisierung nur-wenn-gesetzt.** Ein optionales Feld erscheint in
`to_dict()` nur, wenn der Betreiber es gesetzt hat. Eine Zeile ohne
`tau_points` / `diffuse_tau` verhält sich **byte-identisch** wie vor deren
Einführung — Voraussetzung dafür, dass eine Alt-Config nach einem Upgrade
denselben Config-Fingerprint ergibt (§7.6).

### §5.2 Auswertung: Interpolation, τ(el)-Profil, Laub-Rampe

`core/horizon.py` wertet die Tabelle je Slot aus:

- Die Horizontlinie wird über den Azimut **linear interpoliert**
  (`interp_elevation`); die Tabellen liegen in **10°-Schritten** vor.
- Trägt eine Zeile ein `tau_points`-Profil, wird **je az-Nachbarzeile zuerst
  τ(el) aufgelöst und danach in az interpoliert** („resolve vor interpolate").
  Die Engine reicht `sun_el` durch; fehlt es, gilt der oberste Knoten.
- **Saisonale Laub-Rampe:** bei `seasonal` wird zwischen `tau_bare` und
  `tau_leafed` (bzw. knotenweise zwischen `tau_points_bare` und `tau_points`)
  über den **Day-of-Year** geblendet — eine **Kosinus-Rampe über April und
  November** um die in `const` festgelegten Laub-Anker.
- **Migrationsregel gegen die abgelöste az-Rampe.** Eine Transmittanzrampe, die
  als τ(az) entlang des Sonnenpfads eines Ankertags kodiert war, ist abgelöst:
  sie driftet strukturell (~0,3°/Tag) und erzeugt im Spätsommer Phantom-Beam in
  der Dämmerung. Eine bestehende Rampe wird **einmalig zu `tau_points`
  migriert und nie monatlich neu verankert**. Nach der Migration ist einmal
  `reset_day_ahead_bias` zu fahren (die Fingerprint-Deckelung nach §7.7 tut das
  automatisch, weil `tau_points` / `tau_points_bare` / `diffuse_tau` in den
  Fingerprint eingehen) und ein LTS-Re-Bootstrap empfohlen (`docs/BACKFILL.md`).

### §5.3 Sky-View-Faktor: halbtransparenter Horizont fürs Diffus

Der isotrope Diffusanteil wird je Ebene mit ihrem **eigenen Sky-View-Faktor**
skaliert. Der Himmel **unterhalb** der Horizontlinie geht dabei
**τ-gewichtet** ein statt als Wand: eine halbtransparente Baumreihe verdunkelt
das Diffus nicht wie eine Hauswand. τ = 1 lässt den SVF unverändert, τ = 0
ergibt die volle opake Reduktion.

Trägt eine Zeile ein `tau_points`-Profil, wird der blockierte Keil `[0, h]` an
den Profilknoten **segmentiert** und je Segment mit seiner Mittelpunkts-τ
gewichtet (Band-Integral in geschlossener Form). Trägt sie `diffuse_tau`, wird
für den Diffuspfad **dieses** statt der Beam-τ verwendet.

Weil die τ saisonal sind, ist der SVF **`doy`-abhängig**; die Engine memoisiert
ihn je (Ebene, `doy`). Der Sommer-SVF einer belaubten Baumzeile ist damit
kleiner als der Winter-SVF derselben Zeile.

### §5.4 Wirkung auf die Kurve

Steht die Sonne **unter** der interpolierten Horizontlinie eines Azimuts,
werden **Beam + Zirkumsolar × τ** gerechnet; darüber gilt τ = 1. Der
**Iso-Diffus** wird immer mit dem ebenen-eigenen SVF skaliert.
`diffuse_tau` wirkt **ausschließlich** im SVF — der Beam-Pfad bleibt unberührt,
eine opake Wand bleibt für Beam mit τ 0 opak. Die Horizonttabellen liegen
**versioniert im Repo bzw. im Config-Export**, nicht nur in `.storage`.

## §6 Elektrik: Zelltemperatur, Wirkungsgrade, DC→AC-Kette und Clamps

### §6.1 Ross-Zelltemperatur

`core/electrical.py` rechnet `Tcell = Tamb + k · POA` und daraus den
Temperaturkoeffizienten **−0,34 %/K** auf die Modulleistung. Der
Ross-Koeffizient `k` ist **je Ebene überschreibbar** (`ross_coeff`, §7.3), weil
ihn die Montage bestimmt: ~0,02 freistehend/gut hinterlüftet bis ~0,056
fassadenparallel/schlecht hinterlüftet. Ohne Angabe gilt `ROSS_COEFF`.
Validierung: endlicher Wert in **[0,005; 0,12]**, sonst `bad_ross_coeff`.

### §6.2 DC-seitiger Systemwirkungsgrad

Je Ebene ein `efficiency`-Faktor (§7.3, Default `DEFAULT_EFFICIENCY`) auf die
DC-Leistung — Verkabelung, Mismatch, Verschmutzung.

### §6.3 DC→AC-Kette

Die Engine rechnet und **lernt DC**. Die ausgelieferte AC-Kurve entsteht daraus
je Wechselrichter-Gruppe aus dem Mikro-Wechselrichter-Wirkungsgrad η_inv und
dem AC-Clamp (`electrical.clamp_groups_ac`):

```
AC_Gruppe = min(η_inv · Σ_Ports DC_unclamped · Slot-Faktor, ac_limit_w)
```

Der **DC-Clip-Punkt** liegt entsprechend bei `ac_limit_w / η_inv` — dort clippen
die Ports wirklich, weil der Mikro-Wechselrichter AC-seitig deckelt und den MPP
zurückdrückt.

### §6.4 AC-Clamp je Gruppe und Re-Clamp als letzte Stufe

Der AC-Clamp wirkt je konfigurierter Wechselrichter-Gruppe (`ac_limit_w`, §7.5)
auf die Gruppensumme; das Ergebnis wird proportional auf die Ebenen
zurückverteilt, damit die je Ebene gemeldete Leistung konsistent bleibt.

**Re-Clamp (verbindlich):** alle Lerner-Korrekturen (Intraday-Skalar,
Day-ahead-Bias) und die Quantilbänder laufen als **letzte Stufe erneut** durch
`clamp_groups`. Eine Hochkorrektur (Faktor > 1) kann die ausgelieferte Kurve
damit **nie** über die konfigurierte Wechselrichtergrenze heben. Ebenen ohne
Gruppe haben keine konfigurierte Obergrenze und passieren beide Clamps
unverändert; ihre korrigierten Watt gehen als ceiling-freier Anteil in die
Slot-Obergrenze der Bänder ein (§11.2).

### §6.5 Trennung DC-intern / AC-Ausgang

- **DC ist Lern- und Bewertungsgrundwahrheit:** Lernschichten (§9),
  Skill-Scoreboard und Kill-Gate (§15) rechnen auf der DC-Kurve, gemessen gegen
  `measured_dc_power_total` (§14.3).
- **AC ist der betreiberseitige Standard:** die Haupt-Sensoren
  (`energy_production_today/_tomorrow/_d2`, `power_production_now`, die
  P10/P90-Bänder) melden AC (§14.1).
- Das DC-Modell bleibt als `*_dc`-Diagnose sichtbar (§14.2), damit die
  Grundwahrheit nachvollziehbar bleibt.

## §7 Konfiguration: `site`-Schema, Validierung, Fingerprint und Auslieferungs-Default

Die Generik-Zusage aus §2 ist nur so verbindlich wie die Feldnamen, in denen
sie sich ausdrückt. Dieser Abschnitt benennt die **öffentliche
Konfigurationsoberfläche**: die Schlüssel, die im Config-Entry persistiert
werden, im Objekt-Editor der HA-UI von Hand editierbar sind und teils in den
Config-Fingerprint (§7.7) eingehen. Die Bereichsprüfungen und ihre Fehlercodes
(Übersetzungsschlüssel der Config-Flow-Feldfehler) liegen HA-frei in
`_site_validation.py` (`validate_site`), die Typen in `core/types.py`
(`SiteConfig`, `PlaneConfig`, `HorizonRow`, `InverterGroup`).

### §7.1 Entry-Ebene

Auf Entry-Ebene stehen `name`, `latitude`, `longitude`,
`fetch_interval_seconds`, `recompute_interval_seconds` und das Objekt `site`.
`latitude`/`longitude` werden zusätzlich **in** das `site`-Objekt gespiegelt,
denn Fetcher und Sonnenstand lesen ausschließlich die site-eigenen Koordinaten.

Nur **Laufzeitschalter** (Kill-Switches der Lernschichten §9, der Quantile
§11.2, der Ensemble-Bänder §11.3) und die **Vergleichsliste** (§15.3) leben in
den Options — ein strukturelles Feld dort verschattet `entry.data` dauerhaft.

### §7.2 Site-Ebene (`site`)

| Feld | Bedeutung | Bereich / Default | Fingerprint |
|---|---|---|---|
| `latitude`, `longitude` | Standort für Fetch + Sonnenstand | Pflicht | nein (aber Site-Signatur des Bootstrap-Imports, §12.5) |
| `planes` | Liste der Modulebenen (≥ 1) | `no_planes` | — |
| `groups` | Liste der Wechselrichter-Gruppen | darf leer sein | — |
| `ac_actual_entity` | Entity-ID des **Gesamt**-AC-Zählers hinter allen Wechselrichtern (η-Kalibrierung, §9.6) | optional; leer ⇒ nicht konfiguriert | nein |
| `ac_actual_invert` | negiert diesen Zähler einmalig an der Lesegrenze | optional, Default `false` | nein |
| `albedo` | Bodenalbedo des Reflexterms (§4.6) | optional, geklemmt `[SITE_ALBEDO_MIN, SITE_ALBEDO_MAX]`; ungesetzt ⇒ `ALBEDO_DEFAULT`; Schnee überschreibt mit `ALBEDO_SNOW` | **ja** |
| `bifacial_beam_gain` | Faktor auf **nur** Beam + Zirkumsolar (§4.5) | optional, geklemmt `[SITE_BEAM_GAIN_MIN, SITE_BEAM_GAIN_MAX]`; ungesetzt ⇒ `BEAM_GAIN_DEFAULT` (Identität) | **ja** |

### §7.3 Ebene (`planes[]`)

| Feld | Bedeutung | Bereich / Default | Fingerprint |
|---|---|---|---|
| `name` | Ebenenname, eindeutig; zugleich Default-Shademap-Kanal | `plane_no_name`, `plane_dup_name` | **ja** |
| `azimuth_deg` | Ebenenazimut, **0 = Nord im Uhrzeigersinn** (§20.1) | 0…360, `bad_azimuth` | **ja** |
| `tilt_deg` | Neigung gegen die Horizontale, 90 = senkrecht (§20.2) | 0…90, `bad_tilt` | **ja** |
| `wp` | STC-Peakleistung des Moduls (W) | > 0, `bad_wp` | **ja** |
| `efficiency` | DC-seitiger Systemwirkungsgrad (§6.2) | 0…1, Default `DEFAULT_EFFICIENCY`, `bad_efficiency` | **ja** |
| `horizon` | Horizontzeilen dieser Ebene (§5, §7.4) | stabil nach Azimut sortiert | **ja** (zeilenweise) |
| `actual_entity` | Entity-ID der gemessenen **DC**-Leistung dieses Kanals | optional | nein |
| `shade_group` | poolt den langsamen Lerner: gleiche Gruppe ⇒ **ein** Verschattungs-Pool (§9.2) | optional; leer ⇒ `shade_group_empty`; Namenskollision ⇒ `shade_group_collision` | nein |
| `ross_coeff` | montageabhängiger Ross-Koeffizient (§6.1) | optional, `[0,005; 0,12]`, `bad_ross_coeff`; ungesetzt ⇒ `ROSS_COEFF` | **ja** |

### §7.4 Horizontzeile (`planes[].horizon[]`)

Semantik und Begründung in §5.1, Wirkung in §5.4.

| Feld | Bereich / Regel |
|---|---|
| `azimuth_deg` | 0…360, `bad_horizon_azimuth` |
| `elevation_deg` | 0…90, `bad_horizon_elevation` — die Horizontkante |
| `tau` | 0…1, `bad_tau` (statisch bzw. belaubter Default) |
| `seasonal` | Bool; wenn gesetzt, sind `tau_leafed` **und** `tau_bare` Pflicht (`seasonal_missing_tau`) |
| `tau_leafed`, `tau_bare` | 0…1, `bad_tau` |
| `tau_points` | optional 1…12 Paare `[el, τ]`, `el` streng steigend und ≤ `elevation_deg` (`bad_tau_points`, `tau_points_above_edge`); keine Monotonie in τ erzwungen |
| `tau_points_bare` | optional, nur mit `seasonal` **und** `tau_points`, gleiche Länge und identisches el-Raster (`seasonal_points_mismatch`) |
| `diffuse_tau` | optional 0…`HZ_DIFFUSE_TAU_MAX`, `bad_diffuse_tau` — Effektivradianz, **keine** Transmission |

### §7.5 Wechselrichter-Gruppe (`groups[]`)

| Feld | Bereich / Regel | Fingerprint |
|---|---|---|
| `name` | eindeutig (`group_no_name`, `group_dup_name`) | **ja** |
| `plane_names` | nicht leer, jeder Eintrag ein existierender Ebenenname (`group_no_planes`, `group_unknown_plane`) | nein |
| `ac_limit_w` | > 0 und ≤ `AC_LIMIT_MAX_W` (`bad_ac_limit`) — der AC-Clamp der Gruppe (§6.4) | **ja** |
| `inverter_efficiency` | optional, geklemmt `[INVERTER_EFFICIENCY_MIN, INVERTER_EFFICIENCY_MAX]`, Default `DEFAULT_INVERTER_EFFICIENCY`; DC→AC-Kette (§6.3) | nein (verschiebt nur die AC-Ausgabe, nicht die gelernte DC-Kurve) |

### §7.6 Zwei Regeln für jedes neue Feld

1. **Nur-wenn-gesetzt serialisieren.** Ein optionales Feld wird in `to_dict()`
   nur geschrieben, wenn der Betreiber es gesetzt hat — eine Alt-Config muss
   nach dem Upgrade **byte-identisch** dasselbe Dict ergeben, sonst kippt der
   Fingerprint ohne fachlichen Grund und setzt Lernzustand zurück.
2. **Fingerprint-Pflicht.** Ein Feld, das die **RAW-Kurve** verändert, gehört in
   den Config-Fingerprint (§7.7) — mit gerundetem Wert und kollisionsfreiem
   Sentinel, ebenfalls nur-wenn-gesetzt angehängt. Felder, die die modellierte
   Kurve nicht verändern (Entity-IDs, Shade-Gruppierung, Zählervorzeichen),
   bleiben bewusst draußen, damit ein harmloser Edit kein Lernen zurücksetzt.

### §7.7 Config-Fingerprint und Bias-Reseed

Die Bias-Zellen (§9.5) werden gegen eine bestimmte **prognoserelevante
Konfiguration** gelernt. Neben dem Bias-State wird deshalb ein
`config_fingerprint` persistiert (§16.1): ein SHA-256-Kurzhash über

- je Ebene Azimut, Neigung, Wp, Wirkungsgrad, Ross-Koeffizient und den
  **Horizont** — je Zeile Azimut, Elevation und **alle** Transmittanzfelder
  (`tau`, `seasonal`, `tau_leafed`, `tau_bare`, `tau_points`,
  `tau_points_bare`, `diffuse_tau`, nur-wenn-gesetzt gehasht), denn die
  Horizontzeilen **sind** die τ-tragenden Screens dieser Konfiguration (eine
  τ-Änderung 0 → 0,4 oder ein `tau_points`-Knoten-Edit formt den Direktstrahl
  um; ein `diffuse_tau`-Edit hebt den Iso-Diffus-Floor standortweit),
- die Albedo und den bifazialen Beam-Gain (beide skalieren die Rohkurve
  standortweit),
- die AC-Grenzen der Wechselrichter-Gruppen,
- `CLASSIFIER_VERSION` (§8) — ändert sich die Klassenbedeutung, veralten die je
  Klasse gelernten Zellinhalte semantisch.

**Verhalten bei Abweichung.** Weicht der Fingerprint beim Setup oder nach einem
Options-Reload vom gespeicherten ab, passt das gelernte θ nicht mehr zur
Geometrie und würde im RLS-Steady-State nur unmerklich nachziehen. Daher werden
**alle** Zellen neu angesät (`bias.reseed_day_ahead_bias`): die RLS-Kovarianz
jeder Zelle wird auf `RLS_INIT_COVARIANCE` **wieder geöffnet** (der eigentliche
Lernraten-Hebel hängt an P, nicht an n) und ihr effektives n auf
`DAY_AHEAD_BIAS_RESEED_N` gedeckelt; das aktuelle θ bleibt als **Startwert**
erhalten. Das ist bewusst sanfter als `reset_day_ahead_bias`, das θ auf neutral
löscht. Zusätzlich INFO-Log und das persistente Repair-Issue
`config_changed_bias_reseed` (`ISSUE_CONFIG_CHANGED_BIAS_RESEED`) mit der
Empfehlung, einen Re-Bootstrap oder Reset zu fahren.

Ein **Erststart ohne gespeicherten Fingerprint** (frische Installation)
speichert nur den aktuellen Fingerprint — es wird nichts angesät.

### §7.8 Auslieferungs-Default `const.DEFAULT_SITE`

`const.DEFAULT_SITE` ist ein **Struktur- und Formatbeispiel** einer
mehrebenigen Balkonanlage: acht Modulebenen, vier Wechselrichter-Gruppen mit je
800 VA `ac_limit_w`, Fernfeld-Horizontzeilen (az 60–100 el 13°, az 100–150
el 16°, jeweils τ 0), saisonale Baumzeilen (τ 0,45 belaubt / 0,8 kahl) und eine
harte Wandzeile (az > 212, el 90, τ 0). Sein Inhalt ist hier beschrieben, weil
Tests ihn prüfen und der Config-Flow ihn als Ausgangspunkt anbietet.

**Er ist kein gepflegtes Abbild einer realen Anlage.** Bekannte Abweichungen
(normativer Bestandteil dieses Abschnitts, nicht Historie):

- Der Screen-Sektor **az 135–175 auf den S-Ebenen** ist durch die
  Shademap-Auswertung **widerlegt** — real wirkt eine solche Verschattung auf
  die Front-Ebenen.
- Die Wandkante steht bei **az 212**, real gemessen eher bei az 195.
- Die Ebenen tragen **keine** `albedo`-, `bifacial_beam_gain`-, `tau_points`-
  oder `diffuse_tau`-Schlüssel; es gelten also `ALBEDO_DEFAULT` und
  `BEAM_GAIN_DEFAULT`.
- Die `actual_entity`-Werte sind **fremde Wechselrichter-Entity-IDs**. Wer sie
  übernimmt, hat Messkanäle, die in seiner Instanz nicht existieren — das
  Präsenz-Gate aus §10 meldet genau das.

**Verwendung.** Ein realer Bootstrap läuft **nie** gegen dieses Objekt: die
Aktion `run_bootstrap` nutzt immer die Live-Config (§12.2), und
`scripts/backfill.py` erreicht `DEFAULT_SITE` ausschließlich über das
ausdrückliche Opt-in `--use-default-site` (§12.3). Die inhaltliche Neufassung
des Auslieferungs-Defaults (neutraler Minimal-Standort + Onboarding) ist
Gegenstand von `docs/adr/ADR-0023-onboarding-standortkonfiguration.md` (Status
*Proposed*). Die Herleitung der Zahlenwerte steht in `docs/HISTORIE.md`.

## §8 Wetterklassifikation und Zeitbinnung (gemeinsame Taxonomie)

Day-ahead-Bias (§9.5), Quantilbänder (§11.1), Scoreboard-Strata (§15.2) und der
Offline-/In-Process-Bootstrap (§12) teilen **eine** Bin-Taxonomie. Meinten Zelle
und Trainingssample verschiedene Klassen oder Sonnenstände, wäre jede
Auswertung wertlos.

**Wolkenklasse** (`bias.classify_cloud`) ∈ {`clear`, `mixed`, `overcast`,
`fog`}:

1. **Vorrangige Nebel-Regel:** Sicht < `FOG_VISIBILITY_M` (1000 m) **oder**
   (`cloud_cover_low` > `FOG_CLOUD_LOW_PCT` **und** Monat in `FOG_MONTHS`,
   Okt–Feb).
2. Sonst über den **Clear-Sky-Index** k_c = GHI / Haurwitz(Elevation):
   k_c ≥ `CLOUD_KC_CLEAR_MIN` ⇒ `clear`, k_c ≤ `CLOUD_KC_OVERCAST_MAX` ⇒
   `overcast`, sonst `mixed`. k_c spiegelt die real ankommende Einstrahlung —
   eine reine Gesamtbedeckung wertete Mittel- und Hochwolken voll und routete
   sonnige Nachmittagsstunden in die overcast-Zelle.
3. Unterhalb `CLOUD_KC_MIN_ELEVATION_DEG` (Haurwitz zu grob) oder ohne GHI
   fällt die Klassifikation auf die **Random-Overlap-Schichtbedeckung** zurück.

`CLASSIFIER_VERSION` versioniert die **Bedeutung** dieser Klassen. Eine Änderung
veraltet alle je Klasse gelernten Inhalte semantisch und geht deshalb in den
Config-Fingerprint ein (§7.7).

**Tagesabschnitt in scheinbarer Sonnenzeit.** Ein Slot wird nicht nach der
Ortsuhr einsortiert, sondern nach dem Stundenwinkel der Sonne
(`solpos.hours_from_solar_noon` → `bias.day_part_for_solar`): `midday` ist das
um den wahren Mittag symmetrische Fenster
`|hours_from_solar_noon| < MIDDAY_SOLAR_HALFWIDTH_H`, davor `morning`, danach
`afternoon`. Feste Ortsuhrzeiten driften gegen die Sonne über Sommerzeitwechsel
und Jahreszeit.

Die Zellen sind je Abschnitt **gelernt**, werden aber **stetig angewandt**: an
einer Abschnittsgrenze werden die beiden angrenzenden Faktoren linear über die
Sonnenzeit überblendet (± `DAY_PART_SOLAR_BLEND_HALFWIDTH_H`). Die Prognoseform
kommt aus Wetter × Physik × Verschattung und ist stetig; also muss auch der
aufgesetzte Residualkorrektor stetig sein.

**Zellschlüssel** ist `Wolkenklasse|Tagesabschnitt`.

**Uhr-Binnung als Rückfall.** `bias.day_part_for_hour` mit
`DAY_PART_MORNING_END_HOUR` / `DAY_PART_AFTERNOON_START_HOUR` (Überblendung über
`DAY_PART_BLEND_HALFWIDTH_MIN`) ist **ausschließlich defensiver Rückfall im
nächtlichen Trainingspfad**: `_nightly.day_part_for_hourkey` liest die Länge per
`getattr` vom Coordinator und binnt nur dann nach Uhr, wenn sie fehlt — ein
Training läuft lieber gröber als dass jedes Sample verworfen wird. Im
**Servierpfad gibt es keinen Rückfall**: der Coordinator ruft
`solpos.hours_from_solar_noon(..., self._site.longitude)` unbedingt, und
`Site.longitude` ist ein nicht-optionales `float`. `bias.day_ahead_factor` (die
Uhr-Variante des Faktors) ist **Legacy und nicht normativ** — sie hat keine
Aufrufstelle im Produktivcode; verbindlich ist allein
`bias.day_ahead_factor_solar`.

## §9 Lernschichten

Leitsatz: **alles Gelernte ist geclampt, gegatet, abschaltbar und
rollbackbar**; Degradation ist nie still. Alle vier Schichten sind numpy-frei
und je einzeln über den Options-Flow abschaltbar.

### §9.1 Schicht 1 — Shademap (langsam, geometrisch)

Je **Messkanal** und je Bin (**Sonnenazimut 5° × Elevation 2,5° × Halbjahr**
vor/nach Sommersonnenwende) eine EMA (α 0,15) der **beam-referenzierten
Transmittanz**

```
T = (P_gemessen − P_diffus_modelliert) / P_beam_modelliert
```

— bewusst **nicht** das Gesamtverhältnis gemessen/modelliert: im Schatten
enthält die Messung weiter den Diffus-Sockel; ein Gesamt-Ratio auf den Beam
angewandt würde verschattete Bins systematisch überschätzen und
diffus-unabhängige Verluste (Verschmutzung, η-Fehler) dem Beam zuschreiben. Das
Halbjahr im Bin-Schlüssel verhindert, dass April (laublos) und August (belaubt)
im selben Sonnenstands-Bin aliasen.

**Warm-up:** adaptives α = max(α, 1/(n+1)) — junge Bins sind das arithmetische
Mittel ihrer Samples statt vom Seed dominiert.

**Sample-Gate (quasi-klar):** elevationsabhängiges k_c-Band (Haurwitz ist bei
Tiefstand grob), Stabilität gegenüber dem Nachbarslot, und ein modellierter
Beam-Anteil > 5 % der Wp der Ebene. Zusätzlich ein **messseitiges** Klarheits-
Gate (`SHADEMAP_MEASURED_CLEAR_MIN_FRAC`), damit ein Tag, den die Prognose
fälschlich klar nannte, keinen geometrischen Bin verdunkelt.

**Clamp [0,0 … 1,1]** — volle Okklusion muss darstellbar sein (Hauswand).

**Cold-Start:** ein Bin erbt den **statischen Horizont-Prior** seines
Mittelpunkt-Azimuts; der Übergang läuft über **Shrinkage** w = n/(n+K) statt
über einen harten Min-Sample-Schalter.

**Trainingsreferenz ist der UNGEGATETE Beam.** Die gelernte τ **ersetzt** die
statische Horizont-τ des Bins; träfe das Training auf den bereits gegateten
Beam, wäre es selbstreferenziell (ein verschatteter Bin hat kaum modellierten
Beam) und liefe in einen √T-Fixpunkt. Die Engine liefert deshalb je Slot die
ungegatete Beam-DC und die rohe Diffus-DC am RAW-Arbeitspunkt mit (§16.2).

**Diagnose:** `dump_shademap` gibt die Karte als **Polartabelle** je Kanal aus
(visuell gegen bekannte Hindernisse prüfbar).

### §9.2 Verschattungsgruppen und Read-Time-Pooling

Die Verdeckungsgeometrie (Gebäudekante, Baumreihe) ist eine Eigenschaft des
**Standorts**, nicht eines Moduls; nur der **Impact** unterscheidet sich je
Ausrichtung, und den behandelt der Motor bereits pro Ebene über den Beam-Anteil.
Ebenen mit gleicher `shade_group` gehören daher demselben **Verschattungs-Pool**
an; `PlaneConfig.shade_channel = shade_group or name` ist die **einzige**
Definition der Zuordnung (Default: kanalweise).

- **Messung und alle Gates bleiben pro Ebene.**
- **Die Speicherung ist immer je Modul-Kanal** (Ebenenname): jede Ebene lernt
  ihre Karte einzeln und für immer.
- **Das Pooling geschieht ausschließlich beim Lesen** (im `beam_tau`-Hook des
  Motors und im Verschattungsprofil-Diagramm): der gelernte τ eines Bins ist das
  **n-gewichtete Mittel** über alle Pool-Kanäle
  (`tau_pool = Σ nᵢ·τᵢ / Σ nᵢ`, `n_pool = Σ nᵢ`), auf das dasselbe gemeinsame
  Shrinkage gegen den statischen Prior wirkt (`w = n_pool/(n_pool+K)`).

So kommt ein Sample eines Moduls allen Pool-Mitgliedern zugute, **ohne** die
Einzel-Historien zu verschmelzen: **Gruppieren und Auflösen ist jederzeit
verlustfrei reversibel**. **Caveat:** ein **historisch gemischter Gruppen-Blob**
(nach der Gruppe benannt, nicht nach einer Ebene) wird beim Auflösen verwaist
und unlesbar — wiederherstellbar per `rollback_learners` oder durch erneutes
Gruppieren unter demselben Namen.

**Legacy-Gruppenkanäle** aus einer früheren Merge-Migration werden als
**zusätzliche Pool-Quelle mitgelesen**: ein vorhandener Gruppenkanal fließt in
den Pool seiner Mitglieder ein, sodass seine bereits gepoolte Evidenz
weiterzählt, bis die kanalweisen Live-Daten sie verdünnt haben.

**Alias-Schutz (Validierung):** eine `shade_group` darf nicht dem **Namen** einer
Ebene entsprechen, die nicht selbst diese Gruppe trägt (sonst kollidierte der
Eigen-Kanal eines Nichtmitglieds mit dem Pool); eine nach einem eigenen Mitglied
benannte Gruppe ist erlaubt.

### §9.3 Gruppenvorschlag (`suggest_shade_groups`)

Weil jede Ebene ihren Kanal einzeln lernt, lässt sich die Gruppierung
datengetrieben belegen. Die Aktion vergleicht je Ebenenpaar die beiden Kanäle
**bin-weise über die gemeinsam besuchten Bins**: die Ähnlichkeit ist die
n-gewichtete mittlere τ-Differenz
(`mean_abs_diff = Σ w·|τ_a − τ_b| / Σ w` mit `w = min(n_a, n_b)`). Ein Paar gilt
als *ähnlich*, wenn es mindestens `min_common_bins` gemeinsame Bins hat **und**
`mean_abs_diff ≤ max_diff`; sonst als *verschieden* bzw. bei zu wenig Evidenz
als `insufficient_data`.

Aus den ähnlichen Paaren entsteht per **Complete-Linkage-Agglomeration**
(aufsteigend nach `mean_abs_diff`; zwei Cluster verschmelzen nur, wenn **jedes**
Kreuzpaar ähnlich ist — kein Verketten A~B~C bei zu großem A↔C) ein Vorschlag;
Ebenen ohne Evidenz bleiben Einzelgänger. Beide Schwellen sind pro Aktionsfeld
konfigurierbar (Defaults `SHADE_SIM_MAX_MEAN_DIFF` /
`SHADE_SIM_MIN_COMMON_BINS`). Die Antwort enthält **Ähnlichkeitsmatrix,
Vorschlag und die aktuelle Gruppierung** zum direkten Abgleich.

### §9.4 Schicht 2 — Intraday-Skalar (schnell, transient)

Ein exponentiell abklingendes Verhältnis gemessen/prognostiziert der letzten
2–4 h (τ ≈ 90 min), **im k_c-Raum konditioniert** (Geometrie und Saison
herausnormiert), auf die nächsten ~6 h abklingend angewandt.
**Clamp [0,25 … 2,5].** Der Skalar wird **nie persistiert**: nach jedem Neustart
oder Reload beginnt er bei `INTRADAY_NEUTRAL` (1,0).

Verboten ist allein die Persistenz des **Skalars als Zustand**. Die
Trailing-**Samples** sind neu bewertbare Rohdaten und dürfen beim Setup einmalig
rekonstruiert werden: 5-min-Recorder-Statistik des Gesamt-DC-Sensors als
gemessene Seite, die zwischengespeicherte θ-korrigierte Kurve als modellierte
Seite — **modellierte Seite auf die gemeterten Ebenen beschränkt** (nur Ebenen
mit `actual_entity`, exakt die Teilmenge, die der Gesamt-DC-Sensor summiert; auf
teilgemeterten Anlagen überhöhte die volle Kurve die modellierte Seite sonst und
halbierte den Skalar nach jedem Reload auf den Clamp-Boden). Die Rekonstruktion
läuft nur bei frischem (FRESH/CACHED) Wetter-Cache, sonst sauberer Abfall auf
neutral; `compute_intraday_scalar` läuft damit nach einem Reload sofort
organisch weiter, statt den Trailing-Fenster-Vorlauf neutral zu verbringen.

**Nichtzirkularität.** Die modellierte Seite ist die **bias-referenzierte**
Kurve — Roh-Watt × dem nächtlich eingefrorenen θ-Zellfaktor des Slots
(`_day_factor`) —, **nicht** die reine Roh-Kurve: die ausgelieferte Kurve ist
Roh × θ × Skalar, also korrigierten θ und Skalar sonst denselben Fehler doppelt.
Der Intraday-Faktor selbst geht **nie** in die modellierte Seite ein (θ ist
nächtlich eingefroren ⇒ keine Zirkularität). Ist θ für den Slot inaktiv, ist der
Faktor 1,0 und die modellierte Seite gleich der Roh-Kurve.

Die gemessene Seite ist gegen partiellen Kanalausfall abgesichert: fällt ein
Teil der Messkanäle aus, wird die modellierte Seite auf dieselbe Teilmenge
skaliert, statt das Verhältnis in Richtung Ausfallanteil zu drücken.

### §9.5 Schicht 3 — Day-ahead-Bias (RLS)

**Ein RLS-Bias-Skalar θ je Zelle** (`Wolkenklasse|Tagesabschnitt`, §8),
nächtlich trainiert, per Default aktiv, über den Options-Flow abschaltbar.

- **Modellierte Seite des Trainings** ist die **Slow-only-Kurve**
  (Schattenkarte ∘ Physik, ohne Day-ahead-Faktor; `snap.slow_only_hourly_wh`),
  Fallback-Kette Roh → Korrigiert bei inaktiver Slow-Schicht oder Alt-Snapshot.
  θ wird **auf** die schattenkarten-korrigierte Kurve aufgesetzt; ein Training
  gegen die **reine** Roh-Kurve korrigierte denselben Verschattungsfehler
  doppelt, sobald die Schattenkarte lernt.
- **Servier-Gate:** eine Zelle wird erst **ab `RLS_MIN_SAMPLES` trainierten
  Tagen** serviert; darunter liefert `BiasState.get_bias` exakt
  `DAY_AHEAD_BIAS_NEUTRAL`.
- **Clamps:** das gelernte θ liegt in
  `[DAY_AHEAD_BIAS_MIN, DAY_AHEAD_BIAS_MAX]`.
- **Vergessensfaktor** λ = `RLS_FORGETTING_FACTOR`.
- Angewandt wird θ **stetig** über die Abschnittsgrenzen (§8).

### §9.6 Schicht 4 — Wechselrichter-η-Kalibrierung

Ein **einzelner** gelernter Skalar η_inv je Anlage, kalibriert gegen den
**Gesamt-AC-Zähler** (`ac_actual_entity`, optional invertiert über
`ac_actual_invert`). Es ist eine EMA (α 0,10, adaptiver Warm-up) der
**stündlichen** Verhältnisse `gemessene-AC / modellierte-DC`, aber nur über
**kalibrierfähige** Stunden:

- **ungeclippt** — die Datenblatt-AC (η_default · Σ DC) muss unter 90 % der
  Gruppen-AC-Obergrenze liegen, geprüft auf der **unabhängigen** DC-Seite, damit
  ein Zähler-Glitch nicht zugleich das Gate passiert und das Verhältnis
  verfälscht;
- über einer **Mindestlast** (Σ DC > 100 W, sonst verzerren
  Wechselrichter-Eigenverbrauch und MPPT-Startschwelle das Verhältnis).

Verhältnisse außerhalb **[0,90 … 0,99]** werden **verworfen** (kein plausibles
Wechselrichter-η — etwa ein Zähler, der auch Hauslast sieht oder netto
verrechnet). Erst nach **≥ 20** kalibrierfähigen Stunden wird das gelernte η
vertraut.

Es ist **nie load-bearing**: kein AC-Zähler, zu wenige Samples oder ein
out-of-band-Verhältnis fallen alle auf das konfigurierte bzw. Default-η zurück;
DC-Lernen und Scoreboard bleiben unberührt. `InverterCalState` (`eta`, `n`) lädt
validate-and-clamp wie die übrigen Lerner und reitet **nicht** auf dem
Rollback-Ring (selbst-gatend).

### §9.7 Nächtlicher Trainingsjob

- **Quelle** sind die stündlichen **Langzeitstatistiken** des Recorders für die
  `actual_entity` der Ebenen (im Recorder-Executor gelesen).
- **Idempotent, datums-gekeyt:** ein Tag wird nie doppelt trainiert; der Job ist
  gefahrlos mehrfach ausführbar.
- **Nachholfenster:** es **endet gestern** und **beginnt am Tag nach dem
  neuesten bereits erfassten Ist-Tag**, gedeckelt auf
  `NIGHTLY_CATCHUP_MAX_DAYS` (`_nightly.catchup_days`) — also kein fixer Block
  „N Tage zurück ab gestern", sondern genau die Lücke seit dem letzten erfassten
  Tag, höchstens N Tage breit. Auf einer frischen Installation ohne erfasste
  Tage ist das volle N-Tage-Fenster die Obergrenze; es reicht dann zwangsläufig
  in Zeiträume **vor** der Installation zurück (siehe Anlaufphase-Regel §10).
  Jeder nachgeholte Tag durchläuft dieselben Gates und denselben
  Idempotenzmarker.
- **Zeitstempel-Semantik der Ist-Werte (kritisch):** numerische `start`-Werte
  einer Statistikzeile werden **nach Größenordnung** disambiguiert
  (`_actuals._EPOCH_MS_THRESHOLD`: darüber Millisekunden = WebSocket-Format,
  darunter Sekunden = In-Process-`statistics_during_period`). Die Fehldeutung
  Sekunden-als-Millisekunden faltet alle Stunden eines Tages auf **einen**
  1970-Schlüssel, worauf das Tages-Vollständigkeitsgate jeden Tag verwirft und
  **jedes** nächtliche Lernen still verhungert (Day-ahead-Bias, Shademap,
  Quantile, Scoreboard, Drift-Monitor gleichzeitig). Dieselbe Prüfung gilt
  gleichlautend für `scripts/backfill.py` und den In-Process-Re-Bootstrap
  (§12.2); sie ist regressionsgetestet (§21).
- Jeder Teilschritt ist so gekapselt, dass ein einzelner Fehlschlag weder den
  Rest abbricht noch Home Assistant beeinträchtigt.

### §9.8 Schutzmechanismen

- **Label-Gates im Trainer:** eingefrorene Sensoren (derselbe Wert über
  `LABEL_FROZEN_MIN_REPEATS` aufeinanderfolgende Stunden bei altem
  `last_updated`), verletzte Energie-Monotonie und **Messkanal-Dropout** (ein
  konfigurierter Kanal ohne verwertbare Zeilen oder mit reißender
  Tagesabdeckung) verwerfen den **ganzen Tag** für **beide** geometrischen
  Lerner. Eine teilgemessene Anlage darf nie gegen das Vollmodell trainieren.
  Die Sichtbarkeit dieser Verwürfe regelt §10.
- **Drift-Monitor:** rollierende 7-Tage-Tageslicht-MAE korrigiert vs. reine
  Physik. Verliert eine Schicht `DRIFT_LOSS_STREAK_DAYS` Tage in Folge, wird sie
  **automatisch abgeschaltet** und ein HA-Repair-Issue gesetzt
  (`fast_learner_auto_disabled` / `slow_learner_auto_disabled`, je Config-Entry
  gescoped und **persistent**, damit die Warnung einen Neustart überlebt wie das
  Abschalt-Flag). Ein Verlusttag wird der **schuldigen Schicht** zugeordnet —
  Slow: Schattenkarte vs. Physik; Day-ahead/Fast: korrigiert vs.
  Schattenkarten-Kurve — mit **unabhängigen Streaks**, sodass eine unschuldige
  Schicht nicht mitabgeschaltet wird. Alt-Snapshots ohne Schattenkarten-Kurve
  fallen auf das gemeinsame korrigiert-vs-Physik-Signal zurück. Ein
  „schlechterer Herausforderer" muss die Referenz sowohl relativ als auch über
  einem absoluten Wh-Boden schlagen, damit sieben bedeutungslose Wh keine
  Schicht abschalten. Beim Abschalten wird der **Pre-Streak-Zustand automatisch
  wiederhergestellt** (`restore_layer_snapshot`); das Wiedereinschalten bleibt
  eine ausdrückliche Betreiberhandlung im Options-Flow.
- **Kollaps-Detektor:** liegen alle Kanäle nahe 0, während die Prognose hoch ist
  (Schnee auf den Modulen, Total-Dropout), werden **beide** geometrischen Lerner
  für den Tag eingefroren; nur der geclampte Intraday-Skalar reagiert. Das
  Freeze-Datum ist persistiert, sodass ein Neustart mitten am Tag die Sperre
  nicht aufhebt.
- **Kill-Switches je Lernschicht** im Options-Flow.
- **Snapshot-/Rollback-Ring** `LEARNER_SNAPSHOT_RING` (bewusst größer als
  `DRIFT_LOSS_STREAK_DAYS`, damit ein Rollback stets auf einen Stand **vor** dem
  Streak zugreift; `DRIFT_ROLLBACK_SNAPSHOTS` ist ein Legacy-Alias und **nicht**
  die wirksame Ringtiefe). Ein Snapshot je nächtlichem Lauf;
  `rollback_learners` setzt Bias, Shademap **und** Quantilzustand gemeinsam
  zurück (§16.2).
- **Validate-and-clamp beim Laden:** jede Lerner-Sektion geht durch ihr
  `from_dict`, das kaputte Werte auf neutrale Defaults klemmt. Ein korrupter
  Store ergibt neutrale Faktoren, **nie** einen Setup-Crash.

## §10 Lern-Sichtbarkeit: Messkanal-Präsenz, Verwurfssträhne, Anlaufphase

Die Label-Gates (§9.8) sind richtig — ein Teiltag oder ein eingefrorener Kanal
darf nie Grundwahrheit werden. Ihre Konsequenz darf aber nicht **unsichtbar**
bleiben: `_actuals._actuals_from_stats` verwirft den ganzen Tag für **beide**
Lerner, sobald **ein** konfigurierter Kanal unbrauchbar ist. Meldete das System
das nur als Log-Warnung, sähen Status und Entitäten dabei völlig normal aus —
eine Statuslüge. Zwei Gates machen es sichtbar; beide sind reine **Meldewege**
und verändern weder Physik noch Lernentscheidung.

**(a) Messkanal-Präsenz (Sofort-Check).** Beim Setup des Config-Entries und nach
jeder Konfigurationsänderung (die den Entry neu lädt) wird geprüft, ob jede in
den Ebenen konfigurierte `actual_entity` in dieser HA-Instanz überhaupt
**existiert** (State **oder** Entity-Registry-Eintrag; eine bloß `unavailable`
Entität gilt als vorhanden — das ist ein Gerätefehler, kein
Konfigurationsfehler, und Sache von (b)). Fehlt mindestens eine, wird das
persistente Repair-Issue `actual_entity_missing` gesetzt, das **Ebene und
Entity-ID** nennt und zum Reconfigure mit den eigenen Wechselrichter-Sensoren
auffordert; sind wieder alle vorhanden, wird es gelöscht.

Der Check hängt **nicht** am `config_fingerprint`-Abgleich: dieser läuft in
`async_prime_from_store` vor dem ersten Refresh und beim Kaltstart
typischerweise, bevor die Wechselrichter-Integration ihre Entitäten registriert
hat (Fehlalarm bei jedem Neustart). `actual_entity` ist zudem bewusst **kein**
Fingerprint-Feld (ein Sensor-ID-Tausch darf die Bias-Zellen nicht neu ansäen).
Während HA noch startet, wird der Check auf `EVENT_HOMEASSISTANT_STARTED`
vertagt.

**(b) Verwurfssträhne (nächtlich).** Wird das nächtliche Training
`LEARNING_STALLED_STREAK_DAYS` Tage in Folge **komplett** verworfen, wird ein
Repair-Issue gesetzt, das die **Ursache** nennt — je Gate ein eigenes Issue,
weil die drei Fälle verschiedene Gegenmittel haben:

- `learning_stalled_dead_channel` — Kanal ohne verwertbare LTS-Zeilen ⇒
  Entity-ID / Recorder-Ausschluss prüfen;
- `learning_stalled_frozen_channel` — eingefrorener Sensor ⇒ Gerät neu starten;
- `learning_stalled_low_coverage` — Tagesabdeckung reißt ⇒ Recorder-Lücke,
  Purge oder Modulausfall mitten am Tag.

Je Gate hält `_actuals` den Grund maschinenlesbar fest (`DROPOUT_REASON_*` plus
betroffene Ebene und Entity-ID); die Zuordnung Grund → Issue
(`ISSUE_LEARNING_STALLED_BY_REASON`) ist über `DROPOUT_REASONS` vollständig. Der
Zustand liegt **persistiert** in der Store-Sektion `learning_health` (§16.1) und
überlebt Neustarts. Beim ersten wieder angenommenen Tag: Strähne auf 0, alle
drei Issues gelöscht.

**Keine Fehlalarme in der Anlaufphase (verbindlich).** Eine neue, korrekt
konfigurierte Anlage hat naturgemäß erst einmal keine vollständigen LTS-Tage,
und das Nachholfenster reicht bei leerem Store zwangsläufig in die Zeit **vor**
der Installation zurück (§9.7). Ein verworfener Tag zählt daher nur dann auf die
Strähne, wenn er **strukturell** ist — operationalisiert als: die Integration hat
für diesen Tag eine Prognose **ausgeliefert** (Eintrag im Issued-Ring, §16.2).
Nur dann liefen wir, nur dann hätten die Kanäle geloggt haben müssen. Tage vor
unserer Zeit zählen nie. Die Strähne ist zusätzlich **tagesidempotent**: nur ein
Tag **neuer** als der zuletzt gezählte zählt hoch (ein verworfener Tag wird nicht
erfasst und daher jede Nacht erneut gelesen).

**Eine Karte je Ursache (verbindlich).** Ein kopierter Auslieferungs-Default
(§7.8) löst (a) sofort aus und würde eine Arbeitswoche später zusätzlich (b)
auslösen — zwei Karten, eine Wurzel, ein Handgriff. Solange
`actual_entity_missing` steht, zählt die Strähne daher weiter und bleibt im
Diagnose-Dump sichtbar, setzt aber **keine** eigene `learning_stalled_*`-Karte.
Es geht nichts verloren: die Präsenzkarte nennt genau die Kanäle, über die das
Dead-Channel-Gate stolpert, und die Unterdrückung endet, sobald die Kanäle
auflösen — eine Strähne mit wirklich anderer Ursache erscheint also weiterhin.

Der Vorrang gilt in **beide** Richtungen, auch wenn die Präsenzlücke erst
**nachträglich** auftritt: steht bereits eine `learning_stalled_*`-Karte und
verschwindet danach ein Messkanal (Tippfehler beim Reconfigure, umbenannte oder
gelöschte Wechselrichter-Entität, Reload), dann räumt die nächste gezählte
Verwurfsnacht die stehende Strähnenkarte **weg** und lässt allein die
Präsenzkarte stehen. Das ist gewollt und keine Regression: die Präsenzkarte
nennt ab diesem Moment die spezifischere Ursache und den konkreten Handgriff,
während die Strähnenkarte nur noch die Folge beschriebe. Die Strähne selbst
wird dabei **nicht** zurückgesetzt — sie zählt weiter und bleibt im
Diagnose-Dump lesbar, so dass die Karte nach Behebung der Präsenzlücke sofort
wieder erscheinen kann, wenn der Verwurf eine andere Wurzel hat.

**Der AC-Zähler ist bewusst kein Repair-Issue.** `ac_actual_entity` ist optional,
selbst-gatend (fehlt oder lügt der Zähler, bleibt η auf dem konfigurierten Wert
und das DC-Lernen ist unberührt) und **nie** ein Lern-Blocker. Eine zweite
Repair-Karte neben der blockierenden verwässerte genau das Signal, das Handlung
erfordert; ein fehlender AC-Zähler erscheint daher im Diagnose-Dump
(`actual_channels.ac_missing`) und einmal im Log.

**Sichtbarkeit im Diagnose-Dump (§14.6).** Beide Gates schreiben in die bereits
vorhandenen Accessoren, nicht in einen dritten Sonderweg:
`store_stats()['learning_health']` (letzte Verwurfsursache, betroffene Ebenen,
Strähnenlänge, Schwelle, letzter angenommener Ist-Tag) und
`learner_state_summary()['actual_channels']` (Anzahl konfigurierter Kanäle,
fehlende Kanäle, AC-Zähler-Status). Fern-Diagnose ist damit ohne Log-Zugriff
möglich.

## §11 Unsicherheit: Quantilbänder P10/P50/P90 und Ensemble-Hüllkurve

### §11.1 Verfahren: empirische historische Simulation

Ein **Ring stündlicher relativer Fehler** (`gemessen / korrigierte Prognose`),
gekeyt nach der Taxonomie aus §8 (Wolkenklasse × Tagesabschnitt). Zur
Prognosezeit liefert `quantiles.bands_for_bin` die empirischen
P10/P50/P90-**Multiplikatoren** des Bins (`QUANTILE_P_LOW` / `QUANTILE_P_HIGH`).

- Der Ring ist **datumsfensterbasiert**: jedes Sample trägt das ISO-Datum seines
  Trainingstags, das Fenster ist `QUANTILE_RING_DAYS` relativ zum Trainingstag.
  Ein zusätzlicher Zähl-Cap ist nur Backstop.
- **Per-Tag-Cap** `QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN`, weil die Stunden eines
  Tages stark korreliert sind.
- **Servier-Gate:** ein Band spreizt nur, wenn der Bin **beides** erfüllt —
  `n ≥ QUANTILE_MIN_SAMPLES` **und** Evidenz aus `days ≥ QUANTILE_MIN_DAYS`
  **verschiedenen Tagen**. Beides kommt aus demselben `ring_evidence`-Gate, das
  auch die Diagnose speist, damit Anzeige und Verhalten nicht auseinanderlaufen.
- **Altzustand ohne Datumsstempel.** Ein Ring aus der Zeit vor dem Datumsfenster
  enthält Samples ohne ISO-Datum. Diese zählen **nicht** als null Tage, sondern
  über eine beweisbare Untergrenze mit: `quantiles.ring_evidence` liefert
  `effective_days = |verschiedene Datumsstempel| + ⌈ungestempelt /
  QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN⌉` — die wenigsten Tage, auf die sich so
  viele per-Tag-gedeckelte Samples verteilt haben können. Ohne diese Regel
  bliebe ein voll trainierter Alt-Ring dauerhaft unter dem Gate und alle Bänder
  kollabiert.
- **Cold Start:** ein Bin unter dem Gate kollabiert auf P50
  (p10 == p50 == p90) — **keine Fake-Spreizung und kein Fake-Shift**.
- Nur Stunden, deren korrigierte Prognose `QUANTILE_MIN_FORECAST_WH`
  überschreitet, werden gesampelt, und der relative Fehler wird geklemmt, damit
  eine Dämmerungsstunde nahe null keinen 100×-Multiplikator einschleust.
- Das Verfahren ist eine **reine empirische historische Simulation**;
  ausdrücklich **keine** adaptive konforme Nachführung.

Trainiert wird nächtlich aus der **ausgelieferten korrigierten** Stundenkurve
gegen die gemessene (§9.7); der Rahmen ist damit derselbe wie beim Servieren.

### §11.2 Servieren

Die Multiplikatoren werden **je Stunde bzw. je Slot** auf die korrigierte Kurve
angewandt. Jeder Slot wird an der **physikalischen AC-Obergrenze** gedeckelt
(Summe der Gruppen-`ac_limit_w` plus die korrigierten Watt der ceiling-freien
Ebenen, §6.4).

**Asymmetrische Intraday-Behandlung.** Die servierte Band-Kurve behält den
Intraday-Skalar, aber das **Tages-P10-Aggregat** darf durch einen Hoch-Skalar
nicht steigen: je Slot wird das servierte AC-P10-Band mit
`min(1, skalarfrei/serviert)` des zentralen AC-Strips skaliert — ein Faktor > 1
dividiert sich heraus, ein Faktor ≤ 1 behält das herunterkorrigierte Band. Das
**Tages-P90** behält den Skalar (eine Aufwärtskorrektur darf die optimistische
Flanke weiten).

**Ausgabe:** über `get_forecast` (plane-agnostische Gesamt-P10/P50/P90 in 15 min
und stündlich), über optionale Tages-P10/P90-Sensoren und über die
`wh_period`-Bandattribute auf den Energie-Sensoren (§14.4). Enable-Flag Default
**AN**, Kill-Switch im Options-Flow.

### §11.3 Ensemble-Wetter-Bänder (opt-in, Standard AUS)

Die gelernten Bänder sind pro Wetterklasse **im Mittel** gut kalibriert, aber
blind für die spezifische Unsicherheit **von heute**. Die
Open-Meteo-**Ensemble-API** (`ensemble-api.open-meteo.com/v1/ensemble`, Modell
`icon_seamless`: Kontrollmember unter dem nackten `shortwave_radiation`-Schlüssel
plus die gestörten `…_memberNN`) liefert gestörte Läufe, deren Streuung die
heutige Wetterunsicherheit **ist**.

**Formel (bewusst approximiert).** Pro Stunde bildet der Parser aus jedem
Member-GHI und dem deterministischen GHI (dem Stundenmittel der aktuellen
`WeatherSeries`, gleich verschlüsselt — Stundenstempel markieren das
Intervallende, also −1 h auf den Intervallstart) den **relativen** Faktor
`f_m = clamp(GHI_member / GHI_det, 0…3)`; `(f10, f90)` sind die
**Typ-7-Perzentile** 0,1/0,9 dieser Faktoren — dieselbe Perzentilwahl wie bei den
gelernten Bändern, damit beide dasselbe „80-%-Zentralintervall" meinen. Das ist
eine per-Slot-**Relativ**spreizung, **kein** Engine-Durchlauf je Member: die
Beam/Diffus-Rekomposition je Member wird bewusst weggelassen, weil die
Ensemble-Streuung die **Form** der Unsicherheit liefert, nicht eine absolute
Kurve. Ehrlich benannte Näherung: der GHI-Faktor wird auf das DC-Leistungsband
angewandt, als skaliere Leistung linear mit GHI — nur in erster Ordnung wahr.

**Fusion per Envelope-Max (nie multipliziert).** Pro Slot gewinnt das breitere
Band: `p10 = min(gelernt.p10, f10)`, `p90 = max(gelernt.p90, f90)`, `p50` bleibt
der gelernte Median. **Warum nicht multiplizieren:** der gelernte Residuenring
enthält den Wetterfehler der Klasse bereits — ein Produkt zählte den
Wetteranteil **doppelt**; die Hüllkurve addiert nur die zusätzliche Spreizung,
die das Ensemble heute über die Klimatologie hinaus sieht. **Cold-Start-Gewinn:**
ist das gelernte Band noch die neutrale Identität, liefert das Ensemble die ganze
Spreizung um p50 = 1,0 — echte Wetterstreuung, bevor der Ring Evidenz hat.

**Betrieb.** Eigener Fetch auf ~3-h-Kadenz (Ensembles aktualisieren
~6-stündlich), **nur im Speicher** gecacht (nicht persistiert, kein
Store-Schema-Bump). Eine Stunde mit zu wenigen nutzbaren Membern oder
deterministischem GHI unter der Schwelle fällt auf das gelernte Band zurück.

**Nie tragend.** P50, Headline, Scoreboard und Kill-Gate bleiben **unberührt**;
jeder Ausfall degradiert **nahtlos** auf die gelernten Bänder. Das Ensemble ist
ein **Opt-in-Schalter, Standard AUS**, und ausdrücklich **keine Stufe der
Degradationsleiter** (§13).

**Herkunftsausweis.** Ein `band_source`-Attribut auf den P10/P90-Sensoren fasst
die heutigen Slots zusammen: `learned` (nur Ring), `envelope` (Ensemble hat
irgendwo geweitet) oder `ensemble` (gelernt überall kollabiert, Ensemble lieferte
die ganze Spreizung). Ergänzend liefert dasselbe Attribut sowie die
`get_forecast`-Antwort ein `band_source_by_day`: pro **lokalem Tag** die Zahl der
Slots je Herkunft (`bin`/`envelope`/`ensemble`/`neutral`) — so ist sichtbar,
welche Prognosetage tatsächlich ein trainiertes Band tragen, ohne eine
Slot-Karte in den Recorder zu schreiben (per `_unrecorded_attributes`
ausgenommen). **Kopplung:** `band_source` und `band_source_by_day` existieren
**nur, solange ein Band existiert** — ein quantiles-off- oder
Cold-Start-Zyklus liefert weder Band-Kurven noch eine Herkunftsangabe.

### §11.4 Nutzung durch Konsumenten

P50 = Planung; P10 = konservative Reserve; P90 = Load-Timing (Überschusslasten so
spät wie möglich, ohne Export).

## §12 Bootstrap und Re-Bootstrap: Lernzustand aus Historie rekonstruieren

### §12.1 Zweck und Verbindlichkeit

Ein Bootstrap füllt Bias-, Shademap- und Quantilspeicher aus **Forecasts
as-issued** (Open-Meteo Previous-Runs) gegen die **gemessenen** Langzeitstatistiken
vor — er verkürzt die kalte Anlaufzeit um Monate. Verbindlichkeit: **Pflicht zu
versuchen, kein Blocker.** Das System muss ohne die Previous-Runs-API voll
funktionieren; fehlt sie, greift der Historical-Forecast-Fallback mit einer
WARNING (Analyse statt as-issued, für die geometrische Shademap weiterhin
nützlich), und fehlt auch der, läuft die Integration einfach kalt an.

### §12.2 Standardweg: die Aktion `run_bootstrap`

`balcony_solar_forecast.run_bootstrap` läuft **in-process** in den
Entwicklerwerkzeugen — ohne Long-Lived-Token, ohne `site.json`, und **immer mit
der Live-Config** dieser Installation (`coordinator._site`). Ein Site-Irrtum ist
damit strukturell ausgeschlossen.

**Datenbeschaffung.** (a) Wetter über die integrationseigene aiohttp-Session
(`aiohttp_client.async_get_clientsession`) vom Previous-Runs-API mit
Historical-Forecast-Fallback, gechunkt in `BOOTSTRAP_WEATHER_CHUNK_DAYS`-Fenster
(~90 Tage), damit ein Mehrjahres-Request kein Provider-Limit reißt. (b) Actuals
über einen **In-Process-`statistics_during_period`-Read im Recorder-Executor**
(nicht die WebSocket-API) über die `actual_entity` der Ebenen — numerische
Zeilen-`start` sind hier Epoch-**Sekunden**; der Reduce nutzt deshalb dasselbe
`_actuals._stat_row_hour_key`-Muster mit dem Größentest aus §9.7.

**Ausführung und Lock.** Der reine Rekonstruktions-Kern (`accumulate_days`) läuft
im Executor (CPU-Job) mit INFO-Fortschrittslogs. Ein **einziger** `asyncio.Lock`
je Coordinator (`_bootstrap_lock`) serialisiert den Lauf gegen den nächtlichen
Trainingsjob: der Nightly-Wrapper hält denselben Lock und **wartet** auf einen
laufenden Bootstrap, statt zu überspringen — keine Trainingsnacht geht verloren.
Ein zweiter gleichzeitiger `run_bootstrap` sieht `locked()` und wird sofort mit
einem klaren `ServiceValidationError` abgewiesen.

**Sicherheit: `dry_run` Default TRUE.** Der erste Aufruf holt, rekonstruiert und
liefert nur die Summary, **ohne** den Store zu berühren. Erst ein expliziter
`dry_run: false` importiert über denselben Pfad wie `import_bootstrap`
(`coordinator.async_import_bootstrap`: Rollback-Snapshot, Clamp/Cap, Quantile,
Refresh), sodass die importierte Shademap sofort die nächste servierte Kurve
formt.

**Antwort.** Die Aktion liefert **immer** die Summary
`{days_used, days_skipped, date_range, weather_source, bias_cells,
shademap_channels, shademap_bins, shademap_samples, quantile_bins,
quantile_samples, imported, duration_s}`; im Dry-Run zusätzlich ein `hint` auf
`dry_run: false`.

**Fehlerbilder** (kein Recorder, keine Actuals im Zeitraum, Open-Meteo-Fehler,
leerer oder invertierter Zeitraum, keine `actual_entity`, keine nutzbaren Tage)
werden als `ServiceValidationError` mit verständlicher Meldung zurückgegeben, nie
als Traceback.

**Default-Zeitraum:** `end` = gestern (lokal), `start` = heute −
`BOOTSTRAP_DEFAULT_MAX_DAYS`. Der Deckel ist großzügig, weil Tage ohne Actuals
übersprungen werden — ein zu weiter Start korrigiert sich selbst.

### §12.3 Offline-/CI-Weg: `scripts/backfill.py`

Der externe Einzeiler bleibt der Offline- und CI-Weg (Dev-Rechner, nicht auf der
HA-Box).

- **`--site` ist Pflicht** (Site-Objekt in der `SiteConfig.from_dict`-Form, wie
  der Config-Flow es speichert). Ohne `--site` bricht der Lauf **vor dem ersten
  Fetch** mit einer handlungsleitenden Meldung ab (**Exit-Code 2**), die den
  Aktions-Weg, den Export der Live-Config und das Opt-in nennt. Ein stiller
  Rückfall auf `DEFAULT_SITE` existiert nicht.
- **`--use-default-site`** ist das ausdrückliche Opt-in auf `const.DEFAULT_SITE`
  (Demo, Tests, CI) und protokolliert eine deutliche WARNING mit den bekannten
  Abweichungen aus §7.8.
- Werden **beide** angegeben, gewinnt `--site`.

### §12.4 Gemeinsamer HA-freier Kern

Beide Wege teilen denselben Kern: `core/bootstrap_build.py` (Rekonstruktions-
und Akkumulationsmathematik) und `core/openmeteo_backfill.py` (Previous-Runs- /
Historical-Forecast-Fetch, `PREVIOUS_RUN_LEAD_DAY` = 1, also der ~24 h vor der
Gültigkeit prognostizierte Wert). Die emittierten Bootstrap-Dicts sind
**byte-identisch** — das ist regressionsgetestet (§21).

Der Kern spiegelt die Live-Physik: dieselben `core/`-Funktionen, dieselben
Tageshygiene-Gates wie der nächtliche Trainer (§9.8), dieselbe
beam-referenzierte Transmittanz gegen die **ungegatete** Beam-Referenz (§9.1),
und **Speicherung immer je Ebene** (§9.2), auch für gruppierte Ebenen. Liegen
echte stündliche Ist-Werte vor, werden sie verwendet; sonst wird eine Tagessumme
formerhaltend über die Tageslichtstunden verteilt (bewusst grob — genau deshalb
der n-Cap in §12.5).

### §12.5 Import-Semantik

`store.import_bootstrap` ist **additiv**:

- Ein Payload **ohne** `quantile_state`-Schlüssel lässt den Live-Quantilring
  unangetastet; ein Payload **mit** dem Schlüssel ersetzt ihn wie die anderen
  beiden Lerner.
- Vor dem Ersetzen wird ein **Rollback-Snapshot** abgelegt (§16.2).
- **Site-Signatur-Prüfung:** `bootstrap_build.site_signature` (stabiler Digest
  über Lat/Lon und Ebenennamen) verhindert, dass ein für eine andere Anlage
  gebauter Payload den Lernzustand mit geometrisch falschen Bins überschreibt.
  Ein Payload ohne Signatur wird akzeptiert, aber protokolliert.
- **n-Cap:** backfillte Shademap-Bins erhalten ihr `n` auf `BOOTSTRAP_MAX_BIN_N`
  gedeckelt, weil ihre Samples stundengeglättet sind — die feineren
  15-min-Live-Daten sollen sie schnell überschreiben.
- Unbekannte Schema-Versionen werden **abgelehnt**; alle Werte innerhalb eines
  wohlgeformten Payloads werden geklemmt, nie zurückgewiesen.

### §12.6 Quantil-Seeding

Der Quantilspeicher wird über **denselben** `quantiles.train_quantiles` befüllt
wie live: pro Stunde `relerr = gemessen / korrigiert` mit
`korrigiert = clamp(θ_Zelle) · gegatetes-modelliertes-Wh` (θ nach dem
Tages-RLS-Schritt) in die Bins der Taxonomie aus §8, datumsgefenstert auf
`QUANTILE_RING_DAYS` relativ zum **letzten Backfill-Tag**, mit denselben Ring-
und Per-Tag-Caps wie live (§11.1). Ohne dieses Seeding blieben am Tag 0 nur die
overcast-Bins trainiert und alle anderen Bänder wochenlang auf P50 kollabiert.

## §13 Degradationsleiter (nie still)

```
frische Prognose
  → Last-Good-Cache (Store, konfigurierbare Altersgrenze MAX_PAYLOAD_AGE_HOURS)
  → Reine-Physik-Kurve aus dem letzten gültigen Wetterbild
  → unavailable
```

Konsumenten entscheiden selbst über ihre Fallbacks; die Integration erfindet
keine Werte.

**Jede Stufe ist sichtbar:** das `status`-Feld der Coordinator-Daten, der
`source_status`-Sensor, der `binary_sensor` „degraded" und — wo angebracht — ein
Repair-Issue. Die Prognose-Entitäten gehen ehrlich auf `unavailable`, statt
stille Altwerte zu halten; Diagnose-Entitäten bleiben **immer** verfügbar, damit
„wir sind degradiert" auch dann lesbar ist, wenn die Prognose selbst fehlt.

**Alterung wird nie zurückgesetzt.** Behält der Coordinator einen reicheren
gespeicherten Payload gegenüber einem ärmeren neuen, bleibt der Altersanker
stehen und nur der Scheduler wird zurückgesetzt — sonst servierte eine anhaltende
Teil-Degradation beliebig altes Wetter für immer als „frisch".

Das **Ensemble-Wetter** (§11.3) ist ausdrücklich **keine Stufe dieser Leiter**:
sein Fehlen weitet lediglich die Bänder nicht und ist nie ein Degradationsgrund.

## §14 Konsumenten-Schnittstellen: Entitäten, Attribute, Diagnostics

### §14.1 Prognose-Sensoren (AC) und die Heute-Headline

`energy_production_today` / `_tomorrow` / `_d2` (kWh) und `power_production_now`
(W) melden die **AC**-Kurve (betreiberseitiger Standard hinter den
Wechselrichtern). Dazu Diagnose-Sensoren für Baseline-MAE, Degradationsstatus
und Lernstatus.

Die **Heute-Headline ist eine stabile Day-ahead-Erwartung**: der transiente
Intraday-Skalar wird aus den Slots des aktuellen Tages wieder herausgerechnet
(die servierte `watts`/`wh_period`-Kurve behält ihn).

**Clamp-Interaktion.** Auf einem Slot, dessen hochkorrigierte Gruppenleistung
die AC-Obergrenze trifft (der Re-Clamp greift, servierter Wert = Deckel), ist der
skalarfreie Wert `min(prereclamp / Faktor, Deckel)`: `prereclamp`
(`corrected_unclamped_watts` = erst-geklammert × Faktor) geteilt durch den Faktor
ergibt exakt den skalarfreien servierten Wert, am physischen Deckel gekappt. So
bleibt die Headline day-ahead-stabil — das bloße Behalten des servierten Deckels
ließe sie unter großem Skalar um die volle Faktor-Reserve ballonieren, das
Herausdividieren auf einem geklammerten Slot untertriebe sie. Klammert die
Day-ahead-Kurve schon allein (`prereclamp / Faktor ≥ Deckel`, klarer Mittag),
liefert der Slot weiter den Deckel. Fehlen `slot_ceilings` /
`corrected_unclamped_watts` (alter Cache), bleibt der servierte Deckel
unverändert.

`power_production_now` trägt das je Gruppe konfigurierte η_inv sowie — sobald
kalibriert — das gelernte η als Attribut.

### §14.2 DC-Diagnosesensoren

`power_production_now_dc` und `energy_production_today_dc` / `_tomorrow_dc` /
`_d2_dc` machen das modellinterne **DC** sichtbar; DC bleibt Lern- und
Scoreboard-Grundwahrheit (§6.5). Die η-Attribute lauten `inverter_efficiency`,
`inverter_efficiency_learned` und `inverter_efficiency_source`
(`config | learned`) — ohne AC-Zähler ist η ein wortwörtliches Config-Echo und
sagt das.

### §14.3 Ist-Messung

- `measured_dc_power_total` (W, `MEASUREMENT` ⇒ Langzeitstatistik) ist die
  **ereignisgesteuerte Summe** der `actual_entity`-Sensoren aller Ebenen: sie
  abonniert die Quellsensoren direkt und rechnet bei jeder Änderung neu,
  **unabhängig vom Prognosezyklus**. Sie bleibt verfügbar, solange mindestens
  eine Quelle meldet (Grundwahrheit muss auch bei degradierter Prognose
  weiterlaufen), und wird **nur erzeugt, wenn** mindestens eine Ebene eine
  `actual_entity` konfiguriert hat.
- `measured_ac_power` (W, `MEASUREMENT`) ist die Live-Lesung des **einzelnen**
  Gesamt-AC-Zählers (`ac_actual_entity`, mit optionalem Vorzeichen-Invert) — das
  AC-Pendant zur DC-Summe und der zeittreue Partner der AC-Prognose; **nur
  erzeugt, wenn** ein AC-Zähler konfiguriert ist, sodass das Dashboard einen
  gleichwertigen **AC-gegen-AC**-Vergleich zeigen kann.

Die Attribute `sources` / `source_names` von `measured_dc_power_total` sind die
**Auto-Discovery-Quelle** der mitgelieferten Karten (§18.4).

### §14.4 Volle Kurve

15-min-`watts`- und `wh_period`-Attribute auf den Energie-Sensoren (per
`exclude_attributes` bzw. `_unrecorded_attributes` vom Recorder ausgeschlossen,
weil sie bulkig und je Zyklus neu sind), dazu die P10/P90-Bandkurven als
zusätzliche Attribute, und die Aktion `get_forecast` (15-min und stündlich,
P10/P50/P90 sobald vorhanden) nach dem Muster von `weather.get_forecasts`.

### §14.5 Energy-Dashboard-Hook

`async_get_solar_forecast(hass, config_entry_id)` liefert
`{"wh_hours": {iso_hour: wh, …}}` aus der servierten **AC**-Stundenkurve. Ohne
Prognose liefert der Hook `None` statt einer alten Kurve — das Dashboard zeigt
dann keinen Overlay statt eines stillen Altwerts (§13).

### §14.6 Diagnose-Dump (Config-Entry-Diagnostics)

- `store`-Block mit **echten Füllständen** aus `coordinator.store_stats()`:
  `issued_days`, `actuals_days`, `hourly_actuals_days`, `snapshot_ring`,
  `snapshot_ring_capacity`, `schema_version`, `learning_health`.
- `learners.state`-Block mit **echten Zählungen** aus
  `coordinator.learner_state_summary()`: `bias_cells`, `quantile_bins`,
  `shademap_channels`, `shademap_bins` je Kanal sowie `actual_channels`
  (`configured`, `missing`, `ac_configured`, `ac_missing`).
- `store.learning_health` (`discard_streak`, `last_discard_reason`,
  `last_discard_modules`, `last_discard_day`, `last_accepted_day`,
  `streak_threshold`) beantwortet „warum lernt diese Installation nichts?",
  `learners.state.actual_channels` beantwortet „existieren die Messkanäle
  überhaupt?" — beides ohne Log-Zugriff (§10).
- `forecast`-Block trennt `daily_kwh_dc` und `daily_kwh_ac` (statt eines
  mehrdeutigen `daily_kwh`).
- `quantiles`-Block führt je Bin `n`, `days` und `trained` (= exakt das
  Servier-Gate aus §11.1).
- `scoreboard`- und `ensemble`-Blöcke sind konstruktionsbedingt koordinatenfrei,
  laufen aber dennoch durch den Redactor.
- Der `day_ahead_bias_status`-Sensor führt je Zelle zusätzlich `clamped: true`,
  wenn θ am Bandrand (`DAY_AHEAD_BIAS_MIN`/`MAX`) klebt.

### §14.7 Statusehrlichkeit

Ein Diagnose-Sensor behauptet nie eine Wirkung, die er nicht hat.

- Die Lernstatus-Sensoren melden ausschließlich Werte aus
  `LEARNER_STATUS_VALUES`: `active` (schaltet **und** formt die Kurve gerade),
  `off` (Kill-Switch), `disabled_by_drift`, `frozen` (Kollaps-Detektor) und
  `cold_start` — aktiviert, aber **ohne** gelernten Zustand. `active` wäre dort
  eine Statuslüge.
- Unbekannte Werte melden `None`, nie einen erfundenen Status.
- Das Attribut `bias_cells` bleibt bei leerem Lerner als `{}` mit `cells_n: 0`
  bestehen (ein verschwindendes Attribut sah aus wie ein Defekt).
- `band_source` erscheint **nur, solange ein Band existiert** (§11.3).
- Scoreboard-Sensoren melden `None` statt einer fabrizierten Null, wenn das
  Fenster keine gewerteten Tage hat (§15.4).

## §15 Metriken, Skill-Scoreboard und Kill-Gate

### §15.1 Metrikdefinitionen

- **Tages-kWh-Fehler** ist die **Primärmetrik**: der absolute Fehler der
  Tagesenergie gegen die gemessene Summe. „Tages-kWh" benennt die
  Metrik*familie*; der Ring speichert Wh.
- **Taglicht-Stunden-MAE** ist die berichtete **Zweitmetrik**: mittlerer
  absoluter Stundenfehler, **eingeschränkt auf Tageslichtstunden** (die
  Vereinigung wird auf Stunden beschränkt, in denen mindestens eine Seite
  materiell von 0 verschieden ist — Nacht- und Dämmerungszeilen dürfen den
  Mittelwert nicht verdünnen).
- **nRMSE** wird auf die installierte Anlagenleistung (kWp) normiert.
- **Stratifizierung** nach klar / bewölkt / Nebel / Winter (§8).
- **Zielkorridor:** Day-ahead nRMSE ≤ ~10 % kWp, Tages-kWh-MAE ≤ ~15 % an
  Mischtagen. Nebel bleibt die härteste Klasse; dort hilft vor allem der
  Intraday-Skalar plus breite Bänder.

### §15.2 Scoreboard-Berechnung

Nächtlich, **pro Vortag**, berechnet der Coordinator den Tages-kWh-Fehler von
(a) der Motor-Prognose **as issued** und (b) jeder konfigurierten externen
Vergleichsprognose, jeweils gegen die **gemessene** Ist-Summe, plus die
**Stunden-MAE** des Motors. Rollierendes Fenster
`DEFAULT_SCOREBOARD_WINDOW_DAYS` (Default **14**, konfigurierbar),
**stratifiziert** nach der **dominanten Wetterklasse** des Vortags (§8).

**Fairness / kein Leakage (kritisch):**

- die **Motor**-Zahl kommt aus der **as issued**-Prognose des Vortags (aus dem
  Issued-Ring, §16.2) — **nie** mit heutigem Lernstand nachgerechnet; gewertet
  wird nur ein Tag, dessen Snapshot **vor** dem morgendlichen Cutoff dieses
  lokalen Tages ausgegeben wurde;
- die **Vergleichs**-Zahl ist der Wert **wie er am Vortag stand** (aus der
  Recorder-Historie des Vergleichssensors, am **gleichen** Prognosehorizont
  gelesen — der erste brauchbare State ab dem Ausgabezeitpunkt) — **nie** der
  heutige Wert;
- die **Ist**-Zahl ist die Summe der gemessenen Modulwerte aus dem
  Actuals-Ring.

**Matched-Pair-Auswertung:** für jede Vergleichsprognose werden Motor- und
Vergleichs-MAE über **nur die Tage** gerechnet, an denen dieser Vergleich
gewertet ist. Ein fehlender Vergleichswert (leere Vergleichsliste, umbenannte
Entität, gepurgter Recorder) ist **ABSENT**, nie eine fabrizierte Null — sonst
läse eine fehlende Historie als „Motor gewinnt haushoch". NaN, inf oder negative
Eingaben degradieren zu 0,0 statt eine Exception oder einen unsinnigen Fehler in
die Aggregate zu tragen.

### §15.3 Vergleichsprognosen

`CONF_COMPARISON_SENSORS` ist eine über den Options-Flow editierbare Liste von
`{name, daily_entity}`. Sie ist **generisch, konfigurierbar und wird leer
ausgeliefert** — es ist nichts im Runtime-Default hardcodiert. Halbgefüllte oder
fehlerhafte Zeilen werden beim Normalisieren durch `ComparisonConfig` verworfen.
Eine Beispielkonfiguration steht in `docs/DASHBOARD.md`.

### §15.4 Kill-Gate

`binary_sensor.kill_gate_passed` ist *on*, wenn der Motor über ein **volles**
Fenster gewerteter Tage mindestens `DEFAULT_SCOREBOARD_GATE_MARGIN` (Default
**0,10** = 10 %) besser auf Tages-kWh ist als die beste Baseline, mindestens
`SCOREBOARD_MIN_WINDOW_DAYS` Tage gewertet und mindestens
`SCOREBOARD_MIN_PAIRED_DAYS` gepaarte Tage vorliegen, und der Ring nicht
**stale** ist (der neueste gewertete Tag liegt innerhalb
`SCOREBOARD_MAX_STALENESS_DAYS`).

Ein **unvolles Fenster liefert `None`** — das ist **korrekt, kein Fehlschlag**:
ein Teilfenster darf das Gate nie behaupten.

### §15.5 Sensorik

Die ausgelieferten Objekt-IDs sind **unpräfixiert**, weil der Geräte-Slug bereits
`balcony_solar_forecast` trägt und das Präfix einen `…_forecast_*`-Stutter
erzeugte:

- `daily_kwh_mae` (Primärmetrik),
- `hourly_mae` (Wh je Tageslichtstunde, Zweitmetrik),
- `vs_best_baseline_pct` (positiv = Motor besser als die beste Baseline),
- `comparison_daily_kwh_mae_<slug>` je konfiguriertem Vergleich.

Die internen DATA-Keys der Scoreboard-Summary behalten dagegen ihre
`engine_*`-Form. Der **informative** Within-Stratum-Prozentwert
`engine_vs_best_baseline_pct` entfällt (`null` plus `low_n: true`) unter
`SCOREBOARD_STRATUM_MIN_N` gewerteten Tagen — ein einzelnes fehlgepaartes Paar
erzeugte sonst absurde Werte. Dazu eine **Diagnose-Aufschlüsselung je
Wetterstratum** im Diagnose-Dump.

## §16 Persistenz: Store-Schema, Ringe und Schreibsemantik

### §16.1 Schema und Migrationsinvariante

Ein HA-`Store` je Config-Entry. Die **äußere** Store-Hülle (`STORAGE_VERSION`)
ist auf **1 gepinnt** und wird nie angehoben; migriert wird ausschließlich das
**innere** Schema (aktuell **v3**, additiv über v2).

**Migrationsinvariante (kritisch): kein Lernzustand wird je verworfen oder
zurückgesetzt.** Jeder v2-Schlüssel wird **byte-treu** durchgereicht; die neuen
v3-Sektionen (`quantile_state`, `scoreboard_state`, `comparison_ring`) werden
leer default-injiziert. Eine Migration, die irgendeinen Lernzustand verwirft, ist
ein kritischer Fehler.

**Dasselbe Muster für additive Erweiterungen innerhalb von v3** (etwa
`inverter_cal_state` §9.6, `config_fingerprint` §7.7, `learning_health` §10):
`_empty_state()` injiziert den neutralen Wert, der gemeinsame Ladepfad liest
einen Store **ohne** den Schlüssel auf denselben neutralen Wert, und jede andere
Sektion bleibt byte-treu. Ein bestehender v3-Store lädt damit unverändert.

`learning_health` hält `{discard_streak, last_discard_reason,
last_discard_modules, last_discard_day, last_accepted_day}`.

Unbekannte oder zukünftige Schemaversionen werden mit einer Warnung verworfen
statt geraten.

### §16.2 Ringe und ihre Verträge

- **Horizonttabellen-Cache** und **Last-Good-Wettercache** (§3).
- **Forecast-as-issued-Ring** (`_ISSUED_RING_DAYS` = 90). Der nächtliche
  Snapshot hält je Tag **beide** Stundenkurven — **rohe Physik UND korrigiert** —
  plus die **Slow-only-Kurve** und die per-Ebenen-Stundenkomponenten
  (`beam_wh`, `diffuse_wh`, `ghi`, `kc`). Das ist die konstruktive Voraussetzung
  der schichtgetrennten Drift-Attribution (§9.8), des leakagefreien Scoreboards
  (§15.2) und des Shademap-Trainings aus Stunden-LTS (§9.1). Der Ring ist die
  Quelle von `get_issued_forecast` inklusive des **eingefrorenen** `eta` /
  `eta_source`. Er trägt zusätzlich die Wolkenklasse je ISO-UTC-Stunde, damit der
  nächtliche RLS-Trainer die richtige Zelle trifft (§8).
- **Actuals-Ring** (Tagesenergie je Modul) und **Hourly-Actuals-Ring**
  (`HOURLY_ACTUALS_RING_DAYS`, kürzer gehalten, weil er je Stunde und Kanal
  deutlich schwerer ist).
- **Fehler-/Quantilring** (90 Tage, §11.1).
- **Lerner-Snapshot-Ring** `LEARNER_SNAPSHOT_RING` — jeder Eintrag ein
  `LearnerSnapshot` mit Bias, Shademap **und** Quantilzustand, damit
  `rollback_learners` alle drei konsistent zurücksetzt. Ein Alt-Snapshot ohne
  Quantilfeld lädt mit leerem Quantilzustand (das Vor-Quantil-Verhalten). Der neueste Eintrag steht hinten; der älteste fällt
  beim Überlauf heraus.
- **`learning_health`** (§10).

### §16.3 Schreibsemantik

Schreibvorgänge werden **gebündelt** über `async_delay_save` geplant.
Payload-Writes sind zusätzlich **zeitgetaktet**
(`PAYLOAD_MIN_SAVE_INTERVAL_SECONDS`), sodass eine Wetteränderung höchstens alle
paar Stunden auf die Platte geht. Budget: **≤ 3 gebündelte Writes/Tag**
(eMMC-Schonung). Der nächtliche Job und der Flush bei HA-Stop bzw. beim Unload
garantieren eventuelle Persistenz.

Nach einem **harten Crash** dürfen Last-Good-Cache und As-issued-Log bis zu
einige Stunden verlieren — akzeptiert, weil die Degradationsleiter (§13) greift.

### §16.4 Lade-Robustheit

**Validate-and-clamp je Sektion:** jede Sektion läuft durch ihre klemmende
Dataclass. Ein korrupter, falsch geformter oder unbekannter Blob ergibt eine
**neutrale** Sektion, **nie** einen Setup-Crash, und lässt alle übrigen Sektionen
byte-treu. Auf sauberen Daten ist der Round-Trip die Identität.

## §17 Verschattungsprofil-Diagramm

**Zweck:** für ein wählbares Modul und ein wählbares lokales Datum die
Sonnenbahn (Elevation über Sonnen-Azimut) mit der **effektiven** Beam-Transmittanz
τ zeigen, die die Prognose an jeder Sonnenposition tatsächlich anwendet
(statischer Konfig-Horizont, per Shrinkage geblendet mit der gelernten
Shademap), plus zwei Horizontlinien (statisch und gelernt). Es ist das
interaktive Gegenstück zur `dump_shademap`-Polartabelle.

### §17.1 Entitäten

| Entität | Rolle | Default |
|---|---|---|
| `select.…_shade_profile_module` | Modul-/Kanalwahl | die **Front-Ebene** (die Ausrichtung, die die meisten Ebenen teilen); eine manuelle Wahl wird via `RestoreEntity` über Neustarts gehalten |
| `date.…_shade_profile_date` | Datumswahl (lokaler Kalendertag) | **immer heute** — bewusst NICHT restauriert; jeder Neustart/Reload öffnet auf dem aktuellen Tag |
| `sensor.…_shade_profile` | State = verschatteter Tageslicht-Anteil in % (τ < `SHADE_PROFILE_TAU_THRESHOLD`); Kurven-Arrays als Attribute | — |

Attribute des Sensors (Recorder-ausgeschlossen wie die Energiekurven):

- `time` / `azimuth` / `sun_elevation` / `transmittance` — je ein Eintrag pro
  Tageslicht-Sample der Sonnenbahn; `transmittance` ist die **gepoolte**
  effektive τ (§9.2);
- `transmittance_individual` — die τ des **eigenen** Kanals des Moduls allein,
  sodass Einzel- gegen Gruppensicht vergleichbar ist (leere Liste bei
  ungruppierter Ebene, dann identisch zur gepoolten Sicht — formstabil);
- `sample_n` — je Sample die **gepoolte Shademap-Bin-Evidenz** hinter der
  effektiven τ (0 = nur statischer Prior), aus derselben Read-Pool-Menge
  summiert (`shademap.pooled_bin_n`) und damit **nie** von der gezeigten τ
  abweichend;
- `horizon_azimuth` / `static_horizon` / `shade_horizon` — Horizontlinien auf
  einem Azimut-Raster über die Tageslichtspanne;
- Zusammenfassung: `shaded_fraction`, `mean_transmittance`,
  `has_learned_data` / `learned_bins` (**nur** Bins des visualisierten
  Halbjahrs — Bins des anderen Halbjahrs können die gezeigte Kurve nie
  beeinflussen), `sunrise` / `sunset`, `max_elevation`,
  `axis_azimuth_min` / `axis_azimuth_max` (die jahresstabile Achsenspanne aus
  beiden Sonnenwenden).

### §17.2 Semantik: engine-exakt, nie schöner als die Prognose

Die Transmittanz je Sonnenposition repliziert die Engine-Gate-Logik
(`engine._plane_poa_components`) **exakt**: statischer Prior =
`horizon.transmittance_at` nur bei Sonne ≤ interpolierter Horizontlinie, sonst
1,0; darüber blendet `shademap.effective_tau`.

**Slow-Active-Kopplung:** die gelernte Shademap fließt **nur** ein, wenn der
Slow-Learner aktiv ist (Kill-Switch an, nicht drift-deaktiviert, nicht
kollaps-eingefroren, Bins vorhanden) — exakt das `slow_active`-Gate der
Learner-Hooks. Ist er inaktiv, zeigt das Diagramm die rein statische
Verschattung, genau wie die servierte Prognose.

Berechnet wird das Profil über `coordinator.build_shade_profile_for`; das
Ergebnis wird auf `(Modul, Datum, slow_active, Shademap-Objekt)` memoisiert, sodass der O(Azimut × Elevation)-Scan je Änderung einmal läuft und
nicht je 15-min-Tick.

### §17.3 Berechnung und Tunables

`core/shadeprofile.py` ist pur und HA-frei. Sonnenbahn: der lokale Tag in
`SHADE_PROFILE_STEP_MINUTES`-Schritten, nur Samples mit Elevation > 0.
Horizontlinien: Azimut-Raster `SHADE_PROFILE_AZ_STEP_DEG` über die
Tageslicht-Azimutspanne; die gelernte Verschattungshorizont-Linie ist je Azimut
die höchste Elevation mit effektivem τ < `SHADE_PROFILE_TAU_THRESHOLD`,
gescannt in `SHADE_PROFILE_EL_SCAN_DEG`-Schritten. Der Day-of-Year
(Halbjahres-Split und Laub-Rampe) stammt vom **lokalen Kalenderdatum** — eine
dokumentierte Näherung, die für CET/CEST mit der Engine identisch ist.

## §18 Dashboard und mitgelieferte Lovelace-Karten

### §18.1 Referenz-Dashboard (nur Bordmittel)

`dashboards/balcony_solar_forecast.yaml` ist ein Lovelace-View-YAML **nur mit
Bordmitteln**, das ohne Custom-Cards funktioniert: History-Graph Motor-Gesamt vs.
gemessen (und je Ebene, wo praktikabel), Entities-Card für Lernstatus,
Drift-MAE, Quellenstatus und Kill-Gate, ein Gauge für
`vs_best_baseline_pct`, ein Markdown mit dem Kill-Gate-Verdikt sowie eine
kompakte Shademap-Transmittanz-Tabelle je Kanal (Template/Markdown) **plus** dem
Hinweis, dass `dump_shademap` die rohen Polardaten für einen reicheren Plot
liefert. Installationsschritte in `docs/DASHBOARD.md`.

### §18.2 Aktion `install_dashboard`

Statt das Referenz-YAML zu kopieren und Objekt-IDs von Hand anzupassen, richtet
`balcony_solar_forecast.install_dashboard` das Dashboard mit den **echten
Entity-IDs dieser Installation** ein. Der Betreiber legt **einmal** über die UI
ein leeres Dashboard an und ruft dann die Aktion auf.

Die reine Konfig-Erzeugung liegt in `_dashboard.py` (HA-frei, bare unit-getestet).
Sie spiegelt die Karten des mitgelieferten YAML, ersetzt das opt-in-ApexCharts-
Snippet durch die gebündelte `custom:balcony-shade-profile-card` (§18.3), bettet
an Stelle des per-Modul-History-Graphs die `custom:balcony-power-history-card`
(§18.4) ein und **lässt Karten und Zeilen mit fehlenden Entitäten weg**, sodass
eine Teilinstallation weiterhin rendert. Die IDs stammen aus der Entity-Registry
(`{entry_id}_{key}` → reale `entity_id`), die Vergleichs-MAE-Zeilen und die
gemessenen Modul-Sensoren aus Coordinator und Site-Config.

**Schreibweg (verbindlich):** ausschließlich über die vorhandene
`LovelaceStorage.async_save(config)` des jeweiligen `url_path` aus
`hass.data[LOVELACE_DATA].dashboards` — **nie** über einen neuen
Dashboard-Registry-Eintrag oder eine zweite `DashboardsCollection` (die beim
späteren UI-Bearbeiten Einträge löschen könnte).

**Marker und Safety-Gate:** jede geschriebene Konfig trägt oben
`bsf_managed: <version>`. Ist das Ziel-Dashboard **nicht im Storage-Modus**, wird
abgelehnt (YAML nicht schreibbar). Trägt eine bereits vorhandene, **nicht-leere**
Konfig den Marker **nicht** und ist `overwrite` nicht gesetzt, wird abgelehnt
(kein Überschreiben fremd erstellter Dashboards). Eine leere oder
marker-tragende Konfig wird frei überschrieben — das ist der **idempotente
Refresh**. Die Antwort meldet `dashboard`, `views`, `cards` und die
weggelassenen `missing_entities`.

### §18.3 Verschattungsprofil-Karte

`custom:balcony-shade-profile-card` (`frontend/shade_profile_card.js`) ist
**abhängigkeitsfrei**: vanilla `HTMLElement` plus programmatisch erzeugtes SVG,
keine HACS-Frontend-Installation nötig. Sie zeichnet die Sonnenbahn aus den
Attributen von §17.1:

- **jahresstabile x-Achse** aus `axis_azimuth_min`/`_max`, defensiv mit der
  Tages-Datenspanne vereinigt, sodass die Bahn über Datumswechsel vergleichbar
  bleibt statt saisonal umzuskalieren;
- **Hover-Cursor** (SVG-Overlay, Maus/Touch): Fadenkreuz am nächstgelegenen
  Bahn-Sample plus feste Ablese-Zeile mit Uhrzeit, Azimut samt Himmelsrichtung,
  Verschattung in % (τ), Elevation und `· n=<x>`;
- **Confidence-Skalierung** nach `sample_n`: n = 0 (nur statischer Prior) als
  kleiner **hohler** Ring in der τ-Farbe, n > 0 als gefüllter Punkt, dessen
  Radius mit der Evidenz bis zur Sättigung bei `N_SAT` wächst;
- **Gruppen-/Einzelsicht-Umschalter**, sobald `transmittance_individual` nicht
  leer ist (also die Ebene gruppiert ist);
- **karten-lokaler Vergleichsdatums-Wähler** („Vergleich", per × löschbar), der
  eine zweite Sonnenbahn desselben Moduls für ein anderes Datum als gestrichelte
  Linie mit hohlen τ-Ringen einblendet (deren Verschattungshorizont wird bewusst
  nicht gezeichnet). Er **ändert nie die geteilte Datums-Entität**; die Daten
  liefert die rein lesende Aktion `get_shade_profile`, aufgerufen über das
  stabile Low-Level-Websocket-`call_service` mit `return_response`.

### §18.4 Power-History-Karte

`custom:balcony-power-history-card` (`frontend/power_history_card.js`) zeigt im
Stil des Energie-Dashboards **gestapelte stündliche Balken der gemessenen
Produktion je Modul** (aus den Recorder-Stundenstatistiken der
`actual_entity`-Sensoren), überlagert von einer **gestrichelten Prognoselinie**,
mit einem **Hover-Panel** je Stunde (Werte je Modul, Gesamt, Prognose). Die
Modulkanäle werden über `sources` / `source_names` von
`measured_dc_power_total` **auto-discovered** (keine YAML-Konfiguration der
Kanäle nötig). Sie aktualisiert sich alle **5 min**, aber **nur in der
Live-Ansicht**; eine Vergangenheits-Ansicht ist statisch.

**Tages-/Wochennavigation** (karten-lokal, nicht persistiert): eine Kopfzeile
`◀ [Label] ▶` blättert den gewählten Tag (▶ deaktiviert am heutigen Tag); ein
**Tag|Woche**-Umschalter zeigt eine Wochenansicht mit sieben gestapelten
Tagesbalken (aus `period: "day"`-Mittelwertstatistiken, Mittel-W × 24 h =
Tages-Wh; das Fenster endet am gewählten Tag und springt in 7-Tages-Schritten).

**Vergangene Tage: as-issued statt Nachrechnen.** Im Tagesmodus zeigt die
gestrichelte Linie für vergangene Tage die Prognose **wie ausgegeben** aus dem
90-Tage-Ring (§16.2), gelesen über die schreibgeschützte Aktion
`get_issued_forecast`. Das ist der **eingefrorene ~01:30-Stand ohne Rückschau**,
nie aus dem heutigen Lernstand nachgerechnet, sodass der Vergleich „ausgegeben
vs. gemessen" ehrlich bleibt. Fehlt ein archivierter Snapshot, **entfällt die
Linie** mit explizitem, datiertem Hinweis (inklusive „Archiv seit <Datum>" aus
dem `oldest_available`-Feld der Antwort); eine gezeichnete Linie trägt eine
Herkunfts-Beschriftung („Prognose (live)" vs. „Prognose (Stand 01:30)"). Die
Wochenansicht überlagert je Tagesspalte eine gestrichelte Markierung auf Höhe der
Prognose-Tagessumme: vergangene Tage aus dem Ring, der heutige Tag aus der
Live-`wh_period`-Summe, Tage ohne Snapshot bleiben **ehrlich lückenhaft**.

**Antwortvertrag `get_issued_forecast`** (gilt auch für §16.2 und §19):
`hourly_wh` (bedient/korrigiert) und `raw_hourly_wh` sind **explizit DC**.
Zusätzlich liefert die Aktion `hourly_wh_ac` = DC × `eta`, wobei `eta` die
DC→AC-Effizienz **zum Ausgabezeitpunkt** ist (in den Snapshot eingefroren,
`eta_source: "snapshot"`); ältere Snapshots ohne gespeichertes eta fallen auf das
**aktuelle** gelernte eta zurück und weisen das über `eta_source: "current"` aus
(eine Site-Skalare — Datenblatt-Default, bis die Kalibrierung vertraut ist;
per-Gruppen-Overrides werden nicht abgebildet). `cloud_class_by_hour`
(Day-ahead-Wetterklasse je Stunde) und `applied_factor_by_hour`
(`hourly_wh / raw_hourly_wh`, Stunden mit raw ≈ 0 ausgelassen) machen die
angewandte Korrektur sichtbar. Ein Fehltreffer ist **kein Fehler**.

### §18.5 Auslieferung und Registrierung

Beide Karten werden unter dem gemeinsamen statischen Prefix
`/balcony_solar_forecast/frontend/` ausgeliefert und in `_frontend._CARDS`
geführt. Im **Lovelace-Storage-Modus** werden sie beim Start automatisch je als
Dashboard-Ressource (Modul-Typ) registriert, sodass sie direkt im Kartenwähler
erscheinen; jede Ressourcen-URL ist per `?v=<INTEGRATION_VERSION>`
cache-gebustet (der einzige Cache-Busting-Mechanismus). Im **YAML-Modus** wird
statt der Registrierung ein INFO-Hinweis mit den manuell einzutragenden
Ressourcenzeilen geloggt.

Die Registrierung ist ein Zusatznutzen, **nie ein Setup-Blocker**: jeder Fehler
wird geschluckt und protokolliert. Der opt-in-ApexCharts-Pfad
(`dashboards/shade_profile_apexcharts.yaml`, `custom:apexcharts-card`) bleibt das
einzige bewusst optionale HACS-Artefakt; Details in `docs/DASHBOARD.md`.

## §19 Aktionen (Services): Inventar, Registrierung, Lese-/Schreibgrenze

Die Aktionen sind über die SPEC verteilt bei ihrem jeweiligen Thema definiert;
dieser Abschnitt ist das **vollständige Inventar** und der Einstieg. Er
beschreibt keine eigene Semantik — bei Abweichung gilt der verlinkte
Fachabschnitt.

**Registrierung (verbindlich):** alle Aktionen werden **einmal in `async_setup`**
registriert (`_services.async_register_services`, HA-Quality-Scale
`action-setup`) — unabhängig vom Ladezustand eines Config-Entries. Ein Aufruf
läuft dadurch nie in „Service not found"; jeder Handler löst sein Ziel erst zur
Aufrufzeit aus `hass.data[DOMAIN]` auf. Jede Aktion nimmt ein optionales
`entry_id`. Für die Ziel-Auflösung gibt es genau **zwei** Muster, und sie
unterscheiden sich im Fehlerbild:

- **Einzelziel-Aktionen** (`import_bootstrap`, `rollback_learners`,
  `reset_day_ahead_bias`, `install_dashboard`, `suggest_shade_groups`,
  `get_shade_profile`, `get_issued_forecast`, `run_bootstrap`) lösen über
  `_resolve_single_coordinator` genau **einen** Coordinator auf und verlangen bei
  mehreren Anlagen ein explizites `entry_id`. Das betrifft **alle schreibenden**
  Aktionen und zusätzlich die lesenden, die sich auf genau eine Anlage beziehen
  müssen. Kein Entry eingerichtet, unbekanntes `entry_id` oder mehrere Anlagen
  ohne `entry_id` ⇒ `ServiceValidationError` mit Klartext, nie ein Traceback.
- **Fan-out-Leser** (`get_forecast`, `dump_shademap`) lösen **nicht** auf: sie
  iterieren über `hass.data[DOMAIN]` und antworten je Anlage unter
  `entries[<entry_id>]`; ein übergebenes `entry_id` wirkt hier als **Filter**,
  nicht als Pflichtangabe. Ist kein Entry eingerichtet (oder filtert `entry_id`
  alles weg), ist die Antwort das **leere** `{"entries": {}}` — **kein Fehler**.
  Ein Read oder Dump darf ein Dashboard oder eine Diagnose nicht mit einer
  Exception abbrechen. `dump_shademap` kapselt zusätzlich **jeden Entry einzeln**
  (`_services._dump_one`): ein Coordinator ohne `get_shademap_state` liefert
  `{"channels": {}, "available": false}`, eine Ausnahme beim Auslesen
  `{"channels": {}, "error": <repr>}` **innerhalb** der Antwort statt als
  geworfener Fehler. Konsumenten dieser beiden Aktionen prüfen also auf eine
  leere bzw. je Entry als fehlerhaft markierte Antwort, nicht auf eine Exception.

Die Antwortspalte nennt den `SupportsResponse`-Modus (`SupportsResponse.ONLY`
bzw. `OPTIONAL`).

| Aktion | Antwort | Wirkung | Definition |
|---|---|---|---|
| `get_forecast` | `ONLY` | servierte Kurve (15-min + stündlich), Bänder nur wenn vorhanden — inkl. `band_source`/`band_source_by_day` | §14.4, §11.2 |
| `get_issued_forecast` | `ONLY` | eingefrorene **as-issued**-Tageskurve aus dem 90-Tage-Ring (DC + `hourly_wh_ac`, `cloud_class_by_hour`, `applied_factor_by_hour`); Fehltreffer ist kein Fehler | §18.4, §16.2 |
| `get_shade_profile` | `ONLY` | Verschattungsprofil für Modul/Datum **ohne** Änderung der Live-Auswahl und ohne den Memo zu verdrängen | §17, §18.3 |
| `dump_shademap` | `ONLY` | gelernte Schattenkarte als Polartabelle je Kanal (visuelle Prüfung gegen bekannte Hindernisse) | §9.1 |
| `suggest_shade_groups` | `ONLY` | datengetriebener Gruppierungsvorschlag + Ähnlichkeitsmatrix + aktuelle Gruppierung | §9.3 |
| `run_bootstrap` | `ONLY` | In-Process-Re-Bootstrap mit der Live-Config; **`dry_run` Default `true`** | §12.2 |
| `import_bootstrap` | `OPTIONAL` | Import eines offline erzeugten Bootstrap-Payloads (additiv, mit Rollback-Snapshot) | §12.5 |
| `rollback_learners` | `OPTIONAL` | Bias + Shademap + Quantile gemeinsam aus dem Snapshot-Ring zurücksetzen (nur Zustand, keine Schalter) | §9.8, §16.2 |
| `reset_day_ahead_bias` | `OPTIONAL` | alle gelernten θ-Zellen löschen; die Kurve fällt sofort auf Physik + Shademap | §9.5 |
| `install_dashboard` | `OPTIONAL` | Observability-Dashboard mit den echten Entity-IDs schreiben (Marker-Gate, idempotent) | §18.2 |

**Lese- vs. Schreibgrenze:** die `ONLY`-Aktionen sind — mit Ausnahme von
`run_bootstrap` mit `dry_run: false` — **rein lesend** und dürfen weder
Lernzustand noch Auswahl noch Store verändern. Die beiden Aktionen, die
Lernzustand **ersetzen** (`import_bootstrap` und `run_bootstrap` im
Import-Modus), legen vorher einen Rollback-Snapshot ab; `rollback_learners` liest
aus demselben Ring. `reset_day_ahead_bias` ist bewusst ein **gezielter** Eingriff:
es löscht nur die θ-Zellen und lässt Schalter, Shademap, Drift-Zustand und den
Rollback-Ring unberührt.

## §20 Konventionen und Inbetriebnahme-Checkliste

### §20.1 Azimut

Es gibt genau **eine interne Konvention: 0 = Nord, im Uhrzeigersinn**
(90 = Ost, 180 = Süd, 270 = West). Sie gilt überall im Kern, in der
Konfiguration und in dieser SPEC. Umgerechnet wird **ausschließlich an API- und
Importgrenzen**, und **jede** solche Grenze trägt einen eigenen Unit-Test.

| Kontext | Konvention |
|---|---|
| Standort / SPEC / intern | **0 = N, 90 = O** |
| Open-Meteo GTI-Parameter | 0 = S, −90 = O |
| PVGIS `printhorizon` | 0 = S, −90 = O |

Keine dieser Fremdkonventionen schlägt in den Kern durch: ein Wert, der die
Grenze passiert, ist danach 0 = Nord.

### §20.2 Neigung

Neigung ist gegen die **Horizontale** gemessen: 0 = flach liegend, **90 =
senkrecht** (fassadenparallel).

### §20.3 Inbetriebnahme-Checkliste (klarer Tag)

Pflicht nach der Erstkonfiguration und nach **jeder** Geometrieänderung. An
einem klaren Tag prüfen:

1. **Peak-Zeiten je Ebene** in plausibler Reihenfolge (Ost-Ebenen früh, Süd um
   den wahren Mittag, West-Ebenen am Nachmittag).
2. **Nachmittags-Cutoff sichtbar**, wo ein Hindernis konfiguriert ist.
3. **Modellierte vs. gemessene Kanalleistung** an 2–3 Sonnenständen innerhalb
   **±20 %**.
4. **Kein Output nachts**, plausible Winterflaute.

## §21 Qualitätssicherung: Referenzvektoren, Anker und maschinelle Wächter

**Golden-Tests gegen pvlib-Referenzvektoren** über alle Ebenen — inklusive
Tiefstand 2–10° und der Konventionsgrenzen — sind **Merge-Blocker**. Die
Vektoren werden **außerhalb** dieses Repos in einem Wegwerf-venv mit
pvlib/pandas erzeugt und als `tests/core/reference_vectors.json` eingecheckt;
pvlib und pandas sind **niemals** Laufzeitabhängigkeiten (§2).

**Sonnenstands-Anker:** gegen PVGIS verifizierte Referenzwerte mit dem
Genauigkeitsziel < 0,3° aus §4.1, plus das Tiefstands-/Nachtverhalten. Die
verbindlichen Anker gelten für die Breite des Auslieferungs-Defaults (§7.8,
≈ 48,55° N) und sind die Mittags-Elevationen der beiden Sonnenwenden:
**Sommersonnenwende 64,9° ± 0,4** und **Wintersonnenwende 18,0° ± 0,4**; dazu
die Konventionsanker Mittagsazimut ≈ 180° und Juni-Sonnenaufgangsazimut im
NO-Quadranten (0 = Nord, §20.1). Die Toleranz ist absichtlich weiter als das
Genauigkeitsziel, weil die Anker durch Abtasten der Tageskurve bestimmt werden.

**HA-Freiheit des Kerns:** `tests/core/` importiert die Kernmodule direkt aus
ihren Dateien und läuft mit bare pytest ohne HA — die Invariante aus §2 ist damit
testbar formuliert, nicht nur behauptet.

**Byte-Identitäts-Regressionen:**

- eine Alt-Config ergibt nach dem Upgrade ein identisches `to_dict()` (§7.6);
- eine Horizontzeile ohne `tau_points` / `diffuse_tau` verhält sich wie vor deren
  Einführung (§5.1);
- die Bootstrap-Dicts aus CLI und In-Process-Aktion sind byte-identisch (§12.4).

**Epoch-Größentest** gegen den Sekunden-vs-Millisekunden-Fehler (§9.7) ist
regressionsgetestet.

**SPEC-Integritätswächter** (`tests/test_spec_integrity.py`):

- (a) jede `SPEC §x.y`-Zitierung in `custom_components/`, `tests/`, `scripts/`,
  `dashboards/` und `docs/` löst auf eine reale Überschrift dieses Dokuments auf;
- (b) jede Aktion aus `services.yaml` ist hier benannt;
- (c) jedes öffentliche `site`-Feld aus `const.py` ist hier benannt;
- (d) jeder Top-Level-Abschnitt ist über die Wegweisertabelle (§1.2) erreichbar;
- (e) jeder `docs/…`-Pfad in getrackten Dateien zeigt auf eine **getrackte**
  Datei;
- (f) jede `ISSUE_*`-Id ist hier benannt, in **beiden** ausgelieferten Sprachen
  übersetzt und öffnet exakt die Platzhalter, die
  `ISSUE_TRANSLATION_PLACEHOLDERS` deklariert;
- (g) der `async_setup`-Docstring nennt **Zahl und Namen** aller Aktionen;
- (h) der **Versionsstempel** im Kopf stimmt mit `const.INTEGRATION_VERSION`
  überein;
- (i) dieses Dokument führt **keine** als historisch markierten Abschnitte —
  Historie gehört nach `docs/HISTORIE.md` (§1.3).

`ruff check .` und die vollständige Testsuite sind **grün vor jedem Commit**.
