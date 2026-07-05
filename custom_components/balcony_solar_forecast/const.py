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
INTEGRATION_VERSION = "0.1.0"

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
