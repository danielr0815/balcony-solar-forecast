"""Cross-layer regression tests for the correction stack.

These tests deliberately exercise boundaries between learners.  Local unit
tests for each learner are not sufficient when one layer is trained against a
different curve than the one it is later applied to.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from balcony_solar_forecast.core import bootstrap_build, electrical, quantiles
from balcony_solar_forecast.core.shademap import shademap_bin_key
from balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    InverterGroup,
    PlaneConfig,
    QuantileState,
    SiteConfig,
)


def _site(*, azimuth: float = 180.0) -> SiteConfig:
    return SiteConfig(
        latitude=51.0,
        longitude=10.0,
        planes=(
            PlaneConfig(
                name="P1",
                azimuth_deg=azimuth,
                tilt_deg=70.0,
                wp=400.0,
                actual_entity="sensor.p1_dc",
            ),
        ),
        groups=(),
    )


def test_dc_group_clip_uses_physical_dc_limit() -> None:
    """A group clips DC at AC-limit / eta, not at the lower AC watt value."""
    group = InverterGroup(
        name="inv",
        plane_names=("P1",),
        ac_limit_w=800.0,
        inverter_efficiency=0.9,
    )

    out = electrical.clamp_groups({"P1": 1000.0}, (group,))

    assert out["P1"] == pytest.approx(800.0 / 0.9)


def test_quantile_training_expires_untouched_old_bins() -> None:
    """The 90-day contract applies to every bin, including sparse bins."""
    stale = [[f"2025-01-{day:02d}", value] for day in range(1, 6)
             for value in (0.7, 0.8, 0.9, 1.0)]
    state = QuantileState(bins={"clear|midday": stale})
    sample = quantiles.QuantileSample(
        cloud_class="mixed",
        day_part="morning",
        measured_wh=80.0,
        corrected_wh=100.0,
    )

    trained = quantiles.train_quantiles(
        state, [sample], training_date="2026-08-25"
    )

    assert "clear|midday" not in trained.bins
    assert quantiles.bands_for_bin(
        trained, cloud_class="clear", day_part="midday"
    ).collapsed


def test_bootstrap_rls_uses_live_day_section_energy_gate() -> None:
    """Bootstrap must not age a cell on an observation live RLS rejects."""
    cell = BiasCell()

    updated = bootstrap_build._rls_step(cell, modeled=10.0, measured=8.0)

    assert updated == cell


def test_bootstrap_is_walk_forward_across_shademap_bias_and_quantiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A day is issued from pre-day state, then scored, then learned.

    The seeded shademap represents a slow-only curve of 58.33 Wh from a
    100-Wh static forecast.  A 50-Wh measurement therefore trains theta and
    the quantile residual against 58.33 Wh.  Training either against RAW or
    using the same day's newly fitted theta would yield 0.5 or 1.0 instead.
    """
    site = _site()
    start = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
    wx = bootstrap_build.HourlyWeather(
        start=start,
        ghi=800.0,
        dni=800.0,
        dhi=100.0,
        temp_c=20.0,
    )

    def _fixed_reconstruction(*_args, **_kwargs):
        return bootstrap_build.PlaneHourReconstruction(
            beam_wh=100.0,
            diffuse_wh=0.0,
            gated_total_wh=100.0,
            ghi=800.0,
            kc=1.0,
            sun_az=180.0,
            sun_el=45.0,
            beam_share=0.25,
        )

    monkeypatch.setattr(
        bootstrap_build, "reconstruct_plane_hour", _fixed_reconstruction
    )
    doy = (start.replace(minute=30)).timetuple().tm_yday
    key = shademap_bin_key(180.0, 45.0, doy)
    acc = bootstrap_build.BootstrapAccumulator(
        shade={"P1": {key: [0.5, 100]}}
    )

    assert bootstrap_build.process_day_hourly(
        acc,
        site,
        [wx],
        {"P1": {start.isoformat(): 50.0}},
        svf_by_plane={"P1": 1.0},
        tz=UTC,
    )

    # shrinkage: 100/(100+20) * 0.5 + 20/(100+20) * 1.0
    pre_day_slow = 100.0 * (100.0 / 120.0 * 0.5 + 20.0 / 120.0)
    cell = next(iter(acc.bias.values()))
    assert cell.theta == pytest.approx(50.0 / pre_day_slow, rel=1e-5)
    q_entry = next(iter(acc.quantile_state.bins.values()))[0]
    assert q_entry[1] == pytest.approx(50.0 / pre_day_slow, rel=1e-5)


def test_bootstrap_quantile_uses_served_not_immature_theta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An n=2 theta is persisted but still neutral in the issued forecast."""
    site = _site()
    start = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
    wx = bootstrap_build.HourlyWeather(
        start=start,
        ghi=800.0,
        dni=800.0,
        dhi=100.0,
        temp_c=20.0,
    )

    monkeypatch.setattr(
        bootstrap_build,
        "reconstruct_plane_hour",
        lambda *_args, **_kwargs: bootstrap_build.PlaneHourReconstruction(
            beam_wh=100.0,
            diffuse_wh=0.0,
            gated_total_wh=100.0,
            ghi=800.0,
            kc=1.0,
            sun_az=180.0,
            sun_el=45.0,
            beam_share=0.25,
        ),
    )
    cloud = bootstrap_build._classify_cloud(wx, UTC, elevation_deg=45.0)
    part = bootstrap_build._day_part_for_slot(start, site.longitude)
    key = BiasState.cell_key(cloud, part)
    acc = bootstrap_build.BootstrapAccumulator(
        bias={key: BiasCell(theta=0.5, covariance=1.0, n=2)}
    )

    assert bootstrap_build.process_day_hourly(
        acc,
        site,
        [wx],
        {"P1": {start.isoformat(): 50.0}},
        svf_by_plane={"P1": 1.0},
        tz=UTC,
    )

    q_entry = next(iter(acc.quantile_state.bins.values()))[0]
    assert q_entry[1] == pytest.approx(0.5)


def test_bootstrap_omits_hours_missing_one_metered_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A historical port gap is not a low whole-site production sample."""
    p1 = _site().planes[0]
    p2 = replace(p1, name="P2", actual_entity="sensor.p2_dc")
    site = replace(_site(), planes=(p1, p2))
    starts = (
        datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 11, 0, tzinfo=UTC),
    )
    weather = [
        bootstrap_build.HourlyWeather(
            start=start,
            ghi=800.0,
            dni=800.0,
            dhi=100.0,
            temp_c=20.0,
        )
        for start in starts
    ]
    monkeypatch.setattr(
        bootstrap_build,
        "reconstruct_plane_hour",
        lambda *_args, **_kwargs: bootstrap_build.PlaneHourReconstruction(
            beam_wh=100.0,
            diffuse_wh=0.0,
            gated_total_wh=100.0,
            ghi=800.0,
            kc=1.0,
            sun_az=180.0,
            sun_el=45.0,
            beam_share=0.25,
        ),
    )
    h10, h11 = (start.isoformat() for start in starts)
    acc = bootstrap_build.BootstrapAccumulator()

    assert bootstrap_build.process_day_hourly(
        acc,
        site,
        weather,
        {
            "P1": {h10: 100.0, h11: 100.0},
            "P2": {h10: 100.0},
        },
        svf_by_plane={"P1": 1.0, "P2": 1.0},
        tz=UTC,
    )

    entries = [
        entry
        for ring in acc.quantile_state.bins.values()
        for entry in ring
    ]
    assert len(entries) == 1
    assert entries[0][1] == pytest.approx(1.0)


def test_bootstrap_signature_changes_with_geometry() -> None:
    """Same coordinates/names are not enough to identify correction state."""
    assert bootstrap_build.site_signature(_site(azimuth=180.0)) != (
        bootstrap_build.site_signature(_site(azimuth=90.0))
    )
    site = _site()
    assert bootstrap_build.site_signature(site) == bootstrap_build.site_signature(
        replace(
            site,
            planes=(
                replace(site.planes[0], actual_entity="sensor.renamed"),
            ),
        )
    )


def test_bootstrap_signature_is_canonical_for_semantic_order_and_names() -> None:
    """List order and group labels do not define the learned feature space."""
    p1 = _site().planes[0]
    p2 = replace(p1, name="P2", azimuth_deg=90.0, actual_entity="sensor.p2")
    group = InverterGroup(
        name="old-label",
        plane_names=(p1.name, p2.name),
        ac_limit_w=800.0,
        inverter_efficiency=0.96,
    )
    site = replace(
        _site(),
        planes=(
            replace(p1, shade_group="old-shade-label"),
            replace(p2, shade_group="old-shade-label"),
        ),
        groups=(group,),
    )
    reordered = replace(
        site,
        planes=(
            replace(p2, shade_group="new-shade-label"),
            replace(p1, shade_group="new-shade-label"),
        ),
        groups=(
            replace(
                group,
                name="new-label",
                plane_names=(p2.name, p1.name),
            ),
        ),
    )

    assert bootstrap_build.site_signature(site) == (
        bootstrap_build.site_signature(reordered)
    )
