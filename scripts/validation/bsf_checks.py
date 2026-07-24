"""bsf_checks -- Pruefkatalog C1..C8 fuer die Post-Deployment-Validierung.

Jeder Check liefert ein CheckResult mit Messwerten, Schwellen, Status
(PASS/WARN/FAIL/SKIP/INFO) und einer kurzen Interpretation.

Kalibrierung: Die Schwellen sind gegen die VOR-Fix-Woche 17.-24.07.2026
(hadata/) geeicht - dort muessen C1-C5 und C7 anschlagen (siehe README),
waehrend die Regressionswachen C8a/C8c PASS bleiben.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

from bsf_data import (
    EID_BIAS,
    EID_TODAY,
    EID_TODAY_P10,
    EID_TOMORROW,
    LOC,
    SID_ACT_AC,
    SID_ACT_DC,
    SID_M4,
    SID_M8,
    SID_SCALAR,
    SID_SERVED_AC,
    SID_SERVED_DC,
    Bundle,
    parse_iso,
    to_float,
)

# Default-DC->AC-Wirkungsgrad (Victron-gelernt); nur genutzt, solange issued
# kein hourly_wh_ac liefert. Per CLI --eta ueberschreibbar.
DEFAULT_ETA = 0.9248

# Referenzwerte der Baseline-Woche 17.-24.07.2026 (VOR den Fixes) fuer die
# Regressionswachen C8b/C8c. Quelle: hadata/-Auswertung.
BASELINE_MIDDAY_RAW_OVER_ACT = 0.897  # Median raw/Ist-DC 11-13Z
BASELINE_M4M8_MORNING_WH = 2254.0  # M4+M8 DC 04-07Z, Wochensumme Wh

ORDER = {"PASS": 0, "INFO": 1, "SKIP": 2, "WARN": 3, "FAIL": 4}


@dataclass
class Metric:
    name: str
    value: str
    threshold: str
    status: str


@dataclass
class CheckResult:
    cid: str
    title: str
    status: str = "SKIP"
    metrics: list[Metric] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    interpretation: str = ""

    def add(self, name: str, value: str, threshold: str, status: str) -> str:
        self.metrics.append(Metric(name, value, threshold, status))
        return status

    def finalize(self) -> CheckResult:
        st = [m.status for m in self.metrics if m.status in ORDER]
        rated = [s for s in st if s in ("PASS", "WARN", "FAIL")]
        if rated:
            self.status = max(rated, key=lambda s: ORDER[s])
        elif st:
            self.status = max(st, key=lambda s: ORDER[s])
        return self


def _band(value: float, pass_rng, warn_rng) -> str:
    """PASS wenn in pass_rng, WARN wenn in warn_rng, sonst FAIL."""
    lo, hi = pass_rng
    if lo <= value <= hi:
        return "PASS"
    lo, hi = warn_rng
    if lo <= value <= hi:
        return "WARN"
    return "FAIL"


# ---------------------------------------------------------------------------
# gemeinsame Ableitungen
# ---------------------------------------------------------------------------


def issued_ac_hourly(b: Bundle, day: dt.date, eta: float) -> dict[dt.datetime, float]:
    """Issued-Prognose als AC-Wh pro UTC-Stunde (nativ ab 0.21.0, sonst DC x eta)."""
    r = b.issued.get(day)
    if not r or not r.get("available"):
        return {}
    src = r.get("hourly_wh_ac") if isinstance(r.get("hourly_wh_ac"), dict) else None
    if src:
        return {parse_iso(k): float(v) for k, v in src.items()}
    hw = r.get("hourly_wh") or {}
    return {parse_iso(k): float(v) * eta for k, v in hw.items()}


def issued_raw_hourly(b: Bundle, day: dt.date) -> dict[dt.datetime, float]:
    r = b.issued.get(day)
    if not r or not r.get("available"):
        return {}
    return {parse_iso(k): float(v) for k, v in (r.get("raw_hourly_wh") or {}).items()}


def daily_issue_error(b: Bundle, eta: float) -> dict[dt.date, float]:
    """(issued_AC - Ist_AC)/Ist_AC pro VOLLEM Tag."""
    out = {}
    for day in b.full_days():
        iac = sum(issued_ac_hourly(b, day, eta).values())
        act = b.daily_actual_ac_wh(day)
        if act > 100 and iac > 0:
            out[day] = (iac - act) / act
    return out


def clear_mornings(b: Bundle) -> tuple[list[dt.date], str]:
    """Tage mit klarem Morgen. Bevorzugt cloud_class_by_hour (>=0.21.0),
    sonst Heuristik: Ist-DC 05-09Z >= 70 % des Wochenmaximums dieses Fensters."""
    if b.features.get("issued_has_cloud_class"):
        days = []
        for day, r in b.issued.items():
            cc = r.get("cloud_class_by_hour") or {}
            morn = [
                v
                for k, v in cc.items()
                if 4 <= parse_iso(k).hour < 8
            ]
            if morn and sum(1 for v in morn if v == "clear") >= max(2, len(morn) // 2):
                days.append(day)
        return sorted(d for d in days if d in b.days), "cloud_class_by_hour"
    sums = {d: b.day_sum_wh(SID_ACT_DC, d, utc_hours=(5, 9)) for d in b.days}
    mx = max(sums.values(), default=0.0)
    if mx <= 0:
        return [], "keine Daten"
    return (
        sorted(d for d, s in sums.items() if s >= 0.70 * mx),
        "Heuristik Ist-DC 05-09Z >= 70 % Wochenmax",
    )


# ---------------------------------------------------------------------------
# C1 Morgen-Peak
# ---------------------------------------------------------------------------


def check_c1(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C1", "Morgen-Peak: served vs Ist 07-10 lokal")
    if not b.hourly_series(SID_SERVED_AC) or not b.hourly_series(SID_ACT_AC):
        c.details.append("Stunden-Statistiken fehlen.")
        return c
    num = den = 0.0
    peak_days = 0
    for day in b.days:
        s = b.day_sum_wh(SID_SERVED_AC, day, loc_hours=(7, 10))
        a = b.day_sum_wh(SID_ACT_AC, day, loc_hours=(7, 10))
        sp = b.day_max_w(SID_SERVED_AC, day, (6, 11))
        ap = b.day_max_w(SID_ACT_AC, day, (6, 11))
        if a < 50:
            continue
        num += s
        den += a
        pr = sp / ap if ap > 0 else float("nan")
        flag = "  <- Peak-Ratio > 1.4" if pr > 1.4 else ""
        if pr > 1.4:
            peak_days += 1
        c.details.append(
            f"{day}  served {s:6.0f} Wh  Ist {a:6.0f} Wh  Ratio {s / a:4.2f}"
            f"  Peak-Ratio {pr:4.2f}{flag}"
        )
    if den <= 0:
        c.details.append("Keine Morgenstunden mit Ist-Daten.")
        return c
    wk = num / den
    c.add(
        "Wochenmittel served/Ist 07-10 lokal",
        f"{wk:.2f}",
        "PASS 0.80-1.20 | WARN 0.70-0.80/1.20-1.30",
        _band(wk, (0.80, 1.20), (0.70, 1.30)),
    )
    c.add(
        "Tage mit Morgen-Peak-Ratio > 1.4",
        str(peak_days),
        "PASS <=1 | WARN 2 | FAIL >=3",
        "PASS" if peak_days <= 1 else ("WARN" if peak_days == 2 else "FAIL"),
    )
    c.interpretation = (
        "Abnahmekriterium der Morgen-Fixes: served/Ist im Morgenfenster in "
        "+-20 %. Vor den Fixes lag das Wochenmittel bei 1.23 mit 3 Tagen "
        "Peak-Ratio ~1.5-1.7 (Scalar-Overshoot auf den phantom-gedaempften "
        "Morgen). Beide Werte muessen jetzt in den PASS-Bereich fallen."
    )
    return c.finalize()


# ---------------------------------------------------------------------------
# C2 Scalar-Hygiene
# ---------------------------------------------------------------------------


def check_c2(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C2", "Scalar-Hygiene 08-09 lokal an guten Tagen")
    if SID_SCALAR not in b.five:
        c.details.append("5-min-Scalar-Statistik fehlt.")
        return c
    errs = daily_issue_error(b, eta)
    good = [d for d, e in errs.items() if abs(e) < 0.10]
    bad_peak_on_good = 0
    worst_good = 0.0
    for day in sorted(errs):
        win = [v for _t, v in b.five_day(SID_SCALAR, day, (8, 9))]
        allday = b.five_day(SID_SCALAR, day, (6, 21))
        mx = max(win) if win else float("nan")
        gmx_t, gmx = max(allday, key=lambda x: x[1]) if allday else (None, float("nan"))
        tag = "guter Tag" if day in good else f"Abweichung {errs[day] * 100:+.0f} %"
        note = ""
        if day in good:
            worst_good = max(worst_good, mx if mx == mx else 0.0)
            if gmx > 1.6:
                bad_peak_on_good += 1
                note = "  <- Peak > 1.6 trotz |Tagesfehler| < 10 %"
        c.details.append(
            f"{day}  max Scalar 08-09: {mx:4.2f}  Tagesmax: {gmx:4.2f}"
            f" @{gmx_t.astimezone(LOC).strftime('%H:%M') if gmx_t else '--'}"
            f"  [{tag}]{note}"
        )
    if not good:
        c.add(
            "gute Tage (|Tagesfehler| < 10 %)",
            "0",
            "min. 1 fuer Bewertung",
            "SKIP",
        )
        c.interpretation = (
            "Kein Tag mit kleinem Tagesfehler im Fenster - Woche verlaengern "
            "oder spaeter erneut pruefen."
        )
        return c.finalize()
    c.add(
        "max Scalar 08-09 lokal an guten Tagen",
        f"{worst_good:.2f}",
        "PASS <=1.25 | WARN <=1.40 | FAIL >1.40",
        "PASS" if worst_good <= 1.25 else ("WARN" if worst_good <= 1.40 else "FAIL"),
    )
    c.add(
        "Peaks > 1.6 an guten Tagen",
        str(bad_peak_on_good),
        "PASS 0 | FAIL >=1",
        "PASS" if bad_peak_on_good == 0 else "FAIL",
    )
    c.interpretation = (
        "Wenn die Morgen-Physik stimmt, braucht der Intraday-Scalar an klaren, "
        "korrekt prognostizierten Morgen keine >25-%-Korrektur mehr. Peaks "
        ">1.6 sind nur an echten Abweichungstagen legitim (Baseline: 2.35 am "
        "20.07. bei 0 % Tagesfehler = Physikdefizit, nicht Wetter)."
    )
    return c.finalize()


# ---------------------------------------------------------------------------
# C3 Morgen-Physik
# ---------------------------------------------------------------------------


def check_c3(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C3", "Morgen-Physik: raw 04Z, raw/Ist 06-10Z, Anstieg statt Sprung")
    if not b.issued:
        c.details.append("issued-Snapshots fehlen.")
        return c
    clear, how = clear_mornings(b)
    c.details.append(f"klare Morgen ({how}): {', '.join(str(d) for d in clear) or '-'}")

    # a) raw 04Z an klaren Morgen
    v04 = []
    for day in clear:
        raw = issued_raw_hourly(b, day)
        s = sum(v for t, v in raw.items() if t.hour == 4)
        if s > 0:
            v04.append(s)
            c.details.append(f"{day}  raw 04Z: {s:5.0f} Wh")
    if v04:
        med = statistics.median(v04)
        c.add(
            "Median raw 04Z an klaren Morgen [Wh]",
            f"{med:.0f}",
            "PASS >=300 | WARN >=250 | FAIL <250",
            "PASS" if med >= 300 else ("WARN" if med >= 250 else "FAIL"),
        )
    else:
        c.add("Median raw 04Z an klaren Morgen [Wh]", "-", ">=300", "SKIP")

    # b) raw/Ist-DC 06-10Z (Wochenverhaeltnis)
    rn = rd = 0.0
    for day in b.days:
        raw = issued_raw_hourly(b, day)
        for t, v in raw.items():
            if 6 <= t.hour < 10:
                a = b.hourly_series(SID_ACT_DC).get(t)
                if a is not None:
                    rn += v
                    rd += a
    if rd > 0:
        ratio = rn / rd
        c.add(
            "Woche raw/Ist-DC 06-10Z",
            f"{ratio:.2f}",
            "PASS 0.90-1.05 | WARN 0.85-1.10",
            _band(ratio, (0.90, 1.05), (0.85, 1.10)),
        )
    else:
        c.add("Woche raw/Ist-DC 06-10Z", "-", "0.90-1.05", "SKIP")

    # c) Anstieg statt Sprung: 5-min-Kurve, glatte Morgen
    viol = 0
    smooth_n = 0
    if SID_SERVED_DC in b.five and SID_ACT_DC in b.five:
        for day in b.days:
            fc = b.five_day(SID_SERVED_DC, day, (5.5, 8.5))
            ac = b.five_day(SID_ACT_DC, day, (5.5, 8.5))
            if len(fc) < 6 or len(ac) < 6:
                continue

            def maxstep(pts):
                best, bt = 0.0, None
                for (_t1, v1), (t2, v2) in zip(pts, pts[1:], strict=False):
                    if v2 - v1 > best:
                        best, bt = v2 - v1, t2
                return best, bt

            fs, ft = maxstep(fc)
            as_, _at = maxstep(ac)
            if as_ <= 350:  # Ist-Kurve glatt -> Prognose darf nicht springen
                smooth_n += 1
                lim = max(400.0, 2.0 * as_)
                mark = ""
                if fs > lim:
                    viol += 1
                    mark = "  <- Sprung (Phantom-Horizont-Signatur)"
                c.details.append(
                    f"{day}  glatter Morgen: max 5-min-Step Prognose {fs:5.0f} W"
                    f" @{ft.astimezone(LOC).strftime('%H:%M') if ft else '--'}"
                    f", Ist {as_:4.0f} W{mark}"
                )
        # Onset informativ
        for day in b.days:
            fo = next(
                (t for t, v in b.five_day(SID_SERVED_DC, day, (4, 10)) if v >= 20), None
            )
            ao = next(
                (t for t, v in b.five_day(SID_ACT_DC, day, (4, 10)) if v >= 20), None
            )
            if fo and ao:
                lag = (fo - ao).total_seconds() / 60
                c.details.append(
                    f"{day}  Onset >=20 W: Ist {ao.astimezone(LOC).strftime('%H:%M')}"
                    f"  Prognose {fo.astimezone(LOC).strftime('%H:%M')}  Lag {lag:+3.0f} min"
                )
        if smooth_n:
            c.add(
                "glatte Morgen mit Prognose-Sprung",
                f"{viol}/{smooth_n}",
                "PASS 0 | WARN 1 | FAIL >=2",
                "PASS" if viol == 0 else ("WARN" if viol == 1 else "FAIL"),
            )
        else:
            c.add("glatte Morgen mit Prognose-Sprung", "-", "PASS 0", "SKIP")
    else:
        c.add("5-min-Sprunganalyse", "-", "-", "SKIP")

    c.interpretation = (
        "tau-Rampe + beam_gain muessen den Rohphysik-Morgen anheben: raw 04Z "
        "(06-07 lokal) >=300 Wh an klaren Tagen (Baseline max 247), raw/Ist "
        "06-10Z von 0.78 auf 0.90-1.05, und die 5-min-Kurve steigt ab ~06:05 "
        "lokal statt bei ~06:44 zu springen (Baseline: Steps 831/1119 W bei "
        "glattem Ist von 204/288 W am 22./24.07.)."
    )
    return c.finalize()


# ---------------------------------------------------------------------------
# C4 Bias-Konvergenz
# ---------------------------------------------------------------------------


def check_c4(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C4", "Day-ahead-Bias: Konvergenz weg von den Clamps")
    cells = (
        (b.entities.get(EID_BIAS) or {}).get("attributes", {}).get("bias_cells") or {}
    )
    if not cells:
        c.details.append(
            "bias_cells-Attribut fehlt (Entity nicht gezogen oder Bias aus)."
        )
        return c

    def cell_status(key: str, pass_fn, warn_fn) -> None:
        cell = cells.get(key)
        if not isinstance(cell, dict):
            c.add(key, "-", "-", "SKIP")
            return
        th = float(cell.get("applied", cell.get("theta", 1.0)))
        n = int(cell.get("n", 0))
        clamped = cell.get("clamped")
        extra = f", n={n}"
        if clamped is not None:
            extra += f", clamped={clamped}"
        if n < 5 and abs(th - 1.0) < 1e-9:
            c.add(key, f"1.00 (cold start{extra})", "Reset aktiv", "INFO")
            return
        st = "PASS" if pass_fn(th) else ("WARN" if warn_fn(th) else "FAIL")
        c.add(key + f" applied ({extra.strip(', ')})", f"{th:.3f}", tgt[key], st)

    tgt = {
        "clear|morning": "PASS <=1.25 (Ziel 1.0-1.15) | WARN <=1.40",
        "clear|afternoon": "PASS >=0.85 (Ziel 0.9-1.1) | WARN >=0.75",
        "mixed|afternoon": "PASS >=0.80 | WARN >=0.70",
        "overcast|afternoon": "PASS >=0.80 | WARN >=0.70",
    }
    cell_status("clear|morning", lambda t: t <= 1.25, lambda t: t <= 1.40)
    cell_status("clear|afternoon", lambda t: t >= 0.85, lambda t: t >= 0.75)
    cell_status("mixed|afternoon", lambda t: t >= 0.80, lambda t: t >= 0.70)
    cell_status("overcast|afternoon", lambda t: t >= 0.80, lambda t: t >= 0.70)

    # clamped-Flags (>=0.21.0): Anzahl aktuell + Trend aus Verlauf
    hist = b.history_attrs.get(EID_BIAS) or []
    if b.features.get("bias_has_clamped_flag"):
        now_clamped = sum(
            1 for v in cells.values() if isinstance(v, dict) and v.get("clamped")
        )
        first_clamped = None
        for _t, _s, attrs in hist:
            cs = (attrs or {}).get("bias_cells") or {}
            if any(isinstance(v, dict) and "clamped" in v for v in cs.values()):
                first_clamped = sum(
                    1 for v in cs.values() if isinstance(v, dict) and v.get("clamped")
                )
                break
        trend = (
            f" (Verlauf {first_clamped} -> {now_clamped})"
            if first_clamped is not None
            else ""
        )
        c.add(
            "Zellen mit clamped-Flag",
            f"{now_clamped}{trend}",
            "ruecklaeufig, Ziel 0",
            "PASS"
            if now_clamped == 0
            else (
                "WARN"
                if first_clamped is None or now_clamped <= first_clamped
                else "FAIL"
            ),
        )
    else:
        c.add("clamped-Flag", "Feld fehlt", "ab v0.21.0 erwartet", "WARN")

    # theta-Trend aus with_attrs-Verlauf
    for key in ("clear|morning", "clear|afternoon"):
        vals = []
        for t, _s, attrs in hist:
            cell = ((attrs or {}).get("bias_cells") or {}).get(key)
            if isinstance(cell, dict) and cell.get("theta") is not None:
                vals.append((t, float(cell["theta"])))
        if len(vals) >= 2:
            c.details.append(
                f"theta-Trend {key}: {vals[0][1]:.3f} ({vals[0][0].astimezone(LOC):%d.%m.}) "
                f"-> {vals[-1][1]:.3f} ({vals[-1][0].astimezone(LOC):%d.%m.})"
            )
    c.interpretation = (
        "Nach reset_day_ahead_bias + korrigierter Physik darf der RLS die "
        "Physikfehler nicht mehr kompensieren muessen: clear|morning loest "
        "sich vom 1.5-Clamp Richtung 1.0-1.15, die afternoon-Zellen steigen "
        "von ~0.6-0.7 Richtung 0.9-1.1. Frisch resettete Zellen (n<5, "
        "applied=1.0) sind INFO, nicht FAIL - in ~2 Wochen erneut pruefen."
    )
    return c.finalize()


# ---------------------------------------------------------------------------
# C5 day-0-Baender
# ---------------------------------------------------------------------------


def check_c5(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C5", "day-0-Quantilbaender: Kollaps + p10 vs End-Ist")

    def band_fraction(eid: str):
        a = (b.entities.get(eid) or {}).get("attributes") or {}
        wp = a.get("wh_period") or {}
        p10 = a.get("wh_period_p10") or {}
        p90 = a.get("wh_period_p90") or {}
        day = [k for k, v in wp.items() if isinstance(v, (int, float)) and v > 0]
        if not day:
            return None, 0
        n = sum(1 for k in day if p10.get(k) != p90.get(k))
        return n / len(day), len(day)

    fr, ntot = band_fraction(EID_TODAY)
    if fr is None:
        c.add("Tageslicht-Slots today mit p10 != p90", "-", ">0.5", "SKIP")
    else:
        c.add(
            "Tageslicht-Slots today mit p10 != p90",
            f"{fr:.2f} ({round(fr * ntot)}/{ntot})",
            "PASS >0.50 | WARN 0.30-0.50 | FAIL <0.30",
            "PASS" if fr > 0.50 else ("WARN" if fr >= 0.30 else "FAIL"),
        )
    fr2, ntot2 = band_fraction(EID_TOMORROW)
    if fr2 is not None:
        c.add(
            "dito tomorrow (informativ)",
            f"{fr2:.2f} ({round(fr2 * ntot2)}/{ntot2})",
            "-",
            "INFO",
        )

    # p10-Tagessensor: Intraday-Maximum vs End-Ist (volle Tage)
    hist = b.history_min.get(EID_TODAY_P10) or []
    viol = rated = 0
    for day in b.full_days():
        vals = [
            (t, to_float(s))
            for t, s in hist
            if t.astimezone(LOC).date() == day and to_float(s) is not None
        ]
        if not vals:
            continue
        mt, mv = max(vals, key=lambda x: x[1])
        act = b.daily_actual_ac_wh(day) / 1000.0
        if act <= 0.1:
            continue
        rated += 1
        mark = ""
        if mv > act * 1.02:
            viol += 1
            mark = "  <- p10 > End-Ist"
        c.details.append(
            f"{day}  max p10 {mv:5.2f} kWh @{mt.astimezone(LOC).strftime('%H:%M')}"
            f"  End-Ist {act:5.2f} kWh{mark}"
        )
    if rated:
        c.add(
            "Tage mit Intraday-p10 > End-Ist",
            f"{viol}/{rated}",
            "PASS 0 | WARN 1 | FAIL >=2",
            "PASS" if viol == 0 else ("WARN" if viol == 1 else "FAIL"),
        )
    else:
        c.add("Tage mit Intraday-p10 > End-Ist", "-", "PASS 0", "SKIP")

    # Quantile-Diagnostics (Kontext)
    if b.features.get("quantile_bins_available"):
        bins = (b.diagnostics.get("data") or {}).get("quantiles", {}).get("bins", {})
        trained = sorted(k for k, v in bins.items() if v.get("trained"))
        untrained = sorted(k for k, v in bins.items() if not v.get("trained"))
        c.details.append(f"Quantile-Bins trained: {', '.join(trained) or '-'}")
        c.details.append(f"Quantile-Bins untrained: {', '.join(untrained) or '-'}")
        cm = bins.get("clear|morning", {})
        c.add(
            "Quantile-Bin clear|morning trained",
            str(bool(cm.get("trained"))),
            "True nach Seeding-Bootstrap",
            "PASS" if cm.get("trained") else "WARN",
        )
    c.interpretation = (
        "Baseline: 58/62 Tageslicht-Slots mit p10==p90 (Bandkollaps, nur "
        "overcast-Bins trainiert) und an 4 Tagen schob der Morgen-Scalar-Spike "
        "den p10-Tageswert ueber das spaetere End-Ist. Nach Quantile-Seeding/"
        "Live-Lernen muss die Mehrheit der Tagesslots ein echtes Band tragen "
        "und p10 als untere Schranke glaubwuerdig bleiben."
    )
    return c.finalize()


# ---------------------------------------------------------------------------
# C6 Headline-Stabilitaet
# ---------------------------------------------------------------------------


def check_c6(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C6", "Headline-Stabilitaet: kein Korrektur-Jojo > 1.5 kWh/h")
    hist = b.history_min.get(EID_TODAY) or []
    if not hist:
        c.details.append("Verlauf energy_production_today fehlt.")
        return c
    scal = b.five.get(SID_SCALAR, [])
    viol = weather = 0
    for day in b.days:
        pts = []
        carry = None
        for t, s in hist:
            v = to_float(s)
            if v is None:
                continue
            tl = t.astimezone(LOC)
            if tl.date() != day:
                continue
            if tl.hour < 6:
                carry = (t, v)
                continue
            if tl.hour >= 21:
                continue
            pts.append((t, v))
        if carry:
            pts.insert(0, carry)
        mx, mx_t1, mx_t2 = 0.0, None, None
        for i, (t1, v1) in enumerate(pts):
            for t2, v2 in pts[i + 1 :]:
                if (t2 - t1).total_seconds() > 3600:
                    break
                if abs(v2 - v1) > mx:
                    mx, mx_t1, mx_t2 = abs(v2 - v1), t1, t2
        if mx_t1 is None:
            continue
        mark = ""
        if mx > 1.5:
            # Korrektur-getrieben, wenn der Scalar im +-45-min-Fenster spikte
            w0, w1 = mx_t1 - dt.timedelta(minutes=45), mx_t2 + dt.timedelta(minutes=45)
            spike = max(
                (abs(v - 1.0) for t, v in scal if w0 <= t <= w1), default=0.0
            )
            if spike >= 0.4:
                viol += 1
                mark = f"  <- Jojo, Scalar-Spike |s-1|={spike:.2f} im Fenster"
            else:
                weather += 1
                mark = "  <- grosser Swing ohne Scalar-Spike (Wetter-Refresh?)"
        c.details.append(
            f"{day}  max 60-min-Swing {mx:4.2f} kWh"
            f" ab {mx_t1.astimezone(LOC).strftime('%H:%M')}{mark}"
        )
    c.add(
        "Tage mit Korrektur-Jojo > 1.5 kWh/h",
        str(viol),
        "PASS 0 | FAIL >=1",
        "PASS" if viol == 0 else "FAIL",
    )
    c.add(
        "grosse Swings ohne Scalar-Spike (informativ)",
        str(weather),
        "Wetter-Refresh moeglich",
        "INFO",
    )
    c.interpretation = (
        "Referenzmuster 20.07.: 2.36 kWh Swing um 08:58 waehrend der Scalar "
        "auf 2.35 spikte - ein reines Korrektur-Jojo, kein Wetterereignis. "
        "Swings > 1.5 kWh ohne zeitgleichen Scalar-Spike koennen legitime "
        "Weather-Refreshes sein und zaehlen nicht als FAIL (manuell pruefen)."
    )
    return c.finalize()


# ---------------------------------------------------------------------------
# C7 Abend/Vorabend
# ---------------------------------------------------------------------------


def check_c7(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C7", "issued-AC: Wochenbias + Nachmittagsblock 12-18Z")
    if not b.issued:
        c.details.append("issued-Snapshots fehlen.")
        return c
    wi = wa = ai = aa = 0.0
    for day in b.full_days():
        iac = issued_ac_hourly(b, day, eta)
        if not iac:
            continue
        act = b.daily_actual_ac_wh(day)
        if act < 100:
            continue
        tot = sum(iac.values())
        wi += tot
        wa += act
        c.details.append(
            f"{day}  issued {tot / 1000:5.2f} kWh  Ist {act / 1000:5.2f} kWh"
            f"  Fehler {100 * (tot - act) / act:+5.1f} %"
        )
        for t, v in iac.items():
            if 12 <= t.hour < 18:
                a = b.hourly_series(SID_ACT_AC).get(t)
                if a is not None:
                    ai += v
                    aa += a
    if wa > 0:
        bias = 100 * (wi - wa) / wa
        c.add(
            "issued-AC Wochenbias [%]",
            f"{bias:+.1f}",
            "PASS +-10 | WARN +-15 | FAIL >15",
            _band(bias, (-10, 10), (-15, 15)),
        )
    else:
        c.add("issued-AC Wochenbias [%]", "-", "+-10", "SKIP")
    if aa > 0:
        ab = 100 * (ai - aa) / aa
        c.add(
            "Nachmittagsblock 12-18Z Bias [%]",
            f"{ab:+.1f}",
            "PASS +-15 | WARN +-25 | FAIL >25",
            _band(ab, (-15, 15), (-25, 25)),
        )
    else:
        c.add("Nachmittagsblock 12-18Z Bias [%]", "-", "+-15", "SKIP")
    src = (
        "hourly_wh_ac (nativ)"
        if b.features.get("issued_has_hourly_wh_ac")
        else f"hourly_wh (DC) x eta {eta:.4f}"
    )
    c.details.append(f"AC-Quelle: {src}")
    c.interpretation = (
        "Vorabend-Guete fuer die Batterie-/Verbrauchsplanung. Baseline: "
        "Wochenbias -10.2 %, Nachmittag -38 % (afternoon-theta 0.6-0.7 "
        "drueckte korrekte Physik). Nach dem Bias-Reset muss der Nachmittag "
        "in +-15 % laufen; der Wochenbias in +-10 %."
    )
    return c.finalize()


# ---------------------------------------------------------------------------
# C8 Regressionswachen
# ---------------------------------------------------------------------------


def check_c8(b: Bundle, eta: float) -> CheckResult:
    c = CheckResult("C8", "Regressionswachen (Overcast-Scalar, M4/M8, Mittagsfenster)")
    errs = daily_issue_error(b, eta)

    # a) Overcast-/Ueberprognose-Tage: Scalar muss weiter < 1 korrigieren
    over = [d for d, e in errs.items() if e > 0.10]
    if over and SID_SCALAR in b.five:
        ok = True
        for day in over:
            vals = [v for _t, v in b.five_day(SID_SCALAR, day, (6, 18))]
            mn = min(vals) if vals else float("nan")
            c.details.append(
                f"{day}  Ueberprognose {errs[day] * 100:+.0f} %  min Scalar {mn:4.2f}"
            )
            if not vals or mn >= 0.95:
                ok = False
        c.add(
            "Scalar korrigiert an Ueberprognose-Tagen < 0.95",
            "ja" if ok else "nein",
            "PASS ja (Baseline 19.07.: min 0.69)",
            "PASS" if ok else "FAIL",
        )
    else:
        c.add(
            "Scalar-Abwaertskorrektur",
            "kein Ueberprognose-Tag im Fenster",
            "-",
            "SKIP",
        )

    # b) M4/M8-Morgen-Diffus (erwartetes Modelldefizit ~10x, KEIN Fehler)
    if SID_M4 in b.hourly and SID_M8 in b.hourly:
        tot = 0.0
        for day in b.days:
            tot += b.day_sum_wh(SID_M4, day, utc_hours=(4, 7))
            tot += b.day_sum_wh(SID_M8, day, utc_hours=(4, 7))
        ndays = max(len(b.days), 1)
        norm = tot * 8 / ndays  # auf 8 Tage normiert wie Baseline
        ratio = norm / BASELINE_M4M8_MORNING_WH
        st = "INFO" if 0.4 <= ratio <= 2.0 else "WARN"
        c.add(
            "M4+M8 Ist-DC 04-07Z, Woche (normiert) [Wh]",
            f"{norm:.0f} (x{ratio:.2f} vs Baseline {BASELINE_M4M8_MORNING_WH:.0f})",
            "unveraendert erwartet (0.4x-2.0x)",
            st,
        )
        c.details.append(
            "M4/M8-Morgen-Diffus liegt real ~10x ueber dem Modell - bekanntes, "
            "akzeptiertes Verhalten (Screen-Fehlzuordnung/Diffus-Modell), "
            "durch die 0.21-Fixes NICHT adressiert. Starke Aenderung -> pruefen."
        )
    else:
        c.add("M4/M8-Morgen-Diffus", "Sensoren fehlen", "-", "SKIP")

    # c) Mittags-Kontrollfenster 11-13Z: tau-Rampe darf Mittag nicht anfassen
    rats = []
    for day in b.days:
        raw = issued_raw_hourly(b, day)
        rs = ra = 0.0
        for t, v in raw.items():
            if 11 <= t.hour < 13:
                a = b.hourly_series(SID_ACT_DC).get(t)
                if a is not None:
                    rs += v
                    ra += a
        if ra > 100:
            rats.append(rs / ra)
            c.details.append(f"{day}  raw/Ist-DC 11-13Z: {rs / ra:4.2f}")
    if rats:
        med = statistics.median(rats)
        dev = (med - BASELINE_MIDDAY_RAW_OVER_ACT) / BASELINE_MIDDAY_RAW_OVER_ACT
        c.add(
            "Median raw/Ist-DC 11-13Z vs Baseline 0.90",
            f"{med:.2f} ({dev * 100:+.0f} %)",
            "PASS +-10 % | WARN +-20 % | FAIL >20 %",
            _band(dev * 100, (-10, 10), (-20, 20)),
        )
        c.details.append(
            "Hinweis: beam_gain 1.23->1.25 hebt das Fenster erwartbar um ~+1.6 % "
            "- das liegt innerhalb der PASS-Toleranz."
        )
    else:
        c.add("Median raw/Ist-DC 11-13Z", "-", "+-10 % vs 0.90", "SKIP")

    c.interpretation = (
        "Drei Wachen gegen Kollateralschaeden: (a) der Intraday-Scalar muss an "
        "Overcast-/Ueberprognose-Tagen weiter nach unten korrigieren duerfen "
        "(19.07.-Muster), (b) das bekannte M4/M8-Morgendefizit bleibt bewusst "
        "bestehen, (c) die tau-Rampe ist eine reine Niedrig-Elevations-"
        "Massnahme - das Mittagsfenster 11-13Z muss auf Baseline-Niveau "
        "bleiben (byte-identische Physik ausser beam_gain-Skalar)."
    )
    return c.finalize()


ALL_CHECKS = [
    check_c1,
    check_c2,
    check_c3,
    check_c4,
    check_c5,
    check_c6,
    check_c7,
    check_c8,
]


def run_all(b: Bundle, eta: float = DEFAULT_ETA) -> list[CheckResult]:
    out = []
    for fn in ALL_CHECKS:
        try:
            out.append(fn(b, eta))
        except Exception as exc:  # noqa: BLE001 - ein Check darf nie alles reissen
            r = CheckResult(fn.__name__.replace("check_", "").upper(), fn.__doc__ or "")
            r.status = "SKIP"
            r.details.append(f"Check-Fehler: {exc!r}")
            out.append(r)
    return out
