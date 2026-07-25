# Post-Deployment-Validierung `balcony_solar_forecast` v0.21.0

Runbook + Skriptpaket, um **~1 Woche nach dem Deployment** von v0.21.0 samt
Config-Änderungen (tau-Rampe Ost-Horizont, `bifacial_beam_gain` 1.25,
`reset_day_ahead_bias`) objektiv zu prüfen, ob die Fixes wirken — und ob sie
nichts kaputt gemacht haben.

Die Schwellen aller Checks sind gegen die **VOR-Fix-Woche 17.–24.07.2026**
kalibriert: auf diesen Daten zeigen C1–C7 FAIL und die Regressionswachen C8
PASS (Selbsttest, siehe unten). Nach einer erfolgreichen Fix-Woche müssen
C1–C7 grün werden, während C8 grün **bleibt**.

---

## 1. Voraussetzungen

- Windows (oder Linux/macOS), **Python ≥ 3.11** (getestet mit 3.13/3.14).
  Nur Standardbibliothek — kein `pip install` nötig (numpy/pandas werden
  nicht verwendet).
- Home Assistant 2026.7.x erreichbar, Integration `balcony_solar_forecast`
  v0.21.0 (das Skript erkennt und meldet, wenn noch v0.20.6 läuft).
- Netzzugang zur HA-Instanz. **IP verwenden, nicht Hostname** — `hass` ist
  aus manchen Clients nicht auflösbar: `http://10.102.10.11:8123`.
- Recorder mit Langzeit-Statistiken aktiv (Standard); Analysefenster
  mindestens 5–7 volle Tage nach dem Deployment.

### Token erzeugen

1. HA-Frontend → Profil (Avatar unten links) → Tab **Sicherheit**.
2. Abschnitt **Langlebige Zugriffstoken** → *Token erstellen*,
   Name z. B. `bsf-validation`.
3. Token einmalig kopieren. Der Token braucht ein **Admin**-Konto
   (Diagnostics-Endpoint); ohne Admin laufen alle Checks außer den
   Quantile-/Versions-Zusatzinfos trotzdem.
4. Nach der Validierung Token wieder löschen (Profil → gleiche Stelle).

---

## 2. Aufrufe

Alle Kommandos aus dem Ordner `validation/` heraus.

### Live-Validierung (Standardfall, ~1 Woche nach Deployment)

```powershell
python validate.py --ha-url http://10.102.10.11:8123 --token "<TOKEN>" --json report.json
```

Das Skript zieht alle Daten (REST + WebSocket) in einen Ordner
`bsf_pull_<timestamp>/` und analysiert sie sofort. Der Ordner ist ein
vollständiger Snapshot — die Analyse lässt sich später beliebig oft offline
wiederholen:

```powershell
python validate.py --offline --data-dir bsf_pull_20260801_0900
```

### Weitere Optionen

| Option | Bedeutung |
|---|---|
| `--days N` | Analysefenster (Default 8 Tage rückwärts ab heute) |
| `--data-dir PFAD` | Zielordner für den Pull bzw. Quellordner für `--offline` |
| `--fetch-only` | nur Daten ziehen (Analyse später offline) |
| `--json PFAD` | Report zusätzlich maschinenlesbar exportieren |
| `--eta 0.9248` | DC→AC-Fallback-Wirkungsgrad (nur relevant, solange `get_issued_forecast` kein `hourly_wh_ac` liefert) |
| `--entry-id ID` | nur nötig bei mehreren konfigurierten Sites |

Exit-Code: `0` = alles grün, `1` = nur WARN, `2` = mindestens ein FAIL
(automatisierbar, z. B. wöchentlicher Task).

### Offline-Selbsttest (Kalibrierungsnachweis)

```powershell
python validate.py --offline --data-dir ..\hadata
```

Erwartung auf der VOR-Fix-Woche: **C1–C7 FAIL, C8 PASS** (Exit 2). Damit ist
belegt, dass die Checks die bekannten Defekte tatsächlich erkennen und die
Regressionswachen nicht fälschlich anschlagen.

---

## 3. Was geprüft wird (Prüfkatalog)

| Check | Frage | PASS-Kriterium | Baseline (vor Fix) |
|---|---|---|---|
| **C1** Morgen-Peak | Ist die servierte AC-Prognose 07–10 lokal ehrlich? | Wochenmittel served/Ist 0.80–1.20 **und** ≤1 Tag mit Peak-Ratio >1.4 | 1.23; 3 Tage ~1.5–1.7 |
| **C2** Scalar-Hygiene | Muss der Intraday-Scalar morgens noch Physik kompensieren? | an Tagen mit \|Tagesfehler\|<10 %: max Scalar 08–09 lokal ≤1.25; Peaks >1.6 nur an Abweichungstagen | 2.35 am 20.07. bei 0 % Tagesfehler |
| **C3** Morgen-Physik | Hebt die tau-Rampe den Roh-Morgen, ohne Sprung? | raw 04Z ≥300 Wh (klare Morgen); raw/Ist 06–10Z 0.90–1.05; keine Prognose-Sprünge an glatten Morgen | 226 Wh; 0.78; 3/3 Sprünge (~06:44 statt Anstieg ab ~06:05) |
| **C4** Bias-Konvergenz | Löst sich der RLS von den Clamps? | clear\|morning ≤1.25 (Ziel 1.0–1.15); afternoon-Zellen ≥0.85; clamped-Flags rückläufig | 1.491 (am 1.5-Clamp); 0.58–0.71 |
| **C5** day-0-Bänder | Trägt der Tag echte p10/p90-Bänder? | >50 % der Tageslicht-Slots mit p10≠p90; kein Tag mit Intraday-p10 > End-Ist | 4/62 Slots; 3 Tage p10>Ist |
| **C6** Headline-Stabilität | Kein Korrektur-Jojo der Tagesprognose? | kein 60-min-Swing >1.5 kWh, der mit einem Scalar-Spike zusammenfällt | 2.36 kWh am 20.07. bei Scalar 2.35 |
| **C7** Abend/Vorabend | Stimmt die Vorabend-Prognose als Planungsbasis? | issued-AC-Wochenbias ±10 %; Nachmittag 12–18Z ±15 % | −10.2 %; −38.1 % |
| **C8** Regressionswachen | Haben die Fixes nichts beschädigt? | (a) Scalar korrigiert an Überprognose-Tagen weiter <0.95, (b) M4/M8-Morgen-Diffus unverändert (bekanntes ~10×-Defizit, **kein Fehler**), (c) Mittagsfenster 11–13Z raw/Ist ±10 % vs. Baseline 0.90 | alle PASS |

Anmerkungen zur Semantik (im Skript zentral behandelt):

- **DC vs. AC**: `raw_hourly_wh`/`hourly_wh` aus `get_issued_forecast` sind
  bis v0.20.6 DC-basiert. AC-Vergleiche (C2/C7) nutzen ab v0.21.0 das neue
  `hourly_wh_ac`; fehlt es, wird DC×η (Default 0.9248, Victron-gelernt)
  gerechnet und im Report ausgewiesen. C3/C8c vergleichen bewusst DC↔DC.
- **epoch-ms**: Die Recorder-WS-API liefert `start` in Millisekunden — wird
  automatisch erkannt (kein 1970er-Bug möglich).
- **Partielle Tage** (heute, Lücken) werden erkannt und aus Tagessummen-
  Checks ausgeschlossen; Morgen-Checks nutzen sie weiter.
- **Fehlende v0.21.0-Felder** (`hourly_wh_ac`, `cloud_class_by_hour`,
  `clamped`-Flag) führen nie zum Crash: der Report meldet sie im Block
  „Feature-Erkennung“ und rechnet mit dokumentiertem Fallback.

---

## 4. Interpretation & Nachjustierung („Wenn ein Check rot bleibt“)

Reihenfolge beachten: **erst C8 ansehen, dann C3, dann den Rest** — C1/C2/C6
hängen kausal an der Morgen-Physik (C3), C4/C7 am Bias-Reset.

- **C8c FAIL (Mittagsfenster verschoben)** → Stopp. Die tau-Rampe wirkt
  nicht nur bei niedriger Sonne (Config-Fehler: Rampe reicht in hohe
  Elevationen). Horizont-YAML prüfen, bevor irgendetwas anderes
  nachjustiert wird. (+1–2 % durch beam_gain 1.25 sind erwartbar und PASS.)
- **C3 FAIL, raw/Ist 06–10Z weiter <0.90** → Morgen-Physik hebt zu wenig:
  `bifacial_beam_gain` 1.25 → **1.30** anheben (ein Schritt, dann erneut
  eine Woche messen). Liegt nur `raw 04Z` unter 300 Wh, ist eher der
  Horizont-/tau-Start zu spät → tau-Rampe eine Stufe früher beginnen lassen.
- **C3 FAIL, weiterhin Sprung ~06:44** → tau-Rampe greift nicht (Deployment/
  Config nicht aktiv?). Diagnostics im Report prüfen: läuft wirklich
  v0.21.0? Horizont der OSO-Planes (M2/3/6/7) kontrollieren.
- **C3 PASS, aber C1/C2 weiter FAIL** → Physik stimmt, aber der Scalar
  überschießt noch (Gedächtnis der alten Woche oder zu aggressive
  Verstärkung). Erst 2–3 weitere Tage abwarten; bleibt es, Intraday-Clamp/
  Tau in der Integration prüfen (kein Config-Knopf — Issue aufmachen).
- **C4 FAIL: clear|morning wieder >1.4** → Der RLS lernt erneut gegen ein
  Physikdefizit an → C3 ist in Wahrheit nicht gelöst (siehe dort). Nach
  `reset_day_ahead_bias` brauchen Zellen ~5 Tage (`n≥5`), vorher meldet der
  Check INFO „cold start“ — das ist kein Fehler.
- **C4 FAIL: afternoon-Zellen bleiben ≤0.75** → Nachmittag wird real
  überprognostiziert (nicht mehr Bias-Artefakt). AC-Limit-Clipping (800 W
  pro WR) und Wand-Schatten az≈212° ab ~14:20 lokal gegen die Kurven halten;
  ggf. Screen-/Shademap-Zuordnung revisited (bekannte M2/M3-Fehlzuordnung).
- **C5 FAIL: Bänder weiter kollabiert** → Quantile-Seeding-Bootstrap nicht
  gelaufen oder Bins weiter untrained (Report listet trained/untrained).
  Bootstrap erneut ausführen; danach müssen mindestens die clear-Bins
  trained sein.
- **C5 FAIL: p10 > End-Ist** trotz Band → p10-Kopplung an den Scalar-Spike
  besteht noch; zusammen mit C2 lesen (gleiche Wurzel).
- **C6 FAIL** → nur relevant, wenn der Swing mit einem Scalar-Spike
  zusammenfällt (steht in der Zeile). Reine Wetter-Refresh-Swings zählen
  nicht — bei Verdacht die im Report genannte Uhrzeit gegen die
  Weather-Fetch-Zeiten (30-min-Raster) halten.
- **C7 FAIL: Nachmittag weiter < −15 %** → solange C4-afternoon noch am
  alten Wert klebt: Bias-Reset wirklich ausgeführt? Wenn C4 grün und C7
  trotzdem rot → echte Nachmittags-Physik (siehe C4-afternoon-Punkt).
- **C8b WARN (M4/M8-Morgen-Diffus stark verändert)** → nicht Teil der
  0.21-Fixes; prüfen, ob versehentlich Screen-/Shademap-Änderungen
  deployt wurden. Das ~10×-Defizit selbst ist **erwartet und akzeptiert**.

Generell: **eine Stellschraube pro Woche** (beam_gain ODER tau-Rampe ODER
Reset), sonst ist die nächste Messwoche nicht attribuierbar.

---

## 5. Paketinhalt

| Datei | Zweck |
|---|---|
| `validate.py` | CLI: Fetch + Analyse + Report (Tabelle, `--json`) |
| `bsf_fetch.py` | Datenbezug: REST (`/api/states`, `history/period`, Service `get_issued_forecast` mit `return_response`, Diagnostics) + minimaler stdlib-WebSocket-Client für `recorder/statistics_during_period` (hour + 5minute) |
| `bsf_data.py` | Laden/Normalisieren (epoch-ms, minimal_response, Zeitzone Europe/Berlin inkl. Fallback ohne tzdata) |
| `bsf_checks.py` | Prüfkatalog C1–C8 inkl. Schwellen und Baseline-Konstanten |
| `README.md` | dieses Runbook |

Der Pull-Ordner enthält dieselben fünf JSON-Dateien wie das Referenzpaket
`hadata/` (`actuals_hourly_stats.json`, `fiveminute_stats.json`,
`entities_now.json`, `forecast_sensor_history.json`,
`issued_forecasts_and_diag.json`) — Live- und Offline-Analyse sind dadurch
identisch und jeder Report reproduzierbar.

## 6. Bekannte Annahmen / Grenzen

- Der WebSocket-Client ist minimal (RFC 6455, nur was HA braucht); bei
  Proxys/HTTPS mit Sonderkonfiguration ggf. direkt gegen die interne
  HTTP-IP gehen.
- η=0.9248 ist der Victron-gelernte Gesamtwirkungsgrad; sobald v0.21.0
  `hourly_wh_ac` liefert, wird η ignoriert.
- „Klare Morgen“ ohne `cloud_class_by_hour` (v0.20.6) per Heuristik
  (Ist-DC 05–09Z ≥70 % Wochenmax) — mit v0.21.0 automatisch exakt.
- C8b/C8c vergleichen gegen fest einkodierte Baseline-Konstanten der
  Juli-Woche (2254 Wh bzw. 0.90); bei stark anderer Jahreszeit sind diese
  beiden Wachen nur noch orientierend (Saisongang), die übrigen Checks
  bleiben gültig.
- Die Weather-Refresh-Erkennung in C6 ist indirekt (Scalar-Koinzidenz);
  echte Refresh-Events loggt die Integration nicht in den Recorder.
