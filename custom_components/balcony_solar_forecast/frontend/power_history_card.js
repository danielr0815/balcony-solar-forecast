/**
 * Balcony Power History card — stacked hourly production per module + forecast.
 *
 * OWNERSHIP: this file is SHIPPED AND SERVED BY THE INTEGRATION. The Python
 * side (`_frontend.py`) serves it as a static path under
 *   /balcony_solar_forecast/frontend/power_history_card.js
 * and, in storage-mode Lovelace, auto-registers it as a dashboard resource, so
 * the card shows up in the "Add card" picker with ZERO extra installs and ZERO
 * manual YAML. It is the energy-dashboard-style replacement for the messy
 * 8-line "Measured DC power per module" history-graph: one STACKED bar per local
 * hour (a coloured segment per module M1…M8) plus a dashed FORECAST line, with a
 * hover crosshair that lists every module's value AND the total for that hour.
 *
 * ZERO dependencies: plain `HTMLElement` + shadow DOM + programmatic SVG via
 * `document.createElementNS`. No lit, no CDN imports, no build step. Cache-
 * busting is handled entirely by the versioned resource URL (`?v=<version>`),
 * so this file carries no version string.
 *
 * DATA (all read live, never written):
 *   1. Module list      — the measured-total sensor's `sources` (statistic ids)
 *                         and `source_names` (plane names M1…M8) attributes.
 *   2. Measured bars    — `recorder/statistics_during_period` (period "hour",
 *                         types ["mean"]) for the sources, from local midnight to
 *                         now; hourly Wh per module = mean W × 1 h. Refetched on
 *                         connect, every 5 minutes, and when the local day rolls.
 *   3. Forecast line    — the forecast sensor's `wh_period` attribute (15-min Wh,
 *                         ISO-UTC keys) aggregated to local hours in the card.
 *
 * It is self-contained and imports nothing from the sibling shade-profile card.
 */

const CARD_TAG = "balcony-power-history-card";

// Energy-dashboard-like distinct hues, assigned to modules in config order.
const PALETTE = [
  "#f1c40f",
  "#e67e22",
  "#e74c3c",
  "#9b59b6",
  "#3498db",
  "#1abc9c",
  "#2ecc71",
  "#95a5a6",
];

// Measured-total sensor attribute names (must match the Python contract:
// sensor.MeasuredDcTotalSensor.extra_state_attributes).
const A_SOURCES = "sources";
const A_SOURCE_NAMES = "source_names";
// Forecast sensor attribute (15-min Wh keyed by ISO-UTC slot start; must match
// const.ATTR_WH_PERIOD).
const A_WH_PERIOD = "wh_period";

// Entity auto-discovery patterns (the device slug already carries
// "balcony_solar_forecast", so a loose suffix match is safe).
const RE_TOTAL = /^sensor\..*measured_dc_power_total$/;
const RE_FORECAST = /^sensor\..*energy_production_today$/;

// Measured-statistics refetch cadence (ms) — the recorder writes hourly LTS on
// its own schedule, so 5 min is ample and cheap.
const REFETCH_MS = 5 * 60 * 1000;

// Tiny i18n dict keyed off the two-letter `hass.language`; English fallback.
const I18N = {
  en: {
    title: "Hourly production per module",
    total: "Total",
    forecast: "Forecast",
    noEntities:
      "No measured-power sensor found — is the Balcony Solar Forecast integration set up?",
    noStats: "No hourly statistics yet",
    loading: "…",
  },
  de: {
    title: "Stündliche Produktion je Modul",
    total: "Gesamt",
    forecast: "Prognose",
    noEntities:
      "Kein Messleistungs-Sensor gefunden — ist die Integration „Balcony Solar Forecast“ eingerichtet?",
    noStats: "Noch keine Stundenstatistik",
    loading: "…",
  },
};

const SVGNS = "http://www.w3.org/2000/svg";
const HOURS = 24;

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

function isArray(x) {
  return Array.isArray(x);
}

/** Two-digit zero-padded hour ("0" → "00"). */
function pad2(n) {
  return n < 10 ? `0${n}` : `${n}`;
}

/**
 * Coerce a statistics `start`/`end` (ms-epoch NUMBER per the HA serializer, but
 * tolerate an ISO string) or an ISO slot key into a Date, or null.
 */
function toDate(v) {
  if (typeof v === "number" && Number.isFinite(v)) return new Date(v);
  if (typeof v === "string" && v.trim() !== "") {
    // A bare numeric string is an epoch; anything with date punctuation is ISO.
    if (/^-?\d+$/.test(v.trim())) return new Date(Number(v));
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  return null;
}

/** Local hour [0..23] of a statistics start / ISO key, or -1 if unparseable. */
function localHourOf(v) {
  const d = toDate(v);
  return d ? d.getHours() : -1;
}

/** ISO instant of the current LOCAL midnight (as a UTC-anchored string). */
function localMidnightISO() {
  const now = new Date();
  return new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
    0,
    0,
    0,
    0,
  ).toISOString();
}

/** Stable key for the current local calendar day (day-roll detection). */
function localDayKey() {
  const now = new Date();
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(
    now.getDate(),
  )}`;
}

/** A "nice" axis: rounded max + evenly spaced ticks (~4 intervals). */
function niceAxis(maxVal) {
  if (!(maxVal > 0)) return { max: 1, ticks: [0, 1] };
  const raw = maxVal / 4;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / pow;
  let step;
  if (n <= 1) step = 1;
  else if (n <= 2) step = 2;
  else if (n <= 2.5) step = 2.5;
  else if (n <= 5) step = 5;
  else step = 10;
  step *= pow;
  const niceMax = Math.ceil(maxVal / step) * step;
  const ticks = [];
  for (let t = 0; t <= niceMax + step / 2; t += step) ticks.push(t);
  return { max: niceMax, ticks };
}

/** Axis-tick label: kWh (>=1000 Wh) else Wh. */
function fmtTick(wh, unitKwh) {
  if (unitKwh) {
    const k = wh / 1000;
    return `${Number.isInteger(k) ? String(k) : k.toFixed(1)} kWh`;
  }
  return `${Math.round(wh)} Wh`;
}

/** Readout value: compact kWh over 1000 Wh, whole Wh otherwise. */
function fmtVal(wh) {
  if (wh >= 1000) return `${(wh / 1000).toFixed(2)} kWh`;
  return `${Math.round(wh)} Wh`;
}

class BalconyPowerHistoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._rendered = false;
    // Last-seen state objects (HA state objects are immutable → identity gates
    // the re-render on a real change of the module list or the forecast curve).
    this._lastTotal = undefined;
    this._lastForecast = undefined;
    // Derived render inputs.
    this._modules = []; // [{ id, name, color }]
    this._bars = {}; // stat_id -> number[24] hourly Wh
    this._forecast = null; // number[24] hourly Wh, or null (no line)
    this._loadState = "loading"; // "loading" | "ok" | "empty" | "error"
    // Fetch bookkeeping.
    this._timer = null;
    this._fetchDay = undefined; // localDayKey of the last kicked fetch
  }

  // --- Lovelace card API --------------------------------------------------

  setConfig(config) {
    // All keys optional; auto-discovery fills the rest at render time.
    this._config = config || {};
    this._rendered = false;
    this._lastTotal = this._lastForecast = undefined;
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
      total_sensor: find(RE_TOTAL),
      forecast_sensor: find(RE_FORECAST),
    };
  }

  connectedCallback() {
    if (!this._timer) {
      this._timer = setInterval(() => this._fetch(), REFETCH_MS);
    }
    // A card added while hass is already present must fetch straight away.
    this._ensureFetch();
  }

  disconnectedCallback() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    // Kick the first (or a new-day) statistics fetch as soon as hass arrives.
    this._ensureFetch();

    const ids = this._resolveIds(hass);
    const total = hass.states[ids.total_sensor];
    const forecast = hass.states[ids.forecast_sensor];
    // Re-render only when the module list or the forecast curve actually
    // changed (the async stats fetch re-renders itself on completion).
    if (
      this._rendered &&
      total === this._lastTotal &&
      forecast === this._lastForecast
    ) {
      return;
    }
    this._lastTotal = total;
    this._lastForecast = forecast;
    this._rebuildModules(total);
    this._recomputeForecast(forecast);
    this._render();
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
      total_sensor: c.total_sensor || find(RE_TOTAL),
      forecast_sensor: c.forecast_sensor || find(RE_FORECAST),
    };
  }

  /** Module list [{id, name, color}] from the total sensor's attributes. */
  _rebuildModules(total) {
    const a = (total && total.attributes) || {};
    const sources = isArray(a[A_SOURCES]) ? a[A_SOURCES] : [];
    const names = isArray(a[A_SOURCE_NAMES]) ? a[A_SOURCE_NAMES] : [];
    this._modules = sources.map((id, i) => ({
      id,
      name: (typeof names[i] === "string" && names[i]) || id,
      color: PALETTE[i % PALETTE.length],
    }));
  }

  /** Forecast sensor's 15-min `wh_period` → number[24] local-hour Wh, or null. */
  _recomputeForecast(forecast) {
    if (this._config.hours_forecast === false) {
      this._forecast = null;
      return;
    }
    const wh =
      forecast && forecast.attributes && forecast.attributes[A_WH_PERIOD];
    if (!wh || typeof wh !== "object") {
      this._forecast = null; // attr missing → bars only, no line, no error
      return;
    }
    const arr = new Array(HOURS).fill(0);
    let any = false;
    for (const iso in wh) {
      const h = localHourOf(iso);
      if (h < 0) continue;
      const v = Number(wh[iso]);
      if (!Number.isFinite(v)) continue;
      arr[h] += v;
      any = true;
    }
    this._forecast = any ? arr : null;
  }

  // --- statistics fetch ---------------------------------------------------

  /** Fetch once per new local day (and on first hass) — cheap day compare. */
  _ensureFetch() {
    if (!this._hass || !this.isConnected) return;
    const day = localDayKey();
    if (this._fetchDay === day) return;
    this._fetchDay = day;
    this._fetch();
  }

  /** Pull hourly `mean` statistics for the module sources, ingest, re-render. */
  async _fetch() {
    const hass = this._hass;
    if (!hass || typeof hass.callWS !== "function") return;
    const ids = this._resolveIds(hass);
    const total = hass.states[ids.total_sensor];
    const sources =
      total && total.attributes && isArray(total.attributes[A_SOURCES])
        ? total.attributes[A_SOURCES]
        : [];
    if (!sources.length) return;
    this._fetchDay = localDayKey();
    let result;
    try {
      result = await hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: localMidnightISO(),
        end_time: new Date().toISOString(),
        statistic_ids: sources,
        period: "hour",
        types: ["mean"],
      });
    } catch (err) {
      this._loadState = "error";
      this._render();
      return;
    }
    this._ingestStats(result, sources);
    this._render();
  }

  /** {stat_id: [{start, mean}]} → per-source number[24] hourly Wh (mean W×1h). */
  _ingestStats(result, sources) {
    const bars = {};
    let any = false;
    for (const id of sources) {
      const rows = result && result[id];
      const arr = new Array(HOURS).fill(0);
      if (isArray(rows)) {
        for (const row of rows) {
          const h = localHourOf(row && row.start);
          if (h < 0) continue;
          const mean = Number(row && row.mean);
          if (!Number.isFinite(mean)) continue;
          arr[h] += mean; // mean power (W) × 1 h = Wh
          any = true;
        }
      }
      bars[id] = arr;
    }
    this._bars = bars;
    this._loadState = any ? "ok" : "empty";
  }

  // --- rendering ----------------------------------------------------------

  _render() {
    const t = this._t();
    const root = this.shadowRoot;
    root.textContent = "";
    root.appendChild(this._style());

    const card = document.createElement("ha-card");
    card.setAttribute("header", this._config.title || t.title);
    root.appendChild(card);

    const body = document.createElement("div");
    body.className = "content";
    card.appendChild(body);

    // No total sensor at all → setup hint, nothing else to draw.
    const ids = this._resolveIds(this._hass || {});
    const hasTotal =
      this._hass && this._hass.states && this._hass.states[ids.total_sensor];
    if (!hasTotal || !this._modules.length) {
      body.appendChild(this._message(t.noEntities));
      return;
    }

    body.appendChild(this._plot(t));
    body.appendChild(this._legend(t));
  }

  _style() {
    const style = document.createElement("style");
    style.textContent = `
      .content { padding: 0 16px 16px; }
      .plot { width: 100%; height: auto; display: block; }
      .legend {
        display: flex; flex-wrap: wrap; gap: 6px 14px;
        padding: 10px 2px 0; justify-content: center;
      }
      .legend .item {
        display: inline-flex; align-items: center; gap: 6px;
        font-size: 0.8rem; color: var(--primary-text-color);
      }
      .legend .swatch {
        width: 12px; height: 12px; border-radius: 3px; flex: 0 0 auto;
      }
      .msg {
        padding: 24px 8px; text-align: center;
        color: var(--secondary-text-color);
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

  _legend(t) {
    const wrap = document.createElement("div");
    wrap.className = "legend";
    for (const mod of this._modules) {
      const item = document.createElement("span");
      item.className = "item";
      const sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = mod.color;
      item.appendChild(sw);
      const label = document.createElement("span");
      label.textContent = mod.name;
      item.appendChild(label);
      wrap.appendChild(item);
    }
    return wrap;
  }

  _plot(t) {
    // --- domains ---------------------------------------------------------
    let dataMax = 0;
    for (let h = 0; h < HOURS; h++) {
      let stack = 0;
      for (const mod of this._modules) {
        const v = (this._bars[mod.id] && this._bars[mod.id][h]) || 0;
        if (v > 0) stack += v;
      }
      if (stack > dataMax) dataMax = stack;
      if (this._forecast && this._forecast[h] > dataMax) {
        dataMax = this._forecast[h];
      }
    }
    const axis = niceAxis(dataMax > 0 ? dataMax * 1.05 : 0);
    const yMax = axis.max;
    const unitKwh = yMax >= 1000;

    // --- layout (viewBox ~16:7) -----------------------------------------
    const W = 800;
    const H = 350;
    const m = { top: 14, right: 16, bottom: 28, left: 52 };
    const plotW = W - m.left - m.right;
    const plotH = H - m.top - m.bottom;
    const colW = plotW / HOURS;
    const X = (hourFloat) => m.left + (hourFloat / HOURS) * plotW;
    const Y = (wh) => m.top + (1 - wh / yMax) * plotH;

    const el = svg("svg", {
      class: "plot",
      viewBox: `0 0 ${W} ${H}`,
      preserveAspectRatio: "xMidYMid meet",
      role: "img",
    });

    el.appendChild(this._axes(X, Y, axis, unitKwh, m, plotW, plotH));

    // --- stacked bars ----------------------------------------------------
    const barW = Math.max(1, colW - 3);
    for (let h = 0; h < HOURS; h++) {
      const bx = X(h) + (colW - barW) / 2;
      let acc = 0;
      for (const mod of this._modules) {
        const v = (this._bars[mod.id] && this._bars[mod.id][h]) || 0;
        if (v <= 0) continue;
        const yTop = Y(acc + v);
        const yBot = Y(acc);
        el.appendChild(
          svg("rect", {
            x: bx,
            y: yTop,
            width: barW,
            height: Math.max(0, yBot - yTop),
            fill: mod.color,
          }),
        );
        acc += v;
      }
    }

    // --- forecast: dashed stepped line at the hour widths ----------------
    if (this._forecast) {
      const pts = [];
      for (let h = 0; h < HOURS; h++) {
        const y = Y(this._forecast[h]);
        pts.push(`${X(h)},${y}`, `${X(h + 1)},${y}`);
      }
      el.appendChild(
        svg("polyline", {
          points: pts.join(" "),
          fill: "none",
          stroke: "var(--primary-text-color)",
          "stroke-width": "2",
          "stroke-dasharray": "5 4",
          opacity: "0.7",
        }),
      );
    }

    // --- empty / loading note (bars absent) ------------------------------
    if (this._loadState !== "ok") {
      const note =
        this._loadState === "loading" ? t.loading : t.noStats;
      const text = svg("text", {
        x: m.left + plotW / 2,
        y: m.top + plotH / 2,
        fill: "var(--secondary-text-color)",
        "font-size": "13",
        "text-anchor": "middle",
      });
      text.textContent = note;
      el.appendChild(text);
    }

    // --- hover crosshair + floating readout panel ------------------------
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

    const ctx = {
      svgEl: el,
      crosshair,
      t,
      modules: this._modules,
      bars: this._bars,
      forecast: this._forecast,
      m,
      plotW,
      plotH,
      colW,
      W,
    };
    const onMove = (ev) => this._hoverMove(ev, ctx);
    const onLeave = () => this._hoverLeave(ctx);
    overlay.addEventListener("mousemove", onMove);
    overlay.addEventListener("mouseleave", onLeave);
    overlay.addEventListener("touchstart", onMove, { passive: true });
    overlay.addEventListener("touchmove", onMove, { passive: true });

    return el;
  }

  /** Axis frame: y gridlines + Wh/kWh ticks, x hour ticks every 3 h. */
  _axes(X, Y, axis, unitKwh, m, plotW, plotH) {
    const g = svg("g", {});
    const axisColor = "var(--secondary-text-color)";
    const gridColor = "var(--divider-color, #e0e0e0)";

    // Left + bottom frame.
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

    // Y gridlines + labels.
    for (const tick of axis.ticks) {
      const y = Y(tick);
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
      const label = svg("text", {
        x: m.left - 6,
        y: y + 3,
        fill: axisColor,
        "font-size": "10",
        "text-anchor": "end",
      });
      label.textContent = fmtTick(tick, unitKwh);
      g.appendChild(label);
    }

    // X hour ticks every 3 h ("00" … "21"), fixed 0–24 axis.
    for (let h = 0; h <= HOURS; h += 3) {
      const x = X(h);
      if (h < HOURS) {
        const label = svg("text", {
          x: X(h + 0.5),
          y: m.top + plotH + 14,
          fill: axisColor,
          "font-size": "10",
          "text-anchor": "middle",
        });
        label.textContent = pad2(h);
        g.appendChild(label);
      }
      g.appendChild(
        svg("line", {
          x1: x,
          y1: m.top + plotH,
          x2: x,
          y2: m.top + plotH + 4,
          stroke: axisColor,
          "stroke-width": "1",
        }),
      );
    }

    return g;
  }

  // --- hover crosshair ----------------------------------------------------

  /** Pointer move over the plot → snap to the hovered hour column. */
  _hoverMove(ev, ctx) {
    const rect = ctx.svgEl.getBoundingClientRect();
    if (!rect.width) return;
    const clientX =
      ev.touches && ev.touches.length ? ev.touches[0].clientX : ev.clientX;
    if (typeof clientX !== "number") return;
    const vbx = ((clientX - rect.left) / rect.width) * ctx.W;
    let h = Math.floor((vbx - ctx.m.left) / ctx.colW);
    if (h < 0) h = 0;
    if (h > HOURS - 1) h = HOURS - 1;
    this._drawHover(ctx, h);
  }

  /** Pointer left the plot → drop the crosshair + panel. */
  _hoverLeave(ctx) {
    const g = ctx.crosshair;
    while (g.firstChild) g.removeChild(g.firstChild);
  }

  /** (Re)build the crosshair column highlight + floating readout for hour h. */
  _drawHover(ctx, h) {
    const g = ctx.crosshair;
    while (g.firstChild) g.removeChild(g.firstChild);
    const m = ctx.m;
    const xL = m.left + h * ctx.colW;
    const xc = xL + ctx.colW / 2;

    // (a) faint column highlight.
    g.appendChild(
      svg("rect", {
        x: xL,
        y: m.top,
        width: ctx.colW,
        height: ctx.plotH,
        fill: "var(--primary-text-color)",
        opacity: "0.06",
      }),
    );
    // (b) dashed vertical crosshair line at the column centre.
    g.appendChild(
      svg("line", {
        x1: xc,
        y1: m.top,
        x2: xc,
        y2: m.top + ctx.plotH,
        stroke: "var(--secondary-text-color)",
        "stroke-width": "1",
        opacity: "0.6",
        "stroke-dasharray": "4 3",
      }),
    );

    // (c) readout rows: title, per-module nonzero, total (bold), forecast.
    const rows = [];
    rows.push({ kind: "title", text: `${pad2(h)}:00–${pad2(h)}:59` });
    let total = 0;
    for (const mod of ctx.modules) {
      const v = (ctx.bars[mod.id] && ctx.bars[mod.id][h]) || 0;
      total += v;
      if (v > 0.5) {
        rows.push({ kind: "mod", color: mod.color, name: mod.name, val: v });
      }
    }
    rows.push({ kind: "total", name: ctx.t.total, val: total });
    if (ctx.forecast) {
      rows.push({ kind: "forecast", name: ctx.t.forecast, val: ctx.forecast[h] });
    }

    // (d) panel geometry — anchored left/right of the crosshair, flip at midline
    //     so it never clips the plot edge.
    const rowH = 17;
    const padX = 8;
    const padY = 6;
    const panelW = 176;
    const panelH = padY * 2 + rows.length * rowH;
    const rightSide = xc < ctx.W / 2;
    let px = rightSide ? xc + 10 : xc - 10 - panelW;
    const minX = m.left + 2;
    const maxX = m.left + ctx.plotW - panelW - 2;
    if (px < minX) px = minX;
    if (px > maxX) px = maxX;
    let py = m.top + 4;
    const maxY = m.top + ctx.plotH - panelH - 2;
    if (py > maxY) py = Math.max(m.top + 2, maxY);

    g.appendChild(
      svg("rect", {
        x: px,
        y: py,
        width: panelW,
        height: panelH,
        rx: 6,
        fill: "var(--card-background-color, #fff)",
        stroke: "var(--divider-color, #e0e0e0)",
        "stroke-width": "1",
        opacity: "0.98",
      }),
    );

    rows.forEach((row, i) => {
      const cy = py + padY + i * rowH + 12;
      if (row.kind === "title") {
        const tx = svg("text", {
          x: px + padX,
          y: cy,
          fill: "var(--primary-text-color)",
          "font-size": "11",
          "font-weight": "700",
        });
        tx.textContent = row.text;
        g.appendChild(tx);
        return;
      }
      // Left glyph: colour swatch (module) / dashed tick (forecast).
      if (row.kind === "mod") {
        g.appendChild(
          svg("rect", {
            x: px + padX,
            y: cy - 9,
            width: 10,
            height: 10,
            rx: 2,
            fill: row.color,
          }),
        );
      } else if (row.kind === "forecast") {
        g.appendChild(
          svg("line", {
            x1: px + padX,
            y1: cy - 4,
            x2: px + padX + 10,
            y2: cy - 4,
            stroke: "var(--primary-text-color)",
            "stroke-width": "2",
            "stroke-dasharray": "3 2",
            opacity: "0.7",
          }),
        );
      }
      const bold = row.kind === "total";
      const name = svg("text", {
        x: px + padX + 16,
        y: cy,
        fill: "var(--primary-text-color)",
        "font-size": "11",
        "font-weight": bold ? "700" : "400",
      });
      name.textContent = row.name;
      g.appendChild(name);
      const val = svg("text", {
        x: px + panelW - padX,
        y: cy,
        fill: "var(--primary-text-color)",
        "font-size": "11",
        "font-weight": bold ? "700" : "400",
        "text-anchor": "end",
      });
      val.textContent = fmtVal(row.val);
      g.appendChild(val);
    });
  }
}

if (!customElements.get(CARD_TAG)) {
  customElements.define("balcony-power-history-card", BalconyPowerHistoryCard);
}

// Advertise the card to the Lovelace "Add card" picker.
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === CARD_TAG)) {
  window.customCards.push({
    type: CARD_TAG,
    name: "Balcony Power History",
    description:
      "Stacked hourly production per module + forecast line (Balcony Solar Forecast).",
    preview: true,
    documentationURL:
      "https://github.com/danielr0815/balcony-solar-forecast/blob/main/docs/DASHBOARD.md",
  });
}

// One load banner (no version string — the resource URL carries the version).
console.info(
  "%c Balcony Power History Card ",
  "color:#fff;background:#3498db;font-weight:700;border-radius:4px;padding:2px 6px",
);
