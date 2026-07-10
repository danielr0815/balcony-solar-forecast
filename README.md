# Balcony Solar Forecast

Selbstlernende Mehrebenen-PV-Prognose für Home Assistant — gebaut für
Balkonkraftwerke mit mehreren Modulausrichtungen, starker
Standortverschattung (Gelände, Bäume, Gebäude) und Mikrowechselrichtern
mit Port-genauen Messwerten.

**Status: v0.5.0** — selbstlernende PV-Prognose im Betrieb: Physik-Motor mit
lokaler Transposition, zwei Lernschichten (Intraday-Bias + Shademap),
Drift-Überwachung, P10/P50/P90-Quantilbänder und ein Skill-Scoreboard.
Versionshistorie in [CHANGELOG.md](CHANGELOG.md), vollständige Spezifikation in
[docs/SPEC.md](docs/SPEC.md).

## Installation

### HACS (empfohlen)

1. In Home Assistant **HACS → Integrationen** öffnen.
2. Über das Drei-Punkte-Menü **Benutzerdefinierte Repositories** wählen.
3. URL `https://github.com/danielr0815/balcony-solar-forecast` eintragen,
   Kategorie **Integration**, dann **Hinzufügen**.
4. Das neue Repository in der Liste öffnen und **installieren**.
5. Home Assistant **neu starten**.
6. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach
   *Balcony Solar Forecast* suchen und einrichten.

### Manuell (Fallback)

Den Ordner `custom_components/balcony_solar_forecast` aus diesem Repository in
das `custom_components/`-Verzeichnis der Home-Assistant-Konfiguration kopieren,
Home Assistant neu starten und die Integration wie oben hinzufügen.

## Konfiguration

Die Einrichtung läuft vollständig über den **Config-Flow** (UI, kein YAML). Beim
Hinzufügen der Integration werden abgefragt:

- **Name** der Instanz,
- **Koordinaten** (Breite/Länge des Standorts),
- **Intervalle** für Datenabruf und Neuberechnung,
- das **Site-Objekt**: die Modul-**Ebenen** (Azimut, Neigung, Wp), die
  **Horizont**-Profile je Ebene und die **Wechselrichter-Gruppen** mit ihren
  Mess-Entitäten. Das mitgelieferte Referenz-Setup ist als **editierbarer
  Default** vorbelegt — Vorlage und Testfall, kein Zwang.

Nachträglich lassen sich über **Konfigurieren** (Optionen) anpassen:

- die **Lernschalter** (schneller Bias-Lerner, langsamer Shademap-Lerner,
  Day-ahead-Bias),
- die **Quantilbänder** (P10/P50/P90) an- oder abschalten,
- die **Vergleichssensoren** für das Skill-Scoreboard.

Ergänzende Anleitungen:

- **[docs/DASHBOARD.md](docs/DASHBOARD.md)** — fertiges Observability-Dashboard
  aus Bordmitteln, inklusive Verschattungsprofil-Diagramm.
- **[docs/BACKFILL.md](docs/BACKFILL.md)** — optionaler Bootstrap der beiden
  Lernschichten aus ~2 Jahren historischer Daten (einmaliger Dev-Job, läuft
  nicht auf Home Assistant).

## Kernidee

- **Rohstrahlung statt Fertigprognose:** GHI/DNI/DHI von Open-Meteo
  (frei, 1 API-Call), lokale Hay-Davies-Transposition je Modulebene —
  statt isotroper Server-GTI, die auf steilen Ebenen 6–12 % daneben liegt.
- **Horizont richtig:** je Ebene ein Profil (Azimut, Elevation,
  Transmittanz) — Fernfeld aus PVGIS, Nahfeld vom Betreiber; Direktstrahl
  UND Diffusanteil (Sky-View-Faktor) werden korrigiert.
- **Selbstlernend:** zwei Zeitskalen — ein geometrisches
  Transmissionsfeld je Messkanal × Sonnenstand (lernt Hang, Bäume,
  Gebäudekante) und ein Intraday-Bias-Korrektor (rettet Nebelmorgen).
  Alles geclamped, driftüberwacht, abschaltbar.
- **Verschattung sichtbar:** für ein wählbares Modul und Datum zeigt ein
  Diagramm die Sonnenbahn (Elevation über Azimut) mit der aktuell gelernten
  Verschattung (Transmission τ, eingefärbt) und den Horizontlinien —
  Bedienelemente (Modul/Datum) und Rohdaten als Entitäten der Integration,
  das Diagramm selbst als optionale ApexCharts-Karte
  (siehe [docs/DASHBOARD.md](docs/DASHBOARD.md)).
- **Keine schweren Abhängigkeiten:** stdlib-only zur Laufzeit
  (kein numpy/pandas/pvlib), HA-freier Kern mit Golden-Tests.
- **Standard-Schnittstellen:** kWh-Sensoren (kompatibel zu bestehenden
  Konsumenten), 15-min-Kurve als Service-with-Response und
  Energy-Dashboard-Hook.

## Entwicklung

Beiträge willkommen. Konventionen, Test-Architektur und Release-Prozess stehen
in **[CONTRIBUTING.md](CONTRIBUTING.md)** — insbesondere: der Code ist bewusst
handformatiert (`ruff format` wird **nicht** genutzt) und [docs/SPEC.md](docs/SPEC.md)
ist der verbindliche Vertrag. Dev-Umgebung aufsetzen: `make install`; Tests:
`make test` (Kern-Tests: `make test-core`).

## Lizenz

MIT — siehe [LICENSE](LICENSE).
