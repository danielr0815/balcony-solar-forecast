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

The dev tooling lives in a local `./.venv`. Create it with:

```bash
make install
```

On a machine **without** `make`, run the cross-platform bootstrap directly —
both wrappers call the same pure-stdlib script:

```bash
# Linux / macOS / WSL
./scripts/setup-env.sh          # (or: bash scripts/setup-env.sh)

# Windows (PowerShell)
.\scripts\setup-env.ps1
```

`make install` and both wrappers delegate to
[`scripts/setup_env.py`](scripts/setup_env.py), which creates `./.venv` and
installs the **`[dependency-groups] dev`** group from `pyproject.toml` —
`homeassistant`, `pytest`, `pytest-homeassistant-custom-component`, `ruff`.
Home Assistant is unpinned; `pytest-homeassistant-custom-component` pins the
matching HA version transitively. These packages only run the tests and the
linter — the integration has **no runtime dependencies**. The bootstrap
interpreter creates the venv (`py -3.13` on Windows / via `make`, `python3` on
POSIX; `requires-python >= 3.13`).

## 4. Test architecture — and why the HA plugin is disabled

The code is split into two layers:

- `custom_components/balcony_solar_forecast/core/` — the **pure, HA-free
  engine**. Standard library only: **no numpy / pandas / pvlib at runtime**, and
  the manifest `requirements` stays `[]`. This is where the physics and the
  learners live; it runs on any Python, Windows included.
- `custom_components/balcony_solar_forecast/` (the rest) — the **Home Assistant
  glue**: coordinator, config flow, entities, services.

The whole suite is **unit-style** and runs with the PHACC plugin disabled:

```bash
make test        # == python -m pytest tests -p no:homeassistant   (full suite)
make test-core   # == python -m pytest tests/core -p no:homeassistant
make lint        # == ruff check .
make format      # == ruff check --fix .   (lint autofix — NOT ruff format)
make clean       # remove ./.venv
```

**Why `-p no:homeassistant`?** Every HA-layer test runs against
fakes/monkeypatch and needs only `import homeassistant`, never a real HA
instance — the suite never uses PHACC's fixtures. But
`pytest-homeassistant-custom-component`'s **autouse** fixtures call
`asyncio.get_event_loop()` at setup (which raises on Python 3.12+ for the sync
tests) and importing the plugin pulls the POSIX-only `fcntl` (unimportable on
Windows). So the plugin only ever breaks a suite that never uses it. Disabling
it runs the **full** meaningful suite identically on Linux, macOS, WSL and
Windows (`pytest-asyncio`, installed via PHACC, still drives the async tests).
This is what `make test` and the CI `tests` job do — CI runs the same
`python -m pytest tests -p no:homeassistant`, plus `--cov` flags for a
report-only coverage summary. Do **not** add a `-q`: `pyproject.toml` already
sets `addopts = "-q"`, and a second one escalates to `-qq`, which drops the
`N passed, M skipped` line.

## 5. Versioning & releases

The project's version is written in **three** places and they must stay equal:

- `custom_components/balcony_solar_forecast/manifest.json` → `version`
- `pyproject.toml` → `[project] version`
- `custom_components/balcony_solar_forecast/const.py` → `INTEGRATION_VERSION`

CI enforces this: the `validate` workflow fails on version drift between the
three, and the `release` workflow's version guard fails a **tag** that does not
match all three. HACS installs the tag's zipball, so the strings must already be
correct **in the tagged commit**. Order of operations for a release:

1. Bump all three version strings (keep them identical).
2. Move the `[Unreleased]` entry to `[x.y.z]` in
   [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog + SemVer).
3. *Then* create the tag.

Bumping the versions or the CHANGELOG after tagging is too late — the guard will
have already failed, or worse, shipped the wrong version.

## 6. The `hacs.json` Home Assistant floor

`hacs.json` pins `"homeassistant": "2026.1.0"`. That is the **floor the
config-flow selector APIs and entity conventions were validated against** — the
minimum HA version this integration is known to load and configure cleanly on.
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
