"""Pure (HA-free) validation of the config-flow site object.

Split out of ``config_flow.py`` so it can be unit-tested with plain pytest
without importing Home Assistant or voluptuous. Imports only the pure core
types and ``const`` — nothing from ``homeassistant``.

``validate_site`` round-trips a raw site dict through ``SiteConfig.from_dict``
and applies the SPEC range checks (azimuth 0..360, tilt 0..90, wp > 0, tau
0..1, horizon rows sorted by ascending azimuth). It raises
``SiteValidationError`` carrying a translation-key ``code`` on the first
problem found; the config flow surfaces that code as a field error.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .core.types import PlaneConfig, SiteConfig

# Upper sanity bound for an inverter-group AC limit (W). Local guard only;
# not a physical hard limit — large string inverters exist.
AC_LIMIT_MAX_W = 100000.0


class SiteValidationError(Exception):
    """Raised with an error *code* (translation key) for a bad site object."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_site(raw: Any) -> SiteConfig:
    """Validate a raw site dict and return a *normalised* ``SiteConfig``.

    Range-checks every field and raises ``SiteValidationError`` with a
    translation-key code on the first problem found. Each plane's horizon
    rows are **stable-sorted by ascending azimuth** in the returned config
    so downstream linear interpolation (``horizon.py``) always sees an
    ordered table — the config flow persists this canonical form. A merely
    out-of-order table is therefore normalised, not rejected; only genuinely
    invalid values fail.
    """
    if not isinstance(raw, dict):
        raise SiteValidationError("site_not_object")

    # Structural parse first (missing keys / wrong types -> generic error).
    try:
        site = SiteConfig.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        raise SiteValidationError("site_malformed") from None

    if not site.planes:
        raise SiteValidationError("no_planes")

    plane_names: set[str] = set()
    normalised_planes: list[PlaneConfig] = []
    for plane in site.planes:
        if not plane.name:
            raise SiteValidationError("plane_no_name")
        if plane.name in plane_names:
            raise SiteValidationError("plane_dup_name")
        plane_names.add(plane.name)

        if not 0.0 <= plane.azimuth_deg <= 360.0:
            raise SiteValidationError("bad_azimuth")
        if not 0.0 <= plane.tilt_deg <= 90.0:
            raise SiteValidationError("bad_tilt")
        if not plane.wp > 0.0:
            raise SiteValidationError("bad_wp")
        if not 0.0 <= plane.efficiency <= 1.0:
            raise SiteValidationError("bad_efficiency")

        sorted_horizon = _validate_horizon(plane.horizon)
        normalised_planes.append(replace(plane, horizon=sorted_horizon))

    _validate_groups(site, plane_names)
    return replace(site, planes=tuple(normalised_planes))


def _validate_horizon(horizon) -> tuple:
    """Range-check each horizon row; return the rows stable-sorted by azimuth.

    A stable sort keeps the relative order of rows sharing an azimuth (e.g.
    the shipped 100.0 / 100.01 far-field breakpoints), so canonicalising is
    lossless for the interpolator.
    """
    for row in horizon:
        if not 0.0 <= row.azimuth_deg <= 360.0:
            raise SiteValidationError("bad_horizon_azimuth")
        if not 0.0 <= row.elevation_deg <= 90.0:
            raise SiteValidationError("bad_horizon_elevation")
        if not 0.0 <= row.tau <= 1.0:
            raise SiteValidationError("bad_tau")
        for opt_tau in (row.tau_leafed, row.tau_bare):
            if opt_tau is not None and not 0.0 <= opt_tau <= 1.0:
                raise SiteValidationError("bad_tau")
        if row.seasonal and (row.tau_leafed is None or row.tau_bare is None):
            raise SiteValidationError("seasonal_missing_tau")

    return tuple(sorted(horizon, key=lambda r: r.azimuth_deg))


def _validate_groups(site: SiteConfig, plane_names: set[str]) -> None:
    """Check inverter groups reference real planes and have a sane AC limit."""
    group_names: set[str] = set()
    for group in site.groups:
        if not group.name:
            raise SiteValidationError("group_no_name")
        if group.name in group_names:
            raise SiteValidationError("group_dup_name")
        group_names.add(group.name)
        if not group.plane_names:
            raise SiteValidationError("group_no_planes")
        for pn in group.plane_names:
            if pn not in plane_names:
                raise SiteValidationError("group_unknown_plane")
        if not 0.0 < group.ac_limit_w <= AC_LIMIT_MAX_W:
            raise SiteValidationError("bad_ac_limit")
