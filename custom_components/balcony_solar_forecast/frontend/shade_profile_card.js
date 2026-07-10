/**
 * Balcony Shade Profile card — sun path vs. learned shading (SPEC §15).
 *
 * OWNERSHIP: this file is SHIPPED AND SERVED BY THE INTEGRATION. The Python
 * side (`_frontend.py`) serves it as a static path under
 *   /balcony_solar_forecast/frontend/shade_profile_card.js
 * and, in storage-mode Lovelace, auto-registers it as a dashboard resource, so
 * the card shows up in the "Add card" picker with ZERO extra installs and ZERO
 * manual YAML. It replaces the opt-in HACS `apexcharts-card` snippet
 * (`dashboards/shade_profile_apexcharts.yaml`) and renders the SAME picture:
 *   - the sun path (elevation over azimuth) for a chosen date;
 *   - the currently-learned effective beam transmittance τ coloured per sample
 *     (τ≥0.85 free / green, 0.5≤τ<0.85 partial / amber, τ<0.5 shaded / red —
 *     EXACTLY the ApexCharts thresholds and colours, SHADE_PROFILE_TAU_*);
 *   - the learned shade horizon (filled) and the static config horizon (dashed).
 *
 * ZERO dependencies: plain `HTMLElement` + shadow DOM + programmatic SVG via
 * `document.createElementNS`. No lit, no CDN imports, no build step, no
 * minification. Cache-busting is handled entirely by the versioned resource URL
 * (`?v=<integration version>`), so this file carries no version string.
 *
 * It reads three integration-owned entities (device "Balcony Solar Forecast"),
 * auto-discovered from `hass.states` when not explicitly configured:
 *   - sensor.*_shade_profile         state = shaded fraction %, curve arrays as attributes
 *   - select.*_shade_profile_module  the module/plane picker
 *   - date.*_shade_profile_date      the date picker
 * The only state it writes is via the two control entities (select_option /
 * date.set_value) when the user changes the module or the date.
 */

const CARD_TAG = "balcony-shade-profile-card";

// τ colour thresholds — kept byte-identical to the ApexCharts snippet and to
// SHADE_PROFILE_TAU_THRESHOLD (the τ<0.5 "shaded" cut the sensor's headline and
// the learned shade horizon also use).
const TAU_FREE_MIN = 0.85; // τ ≥ this  → free
const TAU_PARTIAL_MIN = 0.5; // τ in [0.5, 0.85) → partial; below → shaded
const COLOR_FREE = "#2ecc71";
const COLOR_PARTIAL = "#e67e22";
const COLOR_SHADED = "#c0392b";
const COLOR_SUN = "#f1c40f"; // sun-path polyline
const COLOR_SHADE_FILL = "#7f8c8d"; // learned shade horizon fill
const COLOR_STATIC_HORIZON = "#95a5a6"; // static config horizon (dashed)

// Sensor attribute names (must match const.ATTR_SP_* on the Python side).
const A_AZIMUTH = "azimuth";
const A_SUN_ELEVATION = "sun_elevation";
const A_TRANSMITTANCE = "transmittance";
const A_TIME = "time";
const A_HORIZON_AZIMUTH = "horizon_azimuth";
const A_SHADE_HORIZON = "shade_horizon";
const A_STATIC_HORIZON = "static_horizon";
// Year-stable x-axis bounds (widest daylight azimuth span of the whole year at
// the site, both solstices — computed Python-side). Fixing the axis to these
// keeps the sun path comparable across dates instead of rescaling per season.
const A_AXIS_AZ_MIN = "axis_azimuth_min";
const A_AXIS_AZ_MAX = "axis_azimuth_max";

// Entity auto-discovery patterns (entity_id shapes; the device slug already
// carries "balcony_solar_forecast", so a loose contains-match is safe).
const RE_SENSOR = /^sensor\..*shade_profile$/;
const RE_SELECT = /^select\..*shade_profile_module$/;
const RE_DATE = /^date\..*shade_profile_date$/;

// Tiny i18n dict keyed off the two-letter `hass.language`; English fallback.
const I18N = {
  en: {
    module: "Module",
    date: "Date",
    shaded: "shaded",
    noEntities:
      "No shade-profile entities found — is the Balcony Solar Forecast integration set up?",
    noSamples: "No daylight samples for this date.",
    hoverIdle: "Hover the chart for details",
    hoverShading: "Shading",
    hoverElevation: "Elevation",
    compass: ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
  },
  de: {
    module: "Modul",
    date: "Datum",
    shaded: "verschattet",
    noEntities:
      "Keine Verschattungsprofil-Entitäten gefunden — ist die Integration „Balcony Solar Forecast“ eingerichtet?",
    noSamples: "Keine Tageslicht-Datenpunkte für dieses Datum.",
    hoverIdle: "Über das Diagramm fahren für Details",
    hoverShading: "Verschattung",
    hoverElevation: "Elevation",
    compass: ["N", "NO", "O", "SO", "S", "SW", "W", "NW"],
  },
};

const SVGNS = "http://www.w3.org/2000/svg";

/** Create an SVG element with attributes (skips undefined/null) + children. */
function svg(tag, attrs, children) {
  const el = document.createElementNS(SVGNS, tag);
  if (attrs) {
    for (const k in attrs) {
      const v = attrs[k];
      if (v !== undefined && v !== null) el.setAttribute(k, v);
    }
  }
  if (children) for (const c of children) el.appendChild(c);
  return el;
}

/** τ → bucket colour (thresholds identical to the ApexCharts snippet). */
function tauColor(t) {
  if (t >= TAU_FREE_MIN) return COLOR_FREE;
  if (t >= TAU_PARTIAL_MIN) return COLOR_PARTIAL;
  return COLOR_SHADED;
}

/** Local ISO timestamp → "HH:MM" (best-effort; tolerant of odd input). */
function hhmm(iso) {
  if (typeof iso !== "string") return "";
  const t = iso.indexOf("T");
  return t >= 0 ? iso.slice(t + 1, t + 6) : iso.slice(0, 5);
}

function isArray(x) {
  return Array.isArray(x);
}

class BalconyShadeProfileCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._rendered = false;
    // Last-seen state objects (HA state objects are immutable, so identity
    // comparison detects a real change and gates re-render).
    this._lastSensor = undefined;
    this._lastSelect = undefined;
    this._lastDate = undefined;
  }

  // --- Lovelace card API --------------------------------------------------

  setConfig(config) {
    // All keys optional; auto-discovery fills the rest at render time.
    this._config = config || {};
    this._rendered = false; // force a rebuild on the next hass push
    this._lastSensor = this._lastSelect = this._lastDate = undefined;
  }

  getCardSize() {
    return 6;
  }

  /** Picker preview: return discovered ids so the preview renders live data. */
  static getStubConfig(hass) {
    const find = (re) => {
      if (!hass || !hass.states) return undefined;
      for (const id of Object.keys(hass.states)) if (re.test(id)) return id;
      return undefined;
    };
    return {
      sensor: find(RE_SENSOR),
      module_select: find(RE_SELECT),
      date_entity: find(RE_DATE),
    };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    const ids = this._resolveIds(hass);
    const s = hass.states[ids.sensor];
    const sel = hass.states[ids.module_select];
    const d = hass.states[ids.date_entity];
    // Re-render only when one of the three entities actually changed.
    if (
      this._rendered &&
      s === this._lastSensor &&
      sel === this._lastSelect &&
      d === this._lastDate
    ) {
      return;
    }
    this._lastSensor = s;
    this._lastSelect = sel;
    this._lastDate = d;
    this._render(hass, ids, s, sel, d);
    this._rendered = true;
  }

  // --- helpers ------------------------------------------------------------

  _t() {
    const lang = ((this._hass && this._hass.language) || "en")
      .slice(0, 2)
      .toLowerCase();
    return I18N[lang] || I18N.en;
  }

  _resolveIds(hass) {
    const c = this._config;
    const find = (re) => {
      if (!hass || !hass.states) return undefined;
      for (const id of Object.keys(hass.states)) if (re.test(id)) return id;
      return undefined;
    };
    return {
      sensor: c.sensor || find(RE_SENSOR),
      module_select: c.module_select || find(RE_SELECT),
      date_entity: c.date_entity || find(RE_DATE),
    };
  }

  // --- rendering ----------------------------------------------------------

  _render(hass, ids, s, sel, d) {
    const t = this._t();
    const root = this.shadowRoot;
    root.textContent = "";
    root.appendChild(this._style());

    const card = document.createElement("ha-card");
    card.setAttribute("header", this._config.title || "Shade profile");
    root.appendChild(card);

    const body = document.createElement("div");
    body.className = "content";
    card.appendChild(body);

    // Core entity missing → setup hint, nothing else to draw.
    if (!s) {
      body.appendChild(this._message(t.noEntities));
      return;
    }

    body.appendChild(this._controls(hass, ids, s, sel, d, t));
    // Fixed-height hover status line (idle hint when not hovering); lives in the
    // header area so the crosshair readout never shifts the layout. Kept on
    // `this` so the plot's hover handler can update it without a re-render.
    this._readoutEl = this._statusLine(t);
    body.appendChild(this._readoutEl);
    body.appendChild(this._plot(s, t));
  }

  /** Fixed-height status readout, starting on the idle hint. */
  _statusLine(t) {
    const div = document.createElement("div");
    div.className = "readout idle";
    div.textContent = t.hoverIdle;
    return div;
  }

  _style() {
    const style = document.createElement("style");
    style.textContent = `
      .content { padding: 0 16px 16px; }
      .controls {
        display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
        padding: 4px 0 12px;
      }
      .field { display: flex; flex-direction: column; gap: 2px; }
      .field label {
        font-size: 0.75rem; color: var(--secondary-text-color);
      }
      .controls select, .controls input[type="date"] {
        font: inherit; color: var(--primary-text-color);
        background: var(--card-background-color, #fff);
        border: 1px solid var(--divider-color, #e0e0e0);
        border-radius: 6px; padding: 6px 8px; min-height: 34px;
      }
      .badge {
        margin-left: auto; padding: 6px 12px; border-radius: 16px;
        background: var(--secondary-background-color, #f0f0f0);
        color: var(--primary-text-color); font-weight: 600; white-space: nowrap;
      }
      .plot { width: 100%; height: auto; display: block; }
      .readout {
        min-height: 1.4em; line-height: 1.4em;
        padding: 0 0 8px; color: var(--primary-text-color);
        font-size: 0.9rem; font-variant-numeric: tabular-nums;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .readout.idle { color: var(--secondary-text-color); font-style: italic; }
      .msg {
        padding: 24px 8px; text-align: center;
        color: var(--secondary-text-color);
      }
      .note {
        margin-top: 8px; text-align: center;
        color: var(--secondary-text-color); font-size: 0.85rem;
      }
    `;
    return style;
  }

  _message(text) {
    const div = document.createElement("div");
    div.className = "msg";
    div.textContent = text;
    return div;
  }

  _controls(hass, ids, s, sel, d, t) {
    const wrap = document.createElement("div");
    wrap.className = "controls";

    // Module <select> from the select entity's options (current = its state).
    if (sel && isArray(sel.attributes.options)) {
      const field = document.createElement("div");
      field.className = "field";
      const label = document.createElement("label");
      label.textContent = t.module;
      const select = document.createElement("select");
      for (const opt of sel.attributes.options) {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        if (opt === sel.state) o.selected = true;
        select.appendChild(o);
      }
      select.addEventListener("change", (ev) => {
        hass.callService("select", "select_option", {
          entity_id: ids.module_select,
          option: ev.target.value,
        });
      });
      field.appendChild(label);
      field.appendChild(select);
      wrap.appendChild(field);
    }

    // Date <input type="date"> bound to the date entity's state.
    if (d) {
      const field = document.createElement("div");
      field.className = "field";
      const label = document.createElement("label");
      label.textContent = t.date;
      const input = document.createElement("input");
      input.type = "date";
      // The date entity's state is already an ISO YYYY-MM-DD string.
      input.value = /^\d{4}-\d{2}-\d{2}$/.test(d.state) ? d.state : "";
      input.addEventListener("change", (ev) => {
        if (!ev.target.value) return;
        hass.callService("date", "set_value", {
          entity_id: ids.date_entity,
          date: ev.target.value,
        });
      });
      field.appendChild(label);
      field.appendChild(input);
      wrap.appendChild(field);
    }

    // Shaded-fraction badge from the sensor state.
    const badge = document.createElement("div");
    badge.className = "badge";
    const val = Number.parseFloat(s.state);
    badge.textContent = Number.isFinite(val)
      ? `${Math.round(val)}% ${t.shaded}`
      : "—";
    wrap.appendChild(badge);

    return wrap;
  }

  _plot(s, t) {
    const a = s.attributes || {};
    const sunAz = a[A_AZIMUTH];
    const sunEl = a[A_SUN_ELEVATION];
    const tau = a[A_TRANSMITTANCE];
    const time = a[A_TIME];
    const horAz = a[A_HORIZON_AZIMUTH];
    const shadeH = a[A_SHADE_HORIZON];
    const staticH = a[A_STATIC_HORIZON];

    // Sun-path samples require the four arrays to be present, non-empty and of
    // equal length; a mismatch is treated as "no samples" (axes + horizons may
    // still render). Horizon lines render on whatever grid is available.
    const nSun =
      isArray(sunAz) &&
      isArray(sunEl) &&
      isArray(tau) &&
      isArray(time) &&
      sunAz.length > 0 &&
      sunAz.length === sunEl.length &&
      sunAz.length === tau.length &&
      sunAz.length === time.length
        ? sunAz.length
        : 0;

    const nHor =
      isArray(horAz) && (isArray(shadeH) || isArray(staticH))
        ? Math.min(
            horAz.length,
            isArray(shadeH) ? shadeH.length : Infinity,
            isArray(staticH) ? staticH.length : Infinity,
          )
        : 0;

    if (!nSun && !nHor) {
      return this._message(t.noSamples);
    }

    // --- domains ---------------------------------------------------------
    const azValues = [];
    for (let i = 0; i < nSun; i++) azValues.push(sunAz[i]);
    for (let i = 0; i < nHor; i++) azValues.push(horAz[i]);
    let xMin = Math.min(...azValues);
    let xMax = Math.max(...azValues);
    // Year-stable x-axis: union the site's whole-year daylight azimuth span
    // (both solstices, from the sensor) with this date's data span, so the axis
    // does NOT rescale season to season and curves stay comparable across dates.
    // The union is defensive — the year sweep is coarser than the per-date
    // sampling, so a sample must never be able to fall outside the axis. When the
    // attributes are absent or degenerate (max <= min), fall back to the plain
    // per-date span.
    const axisMin = Number(a[A_AXIS_AZ_MIN]);
    const axisMax = Number(a[A_AXIS_AZ_MAX]);
    if (Number.isFinite(axisMin) && Number.isFinite(axisMax) && axisMax > axisMin) {
      xMin = Math.min(xMin, axisMin);
      xMax = Math.max(xMax, axisMax);
    }
    if (xMin === xMax) {
      xMin -= 1;
      xMax += 1;
    }
    const xPad = Math.max(2, (xMax - xMin) * 0.03);
    xMin -= xPad;
    xMax += xPad;

    const elValues = [0];
    for (let i = 0; i < nSun; i++) elValues.push(sunEl[i]);
    for (let i = 0; i < nHor; i++) {
      if (isArray(shadeH)) elValues.push(shadeH[i]);
      if (isArray(staticH)) elValues.push(staticH[i]);
    }
    const yMax = Math.max(...elValues) + 5;

    // --- layout ----------------------------------------------------------
    const W = 700;
    const H = 380;
    const m = { top: 12, right: 14, bottom: 34, left: 44 };
    const plotW = W - m.left - m.right;
    const plotH = H - m.top - m.bottom;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const X = (az) =>
      m.left + ((az - xMin) / (xMax - xMin)) * plotW;
    const Y = (el) =>
      m.top + (1 - clamp(el, 0, yMax) / yMax) * plotH;

    const el = svg("svg", {
      class: "plot",
      viewBox: `0 0 ${W} ${H}`,
      preserveAspectRatio: "xMidYMid meet",
      role: "img",
    });

    // Grid + axes.
    el.appendChild(this._axes(X, Y, xMin, xMax, yMax, m, plotW, plotH, W, H));

    // (1) learned shade horizon — filled polygon down to y=0.
    if (nHor && isArray(shadeH)) {
      const pts = [`${X(horAz[0])},${Y(0)}`];
      for (let i = 0; i < nHor; i++) pts.push(`${X(horAz[i])},${Y(shadeH[i])}`);
      pts.push(`${X(horAz[nHor - 1])},${Y(0)}`);
      el.appendChild(
        svg("polygon", {
          points: pts.join(" "),
          fill: COLOR_SHADE_FILL,
          "fill-opacity": "0.15",
          stroke: "none",
        }),
      );
    }

    // (2) static config horizon — thin dashed line.
    if (nHor && isArray(staticH)) {
      const pts = [];
      for (let i = 0; i < nHor; i++) pts.push(`${X(horAz[i])},${Y(staticH[i])}`);
      el.appendChild(
        svg("polyline", {
          points: pts.join(" "),
          fill: "none",
          stroke: COLOR_STATIC_HORIZON,
          "stroke-width": "1",
          "stroke-dasharray": "4 3",
        }),
      );
    }

    // (3) sun path — polyline.
    if (nSun) {
      const pts = [];
      for (let i = 0; i < nSun; i++) pts.push(`${X(sunAz[i])},${Y(sunEl[i])}`);
      el.appendChild(
        svg("polyline", {
          points: pts.join(" "),
          fill: "none",
          stroke: COLOR_SUN,
          "stroke-width": "2",
          "stroke-linejoin": "round",
        }),
      );

      // (4) one dot per sample, coloured by τ, with a native hover tooltip.
      for (let i = 0; i < nSun; i++) {
        const dot = svg("circle", {
          cx: X(sunAz[i]),
          cy: Y(sunEl[i]),
          r: "3",
          fill: tauColor(tau[i]),
        });
        const title = document.createElementNS(SVGNS, "title");
        title.textContent = `${hhmm(time[i])} · el ${Number(sunEl[i]).toFixed(
          1,
        )}° · τ ${Number(tau[i]).toFixed(2)}`;
        dot.appendChild(title);
        el.appendChild(dot);
      }

      // (5) hover crosshair + status readout. A transparent overlay over the
      // plot area captures pointer moves; a dedicated group holds the crosshair
      // line + highlight ring, rebuilt per hover (never a full card re-render).
      const crosshair = svg("g", { class: "crosshair" });
      el.appendChild(crosshair);
      const overlay = svg("rect", {
        x: m.left,
        y: m.top,
        width: plotW,
        height: plotH,
        fill: "transparent",
        "pointer-events": "all",
      });
      el.appendChild(overlay);

      // Context the single move handler reads; rebuilt each render so a hass
      // push simply swaps it (the old SVG + its listeners are discarded when the
      // shadow DOM is cleared on the next _render).
      const ctx = {
        svgEl: el,
        crosshair,
        readout: this._readoutEl,
        t,
        sunAz,
        sunEl,
        tau,
        time,
        n: nSun,
        X,
        Y,
        xMin,
        xMax,
        m,
        plotW,
        plotH,
        W,
      };
      const onMove = (ev) => this._hoverMove(ev, ctx);
      const onLeave = () => this._hoverLeave(ctx);
      overlay.addEventListener("mousemove", onMove);
      overlay.addEventListener("mouseleave", onLeave);
      overlay.addEventListener("touchstart", onMove, { passive: true });
      overlay.addEventListener("touchmove", onMove, { passive: true });
    }

    // No sun-path samples but horizons drawn → annotate.
    if (!nSun) {
      const box = document.createElement("div");
      box.appendChild(el);
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = t.noSamples;
      box.appendChild(note);
      return box;
    }
    return el;
  }

  // --- hover crosshair ----------------------------------------------------

  /** Pointer move over the plot → snap to the nearest sample, draw crosshair. */
  _hoverMove(ev, ctx) {
    const rect = ctx.svgEl.getBoundingClientRect();
    if (!rect.width) return;
    const clientX =
      ev.touches && ev.touches.length ? ev.touches[0].clientX : ev.clientX;
    if (typeof clientX !== "number") return;
    // Map the pointer's client-x into the fixed viewBox, then invert the
    // x-scale to an azimuth (the viewBox aspect is fixed, so no letterboxing).
    const vbx = ((clientX - rect.left) / rect.width) * ctx.W;
    const az =
      ctx.xMin + ((vbx - ctx.m.left) / ctx.plotW) * (ctx.xMax - ctx.xMin);
    // Nearest sun-path sample by azimuth (arrays ~150 long → a linear scan).
    let best = 0;
    let bestD = Infinity;
    for (let i = 0; i < ctx.n; i++) {
      const dd = Math.abs(ctx.sunAz[i] - az);
      if (dd < bestD) {
        bestD = dd;
        best = i;
      }
    }
    this._drawCrosshair(ctx, best);
    ctx.readout.classList.remove("idle");
    ctx.readout.textContent = this._hoverText(ctx, best);
  }

  /** Pointer left the plot → drop the crosshair group + restore the idle hint. */
  _hoverLeave(ctx) {
    const g = ctx.crosshair;
    while (g.firstChild) g.removeChild(g.firstChild);
    ctx.readout.classList.add("idle");
    ctx.readout.textContent = ctx.t.hoverIdle;
  }

  /** (Re)build the crosshair group for the snapped sample index ``i``. */
  _drawCrosshair(ctx, i) {
    const g = ctx.crosshair;
    while (g.firstChild) g.removeChild(g.firstChild);
    const x = ctx.X(ctx.sunAz[i]);
    const y = ctx.Y(ctx.sunEl[i]);
    // (a) vertical crosshair line across the whole plot height.
    g.appendChild(
      svg("line", {
        x1: x,
        y1: ctx.m.top,
        x2: x,
        y2: ctx.m.top + ctx.plotH,
        stroke: "var(--secondary-text-color)",
        "stroke-width": "1",
        opacity: "0.6",
        "stroke-dasharray": "4 3",
      }),
    );
    // (b) highlight ring around the snapped sample's dot.
    g.appendChild(
      svg("circle", {
        cx: x,
        cy: y,
        r: "6",
        fill: "none",
        stroke: "var(--primary-text-color)",
        "stroke-width": "2",
      }),
    );
  }

  /** Status readout string for the snapped sample (localized compass + τ). */
  _hoverText(ctx, i) {
    const t = ctx.t;
    const az = Number(ctx.sunAz[i]);
    const el = Number(ctx.sunEl[i]);
    const tau = Number(ctx.tau[i]);
    const sector = ((Math.round(az / 45) % 8) + 8) % 8;
    const compass = (t.compass && t.compass[sector]) || "";
    const shadingPct = Math.round((1 - tau) * 100);
    return (
      `${ctx.time[i]} · ${Math.round(az)}° ${compass} · ` +
      `${t.hoverShading} ${shadingPct} % (τ ${tau.toFixed(2)}) · ` +
      `${t.hoverElevation} ${Math.round(el)}°`
    );
  }

  /** Axis frame: gridlines, x ticks every 30° (+ E/S/W), y ticks every 15°. */
  _axes(X, Y, xMin, xMax, yMax, m, plotW, plotH, W, H) {
    const g = svg("g", {});
    const axisColor = "var(--secondary-text-color)";
    const gridColor = "var(--divider-color, #e0e0e0)";

    // Frame lines (left + bottom).
    g.appendChild(
      svg("line", {
        x1: m.left,
        y1: m.top,
        x2: m.left,
        y2: m.top + plotH,
        stroke: axisColor,
        "stroke-width": "1",
      }),
    );
    g.appendChild(
      svg("line", {
        x1: m.left,
        y1: m.top + plotH,
        x2: m.left + plotW,
        y2: m.top + plotH,
        stroke: axisColor,
        "stroke-width": "1",
      }),
    );

    // Compass markers for the sun azimuth (0 = N clockwise).
    const compass = { 90: "E", 180: "S", 270: "W" };

    // X ticks every 30°.
    const startX = Math.ceil(xMin / 30) * 30;
    for (let deg = startX; deg <= xMax; deg += 30) {
      const x = X(deg);
      g.appendChild(
        svg("line", {
          x1: x,
          y1: m.top,
          x2: x,
          y2: m.top + plotH,
          stroke: gridColor,
          "stroke-width": "1",
        }),
      );
      const label = svg(
        "text",
        {
          x,
          y: m.top + plotH + 14,
          fill: axisColor,
          "font-size": "10",
          "text-anchor": "middle",
        },
        [],
      );
      label.textContent = `${deg}°`;
      g.appendChild(label);
      if (compass[deg]) {
        const c = svg(
          "text",
          {
            x,
            y: m.top + plotH + 25,
            fill: axisColor,
            "font-size": "10",
            "font-weight": "700",
            "text-anchor": "middle",
          },
          [],
        );
        c.textContent = compass[deg];
        g.appendChild(c);
      }
    }

    // Y ticks every 15°.
    for (let deg = 0; deg <= yMax; deg += 15) {
      const y = Y(deg);
      g.appendChild(
        svg("line", {
          x1: m.left,
          y1: y,
          x2: m.left + plotW,
          y2: y,
          stroke: gridColor,
          "stroke-width": "1",
        }),
      );
      const label = svg(
        "text",
        {
          x: m.left - 6,
          y: y + 3,
          fill: axisColor,
          "font-size": "10",
          "text-anchor": "end",
        },
        [],
      );
      label.textContent = `${deg}°`;
      g.appendChild(label);
    }

    return g;
  }
}

if (!customElements.get(CARD_TAG)) {
  customElements.define("balcony-shade-profile-card", BalconyShadeProfileCard);
}

// Advertise the card to the Lovelace "Add card" picker.
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === CARD_TAG)) {
  window.customCards.push({
    type: CARD_TAG,
    name: "Balcony Shade Profile",
    description:
      "Sun path vs. learned shading for a chosen module and date (Balcony Solar Forecast).",
    preview: true,
    documentationURL:
      "https://github.com/danielr0815/balcony-solar-forecast/blob/main/docs/DASHBOARD.md",
  });
}

// One load banner (no version string — the resource URL carries the version).
console.info(
  "%c Balcony Shade Profile Card ",
  "color:#fff;background:#f1c40f;font-weight:700;border-radius:4px;padding:2px 6px",
);
