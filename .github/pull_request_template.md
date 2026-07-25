## Was und warum

<!-- Ein Absatz: welches Verhalten ändert sich, und was war der Anlass
     (Messbefund, Incident, Review-Punkt)? -->

## Checkliste

- [ ] **SPEC nachgezogen** — `docs/SPEC.md` beschreibt das neue Verhalten
      thematisch einsortiert (§0 „Änderungsregel"; bestehende Abschnitts-
      nummern bleiben unverändert). *Oder* hier begründet, warum keine
      Vertragsänderung nötig ist (reines Refactoring, nur Tests, nur Tooling):
      <!-- Begründung -->
- [ ] **Tests fallen auf dem Parent-Commit durch** — neue Tests gegen den
      Stand vor dieser Änderung laufen lassen; sie müssen *semantisch*
      fehlschlagen. Bei verhaltensneutralen Refactorings stattdessen:
      Bit-Identität gegen die eingefrorene alte Funktion gezeigt.
- [ ] **`ruff check .` sauber und volle Suite grün**
      (`pytest tests -p no:homeassistant`, kein zusätzliches `-q`;
      kein `ruff format`).
- [ ] **Abwärtskompatibilität optionaler Config-Felder** — neue optionale
      Felder werden in `to_dict()` nur-wenn-gesetzt serialisiert (eine
      Alt-Config ergibt byte-identisch dasselbe Dict); ein Feld, das die
      RAW-Kurve verändert, ist im Config-Fingerprint ergänzt (SPEC §4.1).
      Store-Migrationen sind additiv und verwerfen keinen Lernzustand.
- [ ] **CHANGELOG.md** unter `[Unreleased]` ergänzt.
- [ ] **Version nur im Release-PR** angefasst — und dann an allen drei Stellen
      synchron (`manifest.json`, `pyproject.toml`, `const.INTEGRATION_VERSION`)
      **vor** dem Tag.
