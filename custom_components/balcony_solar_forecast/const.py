"""Constants for the Balcony Solar Forecast integration.

Single source of truth for the domain, config-flow keys, defaults, storage
keys and the shipped operator reference site (8 modules on 4 Hoymiles
HMS-800W-2T micro-inverters). The reference site is a *default*, not a
hardcode: every plane, horizon table and inverter group is editable in the
config flow (SPEC §4, D-P9).

Azimuth convention here and everywhere INTERNAL: 0 = North, clockwise
(90 = East, 180 = South, 270 = West). Conversions to API conventions live
only at the boundaries (SPEC Anhang A).
"""

from __future__ import annotations

DOMAIN = "balcony_solar_forecast"

INTEGRATION_NAME = "Balcony Solar Forecast"
INTEGRATION_VERSION = "0.20.2"

# --- Update behaviour (SPEC §4: fetch 30 min, recompute 15 min) ---
FETCH_INTERVAL_SECONDS = 1800  # Open-Meteo pull cadence
RECOMPUTE_INTERVAL_SECONDS = 900  # engine re-run cadence (15-min slots)
SLOT_MINUTES = 15  # forecast resolution
# today / tomorrow / day-after (d2) — plus ONE buffer day: the fetch runs
# with timezone=UTC, so forecast_days counts UTC days from the current UTC
# date. In the local evening (UTC+2) the LOCAL day-after-tomorrow lies
# partly or fully beyond a 3-UTC-day window (observed live: d2 = 0.0 kWh
# at 01:00 local). One extra day always covers the local 3-day horizon.
FORECAST_DAYS = 4

# --- Data validity / degradation ladder (SPEC §7) ---
MAX_PAYLOAD_AGE_HOURS = 24  # last-good weather still trusted for fresh curve
MAX_PHYSICS_FALLBACK_AGE_HOURS = 72  # pure-physics curve from last weather image

# --- Open-Meteo endpoint (SPEC §4, one call) ---
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MODEL = "icon_seamless"
OPEN_METEO_MINUTELY_15 = (
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
)
OPEN_METEO_HOURLY = (
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "visibility",
    "snowfall",
    "snow_depth",
)

# --- Config-flow keys (top level entry) ---
CONF_NAME = "name"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_FETCH_INTERVAL = "fetch_interval_seconds"
CONF_RECOMPUTE_INTERVAL = "recompute_interval_seconds"
CONF_SITE = "site"  # single object holding the full editable site config

# --- Config-flow keys (inside the site object) ---
CONF_PLANES = "planes"
CONF_GROUPS = "groups"
# Site-level TOTAL-AC meter behind all inverters (the only whole-site AC that is
# actually measured); the AC calibration target in a later phase. Optional.
CONF_AC_ACTUAL_ENTITY = "ac_actual_entity"
# The site AC meter can be SIGN-INVERTED (the operator's meter reports the fed-in
# balcony-solar AC as a NEGATED value). When True the reading is negated ONCE at
# the read boundary so the measured sensor and the calibration reader are
# sign-correct. Optional; default False.
CONF_AC_ACTUAL_INVERT = "ac_actual_invert"
# Site-level ground albedo for the reflected-diffuse term (v0.20). Optional;
# absent => ALBEDO_DEFAULT. Matters disproportionately on steep balcony tilts
# (70-90 deg), where the ground-view factor (1-cos(tilt))/2 reaches 0.4-0.5:
# a dark courtyard/lawn (~0.1) vs the textbook 0.2 shifts the diffuse floor by
# 10-20 %. Snow days still override with ALBEDO_SNOW.
CONF_SITE_ALBEDO = "albedo"
SITE_ALBEDO_MIN = 0.05
SITE_ALBEDO_MAX = 0.9
# plane fields
CONF_PLANE_NAME = "name"
CONF_AZIMUTH = "azimuth_deg"  # 0=N clockwise
CONF_TILT = "tilt_deg"  # degrees from horizontal, 90 = vertical
CONF_WP = "wp"  # module STC peak power, watts
CONF_EFFICIENCY = "efficiency"  # system/DC efficiency, default 0.96
CONF_HORIZON = "horizon"  # list of horizon rows (see below)
CONF_ACTUAL_ENTITY = "actual_entity"  # HA entity id for measured DC power
CONF_SHADE_GROUP = "shade_group"  # optional: planes sharing this learn ONE shademap channel
CONF_ROSS_COEFF = "ross_coeff"  # optional: per-plane Ross cell-temp coefficient (mounting-dependent)
# horizon-row fields
CONF_HZ_AZIMUTH = "azimuth_deg"  # 0=N clockwise
CONF_HZ_ELEVATION = "elevation_deg"  # horizon-line elevation at this azimuth
CONF_HZ_TAU = "tau"  # transmittance 0..1 (static, or leafed default)
CONF_HZ_SEASONAL = "seasonal"  # bool: this row's tau ramps with foliage
CONF_HZ_TAU_LEAFED = "tau_leafed"  # summer transmittance when seasonal
CONF_HZ_TAU_BARE = "tau_bare"  # winter transmittance when seasonal
# inverter-group fields
CONF_GROUP_NAME = "name"
CONF_GROUP_PLANES = "plane_names"  # plane names feeding this AC clamp
CONF_GROUP_AC_LIMIT = "ac_limit_w"  # AC clamp, VA/W
CONF_GROUP_INVERTER_EFFICIENCY = "inverter_efficiency"  # optional: per-group DC->AC efficiency

# --- Physics constants (SPEC §4 physics musts) ---
ALBEDO_DEFAULT = 0.2
ALBEDO_SNOW = 0.5  # applied when snow_depth > SNOW_DEPTH_THRESHOLD_M
SNOW_DEPTH_THRESHOLD_M = 0.01
RB_CAP = 10.0  # geometric beam-ratio cap (low-sun explosion guard)
LOW_SUN_CUTOFF_DEG = 3.0  # below this elevation: circumsolar = 0
# ASHRAE incidence-angle modifier applied to beam+circumsolar in the ENGINE
# (not in the pure transposition, which stays pvlib-comparable for the golden
# vectors): f = 1 - IAM_B0 * (1/cos(theta) - 1), clamped [0, 1]. Glass
# reflection cuts the direct share 5-15% at AOI > 60 deg — on the 70-80 deg
# facade planes the sun spends much of the day there, and without the modifier
# the shademap absorbs the optics deficit as phantom shading.
IAM_B0 = 0.05
# Ross cell-temperature coefficient: Tcell = Tamb + ROSS_COEFF * POA. The value
# is a DEFAULT — the real coefficient is set by the mounting/back-ventilation,
# spanning ~0.02 (well-ventilated free-standing) to ~0.056 (facade-parallel,
# poorly ventilated) in the Ross/Skoplaki literature. Overridable per plane via
# PlaneConfig.ross_coeff for the operator's steep, wall-hugging facade modules.
ROSS_COEFF = 0.0342  # Tcell = Tamb + ROSS_COEFF * POA
TEMP_COEFF_PER_K = -0.0034  # power derate per K above 25 C (-0.34 %/K)
TEMP_REF_C = 25.0
DEFAULT_EFFICIENCY = 0.96
# DC->AC micro-inverter conversion efficiency (HMS-800W-2T-class CEC/EU weighted
# efficiency). DISTINCT from DEFAULT_EFFICIENCY above, which is the DC-side
# system loss folded into dc_power(): this one is the inverter's DC->AC stage,
# applied by electrical.clamp_groups_ac AFTER the DC model, per inverter group.
DEFAULT_INVERTER_EFFICIENCY = 0.965
INVERTER_EFFICIENCY_MIN = 0.80  # sane floor for a configured/loaded eta_inv
INVERTER_EFFICIENCY_MAX = 1.0   # a real converter never gains power
# --- Inverter DC->AC efficiency site calibration (AC-side Phase 3) ----------
# A single site-level LEARNED scalar eta_inv, calibrated against the site's
# TOTAL-AC meter (SiteConfig.ac_actual_entity) so the AC forecast tracks the
# real inverter conversion instead of the datasheet DEFAULT_INVERTER_EFFICIENCY.
# One scalar fits ALL groups: the operator has only a whole-site AC meter and
# the HMS-800W-2T inverters are identical. NEVER load-bearing — no AC meter /
# too few samples / an out-of-band ratio all fall back to the config/default
# eta, and the DC learning + scoreboard stay untouched.
INVERTER_CAL_MIN = 0.90  # physically-plausible micro-inverter operating floor;
INVERTER_CAL_MAX = 0.99  # a measured ratio outside [MIN, MAX] is REJECTED (it is
#                          not a plausible inverter eta — e.g. a meter that also
#                          sees house load or is net-metered)
INVERTER_CAL_EMA_ALPHA = 0.10  # steady-state EMA weight (adaptive warm-up folds
#                                the first 1/ALPHA samples as an exact mean)
INVERTER_CAL_MIN_LOAD_W = 100.0  # below this summed DC the inverter self-
#                                  consumption / MPPT start threshold distorts the
#                                  ratio — skip the sample
INVERTER_CAL_MIN_SAMPLES = 20  # distinct eligible hours before the learned eta is
#                                trusted (else the config/default eta is used)
# Clip-headroom gate for a calibration hour (AC-side Phase 3): the datasheet-
# derived AC (DEFAULT_INVERTER_EFFICIENCY * summed DC) must sit below this
# fraction of the summed group AC ceiling for the hour to count as UNCLIPPED — a
# clipped hour's AC is capped at the ceiling, so its measured-AC/DC ratio would
# understate eta. Gated on the INDEPENDENT DC side (not the measured AC) so a
# meter glitch cannot both pass the gate and corrupt the ratio.
INVERTER_CAL_CLIP_HEADROOM_FRAC = 0.90

# Seasonal foliage ramp (SPEC §13: cosine ramp over April / November).
# Day-of-year anchors for the leafed (summer) plateau; outside is bare.
FOLIAGE_LEAF_ON_DOY = 105  # ~mid-April: bare -> leafed ramp centre
FOLIAGE_LEAF_OFF_DOY = 315  # ~mid-November: leafed -> bare ramp centre
FOLIAGE_RAMP_DAYS = 30  # cosine half-ramp width around each anchor

# --- Storage (SPEC §4: one versioned Store, async_delay_save) ---
STORAGE_VERSION = 1  # Store envelope major (pin forever; migrate via inner)
STORAGE_KEY = f"{DOMAIN}.data"
STORAGE_DATA_VERSION = 1  # inner schema version
STORAGE_SAVE_DELAY_SECONDS = 300  # bundle writes, eMMC-friendly
# Minimum wall-clock between last-good-payload disk writes. The fetch cadence
# (1800 s) is far shorter than this, and async_delay_save re-arms on every
# call, so without a hard time-gate the payload would be rewritten every 30
# min (~48 writes/day). We hold the in-memory copy fresh and only let a
# payload change hit the disk at most every ~6 h; the nightly job and the
# unload/HA-stop flush guarantee eventual persistence (SPEC §4: <=3 bundled
# writes/day; a hard crash may lose a few hours of last-good cache).
PAYLOAD_MIN_SAVE_INTERVAL_SECONDS = 6 * 3600
STORE_KEY_LAST_PAYLOAD = "last_payload"  # last-good Open-Meteo payload
STORE_KEY_ISSUED_LOG = "forecast_issued_log"  # forecast-as-issued ring
STORE_KEY_ACTUALS_LOG = "daily_actuals_log"  # measured DC per module per day

# --- Services (SPEC §8) ---
SERVICE_GET_FORECAST = "get_forecast"

# --- Sensor / entity keys (SPEC §8) ---
SENSOR_ENERGY_TODAY = "energy_production_today"
SENSOR_ENERGY_TOMORROW = "energy_production_tomorrow"
SENSOR_ENERGY_D2 = "energy_production_d2"
SENSOR_POWER_NOW = "power_production_now"
# --- AC-side forecast Phase 2 (SPEC AC-side forecast) ----------------------
# The main energy / power / band sensors above now report the served AC
# (operator-facing standard); the model-internal DC view moves to these new
# DIAGNOSTIC sensors, reading the coordinator's unchanged DC data keys.
SENSOR_POWER_NOW_DC = "power_production_now_dc"
SENSOR_ENERGY_TODAY_DC = "energy_production_today_dc"
SENSOR_ENERGY_TOMORROW_DC = "energy_production_tomorrow_dc"
SENSOR_ENERGY_D2_DC = "energy_production_d2_dc"
# Measured site-total DC power: the live sum of the planes' actual_entity
# sensors (ground truth), an integration-owned sensor independent of the
# forecast coordinator (see sensor.MeasuredDcTotalSensor).
SENSOR_MEASURED_DC_TOTAL = "measured_dc_power_total"
# Measured site-total AC power: the live reading of the site's single AC meter
# (SiteConfig.ac_actual_entity), the AC ground-truth partner of the measured DC
# total (see sensor.MeasuredAcPowerSensor); created only when configured.
SENSOR_MEASURED_AC_POWER = "measured_ac_power"
BINARY_SENSOR_DEGRADED = "degraded"

# --- Shade-profile visualisation entities (sun path vs learned shade) -------
# A per-date sun-path + learned-shade diagram (SPEC §15): the sensor exposes the
# curve arrays as attributes; a `select` picks the module/plane and a `date`
# picks the day to visualise. See core/shadeprofile.py + docs/DASHBOARD.md.
SENSOR_SHADE_PROFILE = "shade_profile"
SELECT_SHADE_PROFILE_MODULE = "shade_profile_module"
DATE_SHADE_PROFILE_DATE = "shade_profile_date"

# --- Attribute names (curve dicts; excluded from recorder) ---
ATTR_WATTS = "watts"  # {iso_utc: W} 15-min instantaneous power
ATTR_WH_PERIOD = "wh_period"  # {iso_utc: Wh} energy per 15-min slot

# --- Degradation status values (binary_sensor "degraded" attribute) ---
STATUS_FRESH = "fresh"
STATUS_CACHED = "cached"  # last-good payload, still within age limit
STATUS_PHYSICS_FALLBACK = "physics_fallback"  # pure physics from old weather
STATUS_UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Reference site (SPEC §2, §4, §13): 8 modules, 4 inverter groups.
# Azimuth 0=N clockwise; tilt from horizontal. Horizon rows are
# (azimuth_deg, elevation_deg, tau) with optional seasonal foliage fields.
# ---------------------------------------------------------------------------

# Far-field horizon shared by all planes (SPEC §13.4): the eastern slope.
# az 60..100 -> 13 deg (tau 0), az 100..150 -> 16 deg (tau 0), "sonst
# PVGIS-Profil" (i.e. open elsewhere until real PVGIS rows are imported).
#
# The horizon interpolator treats the table as a CLOSED 360-degree profile
# (it wraps last -> first row), so a sparse sector table needs explicit
# *terminator* rows bracketing the obstructed sector; otherwise the opaque
# slope would be smeared over the whole circle (west/north/south included)
# and the diffuse sky-view factor computed against a fabricated opaque ring.
# We bracket the 60..150 slope with open rows (elev 0, tau 1) just outside
# it so every other azimuth falls back to an unobstructed, fully transparent
# horizon. The 100.0/100.01 step idiom keeps the two elevations distinct.
_OPEN_ROW_LOW = {CONF_HZ_AZIMUTH: 59.99, CONF_HZ_ELEVATION: 0.0, CONF_HZ_TAU: 1.0}
_OPEN_ROW_HIGH = {CONF_HZ_AZIMUTH: 150.01, CONF_HZ_ELEVATION: 0.0, CONF_HZ_TAU: 1.0}
_FARFIELD_SLOPE = [
    _OPEN_ROW_LOW,
    {CONF_HZ_AZIMUTH: 60.0, CONF_HZ_ELEVATION: 13.0, CONF_HZ_TAU: 0.0},
    {CONF_HZ_AZIMUTH: 100.0, CONF_HZ_ELEVATION: 13.0, CONF_HZ_TAU: 0.0},
    {CONF_HZ_AZIMUTH: 100.01, CONF_HZ_ELEVATION: 16.0, CONF_HZ_TAU: 0.0},
    {CONF_HZ_AZIMUTH: 150.0, CONF_HZ_ELEVATION: 16.0, CONF_HZ_TAU: 0.0},
    _OPEN_ROW_HIGH,
]


def _seasonal_row(az: float, elev: float) -> dict:
    """Foliage-modulated tree row: tau ramps 0.8 bare / 0.45 leafed."""
    return {
        CONF_HZ_AZIMUTH: az,
        CONF_HZ_ELEVATION: elev,
        CONF_HZ_TAU: 0.45,  # leafed default (worst case) for non-seasonal use
        CONF_HZ_SEASONAL: True,
        CONF_HZ_TAU_LEAFED: 0.45,
        CONF_HZ_TAU_BARE: 0.8,
    }


def _wall_row(az: float) -> dict:
    """Hard building wall: full beam occlusion above the edge."""
    return {CONF_HZ_AZIMUTH: az, CONF_HZ_ELEVATION: 90.0, CONF_HZ_TAU: 0.0}


# South planes (right side, 205 deg): tree sector + building wall.
# M4 = lower balcony (tree elev 40, wall from az 212), M8 = upper (tree 30).
#
# The sorted breakpoints the interpolator sees must be a clean sequence of
# non-overlapping sectors (SPEC §13.4), NOT the far-field rows with the tree
# and wall appended (that leaves the far-field 150-deg row *inside* the tree
# sector 135-175, and no boundary between the tree top (175, el 40/30) and
# the wall (212, el 90) — so the line would dip to 16 deg mid-tree and then
# ramp 40->90 straight across the plane's own prime-output azimuths 175-212,
# the operator's measured June peak hours). We therefore build it explicitly:
#   * far-field east slope 60..100..134.99 (its high open terminator dropped);
#   * seasonal tree plateau 135..175 (tau 0.45 leafed / 0.8 bare);
#   * open terminators at 175.01 and 211.99 so the tree does NOT bleed into
#     the plane's ungated prime sector (~175..212, azimuth 205 lives here);
#   * the hard building wall from 212 (el 90, tau 0), wrapping to 360.
def _south_horizon(tree_elev: float) -> list[dict]:
    # Far-field east slope only up to just before the tree sector; the
    # shared _OPEN_ROW_HIGH (az 150.01) is intentionally NOT reused here as
    # it would fall inside the tree sector once the wall closes the circle.
    rows = [
        _OPEN_ROW_LOW,
        {CONF_HZ_AZIMUTH: 60.0, CONF_HZ_ELEVATION: 13.0, CONF_HZ_TAU: 0.0},
        {CONF_HZ_AZIMUTH: 100.0, CONF_HZ_ELEVATION: 13.0, CONF_HZ_TAU: 0.0},
        {CONF_HZ_AZIMUTH: 100.01, CONF_HZ_ELEVATION: 16.0, CONF_HZ_TAU: 0.0},
        # Terminator ending the far-field slope just before the tree sector.
        {CONF_HZ_AZIMUTH: 134.99, CONF_HZ_ELEVATION: 16.0, CONF_HZ_TAU: 0.0},
    ]
    # Seasonal tree plateau across az 135..175.
    rows.append(_seasonal_row(135.0, tree_elev))
    rows.append(_seasonal_row(175.0, tree_elev))
    # Open terminators so the tree plateau does not ramp toward the wall
    # across the plane's own prime-output azimuths (~175..212).
    rows.append({CONF_HZ_AZIMUTH: 175.01, CONF_HZ_ELEVATION: 0.0, CONF_HZ_TAU: 1.0})
    rows.append({CONF_HZ_AZIMUTH: 211.99, CONF_HZ_ELEVATION: 0.0, CONF_HZ_TAU: 1.0})
    # Hard building wall from az 212, wrapping to the north across 360.
    rows.append(_wall_row(212.0))
    rows.append(_wall_row(360.0))
    return rows


# Front / North planes: far-field slope only (their beam-end is geometric).
def _default_horizon() -> list[dict]:
    return list(_FARFIELD_SLOPE)


def _plane(name, az, tilt, wp, horizon, actual_entity):
    return {
        CONF_PLANE_NAME: name,
        CONF_AZIMUTH: float(az),
        CONF_TILT: float(tilt),
        CONF_WP: float(wp),
        CONF_EFFICIENCY: DEFAULT_EFFICIENCY,
        CONF_HORIZON: horizon,
        CONF_ACTUAL_ENTITY: actual_entity,
    }


DEFAULT_SITE = {
    CONF_LATITUDE: 48.547853,
    CONF_LONGITUDE: 12.187272,
    CONF_PLANES: [
        # --- lower balcony (70 deg tilt) ---
        _plane("M1", 25.0, 70.0, 370, _default_horizon(),
               "sensor.inverter_port_1_dc_power"),
        _plane("M2", 115.0, 70.0, 370, _default_horizon(),
               "sensor.inverter_port_2_dc_power"),
        _plane("M3", 115.0, 70.0, 370, _default_horizon(),
               "sensor.inverter_port_1_dc_power_2"),
        _plane("M4", 205.0, 70.0, 430, _south_horizon(40.0),
               "sensor.inverter_port_2_dc_power_2"),
        # --- upper balcony (80 deg tilt) ---
        _plane("M5", 25.0, 80.0, 430, _default_horizon(),
               "sensor.inverter_port_1_dc_power_3"),
        _plane("M6", 115.0, 80.0, 430, _default_horizon(),
               "sensor.inverter_port_2_dc_power_3"),
        _plane("M7", 115.0, 80.0, 430, _default_horizon(),
               "sensor.inverter_port_1_dc_power_4"),
        _plane("M8", 205.0, 80.0, 430, _south_horizon(30.0),
               "sensor.inverter_port_2_dc_power_4"),
    ],
    CONF_GROUPS: [
        {CONF_GROUP_NAME: "WR1", CONF_GROUP_PLANES: ["M1", "M2"],
         CONF_GROUP_AC_LIMIT: 800.0},
        {CONF_GROUP_NAME: "WR2", CONF_GROUP_PLANES: ["M3", "M4"],
         CONF_GROUP_AC_LIMIT: 800.0},
        {CONF_GROUP_NAME: "WR3", CONF_GROUP_PLANES: ["M5", "M6"],
         CONF_GROUP_AC_LIMIT: 800.0},
        {CONF_GROUP_NAME: "WR4", CONF_GROUP_PLANES: ["M7", "M8"],
         CONF_GROUP_AC_LIMIT: 800.0},
    ],
}


# ===========================================================================
# LEARNING CONTRACT (v0.2.0 + v0.3.0 — SPEC §5, §6, §7, §9, §13)
# ---------------------------------------------------------------------------
# Two learning layers, both numpy-free, both clamped/gated/disable-able and
# never silently degrading (SPEC §5 Schutzmechanismen). The FAST learner
# (core/bias.py) has an intraday clear-sky-index scalar + a nightly day-ahead
# RLS bias per (cloud class x day part). The SLOW learner (core/shademap.py)
# is a per-channel, per-(sun-az x sun-el x half-year) EMA of beam-referenced
# transmittance that REPLACES the static horizon tau of the bin.
#
# Every key below is NEW. No existing key above is touched. All owners import
# their tunables from here (single source of truth); no magic numbers in the
# learner modules.
# ===========================================================================

# --- Kill switches / options-flow keys (SPEC §5: per-layer disable) --------
# Both learners default ON (operator decision 2026-07-06: build v0.3 early).
CONF_FAST_LEARNER_ENABLED = "fast_learner_enabled"
CONF_SLOW_LEARNER_ENABLED = "slow_learner_enabled"
CONF_DAY_AHEAD_BIAS_ENABLED = "day_ahead_bias_enabled"
DEFAULT_FAST_LEARNER_ENABLED = True
DEFAULT_SLOW_LEARNER_ENABLED = True
DEFAULT_DAY_AHEAD_BIAS_ENABLED = True

# --- FAST learner: intraday clear-sky-index scalar (SPEC §5) ---------------
# s = exponentially decayed (tau ~ 90 min) ratio measured/forecast site energy
# over a trailing window, computed in CLEAR-SKY-INDEX space, applied to the
# next ~6 h with linear decay toward 1.0, clamped. Re-init to 1.0 on restart;
# NEVER persisted.
INTRADAY_TAU_MINUTES = 90.0            # EMA decay time constant
INTRADAY_TRAILING_WINDOW_MINUTES = 240.0   # 2-4 h look-back for the ratio
INTRADAY_MIN_TRAILING_MINUTES = 120.0      # need >=2 h of samples before acting
INTRADAY_APPLY_HORIZON_MINUTES = 360.0     # forward-apply window (~6 h)
INTRADAY_DECAY_TO_ONE = True               # linear decay toward 1.0 over horizon
INTRADAY_SCALAR_MIN = 0.25
INTRADAY_SCALAR_MAX = 2.5
INTRADAY_NEUTRAL = 1.0
# Only accumulate the intraday ratio where the modeled site energy is
# non-trivial (avoid divide-by-near-zero at dawn/dusk / deep shade).
INTRADAY_MIN_MODELED_WH = 5.0

# --- FAST learner: day-ahead RLS bias per (cloud class x day part) ----------
# One scalar recursive-least-squares state per cell; trained nightly from the
# issued-vs-actuals rings; clamped.
DAY_AHEAD_BIAS_MIN = 0.5
DAY_AHEAD_BIAS_MAX = 1.5
DAY_AHEAD_BIAS_NEUTRAL = 1.0
RLS_FORGETTING_FACTOR = 0.98    # lambda: <1 discounts old days
RLS_INIT_COVARIANCE = 1000.0    # P0: large => fast initial adaptation
RLS_MIN_SAMPLES = 3             # cells with fewer trained days stay neutral

# Cloud classes (SPEC §5/§6). "fog" = forecast visibility < FOG_VISIBILITY_M
# OR (cloud_cover_low > FOG_CLOUD_LOW_PCT AND month in FOG_MONTHS).
CLOUD_CLASS_CLEAR = "clear"
CLOUD_CLASS_MIXED = "mixed"
CLOUD_CLASS_OVERCAST = "overcast"
CLOUD_CLASS_FOG = "fog"
CLOUD_CLASSES = (
    CLOUD_CLASS_CLEAR,
    CLOUD_CLASS_MIXED,
    CLOUD_CLASS_OVERCAST,
    CLOUD_CLASS_FOG,
)
# Total-cloud-cover thresholds (%) separating clear / mixed / overcast when
# the fog test does not fire. Mean of the three cloud layers.
CLOUD_CLEAR_MAX_PCT = 25.0
CLOUD_OVERCAST_MIN_PCT = 75.0
# Fog-class parameters.
FOG_VISIBILITY_M = 1000.0
FOG_CLOUD_LOW_PCT = 85.0
FOG_MONTHS = (10, 11, 12, 1, 2)   # Oct-Feb

# Day parts (SPEC §5). Boundaries in local solar/clock hours (coordinator maps
# a slot's local hour to a part). Midday brackets solar noon.
DAY_PART_MORNING = "morning"
DAY_PART_MIDDAY = "midday"
DAY_PART_AFTERNOON = "afternoon"
DAY_PARTS = (DAY_PART_MORNING, DAY_PART_MIDDAY, DAY_PART_AFTERNOON)
DAY_PART_MORNING_END_HOUR = 10     # [dawn, 10:00) local
DAY_PART_AFTERNOON_START_HOUR = 14  # [14:00, dusk) local; [10,14) = midday
# The day-ahead bias is LEARNED per day part but APPLIED continuously: within
# this half-width (minutes) either side of a day-part boundary the two adjacent
# parts' factors are linearly blended, so the served correction never steps
# across the boundary (a bucketed step has no physical basis — the forecast
# shape comes from weather x physics x shading, which is smooth). The learned
# cells are the anchors; the application interpolates between them.
DAY_PART_BLEND_HALFWIDTH_MIN = 45

# SOLAR day-part boundaries (v0.19): the coordinator bins each slot by its
# APPARENT SOLAR time (solpos.hours_from_solar_noon), NOT the wall clock, so a
# boundary tracks the sun instead of sitting on a fixed local hour like 10:00.
# This removes the DST/seasonal drift of the clock boundaries above (a cell
# learned at "10:00" in summer used to land ~1 h off in solar terms in winter)
# and anchors morning/midday/afternoon symmetrically around solar noon:
#   midday   = |hours_from_solar_noon| <  MIDDAY_SOLAR_HALFWIDTH_H
#   morning  =  hours_from_solar_noon  <= -MIDDAY_SOLAR_HALFWIDTH_H
#   afternoon=  hours_from_solar_noon  >= +MIDDAY_SOLAR_HALFWIDTH_H
# 2.0 h reproduces the old summer intent (~10:00 / ~14:00 CEST) without the
# clock dependence. The blend half-width is expressed in SOLAR hours so the
# smooth transition also tracks the sun. Cell KEYS are unchanged
# (cloud_class|day_part), so no persistence migration and no quantile ripple.
MIDDAY_SOLAR_HALFWIDTH_H = 2.0
DAY_PART_SOLAR_BLEND_HALFWIDTH_H = 0.75  # 45 min, in solar hours

# --- SLOW learner: shademap (SPEC §5, §13) ---------------------------------
# Per measurement channel (module/plane name), per bin
# (sun azimuth SHADEMAP_AZ_BIN_DEG x elevation SHADEMAP_EL_BIN_DEG x half-year
# before/after summer solstice), an EMA of beam-referenced transmittance
# T = (P_measured - P_diffuse_modeled) / P_beam_modeled. Learned map REPLACES
# the static horizon tau of the bin. Applied to beam+circumsolar only.
SHADEMAP_EMA_ALPHA = 0.15
SHADEMAP_AZ_BIN_DEG = 5.0          # sun-azimuth bin width (0=N internal)
SHADEMAP_EL_BIN_DEG = 2.5          # sun-elevation bin width
SHADEMAP_TAU_MIN = 0.0             # full occlusion representable (building wall)
SHADEMAP_TAU_MAX = 1.1
# Cold-start shrinkage toward the static horizon prior: w = n / (n + K).
SHADEMAP_SHRINKAGE_K = 20.0
# Half-year key: True == "after summer solstice" (solstice .. next solstice).
# Northern-hemisphere summer solstice ~ Jun 21 (doy 172). A sample's half is
# derived from its day-of-year relative to this anchor (SPEC §5: April laublos
# vs. August belaubt must not alias).
SUMMER_SOLSTICE_DOY = 172

# Quasi-clear sample gate (SPEC §5): elevation-dependent k_c band, neighbour
# slot stability, and a minimum modeled beam share of the plane's Wp.
# k_c must fall inside [lo, hi]; the band tightens with elevation because
# Haurwitz is crude at low sun (relax the LOW bound at low elevation).
SHADEMAP_KC_LO_HIGH_SUN = 0.85     # k_c lower bound at/above the pivot elevation
SHADEMAP_KC_LO_LOW_SUN = 0.65      # relaxed lower bound at very low sun
SHADEMAP_KC_HI = 1.35              # upper bound (thin-cloud enhancement guard)
SHADEMAP_KC_PIVOT_ELEV_DEG = 20.0  # elevation where the lo bound reaches HIGH_SUN
SHADEMAP_NEIGHBOUR_STABILITY = 0.15  # max relative k_c change vs. adjacent slot
SHADEMAP_MIN_BEAM_SHARE = 0.05     # modeled beam POA power must exceed 5% Wp

# --- Shade-group similarity (suggest_shade_groups service, SPEC §5) ---------
# The suggest_shade_groups service compares two planes' per-channel shademaps
# bin-wise (n-weighted mean |tau_a - tau_b| over the bins both channels visited)
# and proposes a data-driven grouping via complete-linkage agglomeration.
# Two channels of the SAME occlusion, both EMA-smoothed (SHADEMAP_EMA_ALPHA),
# rarely diverge past ~0.05 on their common bins, so 0.06 is a safe "same shade"
# threshold; a pair above it is flagged as differently shaded.
SHADE_SIM_MAX_MEAN_DIFF = 0.06
# Fewer than this many shared sun positions (bins) is anecdote, not evidence: a
# pair below it is "insufficient" and never merged, however close its tau looks.
SHADE_SIM_MIN_COMMON_BINS = 30

# --- GUARDS (SPEC §5 all mandatory) ----------------------------------------
# Label gates (trainer): frozen sensor detection.
LABEL_FROZEN_STALE_SECONDS = 3 * 3600   # unchanged value + last_updated older => missing
LABEL_MONOTONIC_TOLERANCE_WH = 1.0      # energy must be non-decreasing within tol
# Nightly LTS frozen-channel gate: a channel whose hourly means repeat the SAME
# non-zero value for at least this many consecutive daylight hours is a frozen
# Hoymiles/DTU sensor holding a value (the operator's known failure mode) — the
# whole day is discarded for both learners (SPEC §5 channel dropout).
LABEL_FROZEN_MIN_REPEATS = 4
# Channel dropout: if a channel is missing/frozen for the day, discard WHOLE
# day for BOTH learners (SPEC §5).
# Day-completeness gate: a recorder/LTS gap (HA restart or recorder outage
# mid-day) yields a partial-hour sum that must NOT be recorded as the day's
# ground truth (it would score every forecast against a phantom-low measured
# energy and feed the drift/collapse detectors a fake loss). A day is accepted
# only when the best-covered module has hourly-mean rows for at least this
# fraction of the day's DAYLIGHT hours (sun elevation > 0); below it the whole
# day is discarded (empty), so a later catch-up can fill it once LTS is complete.
DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE = 0.75

# Drift monitor: rolling daylight MAE corrected vs pure physics.
DRIFT_WINDOW_DAYS = 7
DRIFT_LOSS_STREAK_DAYS = 7        # consecutive losing days => auto-disable layer
DRIFT_ROLLBACK_SNAPSHOTS = 3     # legacy alias; the live ring depth is LEARNER_SNAPSHOT_RING (must exceed the loss streak)
# A "losing" day = corrected daylight MAE strictly worse than physics MAE by
# more than this relative margin AND by more than DRIFT_LOSS_MIN_ABS_WH in
# absolute Wh. The absolute floor stops a well-trained/clear day (where raw and
# corrected daily totals differ by only a few Wh) from counting as a "loss" on
# rounding-scale noise — seven such coin-flips would otherwise auto-disable and
# roll back a layer over statistically meaningless deltas.
DRIFT_LOSS_MARGIN = 0.02
DRIFT_LOSS_MIN_ABS_WH = 50.0

# Collapse detector (SPEC §5): all channels ~0 while forecast high => snow /
# total dropout => freeze BOTH learners for the day; only the clamped intraday
# scalar reacts.
COLLAPSE_MEASURED_MAX_FRAC = 0.05   # measured site energy < 5% of forecast
COLLAPSE_FORECAST_MIN_WH = 500.0    # ...only when the forecast day is non-trivial

# --- Storage schema v2 (SPEC §5/§9 attribution; §6 backfill) ----------------
# Bump the INNER schema; the outer HA Store envelope (STORAGE_VERSION) stays 1.
# store.py migrates v1 -> v2 in-place (v1 rings preserved, learner state added).
STORAGE_DATA_VERSION_V2 = 2
# New store keys (v2). Existing v1 keys (STORE_KEY_LAST_PAYLOAD /
# _ISSUED_LOG / _ACTUALS_LOG) are unchanged and carried through migration.
STORE_KEY_BIAS_STATE = "bias_state"            # BiasState (day-ahead RLS only; intraday NEVER persisted)
STORE_KEY_SHADEMAP_STATE = "shademap_state"    # ShademapState (per-channel bins)
STORE_KEY_LEARNER_SNAPSHOTS = "learner_snapshots"  # rollback ring: list[LearnerSnapshot]
STORE_KEY_DRIFT_STATE = "drift_state"          # DriftState (rolling MAE + streaks + disable flags)
STORE_KEY_HOURLY_ACTUALS = "hourly_actuals_log"  # {iso_date: {channel: {iso_hour: wh}}}
# Per-day training idempotence markers (verify finding 2026-07-06): the
# restart-time catch-up re-sweeps the last processed day, and the RLS /
# drift-streak updates are NOT internally idempotent — without a persisted
# marker every HA restart would re-train the same day (double-counted RLS
# samples, double-incremented loss streaks -> spurious auto-disable).
STORE_KEY_TRAINED_DAYS = "trained_days"        # sorted list[iso_date]
TRAINED_DAYS_RING = 120
# Rollback ring depth. Must exceed DRIFT_LOSS_STREAK_DAYS so at least one
# pre-streak snapshot survives when auto-disable fires (a ring == the streak
# length would only ever hold poisoned mid-streak states — the exact failure
# the rollback ring must avoid).
LEARNER_SNAPSHOT_RING = 10
# Per-channel hourly actuals ring is far heavier (per-hour, per-module) than
# the daily rings; keep a short window so the eMMC-friendly store stays small.
HOURLY_ACTUALS_RING_DAYS = 14
# Nightly catch-up: on start (and after the first refresh) run the idempotent
# nightly job logic for any missed local day, bounded to this many days back.
NIGHTLY_CATCHUP_MAX_DAYS = 3
# Measured-side quasi-clear gate: a candidate training day must have measured
# site energy at least this fraction of the modeled forecast, otherwise the
# forecast wrongly called the day clear and the sample is pure weather error
# (would darken the geometric shademap). SPEC §5 label gate.
SHADEMAP_MEASURED_CLEAR_MIN_FRAC = 0.8

# --- New services (SPEC §5 diagnose, §6 backfill) --------------------------
SERVICE_IMPORT_BOOTSTRAP = "import_bootstrap"   # ingest scripts/backfill.py JSON
SERVICE_DUMP_SHADEMAP = "dump_shademap"         # polar-table diagnostic export
SERVICE_ROLLBACK_LEARNERS = "rollback_learners"  # restore learner state from the ring
SERVICE_INSTALL_DASHBOARD = "install_dashboard"  # write the observability dashboard
SERVICE_SUGGEST_SHADE_GROUPS = "suggest_shade_groups"  # data-driven shade-group suggestion
SERVICE_GET_SHADE_PROFILE = "get_shade_profile"  # read-only shade profile for a module/date (card compare)
SERVICE_GET_ISSUED_FORECAST = "get_issued_forecast"  # read-only issued day-ahead curve for a past date (card)
SERVICE_RESET_DAY_AHEAD_BIAS = "reset_day_ahead_bias"  # clear the day-ahead RLS bias cells (retrain from scratch)

# --- Bootstrap JSON schema (SPEC §6; scripts/backfill.py <-> store) ---------
# The import service validates + clamps and REJECTS unknown schema versions.
BOOTSTRAP_SCHEMA_VERSION = 1
BOOTSTRAP_KEY_SCHEMA = "schema_version"
BOOTSTRAP_KEY_GENERATED_AT = "generated_at"     # iso utc
BOOTSTRAP_KEY_SITE_SIGNATURE = "site_signature" # lat/lon+plane-name digest sanity check
BOOTSTRAP_KEY_BIAS = "bias_state"               # day-ahead RLS cells
BOOTSTRAP_KEY_SHADEMAP = "shademap_state"       # per-channel bins
# Backfill n-credit cap: hourly-smeared backfilled bins are less trustworthy,
# so their initial EMA sample count is capped so live 15-min data overrides
# quickly (SPEC §6).
BOOTSTRAP_MAX_BIN_N = 5

# --- Attribution / diagnostics (operator decision 2026-07-06, SPEC §9) ------
# The engine computes BOTH curves each cycle; the nightly issued snapshot v2
# stores hourly values of both plus per-plane modeled beam/diffuse/ghi/kc so
# the shademap can be trained from hourly LTS. Diagnostics expose daily MAE of
# raw vs corrected vs baseline.
CORRECTION_SOURCE_NONE = "none"          # raw physics served (learner off/frozen)
CORRECTION_SOURCE_INTRADAY = "intraday"  # intraday scalar applied
CORRECTION_SOURCE_SHADEMAP = "shademap"  # shademap applied
CORRECTION_SOURCE_BOTH = "both"

# Coordinator <-> platform contract additions (self.data keys, v0.2/v0.3):
DATA_KEY_RAW_HOURLY_WH = "raw_hourly_wh"          # {iso_hour: Wh} pure physics
DATA_KEY_CORRECTED_HOURLY_WH = "corrected_hourly_wh"  # {iso_hour: Wh} served curve
DATA_KEY_INTRADAY_SCALAR = "intraday_scalar"      # current applied scalar
DATA_KEY_LEARNER_STATUS = "learner_status"        # dict: enabled/frozen/disabled per layer
DATA_KEY_BIAS_CELLS = "bias_cells"                # dict: {"class|part": {theta, n, applied}} day-ahead RLS cells
DATA_KEY_DRIFT_MAE = "drift_mae"                  # dict: {raw, corrected, baseline, +slow when attributed} rolling MAE
DATA_KEY_CORRECTION_SOURCE = "correction_source"  # one of CORRECTION_SOURCE_*

# --- New diagnostic entities (SPEC §8) -------------------------------------
SENSOR_INTRADAY_SCALAR = "intraday_scalar"
SENSOR_DRIFT_MAE_CORRECTED = "drift_mae_corrected"
BINARY_SENSOR_FAST_LEARNER = "fast_learner_active"
BINARY_SENSOR_SLOW_LEARNER = "slow_learner_active"

# --- Per-layer learner status strings (SPEC §5) ----------------------------
# The coordinator writes exactly these into DATA_KEY_LEARNER_STATUS[<layer>]
# ("fast" / "slow" / "day_ahead"); the LearnerStatusSensor / LearnerActiveSensor
# read them back. sensor.py re-exports these names for its own display code.
LEARNER_LAYER_FAST = "fast"
LEARNER_LAYER_SLOW = "slow"
LEARNER_LAYER_DAY_AHEAD = "day_ahead"
LEARNER_STATUS_ACTIVE = "active"                 # enabled and shaping the curve
LEARNER_STATUS_OFF = "off"                        # user kill switch off
LEARNER_STATUS_DISABLED_BY_DRIFT = "disabled_by_drift"  # drift monitor auto-off
LEARNER_STATUS_FROZEN = "frozen"                  # collapse detector froze it today
# Enabled but holds NO learned state yet (e.g. right after reset_day_ahead_bias
# or on a fresh install before the first nightly training): the layer applies
# NOTHING to the served curve. Reporting "active" here misled the operator into
# thinking a correction was in effect (v0.19.2 status honesty).
LEARNER_STATUS_COLD_START = "cold_start"
LEARNER_STATUS_VALUES = (
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_OFF,
    LEARNER_STATUS_DISABLED_BY_DRIFT,
    LEARNER_STATUS_FROZEN,
    LEARNER_STATUS_COLD_START,
)

# --- Repair issue ids (SPEC §5/§7) -----------------------------------------
ISSUE_FAST_LEARNER_DISABLED = "fast_learner_auto_disabled"
ISSUE_SLOW_LEARNER_DISABLED = "slow_learner_auto_disabled"


# ===========================================================================
# v0.4 CONTRACT: SKILL SCOREBOARD + QUANTILES P10/P50/P90 (SPEC §6, §9, §10, §14)
# ---------------------------------------------------------------------------
# D-P11 (operator 2026-07-06): build the skill scoreboard, the P10/P50/P90
# quantile bands and the observability dashboard; DEFER the battery_manager
# cutover until the scoreboard confirms the kill-gate. Everything below is NEW;
# no existing key above is touched. Owners import their tunables from here
# (single source of truth); no magic numbers in the scoreboard / quantile
# modules. Runtime stays stdlib-only; the store schema bumps v2 -> v3 ADDITIVELY
# (STORAGE_VERSION envelope pinned at 1, inner schema only).
# ===========================================================================

# --- SKILL SCOREBOARD (SPEC §9/§10 — the kill-gate) ------------------------
# Nightly, per yesterday: compute the daily-kWh error of (a) the ENGINE forecast
# AS ISSUED for yesterday (from the issued ring — NEVER recomputed with today's
# learned state), (b) each configured external COMPARISON forecast AS IT STOOD
# during yesterday (read from that entity's recorder history for yesterday —
# NEVER today's value), all against (c) the MEASURED site energy for yesterday
# (sum of the per-module actuals in the actuals ring). Also the engine's hourly
# MAE. STRATIFIED by yesterday's dominant weather class (the coordinator already
# classifies clear/mixed/overcast/fog — reuse, do not reinvent). A rolling
# window (default SCOREBOARD_WINDOW_DAYS) feeds the kill-gate verdict.
CONF_SCOREBOARD_ENABLED = "scoreboard_enabled"
DEFAULT_SCOREBOARD_ENABLED = True
CONF_SCOREBOARD_WINDOW_DAYS = "scoreboard_window_days"
DEFAULT_SCOREBOARD_WINDOW_DAYS = 14  # rolling window length (SPEC §9 kill-gate)
# The kill-gate passes when the engine is at least this fraction better than the
# best baseline on daily-kWh MAE over a FULL window (SPEC §9: >=10% under the
# 8-entry baseline is the primary Phase-1 gate, B9-weighted).
CONF_SCOREBOARD_GATE_MARGIN = "scoreboard_gate_margin"
DEFAULT_SCOREBOARD_GATE_MARGIN = 0.10
# Minimum scored days before the gate can pass at all (a partial window can
# never assert the kill-gate; SPEC §9 "over a full window").
SCOREBOARD_MIN_WINDOW_DAYS = 1
# Minimum number of PAIRED days (days on which BOTH the engine and the
# candidate comparison were scored) before that comparison is eligible to set
# the best-baseline bar for the gate. A comparison scored on a single lucky day
# must not decide the whole verdict; the gate is a matched-pair comparison over
# the days both sides cover (fixes non-paired evaluation, SPEC §9).
SCOREBOARD_MIN_PAIRED_DAYS = 1
# Staleness bound (local days): the newest scored day must be within this many
# days of "today" for the gate to assert at all, else the verdict is suspended
# (None) — a ring whose scoring stopped weeks ago must not keep publishing a
# live-looking pass/fail. The coordinator passes the current local date in.
SCOREBOARD_MAX_STALENESS_DAYS = 3

# --- Comparison forecast sensors (GENERIC + CONFIGURABLE; ship EMPTY) -------
# CONF_COMPARISON_SENSORS is an editable list of objects, each:
#   {CONF_COMPARISON_NAME: str, CONF_COMPARISON_DAILY_ENTITY: entity_id}
# ``daily_entity`` is an HA sensor whose STATE is that comparison's daily-kWh
# forecast for today (same pattern as our own energy_production_today). The
# scoreboard reads its RECORDER HISTORY for yesterday (the value AS IT STOOD
# during yesterday — no leakage). Ships EMPTY by default: the operator's two
# comparisons are DOCUMENTED (docs/DASHBOARD.md + config example), never
# hardcoded in the runtime defaults (D-P9 generic-not-hardcoded).
#
#   Documented example (operator's live site — see docs/DASHBOARD.md):
#     - name "8-Entry Baseline" -> sensor.pv_prognose_heute_alle_module
#     - name "Alt 1600W"        -> sensor.energy_production_today_4
CONF_COMPARISON_SENSORS = "comparison_sensors"
CONF_COMPARISON_NAME = "name"
CONF_COMPARISON_DAILY_ENTITY = "daily_entity"
DEFAULT_COMPARISON_SENSORS: list[dict] = []  # EMPTY by default (D-P9)
# The comparison entity's daily-kWh value for yesterday is read from recorder
# history. We take the LAST recorded state on yesterday's LOCAL calendar day
# (the settled end-of-day forecast the consumer saw). Unit assumed kWh (the
# operator's two comparisons and our own sensor are all kWh). A non-numeric /
# unavailable last state -> that comparison is unscored for the day (not zero).
SCOREBOARD_COMPARISON_UNIT_KWH = True

# --- QUANTILES P10/P50/P90 (SPEC §6/§10) -----------------------------------
# Historical-simulation bands: a 90-day ring of hourly RELATIVE errors
# (measured / corrected-forecast) keyed by (weather class x day part). At
# forecast time the empirical P10/P50/P90 multipliers of the matching bin are
# applied per hour to the corrected curve. Trained nightly from issued(corrected)
# vs actuals — REUSING the existing issued + hourly-actuals rings. Enable flag
# default ON, kill switch in the options flow.
CONF_QUANTILES_ENABLED = "quantiles_enabled"
DEFAULT_QUANTILES_ENABLED = True
# 90-day ring of hourly relative-error samples (SPEC §6).
QUANTILE_RING_DAYS = 90
# A single (class x day part) bin can receive up to a day-part's worth of hourly
# samples per day (~8 daylight hours in summer), so the per-bin FIFO cap is
# QUANTILE_RING_DAYS x this, not QUANTILE_RING_DAYS itself — otherwise a
# frequently-hit summer bin (~6 samples/day) would hold only ~2-3 weeks of
# history and the bands would snap shut after any calm stretch (SPEC §6 90-day
# climatology). Samples now carry the trained day's ISO date, so the ring is
# date-windowed to QUANTILE_RING_DAYS (relative to the training day); this COUNT
# cap is the hard backstop after the date trim. It also bounds the per-day
# contribution to one bin, which underwrites the effective-days lower bound the
# day-diversity collapse gate uses for legacy un-dated samples (QUANTILE_MIN_DAYS).
QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN = 8
# The three band percentiles (SPEC §6: P10/P50/P90 -> 80% central band).
QUANTILE_P_LOW = 10.0
QUANTILE_P_MID = 50.0
QUANTILE_P_HIGH = 90.0
# Cold start (SPEC §6/§10 "no fake spread"): a bin with fewer than this many
# samples collapses its band to P50 (low == mid == high multiplier), so a thin
# bin never fabricates an interval. A bin at/above the floor emits the empirical
# spread. P50 itself, when the bin is empty, defaults to the neutral 1.0.
QUANTILE_MIN_SAMPLES = 20
# A band additionally needs evidence from at least this many DISTINCT days: the
# hourly samples within one day are strongly correlated (same sky, same forecast
# error), so 3 bursty days of ~8 hours each are ~3 independent observations, not
# 24 — the sample-count floor alone would let a handful of days un-collapse a
# band. effective_days = (# distinct sample dates) + ceil(# undated / the per-day
# cap); the undated term is a PROVABLE lower bound on the days a legacy (pre-fix,
# un-dated) ring spans, since the trainer never adds more than
# QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN to one bin on one day (SPEC §6).
QUANTILE_MIN_DAYS = 5
QUANTILE_NEUTRAL_MULT = 1.0
# The per-hour relative error = measured_wh / corrected_forecast_wh, clamped to a
# sane band so a divide-by-near-zero dawn/dusk hour cannot inject a 100x
# multiplier into the ring (SPEC §5 clamp ethos). Only hours whose corrected
# forecast Wh exceeds QUANTILE_MIN_FORECAST_WH are sampled.
QUANTILE_MIN_FORECAST_WH = 5.0
QUANTILE_REL_ERR_MIN = 0.0
QUANTILE_REL_ERR_MAX = 5.0

# --- Storage schema v3 (ADDITIVE over v2; SPEC §14) ------------------------
# Bump the INNER schema v2 -> v3; the outer HA Store envelope (STORAGE_VERSION)
# stays 1. store.py migrates v2 -> v3 ADDITIVELY: every v2 key (the three v1
# rings + the four learner sections + hourly actuals + trained_days) is carried
# through BYTE-FAITHFUL, and the three new v3 sections are default-injected at
# their neutral empty defaults. A migration that drops or resets any learner
# state is a CRITICAL failure (the live install has a populated v2 store on
# disk RIGHT NOW: shademap 7 channels / 851 bins, day-ahead 12 cells, drift +
# rollback + trained_days).
STORAGE_DATA_VERSION_V3 = 3
# New store keys (v3). All EXISTING v2 keys are unchanged and carried through.
STORE_KEY_QUANTILE_STATE = "quantile_state"    # QuantileState: {bin_key: relerr ring}
STORE_KEY_SCOREBOARD_STATE = "scoreboard_state"  # ScoreboardState: rolling window of DayScore
STORE_KEY_COMPARISON_RING = "comparison_ring"  # {iso_date: {comparison_name: daily_kwh}} read-from-recorder cache
# Inverter-efficiency site-calibration learner state (AC-side Phase 3). Added
# ADDITIVELY WITHIN the v3 schema (NO version bump): _empty_state injects the
# neutral InverterCalState and the shared load path default-reads a store that
# lacks the key to neutral, so every existing v3 store stays byte-faithful and
# the migration tests that pin v3 keep passing. Like drift_state it is a
# top-level learner section that does NOT ride the bias/shademap rollback ring
# (it is self-gating + never load-bearing, so a rollback need not touch it).
STORE_KEY_INVERTER_CAL_STATE = "inverter_cal_state"  # InverterCalState (learned eta_inv)

# --- New diagnostic sensors / binary sensors (SPEC §8/§10) -----------------
# Entity object_ids are unprefixed: the device slug already carries
# "balcony_solar_forecast", so these ARE the forecast's own metrics (baselines
# are the comparison_* sensors). Avoids the balcony_solar_forecast_forecast_*
# stutter.
SENSOR_FORECAST_DAILY_KWH_MAE = "daily_kwh_mae"
SENSOR_FORECAST_HOURLY_MAE = "hourly_mae"
# Per-comparison daily-kWh MAE sensor: object_id is suffixed with a slug of the
# comparison name (built by sensor.py from CONF_COMPARISON_NAME).
SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX = "comparison_daily_kwh_mae"
# Positive percent = the integration's own forecast is better than the best
# baseline on daily-kWh MAE.
SENSOR_FORECAST_VS_BEST_BASELINE_PCT = "vs_best_baseline_pct"
# Optional daily P10 / P90 energy sensors (today's band), SPEC §6/§10.
SENSOR_ENERGY_TODAY_P10 = "energy_production_today_p10"
SENSOR_ENERGY_TODAY_P90 = "energy_production_today_p90"
BINARY_SENSOR_KILL_GATE_PASSED = "kill_gate_passed"

# --- Quantile curve attributes on the energy sensors (SPEC §6/§8) ----------
# Additive to the existing ATTR_WATTS / ATTR_WH_PERIOD. Each is a {iso_utc: Wh}
# 15-min band curve, excluded from the recorder like the existing curve attrs.
ATTR_WH_PERIOD_P10 = "wh_period_p10"
ATTR_WH_PERIOD_P50 = "wh_period_p50"
ATTR_WH_PERIOD_P90 = "wh_period_p90"

# --- Coordinator <-> platform contract additions (self.data keys, v0.4) -----
DATA_KEY_QUANTILE_CURVES = "quantile_curves"      # {"p10": {iso: Wh}, "p50": ..., "p90": ...} 15-min
# AC-side band curves (Phase 2): {"p10": {iso_hour: Wh}, "p90": {iso_hour: Wh}}
# HOURLY Wh (the AC bands are computed at hourly resolution; P50 == ac_watts).
DATA_KEY_QUANTILE_CURVES_AC = "quantile_curves_ac"
DATA_KEY_SCOREBOARD = "scoreboard"                # dict: engine_mae / per-comparison mae / vs_best_pct / gate / strata
DATA_KEY_KILL_GATE_PASSED = "kill_gate_passed"    # bool | None (None == not enough window yet)

# --- get_forecast service response additions (SPEC §6/§8) ------------------
# The extended get_forecast response carries plane-agnostic TOTAL p10/p50/p90
# 15-min and hourly curves alongside the existing p50 curve. These keys name the
# blocks in the ServiceResponse dict (see _services.py / services.yaml).
FORECAST_RESP_KEY_P10 = "p10"
FORECAST_RESP_KEY_P50 = "p50"
FORECAST_RESP_KEY_P90 = "p90"

# --- Shade-profile visualisation tunables (SPEC §15) -----------------------
# The sun-path-vs-learned-shade diagram (core/shadeprofile.py). The sun path is
# sampled over the visualised local day at SHADE_PROFILE_STEP_MINUTES; the two
# horizon lines (static config horizon + learned shade horizon) are sampled on a
# fixed azimuth grid at SHADE_PROFILE_AZ_STEP_DEG over the day's daylight azimuth
# span. The learned shade horizon at an azimuth is the elevation below which the
# effective (gated + learner-blended) beam transmittance stays under
# SHADE_PROFILE_TAU_THRESHOLD, located by scanning elevation in
# SHADE_PROFILE_EL_SCAN_DEG steps. All pure geometry/lookup — no HA, no weather.
SHADE_PROFILE_STEP_MINUTES = 5     # sun-path sampling cadence over the local day
SHADE_PROFILE_AZ_STEP_DEG = 1.0    # azimuth grid step for the horizon lines
SHADE_PROFILE_TAU_THRESHOLD = 0.5  # effective-tau crossover = "shaded" for the horizon
SHADE_PROFILE_EL_SCAN_DEG = 1.0    # elevation scan step locating the shade horizon

# Shade-profile sensor attribute names (curve arrays; excluded from the recorder
# via recorder.exclude_attributes + the sensor's _unrecorded_attributes).
ATTR_SP_AZIMUTH = "azimuth"                 # [deg] sun azimuth per sun-path sample
ATTR_SP_SUN_ELEVATION = "sun_elevation"     # [deg] sun elevation per sample
ATTR_SP_TRANSMITTANCE = "transmittance"     # [0..1] effective beam tau per sample (pooled)
# Per-sample effective tau of the MODULE'S OWN channel only (SPEC §5 read-time
# pooling): the operator can compare each module's individual learned shading
# against the pooled group view to decide groupings. Empty list when the module
# is ungrouped (== the pooled view), so the attribute stays shape-stable.
ATTR_SP_TRANSMITTANCE_INDIVIDUAL = "transmittance_individual"
# Pooled shademap-bin sample count per sun-path sample (0 = static prior only):
# the learned evidence behind that sample's effective tau, summed over the read
# pool's channels (SPEC §5). The card sizes each dot by it (confidence viz).
ATTR_SP_SAMPLE_N = "sample_n"
ATTR_SP_TIME = "time"                       # [local ISO] time per sample
ATTR_SP_HORIZON_AZIMUTH = "horizon_azimuth"  # [deg] azimuth grid for the horizon lines
ATTR_SP_SHADE_HORIZON = "shade_horizon"     # [deg] learned shade horizon per grid azimuth
ATTR_SP_STATIC_HORIZON = "static_horizon"   # [deg] config horizon per grid azimuth
# Year-stable x-axis bounds (SPEC §15): the widest daylight sun-azimuth span of
# the whole year at the site (both solstices), so the diagram's x-axis does NOT
# rescale with the season and curves stay comparable across dates. Constant site
# geometry (a function of lat/lon/year only) — excluded from the recorder too.
ATTR_SP_AXIS_AZ_MIN = "axis_azimuth_min"    # [deg] min daylight azimuth over the year
ATTR_SP_AXIS_AZ_MAX = "axis_azimuth_max"    # [deg] max daylight azimuth over the year


# ===========================================================================
# v0.16 CONTRACT: ENSEMBLE-WEATHER UNCERTAINTY BANDS (SPEC §6)
# ---------------------------------------------------------------------------
# Today's learned P10/P50/P90 (core/quantiles.py) come from a residual ring per
# (cloud class x day part): well calibrated ON AVERAGE per weather class, but
# BLIND to TODAY's specific forecast uncertainty. Open-Meteo's ensemble API
# serves N perturbed members whose spread IS the day's weather uncertainty. We
# fold a per-slot RELATIVE spread (member GHI vs the deterministic GHI) into the
# learned band by ENVELOPE-MAX — the wider band wins per slot, never multiplied,
# so the weather share already inside the learned residuals is not double
# counted. The ensemble is NEVER load-bearing: P50 / headline / scoreboard /
# kill-gate are untouched, and any absence/failure degrades seamlessly to the
# learned bands. Opt-in, default OFF. Everything below is NEW; no key above is
# touched. Runtime stays stdlib-only; the ensemble is cached in memory only
# (NOT persisted — no store-schema bump).
# ===========================================================================

# --- Ensemble endpoint (its own host; the deterministic fetch stays on the
# main /forecast endpoint). Live shape recorded 2026-07-11: the hourly block
# carries the control member under the bare ``shortwave_radiation`` key plus the
# perturbed ``shortwave_radiation_member01`` .. ``_member39`` (40 members total
# for icon_seamless), 72 hourly stamps for forecast_days=3, timezone=UTC. The
# hourly radiation stamp marks the interval END (value at T = mean over
# [T-1h, T]), so the parser shifts each stamp −1 h to key by the interval START.
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODEL = "icon_seamless"
ENSEMBLE_GHI_VAR = "shortwave_radiation"  # the single hourly var + member key prefix
ENSEMBLE_FORECAST_DAYS = 3
# Ensembles refresh ~6-hourly; poll at half that so a fresh run is picked up
# promptly without hammering (its own cadence, independent of the main fetch).
ENSEMBLE_FETCH_INTERVAL_S = 3 * 3600
# Fewer usable members than this in an hour => treat that hour as unavailable
# (no trustworthy spread); the slot falls back to the learned band.
ENSEMBLE_MIN_MEMBERS = 10
# Below this deterministic GHI (W/m^2) the member/det ratios are noise (near-zero
# denominator at dawn/dusk / deep night), so the hour is skipped -> learned band.
ENSEMBLE_MIN_DET_GHI = 20.0
# Clamp each member factor (member_ghi / det_ghi) into a sane band so a single
# freak member can't blow the interval open.
ENSEMBLE_FACTOR_MIN = 0.0
ENSEMBLE_FACTOR_MAX = 3.0

# Opt-in kill switch (options flow), default OFF (operator-approved v0.16).
CONF_ENSEMBLE_ENABLED = "ensemble_enabled"
DEFAULT_ENSEMBLE_ENABLED = False

# Coordinator <-> platform contract addition: which source shaped TODAY's band
# slots. "learned" = residual ring only; "ensemble" = learned collapsed
# everywhere and the ensemble supplied the whole spread (cold-start win);
# "envelope" = the ensemble widened at least one slot over the learned band.
DATA_KEY_BAND_SOURCE = "band_source"
BAND_SOURCE_LEARNED = "learned"
BAND_SOURCE_ENSEMBLE = "ensemble"
BAND_SOURCE_ENVELOPE = "envelope"
