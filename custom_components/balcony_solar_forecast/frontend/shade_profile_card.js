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
const COLOR_SUN = "#f1c40f"; // sun-path polyline (+ comparison overlay, dashed)
const COLOR_SHADE_FILL = "#7f8c8d"; // learned shade horizon fill
const COLOR_STATIC_HORIZON = "#95a5a6"; // static config horizon (dashed)

// Confidence visualisation (SPEC §5): each sun-path dot's radius encodes the
// pooled shademap-bin evidence n behind that sample. n=0 (static prior only) →
// a small HOLLOW ring; n>0 → a filled dot whose radius ramps DOT_R_MIN..DOT_R_FULL
// and SATURATES at N_SAT samples (beyond which more evidence adds no size). The
// comparison overlay reuses the sizing but its rings are ALWAYS hollow.
const DOT_R_FULL = 3; // radius at/above N_SAT samples (the pre-confidence size)
const DOT_R_MIN = 1.65; // radius for n=0 / a single sample (~55% of DOT_R_FULL)
const N_SAT = 12; // evidence count at which the dot reaches full size

// Sensor attribute names (must match const.ATTR_SP_* on the Python side).
const A_AZIMUTH = "azimuth";
const A_SUN_ELEVATION = "sun_elevation";
const A_TRANSMITTANCE = "transmittance";
// The module's OWN-channel τ (read-time pooling, SPEC §5). Non-empty only when
// the plane is grouped; drives the "Single" view of the group/single toggle.
const A_TRANSMITTANCE_INDIVIDUAL = "transmittance_individual";
// Pooled shademap-bin evidence n per sun-path sample (SPEC §5). Drives the
// confidence dot sizing; a missing / short array falls back to fixed-size dots.
const A_SAMPLE_N = "sample_n";
const A_TIME = "time";
const A_HORIZON_AZIMUTH = "horizon_azimuth";
const A_SHADE_HORIZON = "shade_horizon";
const A_STATIC_HORIZON = "static_horizon";
// The plotted module + local date, echoed by the sensor's profile dict. Used to
// tag the comparison fetch (re-fetch on module change) + the legend line.
const A_MODULE = "module";
const A_DATE = "date";
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
    view: "View",
    viewGroup: "Group",
    viewSingle: "Single",
    shaded: "shaded",
    compare: "Compare",
    clearCompare: "Clear comparison",
    compareError: "Comparison date could not be loaded.",
    hoverVs: "vs",
    noEntities:
      "No shade-profile entities found — is the Balcony Solar Forecast integration set up?",
    noSamples: "No daylight samples for this date.",
    hoverIdle: "Hover the chart for details",
    hoverShading: "Shading",
    hoverElevation: "Elevation",
    hoverEdge: "Shade edge",
    compass: ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
  },
  de: {
    module: "Modul",
    date: "Datum",
    view: "Ansicht",
    viewGroup: "Gruppe",
    viewSingle: "Einzeln",
    shaded: "verschattet",
    compare: "Vergleich",
    clearCompare: "Vergleich löschen",
    compareError: "Vergleichsdatum konnte nicht geladen werden.",
    hoverVs: "vs",
    noEntities:
      "Keine Verschattungsprofil-Entitäten gefunden — ist die Integration „Balcony Solar Forecast“ eingerichtet?",
    noSamples: "Keine Tageslicht-Datenpunkte für dieses Datum.",
    hoverIdle: "Über das Diagramm fahren für Details",
    hoverShading: "Verschattung",
    hoverElevation: "Elevation",
    hoverEdge: "Schattenkante",
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

/**
 * Evidence count n → dot radius (confidence viz). n=0 (or non-finite) → the
 * minimum radius (drawn hollow by the caller); n>0 ramps DOT_R_MIN..DOT_R_FULL,
 * saturating at N_SAT samples. The confidence sizing only kicks in when the
 * sensor supplies a parallel sample_n array; otherwise callers pass the fixed
 * DOT_R_FULL and never call this.
 */
function dotRadius(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return DOT_R_MIN;
  const frac = Math.min(1, v / N_SAT);
  return DOT_R_MIN + (DOT_R_FULL - DOT_R_MIN) * frac;
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
    // Active τ view for the group/single toggle: "group" = pooled (what the
    // forecast applies, the default) or "single" = this module's own channel.
    // Only meaningful when the sensor exposes a non-empty individual τ array.
    this._view = "group";
    // Last render inputs, so the toggle can force a re-render without a hass push.
    this._renderArgs = null;
    // Last-seen state objects (HA state objects are immutable, so identity
    // comparison detects a real change and gates re-render).
    this._lastSensor = undefined;
    this._lastSelect = undefined;
    this._lastDate = undefined;
    // Card-LOCAL comparison date (SPEC §15): a second sun path overlaid from the
    // read-only get_shade_profile service, kept entirely in the card (it never
    // touches the shared date entity). `_compareDate` is the ISO string in the
    // input; `_compareData` the fetched profile dict; `_compareModule` the module
    // it was fetched for (re-fetched when the primary module changes); the flags
    // gate an in-flight fetch and surface a load error inline.
    this._compareDate = "";
    this._compareData = null;
    this._compareModule = null;
    this._compareError = false;
    this._compareLoading = false;
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

  /** Re-render from the last inputs (used by the group/single toggle). */
  _rerender() {
    const a = this._renderArgs;
    if (a) this._render(a.hass, a.ids, a.s, a.sel, a.d);
  }

  _render(hass, ids, s, sel, d) {
    // Remember the inputs so the toggle can force a re-render without a hass push.
    this._renderArgs = { hass, ids, s, sel, d };
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
    // Legend line (── primary  - - compare) — only when a comparison is loaded
    // for the module now on screen.
    const legend = this._legend(s);
    if (legend) body.appendChild(legend);
    // Inline comparison-load error; the card keeps working regardless.
    if (this._compareError) {
      const errLine = document.createElement("div");
      errLine.className = "note compare-error";
      errLine.textContent = t.compareError;
      body.appendChild(errLine);
    }
    // Fixed-height hover status line (idle hint when not hovering); lives in the
    // header area so the crosshair readout never shifts the layout. Kept on
    // `this` so the plot's hover handler can update it without a re-render.
    this._readoutEl = this._statusLine(t);
    body.appendChild(this._readoutEl);
    body.appendChild(this._plot(s, t));
    // A card-local comparison date follows the primary module: (re)fetch it when
    // it is set but missing / stale for the module now on screen.
    this._maybeRefetchCompare(hass, s);
  }

  // --- comparison date (card-local overlay) -------------------------------

  /** The current comparison profile IF valid for the module now on screen. */
  _activeCompare(s) {
    const cmp = this._compareData;
    if (!cmp || !s) return null;
    const attrs = s.attributes || {};
    // Only overlay a comparison fetched for the module currently plotted (a
    // stale-module response is dropped; a re-fetch is already in flight).
    if (cmp[A_MODULE] && attrs[A_MODULE] && cmp[A_MODULE] !== attrs[A_MODULE]) {
      return null;
    }
    return isArray(cmp[A_AZIMUTH]) && cmp[A_AZIMUTH].length > 0 ? cmp : null;
  }

  /** Module currently plotted by the primary sensor (drives the compare fetch). */
  _currentModule(s) {
    return (s && s.attributes && s.attributes[A_MODULE]) || null;
  }

  /** Legend row: "── <primary date>   - - <compare date>" (only when loaded). */
  _legend(s) {
    const cmp = this._activeCompare(s);
    if (!cmp) return null;
    const attrs = s.attributes || {};
    const wrap = document.createElement("div");
    wrap.className = "legend";
    const mk = (glyph, dateStr) => {
      const item = document.createElement("span");
      item.className = "legend-item";
      const line = document.createElement("span");
      line.className = "legend-line";
      line.textContent = glyph;
      const txt = document.createElement("span");
      txt.textContent = " " + (dateStr || "—");
      item.appendChild(line);
      item.appendChild(txt);
      return item;
    };
    wrap.appendChild(mk("──", attrs[A_DATE]));
    wrap.appendChild(mk("- -", cmp[A_DATE] || this._compareDate));
    return wrap;
  }

  /** Date-input change: empty clears; a value fetches for the current module. */
  _onCompareChange(hass, s, value) {
    if (!value) {
      this._clearCompare();
      return;
    }
    this._compareDate = value;
    this._compareError = false;
    this._compareData = null;
    this._compareModule = null;
    this._loadCompare(hass, this._currentModule(s));
  }

  /** The × button: drop the comparison entirely and re-render. */
  _clearCompare() {
    this._compareDate = "";
    this._compareData = null;
    this._compareModule = null;
    this._compareError = false;
    this._compareLoading = false;
    this._rerender();
  }

  /** (Re)fetch the comparison when it is set but stale for the shown module. */
  _maybeRefetchCompare(hass, s) {
    if (!this._compareDate || this._compareLoading) return;
    const module = this._currentModule(s);
    // Fresh data for this exact module → nothing to do.
    if (this._compareData && this._compareModule === module) return;
    // Don't hammer a module/date pair that just failed.
    if (this._compareError && this._compareModule === module) return;
    this._loadCompare(hass, module);
  }

  /** Fetch the comparison profile via the service, then re-render the overlay. */
  async _loadCompare(hass, module) {
    if (!hass || !this._compareDate) return;
    this._compareLoading = true;
    const iso = this._compareDate;
    try {
      const profile = await this._fetchCompare(hass, module, iso);
      if (iso !== this._compareDate) return; // date changed mid-flight
      this._compareData = profile;
      this._compareModule = module;
      this._compareError = !profile;
    } catch (_e) {
      if (iso !== this._compareDate) return;
      this._compareData = null;
      this._compareModule = module;
      this._compareError = true;
    } finally {
      this._compareLoading = false;
    }
    this._rerender();
  }

  /** Call the read-only get_shade_profile service and return the profile dict. */
  async _fetchCompare(hass, module, iso) {
    // The frontend `callService` wrapper's return-response argument order has
    // churned across HA releases, so use the stable low-level websocket
    // `call_service` command with `return_response: true`.
    const serviceData = { date: iso };
    if (module) serviceData.module = module;
    const res = await hass.callWS({
      type: "call_service",
      domain: "balcony_solar_forecast",
      service: "get_shade_profile",
      service_data: serviceData,
      return_response: true,
    });
    // The command resolves to { context, response }; the service wraps its
    // payload as { result: <profile> }.
    const resp = res && res.response;
    const profile = resp && resp.result;
    return profile && typeof profile === "object" ? profile : null;
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
      .compare-row { display: inline-flex; align-items: center; gap: 6px; }
      .compare-clear {
        font: inherit; line-height: 1; cursor: pointer;
        color: var(--primary-text-color);
        background: var(--card-background-color, #fff);
        border: 1px solid var(--divider-color, #e0e0e0);
        border-radius: 6px; min-height: 34px; padding: 4px 10px;
      }
      .legend {
        display: flex; flex-wrap: wrap; gap: 16px; padding: 0 0 8px;
        color: var(--secondary-text-color); font-size: 0.8rem;
      }
      .legend-item { display: inline-flex; align-items: baseline; }
      .legend-line {
        color: ${COLOR_SUN}; font-weight: 700; letter-spacing: 1px;
        white-space: nowrap;
      }
      .compare-error { color: var(--error-color, #c0392b); }
      .toggle {
        display: inline-flex; border: 1px solid var(--divider-color, #e0e0e0);
        border-radius: 6px; overflow: hidden; min-height: 34px;
      }
      .toggle-btn {
        font: inherit; color: var(--primary-text-color); cursor: pointer;
        background: var(--card-background-color, #fff); border: 0;
        padding: 6px 12px;
      }
      .toggle-btn + .toggle-btn { border-left: 1px solid var(--divider-color, #e0e0e0); }
      .toggle-btn.active {
        background: var(--primary-color, #03a9f4);
        color: var(--text-primary-color, #fff); font-weight: 600;
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

    // Card-LOCAL comparison date (SPEC §15): overlays a SECOND date's sun path
    // via the read-only get_shade_profile service. Empty by default; the × button
    // clears it. It NEVER writes the shared date entity — it stays inside the card.
    {
      const field = document.createElement("div");
      field.className = "field";
      const label = document.createElement("label");
      label.textContent = t.compare;
      const row = document.createElement("div");
      row.className = "compare-row";
      const input = document.createElement("input");
      input.type = "date";
      input.className = "compare-input";
      input.value = /^\d{4}-\d{2}-\d{2}$/.test(this._compareDate)
        ? this._compareDate
        : "";
      input.addEventListener("change", (ev) => {
        this._onCompareChange(hass, s, ev.target.value);
      });
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "compare-clear";
      clear.textContent = "×";
      clear.title = t.clearCompare;
      clear.setAttribute("aria-label", t.clearCompare);
      clear.addEventListener("click", () => this._clearCompare());
      row.appendChild(input);
      row.appendChild(clear);
      field.appendChild(label);
      field.appendChild(row);
      wrap.appendChild(field);
    }

    // Group/Single τ view toggle — only when the sensor exposes a non-empty
    // individual (own-channel) τ array, i.e. this module is grouped (SPEC §5).
    const indiv = s.attributes[A_TRANSMITTANCE_INDIVIDUAL];
    if (isArray(indiv) && indiv.length > 0) {
      const field = document.createElement("div");
      field.className = "field";
      const label = document.createElement("label");
      label.textContent = t.view;
      const group = document.createElement("div");
      group.className = "toggle";
      group.setAttribute("role", "group");
      for (const [key, text] of [
        ["group", t.viewGroup],
        ["single", t.viewSingle],
      ]) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = text;
        btn.className = "toggle-btn" + (this._view === key ? " active" : "");
        btn.addEventListener("click", () => {
          if (this._view === key) return;
          this._view = key;
          this._rerender();
        });
        group.appendChild(btn);
      }
      field.appendChild(label);
      field.appendChild(group);
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
    const indiv = a[A_TRANSMITTANCE_INDIVIDUAL];
    const sampleN = a[A_SAMPLE_N];
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

    // Card-local comparison overlay: a SECOND date's sun path, fetched via the
    // get_shade_profile service and kept in the card. Drawn only when loaded for
    // the module now on screen; its shade horizon is intentionally NOT drawn.
    const cmp = this._activeCompare(s);
    const cAz = cmp ? cmp[A_AZIMUTH] : null;
    const cEl = cmp ? cmp[A_SUN_ELEVATION] : null;
    const cTau = cmp ? cmp[A_TRANSMITTANCE] : null;
    const cN = cmp ? cmp[A_SAMPLE_N] : null;
    const cDate = cmp ? cmp[A_DATE] || this._compareDate : null;
    const nCmp =
      cmp &&
      isArray(cAz) &&
      isArray(cEl) &&
      isArray(cTau) &&
      cAz.length > 0 &&
      cAz.length === cEl.length &&
      cAz.length === cTau.length
        ? cAz.length
        : 0;
    const hasCmpN = isArray(cN) && cN.length === nCmp && nCmp > 0;

    if (!nSun && !nHor && !nCmp) {
      return this._message(t.noSamples);
    }

    // Group/single toggle: colour the dots + drive the hover shading % by the
    // ACTIVE view's τ. The individual (own-channel) array is present only for a
    // grouped plane and runs parallel to the sun-path samples; otherwise the
    // pooled τ is the only view and no suffix is shown (SPEC §5).
    const hasIndiv = isArray(indiv) && indiv.length === nSun && nSun > 0;
    const activeSingle = this._view === "single" && hasIndiv;
    const activeTau = activeSingle ? indiv : tau;
    const viewSuffix = hasIndiv
      ? `(${activeSingle ? t.viewSingle : t.viewGroup})`
      : "";
    // Confidence viz drives the dot sizing only when the sensor supplies a
    // parallel sample_n array (SPEC §5); otherwise dots stay the fixed size.
    const hasSampleN = isArray(sampleN) && sampleN.length === nSun && nSun > 0;

    // --- domains ---------------------------------------------------------
    const azValues = [];
    for (let i = 0; i < nSun; i++) azValues.push(sunAz[i]);
    for (let i = 0; i < nHor; i++) azValues.push(horAz[i]);
    // Union the comparison path's azimuths so its curve fits the same axis.
    for (let i = 0; i < nCmp; i++) azValues.push(cAz[i]);
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
    // The comparison date (e.g. a summer day vs. a winter primary) can reach
    // higher elevations; union it so both paths stay fully on-screen.
    for (let i = 0; i < nCmp; i++) elValues.push(cEl[i]);
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

      // (4) one dot per sample, coloured by the ACTIVE view's τ, SIZED by the
      // pooled evidence n (confidence viz), with a native hover tooltip. n=0
      // (static prior only) → a small HOLLOW ring stroked in the τ colour; n>0 →
      // a filled dot whose radius ramps with evidence to N_SAT. No sample_n array
      // → the fixed full-size filled dot (graceful fallback).
      for (let i = 0; i < nSun; i++) {
        const color = tauColor(activeTau[i]);
        const n = hasSampleN ? Number(sampleN[i]) || 0 : null;
        const filled = !hasSampleN || n > 0;
        const dot = svg("circle", {
          cx: X(sunAz[i]),
          cy: Y(sunEl[i]),
          r: hasSampleN ? dotRadius(n) : DOT_R_FULL,
          fill: filled ? color : "transparent",
          stroke: filled ? null : color,
          "stroke-width": filled ? null : "1.2",
        });
        const title = document.createElementNS(SVGNS, "title");
        const nTxt = hasSampleN ? ` · n=${n}` : "";
        title.textContent = `${hhmm(time[i])} · el ${Number(sunEl[i]).toFixed(
          1,
        )}° · τ ${Number(activeTau[i]).toFixed(2)}${nTxt}`;
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
        tau: activeTau,
        sampleN: hasSampleN ? sampleN : null,
        viewSuffix,
        time,
        n: nSun,
        // Horizon (obstruction) profile, for the hover readout's shade-edge:
        // at the hovered azimuth, the elevation below which the beam is blocked.
        horAz: nHor ? horAz : null,
        shadeH: nHor && isArray(shadeH) ? shadeH : null,
        staticH: nHor && isArray(staticH) ? staticH : null,
        nHor,
        // Comparison overlay (nearest-in-azimuth readout), null when unloaded.
        cmpAz: nCmp ? cAz : null,
        cmpTau: nCmp ? cTau : null,
        cmpDate: nCmp ? cDate : null,
        nCmp,
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

    // (6) card-local comparison overlay: a DASHED sun path (same yellow, 0.6
    // opacity) + HOLLOW τ-coloured rings for the comparison date. Its shade
    // horizon is intentionally NOT drawn (keeps the plot readable); confidence
    // sizing applies but the rings are ALWAYS hollow. Decorative only —
    // pointer-events off so it never steals the primary crosshair's hover.
    if (nCmp) {
      const pts = [];
      for (let i = 0; i < nCmp; i++) pts.push(`${X(cAz[i])},${Y(cEl[i])}`);
      el.appendChild(
        svg("polyline", {
          points: pts.join(" "),
          fill: "none",
          stroke: COLOR_SUN,
          "stroke-width": "2",
          "stroke-linejoin": "round",
          "stroke-dasharray": "5 4",
          opacity: "0.6",
          "pointer-events": "none",
        }),
      );
      for (let i = 0; i < nCmp; i++) {
        el.appendChild(
          svg("circle", {
            cx: X(cAz[i]),
            cy: Y(cEl[i]),
            r: hasCmpN ? dotRadius(cN[i]) : DOT_R_FULL,
            fill: "none",
            stroke: tauColor(cTau[i]),
            "stroke-width": "1.2",
            opacity: "0.8",
            "pointer-events": "none",
          }),
        );
      }
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

  /**
   * Linear-interpolated horizon elevation at azimuth ``az`` from the parallel
   * ``xs`` (azimuth) / ``ys`` (elevation) arrays — the shading-edge angle, i.e.
   * the elevation below which the obstruction blocks the beam. Assumes ``xs``
   * ascending (the sensor emits sorted horizon rows). Returns null when no
   * usable grid is present. Clamps to the endpoints outside the covered range.
   */
  _horizonEdgeAt(xs, ys, az) {
    if (!isArray(xs) || !isArray(ys) || xs.length < 1) return null;
    if (xs.length === 1) return Number(ys[0]);
    if (az <= Number(xs[0])) return Number(ys[0]);
    if (az >= Number(xs[xs.length - 1])) return Number(ys[xs.length - 1]);
    for (let k = 0; k < xs.length - 1; k++) {
      const a0 = Number(xs[k]);
      const a1 = Number(xs[k + 1]);
      if (a0 <= az && az <= a1) {
        const e0 = Number(ys[k]);
        const e1 = Number(ys[k + 1]);
        return a1 === a0 ? e0 : e0 + (e1 - e0) * ((az - a0) / (a1 - a0));
      }
    }
    return null;
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
    // Confidence: the pooled evidence n behind this sample (SPEC §5).
    const nTag = ctx.sampleN ? ` · n=${Number(ctx.sampleN[i]) || 0}` : "";
    // Group/single view tag, only when a distinct individual view exists.
    const suffix = ctx.viewSuffix ? ` · ${ctx.viewSuffix}` : "";
    // Comparison: the comparison sample NEAREST in azimuth, when a compare date
    // is loaded — "· vs <date>: <shading>% (τ x.xx)".
    let cmpTag = "";
    if (ctx.nCmp && ctx.cmpAz) {
      let cb = 0;
      let cbD = Infinity;
      for (let k = 0; k < ctx.nCmp; k++) {
        const dd = Math.abs(Number(ctx.cmpAz[k]) - az);
        if (dd < cbD) {
          cbD = dd;
          cb = k;
        }
      }
      const ctau = Number(ctx.cmpTau[cb]);
      const cpct = Math.round((1 - ctau) * 100);
      cmpTag = ` · ${t.hoverVs} ${ctx.cmpDate}: ${cpct} % (τ ${ctau.toFixed(2)})`;
    }
    // Shade-edge: the obstruction elevation at this azimuth (learned horizon,
    // falling back to the static configured one) — the angle below which the
    // beam is blocked. Appended as "· Schattenkante Y°" when a horizon grid is
    // present, so the operator reads OFF the chart at what elevation the shadow
    // would strike for the hovered sun azimuth.
    let edgeTag = "";
    if (ctx.horAz) {
      let edge = this._horizonEdgeAt(ctx.horAz, ctx.shadeH, az);
      if (edge == null || !isFinite(edge)) {
        edge = this._horizonEdgeAt(ctx.horAz, ctx.staticH, az);
      }
      if (edge != null && isFinite(edge)) {
        edgeTag = ` · ${t.hoverEdge} ${Math.round(edge)}°`;
      }
    }
    return (
      `${ctx.time[i]} · ${Math.round(az)}° ${compass} · ` +
      `${t.hoverShading} ${shadingPct} % (τ ${tau.toFixed(2)}) · ` +
      `${t.hoverElevation} ${Math.round(el)}°${edgeTag}${nTag}${suffix}${cmpTag}`
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
