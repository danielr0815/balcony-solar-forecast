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
 *                         types ["mean"]) for the sources, over the selected day's
 *                         [00:00, +24h) local window; hourly Wh per module =
 *                         mean W × 1 h. Refetched on connect, every 5 minutes, and
 *                         when the local day rolls — but ONLY while viewing today /
 *                         the current week (a past view is static).
 *   3. Forecast line    — TODAY: the forecast sensor's `wh_period` attribute
 *                         (15-min Wh, ISO-UTC keys) aggregated to local hours in
 *                         the card. PAST day: the ISSUED day-ahead curve archived
 *                         in the store, fetched via the read-only
 *                         `get_issued_forecast` action (frozen ~01:30 stand, no
 *                         hindsight). WEEK view: one dashed forecast segment per
 *                         day — issued daily totals for past days (fetched
 *                         concurrently, cached per window), the live wh_period
 *                         sum for today, and an honest GAP where no snapshot is
 *                         archived.
 *
 * NAVIGATION (card-local state, never persisted): a header ◀ [label] ▶ steps the
 * selected day (or, in week mode, the 7-day window) and a Day|Week toggle switches
 * the view. Week mode charts daily Wh per module from `period: "day"` mean
 * statistics (mean W × 24 h). ▶ is disabled once the window ends at today.
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
    noStatsRange: "No statistics for this range",
    // Provenance caption above the plot when a forecast line IS drawn.
    forecastLive: "Forecast (live)",
    forecastIssued: "Forecast (as issued 01:30)",
    // Under-plot notes when NO past-day line is drawn ({d} = the date).
    forecastError: "Forecast lookup failed",
    forecastMissing:
      "No archived forecast for {d} — the archive fills with each nightly run.",
    archiveSince: " (archive since {d})",
    loading: "…",
    viewDay: "Day",
    viewWeek: "Week",
    today: "Today",
    yesterday: "Yesterday",
    prev: "Previous",
    next: "Next",
    weekdays: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
  },
  de: {
    title: "Stündliche Produktion je Modul",
    total: "Gesamt",
    forecast: "Prognose",
    noEntities:
      "Kein Messleistungs-Sensor gefunden — ist die Integration „Balcony Solar Forecast“ eingerichtet?",
    noStats: "Noch keine Stundenstatistik",
    noStatsRange: "Keine Statistikdaten für diesen Zeitraum",
    // Provenance caption above the plot when a forecast line IS drawn.
    forecastLive: "Prognose (live)",
    forecastIssued: "Prognose (Stand 01:30)",
    // Under-plot notes when NO past-day line is drawn ({d} = the date).
    forecastError: "Prognose-Abruf fehlgeschlagen",
    forecastMissing:
      "Keine archivierte Prognose für {d} — der Verlauf füllt sich mit jedem Nachtlauf.",
    archiveSince: " (Archiv seit {d})",
    loading: "…",
    viewDay: "Tag",
    viewWeek: "Woche",
    today: "Heute",
    yesterday: "Gestern",
    prev: "Zurück",
    next: "Weiter",
    weekdays: ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"],
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

/** Local midnight Date of TODAY + `offset` days (offset ≤ 0 = today/past). */
function dayAt(offset) {
  const now = new Date();
  return new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() + offset,
    0,
    0,
    0,
    0,
  );
}

/** Local ISO calendar date ("YYYY-MM-DD") of a Date — the service's date key. */
function isoDateOf(d) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** Stable "YYYY-MM-DD" key of a statistics start / ISO slot key, or "" if bad. */
function localDayKeyOf(v) {
  const d = toDate(v);
  return d ? isoDateOf(d) : "";
}

/** "d.M." (no year) — week-axis + short date label. */
function shortDate(d) {
  return `${d.getDate()}.${d.getMonth() + 1}.`;
}

/** "d.M.Y" — the week label's trailing (window-end) date. */
function shortDateYear(d) {
  return `${d.getDate()}.${d.getMonth() + 1}.${d.getFullYear()}`;
}

/** "d.M.Y" of an ISO "YYYY-MM-DD" date key (falls back to the raw string). */
function shortDateYearISO(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  if (!m) return iso || "";
  return `${Number(m[3])}.${Number(m[2])}.${m[1]}`;
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
    // Card-LOCAL selection (never persisted): the view and the navigation offset
    // in DAYS from today (0 = today / current week, negative = past). In week mode
    // the offset addresses the 7-day window's LAST day and steps by 7.
    this._view = "day"; // "day" | "week"
    this._offset = 0;
    // Last-seen forecast state object + module signature — identity/equality gate
    // the re-render on a real change (the async stats fetch re-renders itself).
    this._lastForecast = undefined;
    this._moduleSig = "";
    // Derived render inputs.
    this._modules = []; // [{ id, name, color }]
    this._dayBars = {}; // stat_id -> number[24] hourly Wh (day mode)
    this._weekBars = {}; // stat_id -> number[7] daily Wh (week mode)
    this._dayForecast = null; // number[24] hourly Wh, or null (no line)
    this._weekForecast = null; // (number|null)[7] daily forecast Wh; null = gap
    // Per-window issued-total cache {windowKey: {isoDate: wh|null}} so navigating
    // back to an already-fetched week never refires its per-day service calls.
    // Never cleared — a handful of numbers per visited window.
    this._weekForecastCache = {};
    // Forecast provenance for the day view: "live" (today's wh_period), "issued"
    // (a past day's archived curve), "missing" (a past day with no snapshot →
    // the dated under-plot note), "error" (the lookup itself failed), or "none"
    // (disabled / today without a curve / loading-neutral while navigating).
    this._forecastState = "none";
    this._forecastError = ""; // short message behind the "error" note
    this._oldestIssued = null; // oldest archived ISO date (service-reported)
    this._loadState = "loading"; // "loading" | "ok" | "empty" | "error"
    // Fetch bookkeeping.
    this._timer = null;
    // localDayKey of the last kicked live fetch. Deliberately NOT named after
    // the `_fetchDay(...)` METHOD: assigning an instance property with a
    // method's name shadows that method, which turned the first fetch into a
    // TypeError and killed every statistics load in v0.15.0/v0.16.0.
    this._liveDayKey = undefined;
    this._fetchSeq = 0; // generation token — a stale async fetch is ignored
  }

  // --- Lovelace card API --------------------------------------------------

  setConfig(config) {
    // All keys optional; auto-discovery fills the rest at render time.
    this._config = config || {};
    this._rendered = false;
    this._lastForecast = undefined;
    this._moduleSig = "";
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
      // The 5-min auto-refresh only fires while viewing the live window (today /
      // current week); a past view is static, so it is never refetched on a tick.
      this._timer = setInterval(() => {
        if (this._isLive()) this._fetch();
      }, REFETCH_MS);
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
    // Kick the first (or, while live, a new-day) statistics fetch as hass arrives.
    this._ensureFetch();

    const ids = this._resolveIds(hass);
    const total = hass.states[ids.total_sensor];
    const forecast = hass.states[ids.forecast_sensor];
    const liveDay = this._view === "day" && this._offset === 0;
    // The module list rarely changes; compute a cheap signature so a plain
    // measured-power tick does NOT re-render a static past/week view.
    this._rebuildModules(total);
    const sig = this._modules.map((m) => `${m.id}|${m.name}`).join(",");
    let needRender = !this._rendered || sig !== this._moduleSig;
    this._moduleSig = sig;
    // In the live today view the dashed line tracks the forecast sensor's curve;
    // in a past/week view the forecast comes from the service fetch (or none), so
    // a forecast-sensor push must not touch it.
    if (liveDay && forecast !== this._lastForecast) {
      this._recomputeForecast(forecast);
      needRender = true;
    }
    this._lastForecast = forecast;
    if (needRender) {
      this._render();
      this._rendered = true;
    }
  }

  // --- navigation (card-local) --------------------------------------------

  /** True while the selection addresses the live window (today / current week). */
  _isLive() {
    return this._offset === 0;
  }

  /** Days per navigation step: a week view moves the whole 7-day window. */
  _step() {
    return this._view === "week" ? 7 : 1;
  }

  /** ◀ / ▶ handler: shift the offset into the past / toward today, then reload. */
  _navigate(dir) {
    const next = this._offset + dir * this._step();
    if (next > 0) return; // ▶ never advances past today (its button is disabled)
    if (next === this._offset) return;
    this._offset = next;
    this._reload();
  }

  /** Day|Week toggle: switch view, reset to the current window, reload. */
  _setView(view) {
    if (view === this._view) return;
    this._view = view;
    this._offset = 0;
    this._reload();
  }

  /** A nav/toggle click refetches the new window and fully re-renders. */
  _reload() {
    this._loadState = "loading";
    // Loading-neutral forecast state BEFORE the render: the previous selection's
    // line (or missing/error note) must not linger while the new window loads —
    // a stale line was read by the operator as "the forecast is not updating".
    this._dayForecast = null;
    this._weekForecast = null;
    this._forecastState = "none";
    this._forecastError = "";
    this._render();
    this._fetch();
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

  /** Forecast sensor's 15-min `wh_period` → the live TODAY line (number[24]). */
  _recomputeForecast(forecast) {
    if (this._config.hours_forecast === false) {
      this._dayForecast = null;
      this._forecastState = "none";
      return;
    }
    const wh =
      forecast && forecast.attributes && forecast.attributes[A_WH_PERIOD];
    if (!wh || typeof wh !== "object") {
      // attr missing → bars only, no line, no error (it is today, still live).
      this._dayForecast = null;
      this._forecastState = "none";
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
    this._dayForecast = any ? arr : null;
    this._forecastState = any ? "live" : "none";
  }

  // --- statistics fetch ---------------------------------------------------

  /** Fetch on first hass, and — while live — once per new local day (day roll). */
  _ensureFetch() {
    if (!this._hass || !this.isConnected) return;
    if (this._liveDayKey === undefined) {
      this._liveDayKey = localDayKey();
      this._fetch();
      return;
    }
    // Day-roll handling applies ONLY to the live window; a past view is static.
    if (this._isLive()) {
      const day = localDayKey();
      if (this._liveDayKey !== day) {
        this._liveDayKey = day;
        this._fetch();
      }
    }
  }

  /** Sources (statistic ids) from the total sensor, or [] when unavailable. */
  _sources(hass) {
    const ids = this._resolveIds(hass);
    const total = hass.states[ids.total_sensor];
    return total && total.attributes && isArray(total.attributes[A_SOURCES])
      ? total.attributes[A_SOURCES]
      : [];
  }

  /** Fetch the selected window (day or week) for the module sources, re-render. */
  async _fetch() {
    const hass = this._hass;
    if (!hass || typeof hass.callWS !== "function") return;
    const sources = this._sources(hass);
    if (!sources.length) return;
    if (this._isLive()) this._liveDayKey = localDayKey();
    const seq = ++this._fetchSeq; // any newer fetch/selection supersedes this one
    if (this._view === "week") {
      await this._fetchWeek(hass, sources, seq);
    } else {
      await this._fetchDay(hass, sources, seq);
    }
  }

  /** Day view: hourly means over [selected 00:00, next 00:00) → Wh, +line. */
  async _fetchDay(hass, sources, seq) {
    const start = dayAt(this._offset);
    const end = dayAt(this._offset + 1); // next LOCAL midnight (DST-exact)
    const now = new Date();
    // Today stops at "now" (matches the original live behaviour); a past day
    // spans the whole local day.
    const endTime = (this._offset === 0 ? now : end).toISOString();
    let result;
    try {
      result = await hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        end_time: endTime,
        statistic_ids: sources,
        period: "hour",
        types: ["mean"],
      });
    } catch (err) {
      if (seq !== this._fetchSeq) return;
      this._loadState = "error";
      this._render();
      return;
    }
    if (seq !== this._fetchSeq) return; // selection changed mid-flight
    this._ingestDay(result, sources);
    // Forecast line: TODAY uses the live wh_period curve; a PAST day reads the
    // archived issued curve via the read-only service (awaited below).
    if (this._offset === 0) {
      const ids = this._resolveIds(hass);
      this._recomputeForecast(hass.states[ids.forecast_sensor]);
      this._render();
      return;
    }
    await this._fetchIssued(hass, isoDateOf(start), seq);
    if (seq !== this._fetchSeq) return;
    this._render();
  }

  /** Week view: daily means over the 7-day window → per-source daily Wh. */
  async _fetchWeek(hass, sources, seq) {
    // The window ENDS at the selected day; step back 6 days for its start. The
    // seven local-midnight day starts drive both the query and the bucketing.
    const days = [];
    for (let i = 0; i < 7; i++) days.push(dayAt(this._offset - 6 + i));
    const start = days[0];
    const windowEnd = dayAt(this._offset + 1); // day AFTER the window end
    const now = new Date();
    const endTime = (this._offset === 0 ? now : windowEnd).toISOString();
    let result;
    try {
      result = await hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        end_time: endTime,
        statistic_ids: sources,
        period: "day",
        types: ["mean"],
      });
    } catch (err) {
      if (seq !== this._fetchSeq) return;
      this._loadState = "error";
      this._render();
      return;
    }
    if (seq !== this._fetchSeq) return;
    this._ingestWeek(result, sources, days);
    // Bars render straight away; the per-day forecast overlay follows once the
    // (cached / concurrent) issued lookups land.
    this._render();
    await this._fetchWeekForecast(hass, days, seq);
    if (seq !== this._fetchSeq) return;
    this._render();
  }

  /**
   * Week forecast overlay: one daily forecast total (Wh) per window day.
   * Past days read the ISSUED archived curve — CONCURRENTLY, one
   * get_issued_forecast call per not-yet-cached day; TODAY inside the window
   * uses the LIVE wh_period sum from the forecast sensor (never the service).
   * A day with no archived snapshot stays null → the overlay keeps an honest
   * GAP there. Results are cached per window so navigating back to an
   * already-fetched week never refires its service calls.
   */
  async _fetchWeekForecast(hass, days, seq) {
    if (this._config.hours_forecast === false) {
      this._weekForecast = null;
      return;
    }
    const todayIso = isoDateOf(new Date());
    const key = `${isoDateOf(days[0])}_${isoDateOf(days[6])}`;
    const cached = this._weekForecastCache[key] || {};
    const lookups = [];
    for (const d of days) {
      const iso = isoDateOf(d);
      if (iso === todayIso || iso in cached) continue;
      lookups.push(
        this._issuedTotal(hass, iso).then((wh) => {
          cached[iso] = wh;
        }),
      );
    }
    if (lookups.length) await Promise.all(lookups);
    if (seq !== this._fetchSeq) return;
    this._weekForecastCache[key] = cached;
    const totals = new Array(7).fill(null);
    for (let i = 0; i < 7; i++) {
      const iso = isoDateOf(days[i]);
      if (iso === todayIso) {
        // Live value — recomputed on every (5-min) refresh, never cached.
        totals[i] = this._liveForecastTotal(hass);
      } else if (typeof cached[iso] === "number") {
        totals[i] = cached[iso];
      }
    }
    this._weekForecast = totals;
  }

  /** One issued lookup → that day's total Wh, or null (no snapshot / failed). */
  async _issuedTotal(hass, iso) {
    try {
      const res = await hass.callWS({
        type: "call_service",
        domain: "balcony_solar_forecast",
        service: "get_issued_forecast",
        service_data: { date: iso },
        return_response: true,
      });
      const resp = res && res.response;
      const result = resp && resp.result;
      if (!result || typeof result !== "object" || result.available === false) {
        return null;
      }
      let total = 0;
      const wh = result.hourly_wh;
      if (wh && typeof wh === "object") {
        for (const k in wh) {
          const v = Number(wh[k]);
          if (Number.isFinite(v)) total += v;
        }
      }
      return total;
    } catch (_e) {
      // A failed week-day lookup degrades to a gap (never breaks the view).
      return null;
    }
  }

  /** Today's LIVE forecast total (Wh) from the sensor's wh_period, or null. */
  _liveForecastTotal(hass) {
    const ids = this._resolveIds(hass);
    const forecast = hass.states[ids.forecast_sensor];
    const wh =
      forecast && forecast.attributes && forecast.attributes[A_WH_PERIOD];
    if (!wh || typeof wh !== "object") return null;
    let total = 0;
    let any = false;
    for (const iso in wh) {
      const v = Number(wh[iso]);
      if (!Number.isFinite(v)) continue;
      total += v;
      any = true;
    }
    return any ? total : null;
  }

  /** Call the read-only get_issued_forecast action → the archived day line. */
  async _fetchIssued(hass, iso, seq) {
    if (this._config.hours_forecast === false) {
      this._dayForecast = null;
      this._forecastState = "none";
      return;
    }
    let result;
    try {
      // The frontend `callService` wrapper's return-response argument order has
      // churned across HA releases, so use the stable low-level websocket
      // `call_service` command with `return_response: true` (as the shade card).
      const res = await hass.callWS({
        type: "call_service",
        domain: "balcony_solar_forecast",
        service: "get_issued_forecast",
        service_data: { date: iso },
        return_response: true,
      });
      const resp = res && res.response;
      result = resp && resp.result;
    } catch (err) {
      if (seq !== this._fetchSeq) return;
      // The LOOKUP failed (websocket/service error) — NOT the same thing as
      // "no snapshot archived". Surface it as an explicit error note so the
      // operator never misreads a transport hiccup as a missing forecast.
      this._dayForecast = null;
      this._forecastState = "error";
      this._forecastError =
        err && err.message ? String(err.message) : "";
      return;
    }
    if (seq !== this._fetchSeq) return;
    if (!result || typeof result !== "object" || result.available === false) {
      // No snapshot archived for that day → no line, explicit dated note
      // (plus "archive since <date>" when the service reports its oldest day).
      this._dayForecast = null;
      this._forecastState = "missing";
      this._oldestIssued =
        result && typeof result.oldest_available === "string"
          ? result.oldest_available
          : null;
      return;
    }
    const wh = result.hourly_wh;
    const arr = new Array(HOURS).fill(0);
    if (wh && typeof wh === "object") {
      for (const key in wh) {
        const h = localHourOf(key);
        if (h < 0) continue;
        const v = Number(wh[key]);
        if (!Number.isFinite(v)) continue;
        arr[h] += v;
      }
    }
    this._dayForecast = arr;
    this._forecastState = "issued";
  }

  /** {stat_id: [{start, mean}]} → per-source number[24] hourly Wh (mean W×1h). */
  _ingestDay(result, sources) {
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
    this._dayBars = bars;
    this._loadState = any ? "ok" : "empty";
  }

  /** {stat_id: [{start, mean}]} → per-source number[7] daily Wh (mean W×24h). */
  _ingestWeek(result, sources, days) {
    // The daily-mean statistic is the day's AVERAGE power (W); integrating a
    // constant mean over the 24 h day recovers the day's energy exactly:
    // ∫ mean dt = mean × 24 h = daily Wh. Each row is bucketed to its column by
    // local calendar date so a DST-short/long day still lands in the right slot.
    const index = {};
    for (let i = 0; i < 7; i++) index[isoDateOf(days[i])] = i;
    const bars = {};
    let any = false;
    for (const id of sources) {
      const rows = result && result[id];
      const arr = new Array(7).fill(0);
      if (isArray(rows)) {
        for (const row of rows) {
          const i = index[localDayKeyOf(row && row.start)];
          if (i === undefined) continue;
          const mean = Number(row && row.mean);
          if (!Number.isFinite(mean)) continue;
          arr[i] += mean * HOURS; // mean power (W) × 24 h = daily Wh
          any = true;
        }
      }
      bars[id] = arr;
    }
    this._weekBars = bars;
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

    body.appendChild(this._header(t));
    if (this._view === "week") {
      body.appendChild(this._weekPlot(t));
    } else {
      // Provenance caption ABOVE the plot whenever a line is drawn: live curve
      // vs the archived ~01:30 issued stand (the operator must never guess).
      const caption = this._provenanceCaption(t);
      if (caption) body.appendChild(caption);
      body.appendChild(this._dayPlot(t));
      // Under-plot note when a past day draws NO line: missing vs failed.
      const note = this._forecastNote(t);
      if (note) body.appendChild(note);
    }
    body.appendChild(this._legend(t));
  }

  /** Right-aligned caption above the day plot while a forecast line is drawn. */
  _provenanceCaption(t) {
    if (!this._dayForecast) return null;
    let text = null;
    if (this._forecastState === "live") text = t.forecastLive;
    else if (this._forecastState === "issued") text = t.forecastIssued;
    if (!text) return null;
    const div = document.createElement("div");
    div.className = "provenance";
    div.textContent = text;
    return div;
  }

  /** The day view's under-plot forecast note (or null): missing vs error. */
  _forecastNote(t) {
    if (this._offset === 0) return null;
    let text = null;
    if (this._forecastState === "error") {
      text = t.forecastError;
      if (this._forecastError) text += ` (${this._forecastError})`;
    } else if (this._forecastState === "missing") {
      text = t.forecastMissing.replace(
        "{d}",
        shortDateYear(dayAt(this._offset)),
      );
      if (this._oldestIssued) {
        text += t.archiveSince.replace(
          "{d}",
          shortDateYearISO(this._oldestIssued),
        );
      }
    }
    if (!text) return null;
    const div = document.createElement("div");
    div.className = "note";
    div.textContent = text;
    return div;
  }

  /** Header row: ◀ [label] ▶ day/week nav on the left, Day|Week toggle right. */
  _header(t) {
    const wrap = document.createElement("div");
    wrap.className = "header";

    // Left: previous / label / next. ▶ is disabled once the window ends today.
    const nav = document.createElement("div");
    nav.className = "nav";
    const prev = document.createElement("button");
    prev.type = "button";
    prev.className = "nav-btn";
    prev.textContent = "◀";
    prev.title = t.prev;
    prev.setAttribute("aria-label", t.prev);
    prev.addEventListener("click", () => this._navigate(-1));
    const label = document.createElement("span");
    label.className = "nav-label";
    label.textContent = this._navLabel(t);
    const next = document.createElement("button");
    next.type = "button";
    next.className = "nav-btn";
    next.textContent = "▶";
    next.title = t.next;
    next.setAttribute("aria-label", t.next);
    next.disabled = this._offset >= 0;
    next.addEventListener("click", () => this._navigate(1));
    nav.appendChild(prev);
    nav.appendChild(label);
    nav.appendChild(next);
    wrap.appendChild(nav);

    // Right: Day|Week two-option toggle (same visual style as the shade card's).
    const toggle = document.createElement("div");
    toggle.className = "toggle";
    toggle.setAttribute("role", "group");
    for (const [key, text] of [
      ["day", t.viewDay],
      ["week", t.viewWeek],
    ]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = text;
      btn.className = "toggle-btn" + (this._view === key ? " active" : "");
      btn.addEventListener("click", () => this._setView(key));
      toggle.appendChild(btn);
    }
    wrap.appendChild(toggle);
    return wrap;
  }

  /** The nav label: Today/Yesterday/date (day) or the 7-day window (week). */
  _navLabel(t) {
    if (this._view === "week") {
      const end = dayAt(this._offset);
      const start = dayAt(this._offset - 6);
      return `${shortDate(start)}–${shortDateYear(end)}`;
    }
    if (this._offset === 0) return t.today;
    if (this._offset === -1) return t.yesterday;
    const lang = ((this._hass && this._hass.language) || "en").replace("_", "-");
    const d = dayAt(this._offset);
    try {
      return d.toLocaleDateString(lang);
    } catch (_e) {
      return isoDateOf(d);
    }
  }

  _style() {
    const style = document.createElement("style");
    style.textContent = `
      .content { padding: 0 16px 16px; }
      .header {
        display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
        justify-content: space-between; padding: 4px 0 12px;
      }
      .nav { display: inline-flex; align-items: center; gap: 8px; }
      .nav-btn {
        font: inherit; line-height: 1; cursor: pointer;
        color: var(--primary-text-color);
        background: var(--card-background-color, #fff);
        border: 1px solid var(--divider-color, #e0e0e0);
        border-radius: 6px; min-height: 34px; min-width: 34px; padding: 4px 10px;
      }
      .nav-btn:disabled { opacity: 0.4; cursor: default; }
      .nav-label {
        min-width: 8.5em; text-align: center; font-weight: 600;
        color: var(--primary-text-color); font-variant-numeric: tabular-nums;
      }
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
      .plot { width: 100%; height: auto; display: block; }
      .provenance {
        text-align: right; padding: 0 2px 2px;
        color: var(--secondary-text-color); font-size: 0.75rem;
      }
      .note {
        margin-top: 8px; text-align: center;
        color: var(--secondary-text-color); font-size: 0.85rem;
      }
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

  _dayPlot(t) {
    // --- domains ---------------------------------------------------------
    let dataMax = 0;
    for (let h = 0; h < HOURS; h++) {
      let stack = 0;
      for (const mod of this._modules) {
        const v = (this._dayBars[mod.id] && this._dayBars[mod.id][h]) || 0;
        if (v > 0) stack += v;
      }
      if (stack > dataMax) dataMax = stack;
      if (this._dayForecast && this._dayForecast[h] > dataMax) {
        dataMax = this._dayForecast[h];
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
        const v = (this._dayBars[mod.id] && this._dayBars[mod.id][h]) || 0;
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
    if (this._dayForecast) {
      const pts = [];
      for (let h = 0; h < HOURS; h++) {
        const y = Y(this._dayForecast[h]);
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
      // A PAST day with no bars is "no statistics for this range" (the recorder
      // may not have covered that day); today with none is "not yet".
      const empty = this._offset === 0 ? t.noStats : t.noStatsRange;
      const note = this._loadState === "loading" ? t.loading : empty;
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
      mode: "day",
      cols: HOURS,
      modules: this._modules,
      bars: this._dayBars,
      forecast: this._dayForecast,
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

  /** Week view: 7 stacked day-bars + a dashed forecast segment per day. */
  _weekPlot(t) {
    const COLS = 7;
    const days = [];
    for (let i = 0; i < COLS; i++) days.push(dayAt(this._offset - 6 + i));

    // --- domain: max stacked daily total across the 7 days ---------------
    let dataMax = 0;
    for (let i = 0; i < COLS; i++) {
      let stack = 0;
      for (const mod of this._modules) {
        const v = (this._weekBars[mod.id] && this._weekBars[mod.id][i]) || 0;
        if (v > 0) stack += v;
      }
      if (stack > dataMax) dataMax = stack;
      const f = this._weekForecast && this._weekForecast[i];
      if (typeof f === "number" && f > dataMax) dataMax = f;
    }
    const axis = niceAxis(dataMax > 0 ? dataMax * 1.05 : 0);
    const yMax = axis.max;
    const unitKwh = yMax >= 1000;

    // --- layout (same viewBox as the day view) ---------------------------
    const W = 800;
    const H = 350;
    const m = { top: 14, right: 16, bottom: 34, left: 52 };
    const plotW = W - m.left - m.right;
    const plotH = H - m.top - m.bottom;
    const colW = plotW / COLS;
    const X = (i) => m.left + (i / COLS) * plotW;
    const Y = (wh) => m.top + (1 - wh / yMax) * plotH;

    const el = svg("svg", {
      class: "plot",
      viewBox: `0 0 ${W} ${H}`,
      preserveAspectRatio: "xMidYMid meet",
      role: "img",
    });

    el.appendChild(this._weekAxes(X, Y, axis, unitKwh, m, plotW, plotH, days, t));

    // --- stacked day bars ------------------------------------------------
    const barW = Math.max(1, colW * 0.6);
    for (let i = 0; i < COLS; i++) {
      const bx = X(i) + (colW - barW) / 2;
      let acc = 0;
      for (const mod of this._modules) {
        const v = (this._weekBars[mod.id] && this._weekBars[mod.id][i]) || 0;
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

    // --- forecast: one dashed segment per day (issued past / live today) --
    if (this._weekForecast) {
      // A horizontal dash at the day's forecast-total height, centred over its
      // column (same width as the bar). A day without an archived snapshot has
      // NO segment — an honest gap, mirroring the day view's missing note.
      const segHalf = colW * 0.3;
      for (let i = 0; i < COLS; i++) {
        const v = this._weekForecast[i];
        if (typeof v !== "number") continue;
        const y = Y(v);
        const xc = X(i) + colW / 2;
        el.appendChild(
          svg("line", {
            x1: xc - segHalf,
            y1: y,
            x2: xc + segHalf,
            y2: y,
            stroke: "var(--primary-text-color)",
            "stroke-width": "2",
            "stroke-dasharray": "5 4",
            opacity: "0.7",
          }),
        );
      }
    }

    // --- empty / loading note --------------------------------------------
    if (this._loadState !== "ok") {
      const note =
        this._loadState === "loading" ? t.loading : t.noStatsRange;
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

    // --- hover crosshair + floating readout panel (per day) --------------
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
      mode: "week",
      cols: COLS,
      days,
      modules: this._modules,
      bars: this._weekBars,
      forecast: this._weekForecast,
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

  /** Week axis frame: y gridlines + Wh/kWh ticks, x weekday + date per day. */
  _weekAxes(X, Y, axis, unitKwh, m, plotW, plotH, days, t) {
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

    // X labels: short weekday over the "d.M." date, centred under each column.
    const colW = plotW / 7;
    for (let i = 0; i < 7; i++) {
      const d = days[i];
      const cx = m.left + i * colW + colW / 2;
      const wd = svg("text", {
        x: cx,
        y: m.top + plotH + 14,
        fill: axisColor,
        "font-size": "10",
        "text-anchor": "middle",
      });
      wd.textContent = (t.weekdays && t.weekdays[d.getDay()]) || "";
      g.appendChild(wd);
      const dt = svg("text", {
        x: cx,
        y: m.top + plotH + 25,
        fill: axisColor,
        "font-size": "9",
        "text-anchor": "middle",
      });
      dt.textContent = shortDate(d);
      g.appendChild(dt);
    }

    return g;
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

  /** Pointer move over the plot → snap to the hovered column (hour or day). */
  _hoverMove(ev, ctx) {
    const rect = ctx.svgEl.getBoundingClientRect();
    if (!rect.width) return;
    const clientX =
      ev.touches && ev.touches.length ? ev.touches[0].clientX : ev.clientX;
    if (typeof clientX !== "number") return;
    const vbx = ((clientX - rect.left) / rect.width) * ctx.W;
    let i = Math.floor((vbx - ctx.m.left) / ctx.colW);
    if (i < 0) i = 0;
    if (i > ctx.cols - 1) i = ctx.cols - 1;
    this._drawHover(ctx, i);
  }

  /** Pointer left the plot → drop the crosshair + panel. */
  _hoverLeave(ctx) {
    const g = ctx.crosshair;
    while (g.firstChild) g.removeChild(g.firstChild);
  }

  /** Readout rows for column ``i``: title, per-module nonzero, total, forecast. */
  _readoutRows(ctx, i) {
    const rows = [];
    if (ctx.mode === "week") {
      const d = ctx.days[i];
      const wd = (ctx.t.weekdays && ctx.t.weekdays[d.getDay()]) || "";
      rows.push({ kind: "title", text: `${wd} ${shortDateYear(d)}` });
    } else {
      rows.push({ kind: "title", text: `${pad2(i)}:00–${pad2(i)}:59` });
    }
    let total = 0;
    for (const mod of ctx.modules) {
      const v = (ctx.bars[mod.id] && ctx.bars[mod.id][i]) || 0;
      total += v;
      if (v > 0.5) {
        rows.push({ kind: "mod", color: mod.color, name: mod.name, val: v });
      }
    }
    rows.push({ kind: "total", name: ctx.t.total, val: total });
    // Day view: the hourly line value. Week view: the day's forecast total —
    // null on a gap day (no archived snapshot), rendered as "—".
    if (ctx.forecast) {
      rows.push({ kind: "forecast", name: ctx.t.forecast, val: ctx.forecast[i] });
    }
    return rows;
  }

  /** (Re)build the crosshair column highlight + floating readout for column i. */
  _drawHover(ctx, i) {
    const g = ctx.crosshair;
    while (g.firstChild) g.removeChild(g.firstChild);
    const m = ctx.m;
    const xL = m.left + i * ctx.colW;
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
    const rows = this._readoutRows(ctx, i);

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
      val.textContent = typeof row.val === "number" ? fmtVal(row.val) : "—";
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
      "Stacked hourly/daily production per module + forecast line, with day and week navigation (Balcony Solar Forecast).",
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
