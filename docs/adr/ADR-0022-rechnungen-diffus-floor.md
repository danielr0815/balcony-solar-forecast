# Begleit-Rechnung zu ADR-0022: Diffus-Floor / Wand-SVF (Thema 2)

Datenquelle: Live-HA-Recorder-Stundenstatistiken (Stundenmittel W, epoch-ms),
Zuordnung port→Modul (M4 = sensor.inverter_port_2_dc_power_2,
M8 = …_dc_power_4, M2 = …_port_2_dc_power). Modellwerte aus der
Offline-Reproduktion der Engine-Physik (7-Tage-Morgen-Physik-Forensik;
SVF 0,288/0,294 aus den Live-Diagnostics). Alle Zeiten UTC.

## 1. Roh-Extraktion (Stundenmittel W, 04Z / 05Z / 11Z / 14Z)

Extraktion (Python, reproduzierbar):

```python
import json, datetime
d = json.load(open("hadata/actuals_hourly_stats.json"))
def hourly(sid):
    return {datetime.datetime.fromtimestamp(r["start"]/1000,
            datetime.timezone.utc).strftime("%m-%d %H"): r.get("mean")
            for r in d["stats"][sid]}
```

| Tag | Typ | M4 04Z | M4 05Z | M8 04Z | M8 05Z | M2 04Z (Referenz OSO) | M4 11Z | M4 14Z |
|---|---|---|---|---|---|---|---|---|
| 17.07. | mixed | 22,6 | 29,5 | 23,8 | 30,8 | 60,7 | 221,2 | 23,9 |
| 18.07. | klar | 27,1 | 59,8 | 29,7 | 60,4 | 72,7 | 238,1 | 23,4 |
| **19.07.** | **overcast** | **8,1** | 33,0 | **9,3** | 34,7 | 14,6 | 131,7 | 27,7 |
| 20.07. | klar | 22,5 | 50,8 | 25,0 | 52,5 | 59,7 | 248,7 | 24,6 |
| 21.07. | klar | 24,6 | 61,8 | 26,8 | 63,1 | 65,9 | 218,9 | 30,6 |
| 22.07. | klar | 26,6 | 66,2 | 29,1 | 66,4 | 67,4 | 95,5 | 31,8 |
| 23.07. | mixed | 23,6 | 63,3 | 25,9 | 64,2 | 56,1 | 178,4 | 31,2 |
| 24.07. | wolkenlos | 29,2 | 66,6 | 31,6 | 66,7 | 71,6 | 300,5 | 20,2 |

Beobachtungen:
- Klar 04Z: M4 22,5–29,2 W; Overcast 04Z: 8,1 W ⇒ Faktor ~3 zwischen den
  Regimen. Der M4/M8-Dämmerungs-Floor ist also NICHT rein isotrop —
  ein substantieller Anteil existiert nur bei klarem Himmel (Beam vorhanden).
- M4 14Z (wandverschattet, Sonne hinter Hauswand-Kante ~12:22Z): 20–32 W auf
  ALLEN Tagestypen ≈ gleiches Niveau wie der Morgen-Floor ⇒ derselbe
  Diffus-Floor-Mechanismus wirkt auch nachmittags; D2 hilft dort mit.

## 2. Umrechnung DC ↔ POA

Näherung bei kleinem POA (Ross-Derate vernachlässigbar):
`POA [W/m²] ≈ P_DC / (Wp · η_mod) · 1000` mit Wp = 430, η_mod = 0,96.

| Größe | DC (W) | POA (W/m²) |
|---|---|---|
| M4 klar 04Z (typ. 27) | 27 | ≈ 65 (Report-konsistent: „~ Niveau unverbaute DHI") |
| M4 overcast 04Z | 8,1 | ≈ 20 |
| Modell diffus-only 04Z (Report) | 2,8–2,9 | ≈ 6,8 |

## 3. Modell-Zerlegung des 6,8-W/m²-Floors (M4, tilt 70, SVF 0,288, Albedo 0,15)

Engine-Semantik: `diffuse_poa = iso_unobstructed · SVF + ground`,
`iso_unobstructed = DHI·(1−Ai)·(1+cos β)/2`, `ground = albedo·GHI·(1−cos β)/2`.
Tilt-Faktoren M4 (β = 70°): sky 0,671, ground 0,329.

Klare 04Z-Stunde (Sonnen-el 3–12°, Mittelwerte): GHI ≈ 60–110, DHI ≈ 55–65,
Ai = DNI/E0n ≈ 0,3–0,5 (klar, Dämmerung) ⇒

- ground ≈ 0,15 · (60…110) · 0,329 ≈ **3,0…5,4 W/m²**
- iso·SVF = 6,8 − ground ≈ **1,4…3,8 W/m²** ⇒ iso_unobstructed ≈ **5…13 W/m²**

(Klein, weil Hay-Davies den klaren Dämmerungs-DHI großteils als Zirkumsolar
führt; Zirkumsolar ist für az205 mit cosθ=0 gleich null und wird zusätzlich vom
Horizont-Gate gehalten.)

## 4. Obergrenze einer Diffus-Reflektanz ρ (Option D1/D2)

Mechanik: blockierter Dom-Anteil (1−SVF) strahlt mit ρ-facher mittlerer
Himmelsradianz ⇒ `SVF_eff = SVF + ρ·(1−SVF)`; nur der iso-Term skaliert.

**Klarer Tag:** benötigt 65 ≈ iso_unob·SVF_eff + ground
⇒ SVF_eff ≈ (65 − 4)/ (5…13) ≈ **4,7…12** ⇒ ρ ≈ **5…16** — unphysikalisch
(ρ ≤ 1). Selbst ρ = 1 liefert nur iso_unob + ground ≈ **8…18 W/m²**
(2,8 W ⇒ ~3,5–7,7 W DC) — ~20–30 % der klaren Lücke.
**⇒ Kein Reflexions-/SVF-Fix kann die klare Beobachtung erklären.**

**Overcast-Tag 19.07. (die Diskriminante):** kein Beam, Ai ≈ 0,
DHI ≈ GHI ≈ 40 (04Z-Mittel, konservativ) ⇒
iso_unob ≈ 40·0,671 ≈ 27 W/m², ground ≈ 0,15·40·0,329 ≈ 2,0 W/m².
Gemessen 20 W/m²:

```
20 = 27 · SVF_eff + 2,0   ⇒   SVF_eff ≈ 0,67
ρ = (SVF_eff − SVF)/(1 − SVF) = (0,67 − 0,29)/0,71 ≈ 0,53
```

Sensitivität: DHI 30…60 ⇒ ρ ≈ 0,35…0,75. **ρ ≈ 0,5 (helle Putzwand) erklärt
den isotropen Anteil vollständig** — daher der ADR-Vorschlag
`diffuse_tau: 0.5` auf den Wand-Zeilen az195–360 (M4/M8) bzw. az295–360 (M1/M5).

Konsistenz mit dem Forensik-Report: dort „tau_wand = 0,3 ⇒ SVF 0,29 → ~0,5"
⇒ Wand-Anteil am blockierten Dom ≈ (0,5−0,29)/0,3 ≈ 0,7. Mit ρ = 0,5:
SVF_eff ≈ 0,29 + 0,5·0,7 ≈ **0,64** (der Goldwert im ADR-Testplan; die
restlichen 0,3 des blockierten Doms sind Baum-/Screen-Sektoren, die ihr
eigenes — künftig el-abhängiges — tau behalten).

## 5. Der beam-gebundene Rest (Option D3, zurückgestellt)

Klar-minus-overcast: ≈ 65 − 20 = **~45 W/m²**, existiert nur bei DNI > 0.
Rückseiten-Geometrie M4 (Front az205, tilt 70 ⇒ Rück-Normale az25, el −20):
Sonne 04:30Z ≈ az68, el 8:

```
cosθ_rear = cos(−20°)·cos(8°)·cos(68°−25°) + sin(−20°)·sin(8°)
          = 0,940·0,990·0,731 − 0,342·0,139 ≈ 0,63
```

Ansatz `rear_poa = f_rear · DNI · cosθ_rear · tau_baum(el)`:
mit DNI ≈ 300–600, tau_baum(el 5–10) ≈ 0,25–0,9, f_rear ≈ 0,15–0,25
(Bifazialität ~0,7 × Spalt-/Wand-Sichtfaktor ~0,2–0,35) ⇒
rear_poa ≈ **10–85 W/m²** — deckt die 45 W/m² in der Mitte des Bandes ab.
Der Koeffizient f_rear ist aus den vorhandenen Daten nur auf Faktor ~2 genau
bestimmbar ⇒ Fit erst nach Stabilisierung der Referenzphysik (Thema 1 + D2),
eine Woche klare-Morgen-Residuen von M4/M8 genügt dann.

Gegenprobe Mittag: Sonne vor der Plane ⇒ cosθ_rear < 0 ⇒ rear_poa = 0 —
keine Mittags-Nebenwirkung (der entscheidende Vorteil gegenüber jedem
statischen DHI-Floor: ein `c·DHI`-Floor mit c ≈ 1, der die klare
04Z-Beobachtung träfe, würde mittags +90 W/m² fabrizieren).

## 6. Site-weite Größenordnung von D2 (`diffuse_tau 0,5` auf Wänden)

- M4/M8: Δiso ≈ iso_unob · (0,64 − 0,29). Morgens/abends ≈ +2–8 W/m²
  (→ +1–3,5 W DC je Modul), mittags Δ ≈ +10–20 W/m² auf den iso-Anteil
  (iso mittags ≈ 17 → ~38 W/m²; gesamt-POA mittags > 500 ⇒ relativ < +4 %).
- Wandverschatteter M4/M8-Nachmittag (12–17Z): Modell-Floor steigt Richtung
  des gemessenen 24–31-W-Niveaus (Deckung ~50–70 %).
- M1/M5 (Wandanteil des Doms kleiner, da az25-Plane die az295–360-Wand
  seitlich sieht): SVF-Anhebung geschätzt +0,1–0,2 ⇒ wenige W je Modul.
- Summe ≈ **+0,1–0,2 kWh/Tag** von den ~0,3–0,5 kWh/Tag Gesamtdefizit;
  Rest = beam-gebundener M4/M8-Anteil (D3) + Dämmerungsanteil, den Thema 1
  über tau(el)-gewichtete Baum-Sektoren im SVF zusätzlich verkleinert.

*(Alle Werte Stunden-/Bandschätzungen auf Basis der obigen Messtabelle und der
Report-Reproduktion; für die Release-Abnahme gelten die Goldwert- und
Regressionstests aus dem ADR-Testplan, nicht diese Überschlagsrechnung.)*
