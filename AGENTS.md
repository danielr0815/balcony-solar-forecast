# AGENTS.md — Einstieg für Coding-Agenten

Home-Assistant-**Custom-Integration** (HACS, Domain `balcony_solar_forecast`),
kein PyPI-Paket: HA lädt den Code aus `custom_components/`. Reiner Python-Code,
keine Runtime-Dependencies (`requirements: []` im Manifest), stdlib-only im Kern.

**Verbindlich lesen:** [CLAUDE.md](CLAUDE.md) (10 harte Regeln) und vor größeren
Änderungen [docs/project-knowledge/](docs/project-knowledge/) (00 = Index/Einstieg,
07 = Entwicklung/Tests/Release). Der Vertrag fürs Verhalten ist
[docs/SPEC.md](docs/SPEC.md) (deutsch) — jede Verhaltensänderung zieht sie im
selben PR nach.

## Projektstruktur

- `custom_components/balcony_solar_forecast/core/` — reiner Rechenkern ohne
  HA-Imports (Physik, Lerner, Scoreboard, Quantile). Läuft überall (auch Windows).
- `custom_components/balcony_solar_forecast/` (Rest) — HA-Schicht: Coordinator,
  Config Flow, Entities, Services, zwei gebündelte Lovelace-Karten.
- `tests/` — HA-Layer-Tests (gegen Fakes); `tests/core/` — reine Kern-Tests;
  `tests/harness/` — Node-Runtime-Harness für die Power-Card (skippt ohne node).
- `tests/test_spec_integrity.py` — der maschinelle SPEC-Vertrag (siehe unten).

## Setup (einmalig)

```bash
uv sync --group dev        # erzeugt .venv aus uv.lock (einzige Wahrheit,
                           # auch in CI); installiert Python 3.14 bei Bedarf
```

Alternativen: `make install` (dünne Hülle um uv), Devcontainer
(`.devcontainer/` — postCreateCommand macht dasselbe, inkl. Node-Feature für
den JS-Harness), oder `scripts/setup-env.sh` / `scripts/setup-env.ps1`
(installieren uv, falls fehlend). Python-Version steht in `.python-version`.

## Tests

```bash
uv run pytest tests -p no:homeassistant        # volle Suite (läuft überall)
uv run pytest tests/core -p no:homeassistant   # nur der HA-freie Kern
```

**`-p no:homeassistant` ist Pflicht**, überall (Makefile, CI, Devcontainer):
das PHACC-Plugin (pytest-homeassistant-custom-component) zieht autouse-Fixtures
mit `asyncio.get_event_loop()` (wirft ab Python 3.12 für sync-Tests) und
POSIX-only `fcntl` — kein Test benutzt es, es würde die Suite nur brechen.
pytest-asyncio (via PHACC installiert) treibt die async Tests weiterhin.
Kein zweites `-q` auf der Kommandozeile: `pyproject.toml` setzt bereits
`addopts = "-q"`, ein weiteres verschluckt die Ergebniszeile.

## Lint, Typen (vor jedem Push)

```bash
uv run ruff check .     # bzw. `uv run ruff check --fix .` zum Anwenden
uv run mypy             # Baseline: prüft core/, siehe [tool.mypy]
```

- **`ruff format` ist VERBOTEN** — der Code ist absichtlich handformatiert
  (alignierte Argumente, bewusste Zeilenumbrüche, `E501` aus). Kein
  Formatter in pre-commit, CI oder Editor (format-on-save ist in
  `.vscode/settings.json` explizit aus). Nur die Zeilen anfassen, die die
  eigene Änderung braucht.
- Dev-Versionen sind **Mindestversionen** (`>=`) in pyproject
  `[dependency-groups]`; die exakten stehen im `uv.lock` — Updates via
  Dependabot oder `uv lock --upgrade` (im PR prüfen). Einzige Ausnahme mit
  Voll-Pin: `pytest-homeassistant-custom-component`, weil es homeassistant
  selbst exakt pinnt und damit die HA-Kopplung führt (Begründung im
  pyproject-Kommentar).
- mypy ist bewusst eine Baseline (nur `core/`; acht ältere Module mit
  bekannten Fehlern sind im pyproject-Kommentar namentlich ausgenommen, die
  übrigen neun — inkl. `engine.py` — müssen sauber bleiben). Scope erweitern,
  bevor Regeln verschärft werden.
- Optional: `pre-commit install` — der Hook (`.pre-commit-config.yaml`,
  nur `ruff-check --fix`) spiegelt die im Lockfile fixierte ruff-Version.

## Versionierung (CI-Gates, nicht brechen!)

Version steht an **drei Stellen**, die immer gleich sein müssen:

- `custom_components/balcony_solar_forecast/manifest.json` → `version`
- `pyproject.toml` → `[project] version`
- `custom_components/balcony_solar_forecast/const.py` → `INTEGRATION_VERSION`

Die CI bricht bei Drift, der Release-Guard prüft zusätzlich gegen den Git-Tag.
Version nur im Release-PR anfassen (HACS liefert den Zipball des Tags aus).

**SPEC-Vertrag:** `tests/test_spec_integrity.py` (läuft in der normalen Suite,
bricht den Build) erzwingt: `SPEC §…`-Zitate im Code müssen auflösen, jede
Aktion aus `services.yaml` und jedes `site`-Config-Feld aus `const.py` muss in
der SPEC stehen, der §1.2-Wegweiser muss vollständig sein, und der
Kopfstempel „Gilt für Version" muss zu `INTEGRATION_VERSION` passen. Dazu der
advisory `spec-reminder`-CI-Job bei PRs.

## Konventionen

- Kommentare dokumentieren das **Warum** (Vorfall/Review, der eine Logik
  motiviert hat) — bitte so weiterführen. Belege mit Datei +
  Funktions-/Konstantenname, **nie mit Zeilennummern**.
- Kleine, fokussierte Änderungen; jede Änderung mit Test, der sie beweist
  (neue Tests müssen den alten Code durchfallen lassen — CLAUDE.md Regel 6).
- Voller Workflow: [CONTRIBUTING.md](CONTRIBUTING.md).
