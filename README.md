# Balcony Solar Forecast

Selbstlernende Mehrebenen-PV-Prognose für Home Assistant — gebaut für
Balkonkraftwerke mit mehreren Modulausrichtungen, starker
Standortverschattung (Gelände, Bäume, Gebäude) und Mikrowechselrichtern
mit Port-genauen Messwerten.

**Status: v0.5.0** (v0.1.0 live deployed — Phase 1, reiner Physik-Motor im
14-Tage-Parallellauf gegen die 8-Entry-Baseline; **v0.2.0 + v0.3.0 fertig
implementiert** — Phase 2/3 mit beiden Lernschichten: schneller Intraday-/
Day-ahead-Bias-Lerner und langsamer Shademap-Lerner, voll in den Motor
verdrahtet, plus Drift-Monitor mit Auto-Abschaltung, Kollaps-Detektor,
Rollback-Ring, nächtliche Stunden-Ist-Werte, Catch-up nach Ausfall und
Previous-Runs-Backfill; **v0.4.0 fertig** — Phase 4: Skill-Scoreboard
(Kill-Gate: Motor-vs-Baseline-vs-Ist, stratifiziert, leckfrei „as issued"),
P10/P50/P90-Quantilbänder (historische Simulation im Service/Attributen) und
ein Observability-Dashboard mit Bordmitteln; alles geclamped, gegated und
abschaltbar, SPEC §5–§10, §14; **v0.5.0** — Verschattungs-Diagramm je Datum &
Modul (Sonnenbahn vs. aktuell gelernte Verschattung) plus reproduzierbare
Dev-Umgebung (venv/make) und GitHub-Actions-CI). Die vollständige Spezifikation
steht in [docs/SPEC.md](docs/SPEC.md); Dashboard-Installation in
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

Die Integration ist ein Home-Assistant-Custom-Component (wird aus
`custom_components/` geladen, nicht `pip install`ed) und hat **keine
Laufzeit-Abhängigkeiten** (`requirements: []`). Für Tests und Linter gibt es eine
lokale venv — Setup identisch zum
[battery-manager-ha](https://github.com/danielr0815/battery-manager-ha):
Home Assistant, pytest, pytest-homeassistant-custom-component und ruff aus der
`[dependency-groups] dev` in [pyproject.toml](pyproject.toml). Home Assistant ist
ungepinnt; `pytest-homeassistant-custom-component` legt die passende HA-Version
fest (aktuell **HA 2026.2.3**).

### Umgebung aufsetzen (neuer Rechner, Linux oder Windows)

```bash
make install          # legt ./.venv an und installiert die Dev-Tools
```

Ohne `make` — dasselbe über das plattformübergreifende Bootstrap-Skript:

```bash
# Linux / macOS / WSL
./scripts/setup-env.sh            # (oder: bash scripts/setup-env.sh)

# Windows (PowerShell)
.\scripts\setup-env.ps1
```

Beide rufen [`scripts/setup_env.py`](scripts/setup_env.py) auf (reine
Standardbibliothek) und funktionieren auf einem frischen Rechner vor jeder
Installation. Die venv-Python-Version kommt vom Bootstrap-Interpreter
(`make`/Windows nutzen `py -3.13`, POSIX `python3`; `requires-python >= 3.13`).

### Tests & Linter

```bash
make test        # volle Suite (plattformunabhängig, PHACC-Plugin deaktiviert)
make test-core   # nur der reine Kern (ohne Home Assistant)
make lint        # ruff check
make format      # ruff check --fix
make clean       # venv entfernen
```

> **Warum `-p no:homeassistant`?** Die Suite ist Unit-Test-artig: jeder
> HA-Schicht-Test läuft gegen Fakes/Monkeypatch und braucht nur
> `import homeassistant`, keine echte HA-Instanz. Die autouse-Fixtures von
> `pytest-homeassistant-custom-component` rufen beim Setup
> `asyncio.get_event_loop()` auf (wirft auf Python 3.12+ bei den sync-Tests) und
> der Import zieht das POSIX-only `fcntl` (auf Windows nicht importierbar) — das
> Plugin bricht also nur eine Suite, die seine Fixtures nie nutzt. Deaktiviert
> läuft die **volle** Suite überall gleich (`pytest-asyncio` treibt die
> async-Tests weiter). Genau das macht `make test` und der CI-`tests`-Job.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
