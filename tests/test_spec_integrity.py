"""Machine-guarded integrity of the SPEC contract (SPEC §0).

``docs/SPEC.md`` is the project's contract, and the code cites it by section
number in hundreds of places. Those citations are load-bearing: they are how a
reader gets from a line of physics to the paragraph that justifies it. Keeping
them true has so far relied on discipline alone — this module turns that
discipline into a test.

Four guards, all pure (no Home Assistant, no fixtures — plain file reads):

  (a) **Citation integrity.** Every ``SPEC §x[.y]`` cited anywhere under
      ``custom_components/`` (``.py``, ``services.yaml``, the frontend ``.js``)
      or ``tests/`` must resolve to a real heading in ``docs/SPEC.md``. This is
      the guard that makes SPEC §0's "section numbers are immutable" rule
      enforceable rather than merely stated.
  (b) **Service coverage.** Every service defined in ``services.yaml`` is
      mentioned in the SPEC — a new action cannot ship undocumented.
  (c) **Config-surface coverage.** Every public field of the ``site`` config
      object (the ``CONF_*`` block in ``const.py``) is mentioned in the SPEC —
      a new operator-visible field cannot ship undocumented.
  (d) **Signpost completeness.** Every top-level section of the SPEC is picked
      up by the SPEC §0 signpost (topic table or the historical list), so a new
      section cannot grow past the map that is supposed to route readers to it.

Deliberately *lower bounds*: a mere word match proves the name appears, not
that the prose is good. The point is that the mechanical gap — a field or an
action that the SPEC has never heard of, a citation that points nowhere —
fails a test instead of aging quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SPEC_PATH = _REPO / "docs" / "SPEC.md"
_INTEGRATION = _REPO / "custom_components" / "balcony_solar_forecast"
_SERVICES_YAML = _INTEGRATION / "services.yaml"
_CONST_PY = _INTEGRATION / "const.py"
_SCAN_ROOTS = (_REPO / "custom_components", _REPO / "tests")
# Text-bearing sources that carry citations: python, the service manifest, the
# bundled Lovelace cards, icons/manifest json.
_SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".js", ".json"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# SPEC parsing.
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.M)
# "§0 Wegweiser", "4. Zielarchitektur", "14.1 Skill-Scoreboard" -> 0 / 4 / 14.1
_NUMBERED_RE = re.compile(r"^(?:§\s*)?(\d+(?:\.\d+)*)[.):]?(?:\s|$)")
# "Anhang A: Konventionen" -> "Anhang A"
_APPENDIX_RE = re.compile(r"^(Anhang\s+[A-Z])\b")


def _headings() -> list[tuple[int, str, str]]:
    """Return ``(level, ident, title)`` for every identifiable SPEC heading.

    ``ident`` is the section number ("14.1") or the appendix key ("Anhang A");
    unnumbered prose headings (e.g. "### Was dieses Dokument ist") are skipped.
    """
    out: list[tuple[int, str, str]] = []
    for m in _HEADING_RE.finditer(_read(_SPEC_PATH)):
        level, title = len(m.group(1)), m.group(2)
        num = _NUMBERED_RE.match(title)
        if num is not None:
            out.append((level, num.group(1), title))
            continue
        app = _APPENDIX_RE.match(title)
        if app is not None:
            out.append((level, app.group(1), title))
    return out


# ---------------------------------------------------------------------------
# (a) Citation integrity.
# ---------------------------------------------------------------------------

# One citation *run*: the "SPEC §" prefix plus any number of further sections
# chained with a separator, so "SPEC §9/§10", "SPEC §6, §9, §10" and
# "SPEC §5 und §6" each yield all of their sections. A trailing sentence period
# is not consumed (the number pattern needs a digit after the dot).
_CITE_RUN_RE = re.compile(
    r"SPEC\s*§\s*\d+(?:\.\d+)*"
    r"(?:\s*(?:[/,+&]|und)\s*§\s*\d+(?:\.\d+)*)*"
)
_SECTION_NO_RE = re.compile(r"\d+(?:\.\d+)*")


def _scan_citations() -> list[tuple[str, int, str]]:
    """Every SPEC citation in the tree as ``(relative path, line, section)``."""
    found: list[tuple[str, int, str]] = []
    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _SCAN_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            text = _read(path)
            rel = path.relative_to(_REPO).as_posix()
            for run in _CITE_RUN_RE.finditer(text):
                line = text.count("\n", 0, run.start()) + 1
                for sec in _SECTION_NO_RE.findall(run.group(0)):
                    found.append((rel, line, sec))
    return found


def test_every_spec_citation_resolves_to_a_heading():
    """A cited section number must exist as a heading in docs/SPEC.md.

    This is the enforcement arm of the immutability rule: renumbering or
    deleting a section breaks here, naming the exact citation sites to fix.
    """
    citations = _scan_citations()
    # Sanity: the scanner must actually see the citation corpus (a silent
    # regex/glob regression would make this test vacuously green).
    assert len(citations) > 300, f"citation scanner found only {len(citations)}"

    known = {ident for _lvl, ident, _title in _headings()}
    dangling = sorted(
        {(sec, rel, line) for rel, line, sec in citations if sec not in known}
    )
    assert not dangling, (
        "SPEC citations point at sections that do not exist in docs/SPEC.md:\n"
        + "\n".join(f"  §{sec}  <- {rel}:{line}" for sec, rel, line in dangling)
        + "\n\nFix by adding the missing section to docs/SPEC.md (numbers are"
        " append-only, never reused) or by correcting the citation."
    )


# ---------------------------------------------------------------------------
# (b) Service coverage.
# ---------------------------------------------------------------------------

_SERVICE_KEY_RE = re.compile(r"^([a-z][a-z0-9_]*):\s*$", re.M)


def test_every_service_is_documented_in_the_spec():
    """Each service in services.yaml must be named somewhere in the SPEC."""
    services = _SERVICE_KEY_RE.findall(_read(_SERVICES_YAML))
    assert len(services) >= 10, f"service scanner found only {services}"

    spec = _read(_SPEC_PATH)
    missing = [s for s in services if not re.search(rf"\b{s}\b", spec)]
    assert not missing, (
        "services defined in services.yaml but never named in docs/SPEC.md: "
        + ", ".join(missing)
        + "\n\nDocument the action in its defining section and list it in the"
        " §16 overview."
    )


# ---------------------------------------------------------------------------
# (c) Config-surface coverage.
# ---------------------------------------------------------------------------

_SITE_BLOCK_MARKER = "# --- Config-flow keys (inside the site object) ---"
_CONF_ASSIGN_RE = re.compile(r'^(CONF_[A-Z0-9_]+)\s*=\s*"([^"]+)"')

# Deliberate exceptions to (c), each with its reason. Keep this list short: an
# entry means "the SPEC is NOT required to name this key", which is a hole in
# the guard, not a shortcut.
_FIELD_ALLOWLIST = {
    # Shared by planes and inverter groups. As a bare word "name" matches
    # almost any prose, so requiring it proves nothing; the field itself is
    # documented in the §4.1 schema table.
    "name",
}


def _site_config_fields() -> dict[str, str]:
    """``{CONF_* constant: string value}`` for the site-object config block."""
    lines = _read(_CONST_PY).splitlines()
    start = lines.index(_SITE_BLOCK_MARKER) + 1
    fields: dict[str, str] = {}
    for line in lines[start:]:
        if line.startswith("# --- "):  # next const.py section
            break
        m = _CONF_ASSIGN_RE.match(line)
        if m is not None:
            fields[m.group(1)] = m.group(2)
    return fields


def test_every_public_site_config_field_is_documented_in_the_spec():
    """Each operator-editable site/plane/horizon/group key appears in the SPEC.

    The site object is the integration's public configuration surface: it is
    persisted in the config entry, edited by hand in the object selector and
    feeds the config fingerprint. A key the SPEC has never named is a key
    nobody can be held to.
    """
    fields = _site_config_fields()
    assert len(fields) >= 20, f"const.py site-block scanner found {fields}"

    spec = _read(_SPEC_PATH)
    missing = sorted(
        f"{value} ({const})"
        for const, value in fields.items()
        if value not in _FIELD_ALLOWLIST and not re.search(rf"\b{value}\b", spec)
    )
    assert not missing, (
        "site-config fields defined in const.py but never named in"
        " docs/SPEC.md:\n  " + "\n  ".join(missing)
        + "\n\nAdd the field to the §4.1 schema table (name, meaning, range /"
        " default) and to the section that describes its behaviour."
    )


# ---------------------------------------------------------------------------
# (d) Signpost completeness.
# ---------------------------------------------------------------------------

_SIGNPOST_START = "### Thema → maßgebliche Abschnitte"
_SIGNPOST_END = "### Änderungsregel"

# The signpost cannot route to itself; §0 IS the signpost.
_SIGNPOST_ALLOWLIST = {"0"}


def test_every_top_level_section_is_covered_by_the_signpost():
    """Every ``##`` section is reachable from the SPEC §0 signpost.

    The signpost block spans the topic table AND the "historisch, nicht
    normativ" list: a section is covered if it is routed to as current
    doctrine or explicitly marked historical. Anything else is a section that
    grew past the map.
    """
    spec = _read(_SPEC_PATH)
    start = spec.index(_SIGNPOST_START)
    block = spec[start : spec.index(_SIGNPOST_END, start)]

    referenced = {n.split(".")[0] for n in re.findall(r"§\s*(\d+(?:\.\d+)*)", block)}
    referenced |= set(re.findall(r"Anhang\s+[A-Z]", block))

    uncovered = [
        f"{ident} ({title})"
        for level, ident, title in _headings()
        if level == 2 and ident not in referenced and ident not in _SIGNPOST_ALLOWLIST
    ]
    assert not uncovered, (
        "top-level SPEC sections missing from the §0 signpost:\n  "
        + "\n  ".join(uncovered)
        + "\n\nAdd a row to the topic table (normative) or an entry to the"
        " 'Historisch, nicht normativ' list."
    )
