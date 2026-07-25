# Arbeitsanweisung für KI-gestützte Sessions

Kurzfassung der Regeln, die in diesem Repo wirklich tragen. Ausführlich:
**[docs/SPEC.md](docs/SPEC.md)** (der Vertrag, deutsch),
**[CONTRIBUTING.md](CONTRIBUTING.md)** (Setup, Stil, Release) und
**[docs/project-knowledge/](docs/project-knowledge/)** (Architektur, Physik,
Lernschichten, HA-Oberfläche, Runbook, Forensik, Entwicklung).

`balcony_solar_forecast` ist eine HA-Custom-Integration (HACS), die aus den
**Rohstrahlungskomponenten** von Open-Meteo lokal eine 15-Minuten-PV-Prognose je
Modulebene rechnet und sich an den gemessenen Ist-Werten selbst korrigiert.
Reine Standardbibliothek: `manifest.json` führt `requirements: []`, kein
numpy/pandas/pvlib.

## Harte Regeln

1. **Wahrheitsquelle ist der Code.** Aussagen über Verhalten, Konstanten,
   Feldnamen, Services oder Sensoren am aktuellen Stand nachprüfen. Belege mit
   Datei + Funktions-/Konstantenname, **nie mit Zeilennummern** (die veralten
   sofort). Lieber „nicht modelliert / offen" schreiben als plausibel klingen.
2. **`core/` ist HA-frei.** `custom_components/balcony_solar_forecast/core/`
   importiert nichts aus `homeassistant`, und `const.py` bleibt ebenfalls
   HA-frei (core importiert daraus). Einzige dokumentierte Ausnahme:
   `core/openmeteo_backfill.py` fasst Netzwerk an — mit **lazy** `aiohttp`-Import
   und injizierter Session, weiterhin ohne HA-Import. Neue Mathematik gehört in
   `core/`, neuer HA-Glue eine Ebene darüber.
3. **`docs/SPEC.md` ist der Vertrag.** Jede Verhaltensänderung zieht die SPEC im
   **selben PR** nach. Neues gehört thematisch einsortiert (§0
   „Änderungsregel"), **bestehende Abschnittsnummern werden nie umnummeriert** —
   der Code zitiert sie hundertfach als `SPEC §x.y`.
   `tests/test_spec_integrity.py` erzwingt das maschinell: Zitate müssen
   auflösen, jede Aktion aus `services.yaml` und jedes `site`-Config-Feld aus
   `const.py` muss in der SPEC vorkommen, jeder Top-Level-Abschnitt im
   Wegweiser stehen.
4. **Version an drei Stellen, synchron, VOR dem Tag:**
   `custom_components/balcony_solar_forecast/manifest.json` (`version`),
   `pyproject.toml` (`[project] version`),
   `custom_components/balcony_solar_forecast/const.py` (`INTEGRATION_VERSION`).
   CI prüft die drei gegeneinander, der Release-Guard zusätzlich gegen den
   Git-Tag. HACS liefert den Zipball des Tags aus — ein Bump nach dem Tag wirkt
   nicht. Version nur im Release-PR anfassen.
5. **Tests und Lint:**
   ```
   .venv\Scripts\python.exe -m pytest tests -p no:homeassistant     # Windows
   make test                                                        # POSIX
   .venv\Scripts\python.exe -m ruff check .
   ```
   `-p no:homeassistant` ist Pflicht: das PHACC-Plugin zieht POSIX-only `fcntl`
   und autouse-Fixtures, die unter Python ≥ 3.12 werfen — kein Test benutzt es.
   `pyproject.toml` setzt bereits `addopts = "-q"`; **kein zweites `-q`** auf der
   Kommandozeile, sonst verschluckt pytest die Ergebniszeile. `ruff format` ist
   verboten (der Code ist absichtlich handformatiert, `E501` ist aus).
6. **Neue Tests müssen den alten Code durchfallen lassen** — und das wird
   bewiesen, nicht behauptet: Worktree auf den Parent-Commit, nur die neuen
   Testdateien hineinkopieren, Suite laufen lassen. Ein *semantischer*
   Fehlschlag zählt mehr als ein `TypeError` auf eine neue Signatur. Umgekehrt
   bei verhaltensneutralen Refactorings: Bit-Identität gegen eine eingefrorene
   Kopie der alten Funktion über viele geseedete Zufallseingaben zeigen.
7. **Optionale Config-Felder nur-wenn-gesetzt serialisieren.** Ein neues
   optionales Feld erscheint in `to_dict()` nur, wenn es gesetzt ist — eine
   Alt-Config muss nach dem Upgrade **byte-identisch** dasselbe Dict ergeben,
   sonst kippt der Config-Fingerprint und setzt ohne fachlichen Grund
   Lernzustand zurück. Spiegelbildlich im Fingerprint
   (`coordinator._config_fingerprint`): nur-wenn-gesetzt anhängen, Werte runden,
   Sentinels kollisionsfrei wählen. **Ein Feld, das die RAW-Kurve verändert,
   MUSS in den Fingerprint** (Feldliste + Fingerprint-Spalte: SPEC §4.1).
   Store-Migrationen sind additiv: der äußere Store-Envelope bleibt für immer
   Version 1, migriert wird die innere `schema_version`, alte Schlüssel gehen
   byte-treu durch. Eine Migration, die Lernzustand verwirft, ist ein
   kritischer Fehler.
8. **RAW ist die Lern-Wahrheit.** Jede Lernschicht trainiert gegen genau die
   Kurve, auf die sie angewandt wird: Shademap gegen die ungegatete, unclamped
   Physik-Referenz (`beam_poa_ungated`), Day-Ahead-Bias gegen slow_only
   (Shademap ohne Bias), Intraday-Skalar gegen raw × θ, Quantile gegen die
   issued-corrected Kurve. Wer die Schichtung bricht, baut eine
   Doppelkorrektur — die häufigste Fehlerklasse dieses Projekts, sichtbar als
   Übertreibung am Morgen. Ein besserer statischer Prior schlägt immer einen
   kompensierenden Lerner: alle Lerner sind geclamped und sättigen am Rand.
9. **DC/AC-Basis immer explizit nennen.** Gemessen ist nur die DC-Leistung je
   Port; **DC** ist die Lern- und Scoreboard-Wahrheit, **AC** der Standard der
   betreiberseitigen Haupt-Sensoren. Dazwischen liegen rund 8 % — jeder
   Vergleich ohne genannte Basis produziert ein Einheiten-Artefakt.
10. **Azimut: 0 = Nord, im Uhrzeigersinn** (90 = Ost, 180 = Süd, 270 = West) —
    durchgängig für Sonnenazimut, Ebenen und Horizontzeilen. Im Kern gibt es
    keine Umrechnung; Fremdquellen mit 0 = Süd (Open-Meteo-GTI, PVGIS) müssen
    von Hand gedreht werden.

## Vor dem PR

`ruff check` sauber, volle Suite grün, CHANGELOG-Eintrag unter `[Unreleased]`,
SPEC nachgezogen (oder im PR begründet, warum nicht nötig) — Checkliste in
[.github/pull_request_template.md](.github/pull_request_template.md).

## Live-Zustände sind nicht aus dem Repo belegbar

Gelernte θ-Werte, Shademap-Bins, Quantil-Füllstände, Scoreboard/Kill-Gate, die
tatsächlich konfigurierte Geometrie, aktive Kill-Switches, Degradationsstatus:
verbindlich ist der **Diagnostics-Download des Config-Entries**, ergänzend die
Aktionen `dump_shademap`, `get_issued_forecast`, `get_forecast` und die
Attribute des Day-Ahead-Bias-Sensors. Quelle und Datum immer mitnennen; jede
Zahl aus einer älteren Analyse ist eine Momentaufnahme, kein heutiger Zustand.
