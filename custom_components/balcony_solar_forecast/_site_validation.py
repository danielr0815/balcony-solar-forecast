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

import math
from dataclasses import replace
from typing import Any

from .const import (
    CONF_PLANES,
    CONF_SHADE_GROUP,
    HZ_DIFFUSE_TAU_MAX,
    SITE_MAX_HORIZON_POINTS,
    SITE_MAX_PLANES,
    SITE_MAX_SHADE_GROUPS,
)
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

    # Cardinality caps (SPEC §7.2-§7.4): the site object arrives as free JSON
    # via the object selector, so bound what a pasted/crafted object can
    # allocate. The shade-group pool is checked FIRST: each distinct group is
    # a whole pooled shademap channel (the costlier object), and with the
    # plane cap at SITE_MAX_PLANES an over-limit group count can only occur
    # together with too many planes — report the more specific limit.
    n_groups = len({p.shade_group for p in site.planes if p.shade_group})
    if n_groups > SITE_MAX_SHADE_GROUPS:
        raise SiteValidationError("too_many_shade_groups")
    if len(site.planes) > SITE_MAX_PLANES:
        raise SiteValidationError("too_many_planes")

    # Raw plane dicts (parallel to ``site.planes``; from_dict + the horizon-only
    # sort preserve plane order) so an EXPLICIT empty/whitespace shade_group can
    # be rejected — from_dict has already normalised such a value to None, so the
    # parsed config alone cannot tell "blank field" from "no field".
    raw_planes = raw.get(CONF_PLANES, [])
    if not isinstance(raw_planes, list):
        raw_planes = []

    plane_names: set[str] = set()
    normalised_planes: list[PlaneConfig] = []
    for idx, plane in enumerate(site.planes):
        if not plane.name:
            raise SiteValidationError("plane_no_name")
        if plane.name in plane_names:
            raise SiteValidationError("plane_dup_name")
        plane_names.add(plane.name)

        raw_plane = raw_planes[idx] if idx < len(raw_planes) else None
        if isinstance(raw_plane, dict) and CONF_SHADE_GROUP in raw_plane:
            raw_group = raw_plane.get(CONF_SHADE_GROUP)
            # A present-but-blank value is a fat-finger, not "no group": reject
            # it rather than silently dropping the operator's grouping intent.
            if raw_group is not None and (
                not isinstance(raw_group, str) or not raw_group.strip()
            ):
                raise SiteValidationError("shade_group_empty")

        if not 0.0 <= plane.azimuth_deg <= 360.0:
            raise SiteValidationError("bad_azimuth")
        if not 0.0 <= plane.tilt_deg <= 90.0:
            raise SiteValidationError("bad_tilt")
        if not plane.wp > 0.0:
            raise SiteValidationError("bad_wp")
        if not 0.0 <= plane.efficiency <= 1.0:
            raise SiteValidationError("bad_efficiency")
        # Optional Ross override: a generous physical band (Ross/Skoplaki
        # literature spans ~0.02..0.056; the [0.005, 0.12] guard just rejects
        # nonsense / non-finite values, not tuning choices).
        if plane.ross_coeff is not None and not (
            math.isfinite(plane.ross_coeff)
            and 0.005 <= plane.ross_coeff <= 0.12
        ):
            raise SiteValidationError("bad_ross_coeff")

        sorted_horizon = _validate_horizon(plane.horizon)
        normalised_planes.append(replace(plane, horizon=sorted_horizon))

    _validate_groups(site, plane_names)
    _validate_shade_groups(site)
    return replace(site, planes=tuple(normalised_planes))


def _validate_horizon(horizon) -> tuple:
    """Range-check each horizon row; return the rows stable-sorted by azimuth.

    A stable sort keeps the relative order of rows sharing an azimuth (e.g.
    the shipped 100.0 / 100.01 far-field breakpoints), so canonicalising is
    lossless for the interpolator.
    """
    # Row-count cap first (SPEC §7.4): the per-slot interpolation cost scales
    # with the table length, and the object selector accepts free JSON.
    if len(horizon) > SITE_MAX_HORIZON_POINTS:
        raise SiteValidationError("too_many_horizon_points")
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
        # Diffuse override (ADR §3.7, v0.22 D2): 0 <= diffuse_tau <= 0.8, valid
        # independently of tau / tau_points. The 0.8 cap is a guard-rail — values
        # near 1 ("sector invisible to the diffuse") would cloak the beam-bound
        # rest the field is deliberately NOT meant to hide (ADR §3.4).
        if row.diffuse_tau is not None and not (
            math.isfinite(row.diffuse_tau)
            and 0.0 <= row.diffuse_tau <= HZ_DIFFUSE_TAU_MAX
        ):
            raise SiteValidationError("bad_diffuse_tau")
        _validate_tau_points(row)

    return tuple(sorted(horizon, key=lambda r: r.azimuth_deg))


def _validate_tau_points(row) -> None:
    """Range-check an inline elevation profile ``tau_points`` (ADR §2.5, v0.22).

    Rules (additive; a row without ``tau_points`` is untouched, backward
    compatible):
      1. 1..12 ``(el, tau)`` pairs when present (``bad_tau_points``).
      2. ``el`` strictly ascending; ``0 <= el <= elevation_deg`` of the row —
         a knot above the edge is meaningless (above the edge tau is 1 by
         definition) and rejected as ``tau_points_above_edge``.
      3. ``0 <= tau <= 1`` per knot (``bad_tau``, the existing key).
      4. ``tau_points_bare`` only with a ``seasonal`` row that also has
         ``tau_points``, and then with the SAME length and identical el raster
         (``seasonal_points_mismatch``); its taus obey rule 3 too.
      NO monotonicity is enforced — real canopies have gaps (the measured
      7-8 deg dip below 6-7 deg is legitimate).
    """
    pts = row.tau_points
    if pts is None:
        # A bare profile without a leafed one is a mismatch (nothing to blend).
        if row.tau_points_bare is not None:
            raise SiteValidationError("seasonal_points_mismatch")
        return
    if not 1 <= len(pts) <= 12:
        raise SiteValidationError("bad_tau_points")
    prev_el: float | None = None
    for el, tau in pts:
        if not 0.0 <= el <= row.elevation_deg:
            raise SiteValidationError("tau_points_above_edge")
        if prev_el is not None and not el > prev_el:
            raise SiteValidationError("bad_tau_points")
        prev_el = el
        if not 0.0 <= tau <= 1.0:
            raise SiteValidationError("bad_tau")

    bare = row.tau_points_bare
    if bare is not None:
        if not row.seasonal or len(bare) != len(pts):
            raise SiteValidationError("seasonal_points_mismatch")
        for (el, _t), (bel, btau) in zip(pts, bare, strict=False):
            if abs(el - bel) > 1e-9:
                raise SiteValidationError("seasonal_points_mismatch")
            if not 0.0 <= btau <= 1.0:
                raise SiteValidationError("bad_tau")


def _validate_shade_groups(site: SiteConfig) -> None:
    """Guard the shade-group → shademap-channel aliasing (SPEC §9.2).

    A ``shade_group`` is the shademap channel its member planes pool their shade
    learning into (``PlaneConfig.shade_channel``). It must not equal the NAME of
    a plane that does NOT itself carry that same group: otherwise that plane's
    OWN per-plane channel (``shade_channel == name``) would silently collide with
    the group's pooled channel, aliasing a non-member's learning into the pool.
    A group named after one of its own members is allowed and means "the others
    share that module's shading" (the named plane carries the group too).
    """
    for plane in site.planes:
        group = plane.shade_group
        if group is None:
            continue
        named = site.plane_by_name(group)
        if named is not None and named.shade_group != group:
            raise SiteValidationError("shade_group_collision")


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
