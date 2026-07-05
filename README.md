# Balcony Solar Forecast

Selbstlernende Mehrebenen-PV-Prognose für Home Assistant — gebaut für
Balkonkraftwerke mit mehreren Modulausrichtungen, starker
Standortverschattung (Gelände, Bäume, Gebäude) und Mikrowechselrichtern
mit Port-genauen Messwerten.

**Status: v0.1.0 implementiert** (Phase 1 — reiner Physik-Motor, 1085
Tests grün, pvlib-Golden-Vektoren, adversarial reviewed) — als Nächstes:
Deploy + 14-Tage-Parallellauf gegen die 8-Entry-Baseline (Kill-Gate,
SPEC §9). Die vollständige Spezifikation steht in
[docs/SPEC.md](docs/SPEC.md).

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
- **Keine schweren Abhängigkeiten:** stdlib-only zur Laufzeit
  (kein numpy/pandas/pvlib), HA-freier Kern mit Golden-Tests.
- **Standard-Schnittstellen:** kWh-Sensoren (kompatibel zu bestehenden
  Konsumenten), 15-min-Kurve als Service-with-Response und
  Energy-Dashboard-Hook.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
