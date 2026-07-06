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
INTEGRATION_VERSION = "0.3.0"

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
# plane fields
CONF_PLANE_NAME = "name"
CONF_AZIMUTH = "azimuth_deg"  # 0=N clockwise
CONF_TILT = "tilt_deg"  # degrees from horizontal, 90 = vertical
CONF_WP = "wp"  # module STC peak power, watts
CONF_EFFICIENCY = "efficiency"  # system/DC efficiency, default 0.96
CONF_HORIZON = "horizon"  # list of horizon rows (see below)
CONF_ACTUAL_ENTITY = "actual_entity"  # HA entity id for measured DC power
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

# --- Physics constants (SPEC §4 physics musts) ---
ALBEDO_DEFAULT = 0.2
ALBEDO_SNOW = 0.5  # applied when snow_depth > SNOW_DEPTH_THRESHOLD_M
SNOW_DEPTH_THRESHOLD_M = 0.01
RB_CAP = 10.0  # geometric beam-ratio cap (low-sun explosion guard)
LOW_SUN_CUTOFF_DEG = 3.0  # below this elevation: circumsolar = 0
ROSS_COEFF = 0.0342  # Tcell = Tamb + ROSS_COEFF * POA
TEMP_COEFF_PER_K = -0.0034  # power derate per K above 25 C (-0.34 %/K)
TEMP_REF_C = 25.0
DEFAULT_EFFICIENCY = 0.96

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
BINARY_SENSOR_DEGRADED = "degraded"

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

# Drift monitor: rolling daylight MAE corrected vs pure physics.
DRIFT_WINDOW_DAYS = 7
DRIFT_LOSS_STREAK_DAYS = 7        # consecutive losing days => auto-disable layer
DRIFT_ROLLBACK_SNAPSHOTS = 3     # legacy alias; the live ring depth is LEARNER_SNAPSHOT_RING (must exceed the loss streak)
# A "losing" day = corrected daylight MAE strictly worse than physics MAE by
# more than this relative margin (avoids flapping on ties/noise).
DRIFT_LOSS_MARGIN = 0.02

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
DATA_KEY_DRIFT_MAE = "drift_mae"                  # dict: {raw, corrected, baseline} rolling MAE
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
LEARNER_STATUS_VALUES = (
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_OFF,
    LEARNER_STATUS_DISABLED_BY_DRIFT,
    LEARNER_STATUS_FROZEN,
)

# --- Repair issue ids (SPEC §5/§7) -----------------------------------------
ISSUE_FAST_LEARNER_DISABLED = "fast_learner_auto_disabled"
ISSUE_SLOW_LEARNER_DISABLED = "slow_learner_auto_disabled"
