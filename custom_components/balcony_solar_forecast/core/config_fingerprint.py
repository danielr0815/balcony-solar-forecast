"""Shared semantic configuration identity for learning and change previews."""
from __future__ import annotations

from ..const import CLASSIFIER_VERSION
from .types import HorizonRow, PlaneConfig, SiteConfig


def site_fingerprint(site: SiteConfig, *, classifier_version: int = CLASSIFIER_VERSION) -> str:
    """Stable digest of the forecast-relevant site fields (A4/FOR-4).

    Covers exactly the config the day-ahead bias cells are conditioned on:
    the site LOCATION (lat/lon — they set the entire sun geometry every
    theta cell was learned against, so a location reconfigure must re-seed;
    rounded to 4 decimals, so float
    re-serialisation can never spuriously flip the hash),
    each plane's azimuth / tilt / wp / efficiency / ross_coeff / horizon
    profile (per row: elevation AND the transmittance fields tau / seasonal /
    tau_leafed / tau_bare AND the v0.22 inline elevation profiles
    tau_points / tau_points_bare AND the v0.22 per-row diffuse override
    diffuse_tau — the horizon rows ARE the tau-carrying "screens" of SPEC §7.7,
    so a τ 0→0.4 edit OR a tau_points knot edit reshapes the modeled beam by
    +50–150 Wh/day mornings (ADR-2), and setting diffuse_tau on the wall rows
    lifts the modeled iso-diffuse floor by +0.1–0.2 kWh/day site-wide (ADR-3);
    both reshape the RAW curve the bias cells are conditioned on and MUST
    re-seed),
    the site albedo, the bifacial beam gain (T6 — the A1 1.0→1.25 rollout runs
    through here and changes the direct-POA share site-wide), each plane's
    shade-pool membership, every inverter group's member set / AC limit / configured
    eta (the latter two set the served-DC clip point), and the
    cloud-classification taxonomy version
    (CLASSIFIER_VERSION, A5) — a change to any of these makes the learned theta
    fit a now-stale geometry or class meaning, so a differing fingerprint
    triggers a bias re-seed. Fields that do NOT change the modeled curve
    (entity ids, shade-/inverter-group labels, meter sign) are excluded so a benign
    edit never resets learning. Planes, groups and group members are sorted
    into semantic order. Rounded so float re-serialisation can never
    spuriously flip the hash.
    """
    import hashlib

    def _pts(pts: tuple[tuple[float, float], ...]) -> str:
        # Serialise an inline (el, tau) elevation profile, rounded like the
        # scalars so a float re-serialisation can never spuriously flip the
        # hash. Emitted ONLY when the profile is set (mirrors the
        # nur-wenn-gesetzt to_dict rule), so a row without tau_points keeps
        # its exact pre-0.22 signature — a config whose modeled curve is
        # byte-identical after upgrade keeps its fingerprint and is NOT
        # re-seeded; adding/editing a knot appends the segment and flips it.
        return "/".join(f"{round(el, 2)}:{round(t, 4)}" for el, t in pts)

    def _hz_row(r: HorizonRow) -> str:
        tl = "-" if r.tau_leafed is None else f"{round(r.tau_leafed, 4)}"
        tb = "-" if r.tau_bare is None else f"{round(r.tau_bare, 4)}"
        s = (
            f"{round(r.azimuth_deg, 2)},{round(r.elevation_deg, 2)}"
            f",t{round(r.tau, 4)},s{int(r.seasonal)},tl{tl},tb{tb}"
        )
        if r.tau_points is not None:
            s += f",tp{_pts(r.tau_points)}"
        if r.tau_points_bare is not None:
            s += f",tpb{_pts(r.tau_points_bare)}"
        if r.diffuse_tau is not None:
            # Only-when-set (mirrors the nur-wenn-gesetzt to_dict rule): a row
            # the operator never marked keeps its exact pre-0.22 signature, so
            # a byte-identical legacy config is NOT re-seeded; setting/editing
            # diffuse_tau appends the segment and flips the fingerprint.
            s += f",dt{round(r.diffuse_tau, 4)}"
        return s

    def _plane_sig(p: PlaneConfig) -> str:
        hz = ";".join(_hz_row(r) for r in p.horizon)
        ross = "-" if p.ross_coeff is None else f"{round(p.ross_coeff, 4)}"
        return (
            f"{p.name}:az{round(p.azimuth_deg, 2)}:tl{round(p.tilt_deg, 2)}"
            f":wp{round(p.wp, 2)}:ef{round(p.efficiency, 4)}:ro{ross}"
            f":hz[{hz}]"
        )

    albedo = "-" if site.albedo is None else f"{round(site.albedo, 4)}"
    beam_gain = (
        "-"
        if site.bifacial_beam_gain is None
        else f"{round(site.bifacial_beam_gain, 4)}"
    )
    shade_members: dict[str, list[str]] = {}
    for plane in site.planes:
        if plane.shade_group:
            shade_members.setdefault(plane.shade_group, []).append(
                plane.name
            )
    parts = [
        f"loc={round(site.latitude, 4)},{round(site.longitude, 4)}",
        *sorted(_plane_sig(p) for p in site.planes),
        *sorted(
            f"shade:{','.join(sorted(names))}"
            for names in shade_members.values()
            if len(names) > 1
        ),
        f"albedo={albedo}",
        f"beam_gain={beam_gain}",
        *sorted(
            f"grp:{','.join(sorted(g.plane_names))}:ac{round(g.ac_limit_w, 2)}"
            f":eta{round(g.inverter_efficiency, 4)}"
            for g in site.groups
        ),
        # Cloud-classification taxonomy version (A5): a change to the class
        # boundaries (layer-cover -> k_c) makes every learned theta cell fit a
        # now-stale class meaning, so bump == fingerprint change == re-seed.
        f"clsver={classifier_version}",
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
