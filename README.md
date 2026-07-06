# Balcony Solar Forecast

Selbstlernende Mehrebenen-PV-Prognose für Home Assistant — gebaut für
Balkonkraftwerke mit mehreren Modulausrichtungen, starker
Standortverschattung (Gelände, Bäume, Gebäude) und Mikrowechselrichtern
mit Port-genauen Messwerten.

**Status: v0.4.0** (v0.1.0 live deployed — Phase 1, reiner Physik-Motor im
14-Tage-Parallellauf gegen die 8-Entry-Baseline; **v0.2.0 + v0.3.0 fertig
implementiert** — Phase 2/3 mit beiden Lernschichten: schneller Intraday-/
Day-ahead-Bias-Lerner und langsamer Shademap-Lerner, voll in den Motor
verdrahtet, plus Drift-Monitor mit Auto-Abschaltung, Kollaps-Detektor,
Rollback-Ring, nächtliche Stunden-Ist-Werte, Catch-up nach Ausfall und
Previous-Runs-Backfill; **v0.4.0 fertig** — Phase 4: Skill-Scoreboard
(Kill-Gate: Motor-vs-Baseline-vs-Ist, stratifiziert, leckfrei „as issued"),
P10/P50/P90-Quantilbänder (historische Simulation im Service/Attributen) und
ein Observability-Dashboard mit Bordmitteln; alles geclamped, gegated und
abschaltbar, SPEC §5–§10, §14). Die vollständige Spezifikation steht in
[docs/SPEC.md](docs/SPEC.md); Dashboard-Installation in
[docs/DASHBOARD.md](docs/DASHBOARD.md).

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
