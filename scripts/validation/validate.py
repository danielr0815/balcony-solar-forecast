#!/usr/bin/env python3
"""validate.py -- Post-Deployment-Validierung balcony_solar_forecast v0.21.0.

Prueft ~1 Woche nach Deployment objektiv, ob die Morgen-/Bias-/Band-Fixes
wirken (Pruefkatalog C1-C8, siehe README.md).

Aufrufe:
  Offline-Selbsttest gegen archivierte Daten (VOR-Fix-Woche => erwartete FAILs):
      python validate.py --offline --data-dir ..\\hadata

  Live gegen Home Assistant (zieht Daten und analysiert sie):
      python validate.py --ha-url http://10.102.10.11:8123 --token <TOKEN>

  Das Token kann auch aus der Umgebung kommen (bevorzugt — ein --token-CLI-Arg
  steht in der Prozessliste; ueber http:// reist es im Klartext):
      HA_LONG_LIVED_TOKEN=<TOKEN> python validate.py --ha-url http://...

Nur Python-stdlib (>= 3.11); numpy wird nicht benoetigt.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys

from bsf_checks import DEFAULT_ETA, run_all
from bsf_data import LOC, load_bundle

EXIT_OK, EXIT_WARN, EXIT_FAIL = 0, 1, 2


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Post-Deployment-Validierung balcony_solar_forecast",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--ha-url", help="HA-Basis-URL, IP verwenden! z.B. http://10.102.10.11:8123 (besser https:// — über http reist das Token im Klartext)")
    p.add_argument(
        "--token",
        help="Long-Lived Access Token (default: env HA_LONG_LIVED_TOKEN — bevorzugt, ein CLI-Arg steht in der Prozessliste)",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Nicht fetchen, vorhandenes --data-dir analysieren",
    )
    p.add_argument(
        "--data-dir",
        help="Datenverzeichnis (offline: Pflicht; live: Ablageort, default bsf_pull_<ts>)",
    )
    p.add_argument("--days", type=int, default=8, help="Analysefenster in Tagen")
    p.add_argument(
        "--eta",
        type=float,
        default=DEFAULT_ETA,
        help="DC->AC-Wirkungsgrad-Fallback, solange issued kein hourly_wh_ac hat",
    )
    p.add_argument("--entry-id", help="Config-Entry-Id (nur noetig bei mehreren Sites)")
    p.add_argument("--json", dest="json_out", help="Report zusaetzlich als JSON-Datei")
    p.add_argument(
        "--fetch-only", action="store_true", help="Nur Daten ziehen, keine Analyse"
    )
    a = p.parse_args(argv)
    if not a.token:
        # Env fallback resolved AFTER parsing: with ArgumentDefaultsHelpFormatter
        # an argparse default=<env value> would print the token into --help.
        a.token = os.environ.get("HA_LONG_LIVED_TOKEN")
    if a.offline and not a.data_dir:
        p.error("--offline braucht --data-dir")
    if not a.offline and not (a.ha_url and a.token):
        p.error("Entweder --offline --data-dir ODER --ha-url + --token angeben")
    return a


def render(results, bundle, mode: str) -> str:
    lines: list[str] = []
    w = lines.append
    w("=" * 78)
    w("balcony_solar_forecast - Post-Deployment-Validierung (Pruefkatalog C1-C8)")
    w("=" * 78)
    ver = bundle.features.get("integration_version") or "unbekannt"
    w(f"Modus: {mode}   Datenverzeichnis: {bundle.data_dir}")
    w(f"Integration laut Diagnostics: v{ver}")
    if bundle.days:
        full = bundle.full_days()
        w(
            f"Analysefenster (lokale Tage): {bundle.days[0]} .. {bundle.days[-1]}"
            f"  ({len(full)} volle Tage"
            + (
                f", partial: {', '.join(str(d) for d in sorted(bundle.partial_days))}"
                if bundle.partial_days
                else ""
            )
            + ")"
        )
    w("")
    w("Feature-Erkennung (v0.21.0-Felder):")
    f = bundle.features
    w(f"  issued hourly_wh_ac        : {'JA' if f.get('issued_has_hourly_wh_ac') else 'nein'}")
    w(f"  issued cloud_class_by_hour : {'JA' if f.get('issued_has_cloud_class') else 'nein'}")
    w(f"  bias_cells clamped-Flag    : {'JA' if f.get('bias_has_clamped_flag') else 'nein'}")
    w(f"  Quantile-Diagnostics       : {'JA' if f.get('quantile_bins_available') else 'nein'}")
    for n in bundle.notes:
        w(f"  Hinweis: {n}")
    w("")

    for r in results:
        head = f"[{r.cid}] {r.title} "
        w(head + "." * max(1, 70 - len(head)) + f" {r.status}")
        for m in r.metrics:
            w(f"    {m.name:<46} {m.value:<26} [{m.status}]")
            w(f"      Schwelle: {m.threshold}")
        for d in r.details:
            w(f"      {d}")
        if r.interpretation:
            w(f"    -> {r.interpretation}")
        w("")

    w("-" * 78)
    w("GESAMTUEBERSICHT")
    for r in results:
        w(f"  {r.cid}  {r.status:<5}  {r.title}")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_warn = sum(1 for r in results if r.status == "WARN")
    w("")
    if n_fail:
        w(
            f"ERGEBNIS: {n_fail} FAIL, {n_warn} WARN - Fixes greifen (noch) nicht "
            "vollstaendig. Nachjustierung siehe README (Abschnitt 'Wenn ein Check "
            "rot bleibt')."
        )
    elif n_warn:
        w(f"ERGEBNIS: 0 FAIL, {n_warn} WARN - weitgehend gruen, WARNs beobachten.")
    else:
        w("ERGEBNIS: alle Checks gruen - Deployment validiert.")
    w("-" * 78)
    return "\n".join(lines)


def to_json(results, bundle, mode: str) -> dict:
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": mode,
        "data_dir": os.path.abspath(bundle.data_dir),
        "integration_version": bundle.features.get("integration_version"),
        "features": bundle.features,
        "notes": bundle.notes,
        "window": {
            "days": [str(d) for d in bundle.days],
            "partial_days": [str(d) for d in sorted(bundle.partial_days)],
        },
        "checks": [
            {
                "id": r.cid,
                "title": r.title,
                "status": r.status,
                "metrics": [
                    {
                        "name": m.name,
                        "value": m.value,
                        "threshold": m.threshold,
                        "status": m.status,
                    }
                    for m in r.metrics
                ],
                "details": r.details,
                "interpretation": r.interpretation,
            }
            for r in results
        ],
        "summary": {
            "fail": sum(1 for r in results if r.status == "FAIL"),
            "warn": sum(1 for r in results if r.status == "WARN"),
            "pass": sum(1 for r in results if r.status == "PASS"),
            "skip": sum(1 for r in results if r.status in ("SKIP", "INFO")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(errors="replace")  # Windows-Konsole (cp850/cp1252)
    a = parse_args(argv if argv is not None else sys.argv[1:])

    mode = "offline"
    data_dir = a.data_dir
    if not a.offline:
        mode = "live"
        if not data_dir:
            data_dir = f"bsf_pull_{dt.datetime.now(LOC):%Y%m%d_%H%M}"
        from bsf_fetch import FetchError, fetch_all

        try:
            fetch_all(
                a.ha_url, a.token, data_dir, days=a.days, entry_id=a.entry_id
            )
        except FetchError as e:
            print(f"FETCH-FEHLER: {e}", file=sys.stderr)
            return EXIT_FAIL
        if a.fetch_only:
            print(f"--fetch-only: Daten liegen in {data_dir}")
            return EXIT_OK

    if not os.path.isdir(data_dir):
        print(f"Datenverzeichnis nicht gefunden: {data_dir}", file=sys.stderr)
        return EXIT_FAIL

    bundle = load_bundle(data_dir)
    if not bundle.days:
        print(
            "Keine Ist-Daten im Paket (actuals_hourly_stats.json leer/fehlend) - "
            "Abbruch.",
            file=sys.stderr,
        )
        return EXIT_FAIL
    results = run_all(bundle, eta=a.eta)
    print(render(results, bundle, mode))

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump(to_json(results, bundle, mode), fh, ensure_ascii=False, indent=1)
        print(f"JSON-Report: {a.json_out}")

    if any(r.status == "FAIL" for r in results):
        return EXIT_FAIL
    if any(r.status == "WARN" for r in results):
        return EXIT_WARN
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
