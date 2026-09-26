# Contributing to Balcony Solar Forecast

Thanks for your interest! This is a Home Assistant **custom integration** (not a
PyPI package): Home Assistant loads it from `custom_components/`, it is never
`pip install`ed, and its manifest ships `requirements: []`. New here? Read
**[docs/SPEC.md](docs/SPEC.md)** first — it is the German founding specification
and the binding contract for how the engine behaves (see *SPEC.md is the
contract* below).

A few of this project's conventions are unusual and easy to violate by accident.
Please read the first two sections carefully before you touch anything.

## 1. Do NOT reformat the code

The code is **intentionally hand-formatted** — aligned call arguments,
deliberate line breaks, comments placed to explain the *why* next to the logic
they justify. The linter is enforced, the formatter is not:

- `ruff check` (lint) **is** enforced and must stay clean. CI runs
  `ruff check .` and nothing else for style.
- `ruff format` (the formatter) is **deliberately not used**. Never run
  `ruff format`, and never let an editor "format on save" reflow this code.
- Do not reflow, re-wrap, or re-indent existing code you are not otherwise
  changing. Touch only the lines your change actually needs.

`E501` (line length) is ignored on purpose (see `[tool.ruff.lint] ignore` in
`pyproject.toml`) precisely so hand-alignment survives. Match the style of the
surrounding code and keep the intent comments accurate.

## 2. SPEC.md is the contract

**[docs/SPEC.md](docs/SPEC.md)** (German) is the project's binding
specification. It is not background reading — it is the source of truth the code
is written against, and comments throughout the code cite it (`SPEC §4`,
`SPEC §9.1`, …).

- It is a **current-state specification**: it describes only what the shipped
  version does. Its header carries "Gilt für Version: <X>", checked against
  `const.INTEGRATION_VERSION`. Provenance, decision logs and the old→new section
  mapping live in **[docs/HISTORIE.md](docs/HISTORIE.md)** (not normative).
- Any feature or behavioural change **must update the SPEC in the same PR**.
- **File it by topic, not by release.** New behaviour becomes a subsection at
  the end of the section that owns the topic (see the signpost table in
  **§1.2**), or a new top-level section with a thematic title. No version-keyed
  addendum sections, no "since v0.x" in the prose.
- **Section numbers are append-only.** Never renumber, delete or re-use an
  existing number; the code cites them. Retitling and rewriting the content is
  fine. Behaviour that no longer holds is **replaced**, not marked historical —
  move the reasoning to `docs/HISTORIE.md`.
- When you change behaviour that an existing section describes, update that
  section so the SPEC and the code never disagree.
- Keep the **`SPEC §…` citations in code comments accurate**. If you move logic
  a comment points at a section, fix the citation. The comments are
  load-bearing — they record the incident, finding or review that motivated the
  logic.

### Keeping the SPEC current — the mechanics

Discipline alone let the SPEC drift once already, so three mechanisms back it
up now:

- **[`tests/test_spec_integrity.py`](tests/test_spec_integrity.py)** (runs in
  the normal suite, **fails the build**): every `SPEC §…` citation under
  `custom_components/`, `tests/`, `scripts/`, `dashboards/` and `docs/` must
  resolve to a real heading; every service in `services.yaml` and every public
  `site` config field in `const.py` must be named in the SPEC; every top-level
  section must be covered by the §1.2 signpost; the version stamp must match
  `INTEGRATION_VERSION`. When it fails, the message names the exact citation
  site or field.
- **The `spec-reminder` CI job** (advisory, `continue-on-error`, never a gate):
  on a PR that touches `custom_components/` without touching `docs/SPEC.md`, it
  prints a warning annotation. Pull the SPEC along, or say in the PR why the
  contract did not change.
- **[`.github/pull_request_template.md`](.github/pull_request_template.md)** and
  **[`CLAUDE.md`](CLAUDE.md)** carry the same checklist for humans and for
  AI-assisted sessions.

New operator-visible config fields have a home: the **§7** schema tables
(field name, meaning, range/default, and whether it enters the config
fingerprint). Add the row in the same PR — guard (c) above checks the field
name is there at all.

## 3. Dev environment

The dev tooling lives in a local `./.venv`, created by **uv** from the
committed `uv.lock` (the single source of truth for every tool version — CI
uses the same lockfile):

```bash
uv sync --locked --group dev      # or: make install
```

uv installs Python 3.14 itself if needed (pinned in `.python-version`;
`requires-python >= 3.14.2` matches HA's own floor). On a machine **without
uv**, the cross-platform bootstrap installs it first, then delegates to the
same `uv sync`:

```bash
# Linux / macOS / WSL
./scripts/setup-env.sh          # (or: bash scripts/setup-env.sh)

# Windows (PowerShell)
.\scripts\setup-env.ps1
```

The bootstrap uses [`scripts/setup_env.py`](scripts/setup_env.py) (pure
stdlib, Python 3.10+). On Windows an existing `uv` is used directly; otherwise
`py -3` or `python` bootstraps uv, which installs the required Python 3.14. Alternatively there is a **devcontainer** (`.devcontainer/`) whose
`postCreateCommand` runs the same `uv sync --locked --group dev`; it also carries a
Node feature so the JS card harness (`tests/harness/`) runs instead of
skipping.

The dev group in `pyproject.toml` is `homeassistant`, `pytest`, `pytest-cov`,
`pytest-homeassistant-custom-component`, `ruff`, `mypy`. These packages only
run the tests and the linter/typer — the integration has **no runtime
dependencies**. The `>=` entries are minimums; the exact versions live in
`uv.lock` (updates via Dependabot or `uv lock --upgrade`, reviewed in a PR).
Sole full pin: `pytest-homeassistant-custom-component` — it pins HA (and
pytest/pytest-cov) exactly itself, so it leads the HA coupling (rationale in
the pyproject comment).

## 4. Test architecture — and why the HA plugin is disabled

The code is split into two layers:

- `custom_components/balcony_solar_forecast/core/` — the **pure, HA-free
  engine**. Standard library only: **no numpy / pandas / pvlib at runtime**, and
  the manifest `requirements` stays `[]`. This is where the physics and the
  learners live; it runs on any Python, Windows included.
- `custom_components/balcony_solar_forecast/` (the rest) — the **Home Assistant
  glue**: coordinator, config flow, entities, services.

Portable unit tests use fakes; reusable coordinators, stores and weather inputs
live in `tests/helpers/`. Test modules do not import other test modules.
The separate Linux suite starts real Home Assistant, its event bus, flow manager,
entity platforms and storage. It uses `--confcutdir` to exclude the portable
suite's package shims:

```bash
uv run pytest tests --ignore=tests/integration -p no:homeassistant
uv run pytest tests/core -p no:homeassistant
uv run pytest --confcutdir=tests/integration tests/integration -p no:homeassistant
uv run ruff check .
uv run mypy
uv run python scripts/check_mypy_baseline.py
uv run python scripts/check_core_imports.py
```

PHACC is disabled in both suites: its autouse fixtures are unused, can conflict
with synchronous tests' event-loop setup, and import POSIX-only `fcntl` on
Windows. `pytest-asyncio` still runs async tests. Do not add `-q`: `pyproject.toml`
already sets it; a second occurrence hides pytest's result summary.

The normal CI unit job enforces **95% statement coverage**. A separate job
reports branch coverage without a percentage gate; uncovered decisions guide
review, not tests written just to increase a number. `scripts/mutation_smoke.py`
checks three deliberate semantic defects in a temporary copy: disabled group
clamping, acceptance of stale power labels, and lost nightly catchup gaps. The
baseline test must pass and each mutant must cause an assertion failure;
collection errors do not count as detection.

Test intent determines the evidence (see CLAUDE rule 6): bugfix tests fail
semantically on the previous implementation; feature tests check the new
contract; refactor tests compare old and new behavior. Characterization and
regression tests may already pass if they secure an independent contract or
reference. Avoid expectations calculated by the production function itself.

Golden vectors are committed and required; a missing file fails collection.
Reproduce them with the separately locked, optional pvlib environment:

```bash
uv run --script --locked scripts/generate_reference_vectors.py --check
# Only after reviewing reference-input/model changes:
uv run --script --locked scripts/generate_reference_vectors.py --write
```

The generator uses pvlib 0.15.2, explicit model parameters and
`scripts/reference_inputs.json`; pvlib is not a runtime or normal test dependency.

`mypy` preserves the clean-core gate. `scripts/check_mypy_baseline.py` also checks
all previously suppressed core modules and the critical HA boundaries listed
in `HA_BOUNDARIES`. Its committed diagnostic baseline names files and symbols,
rejects new errors, and requires removing fixed entries. Inspect every diagnostic
before using `--write-baseline`; never refresh it just to turn CI green. Reduce
legacy errors without blanket `Any`, casts or module suppressions.

`make` remains an optional wrapper around uv (`make test`, `make test-core`,
`make lint`). `make format` means `ruff check --fix`, never `ruff format`.
CI installs with `uv sync --locked --group dev` and then uses `uv run --no-sync`:
a stale lockfile fails instead of being silently rewritten. Update dependencies
explicitly with `uv lock --upgrade` and review both metadata and lockfile.

## 5. Versioning & releases

The project's version is written in **three** places and they must stay equal:

- `custom_components/balcony_solar_forecast/manifest.json` → `version`
- `pyproject.toml` → `[project] version`
- `custom_components/balcony_solar_forecast/const.py` → `INTEGRATION_VERSION`

CI enforces equality. HACS installs the tag's zipball, so all metadata must be
correct in the exact commit being released:

1. Prepare a release PR: bump the three strings and the SPEC version stamp;
   move `[Unreleased]` into a dated `## [x.y.z] - YYYY-MM-DD` changelog section.
2. Merge after review and green checks. Wait for the **push** run of
   `validate.yml` on the resulting main commit to finish successfully.
3. Dispatch **Release** (`.github/workflows/release.yml`) from `main`, supplying
   the version without `v` and the full 40-character commit SHA.

The read-only preflight verifies main ancestry, exact checkout, version equality,
SPEC stamp, dated changelog and the latest Validate push run for that exact SHA.
An older green run cannot override a newer failed or unfinished run. Only the
publish job has write permission; it repeats these checks immediately before
creating the tag and release. It never force-moves a tag. Release notes come
from that version's changelog section. Do not create tags or published releases
manually before validation: the workflow is the prepublication gate.

## 6. The `hacs.json` Home Assistant floor

`hacs.json` pins `"homeassistant": "2026.3.0"`. That is the **floor the
config-flow selector APIs and entity conventions were validated against** — the
minimum HA version this integration is known to load and configure cleanly on.
CI reads this exact version from `hacs.json` and tests both the portable suite
and real HA lifecycle against it; a wildcard would not verify the declared floor.
Raise it **consciously** (when you adopt an API that needs a newer HA, and after
testing on it); **never lower it** without validating the selectors and entity
setup on the older version first.

## 7. Submitting a PR

Before you open a PR, make sure:

- [ ] The **full suite is green**: `make test` (and `make test-core` for a quick
      core-only loop).
- [ ] **`ruff check` is clean** (`make lint`). No `ruff format` — see §1.
- [ ] There is a **CHANGELOG.md** entry under `[Unreleased]`.
- [ ] **docs/SPEC.md is updated** if behaviour changed, and any moved/renamed
      `SPEC §…` citations in code comments are fixed.

Small, focused PRs are easiest to review. If you're planning something large,
open an issue first to discuss the approach.
