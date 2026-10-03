"""Independent evidence scenarios, including ambiguity and causal availability."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from balcony_solar_forecast.core.panel_weather import (
    PanelFrame,
    PanelObservation,
    PanelWeatherMonitor,
)

START = datetime(2026, 6, 21, 10, tzinfo=UTC)


def frame(t, values=(100, 100, 100), **kwargs):
    panels = tuple(PanelObservation(str(i), str(i // 2), 'east' if i < 2 else 'south',
                                    t, v, 100) for i, v in enumerate(values))
    return PanelFrame(t, t, 'model1', panels, **kwargs)


def test_common_drop_is_evidence_not_verified_cloud():
    monitor = PanelWeatherMonitor()
    assert monitor.observe(frame(START), now=START).state == 'warming_up'
    t = START + timedelta(minutes=5)
    evidence = monitor.observe(frame(t, (60, 60, 60), forecast_class='overcast'), now=t)
    assert evidence.state == 'common_change'
    assert evidence.common_ramp == -.4
    assert evidence.reason == 'cloud_or_common_curtailment'
    assert evidence.forecast_class == 'overcast'


def test_single_orientation_shadow_is_not_common_weather():
    monitor = PanelWeatherMonitor()
    monitor.observe(frame(START), now=START)
    t = START + timedelta(minutes=5)
    assert monitor.observe(frame(t, (50, 50, 100)), now=t).state == 'steady'


def test_geometry_change_alone_is_removed_by_reference():
    monitor = PanelWeatherMonitor()
    monitor.observe(frame(START), now=START)
    t = START + timedelta(minutes=5)
    next_frame = frame(t, (70, 70, 70))
    next_frame = replace(next_frame, panels=tuple(replace(p, reference_watts=70) for p in next_frame.panels))
    assert monitor.observe(next_frame, now=t).residual_ratio == 1
    assert monitor.observe(next_frame, now=t).reason == 'repeated_frame'


def test_future_labels_cannot_enter_an_earlier_prediction():
    t = START + timedelta(minutes=5)
    assert PanelWeatherMonitor().observe(frame(t), now=START).reason == 'not_yet_available'


def test_stale_clipped_duplicate_or_same_group_channels_are_unknown():
    original = frame(START)
    variants = [
        replace(original, panels=tuple(replace(p, reported_at=START-timedelta(minutes=3)) for p in original.panels)),
        replace(original, panels=tuple(replace(p, clipped=True) for p in original.panels)),
        replace(original, panels=tuple(replace(p, source='one') for p in original.panels)),
        replace(original, panels=tuple(replace(p, group='one') for p in original.panels)),
    ]
    for variant in variants:
        assert PanelWeatherMonitor().observe(variant, now=START).state == 'unknown'


def test_model_change_discards_old_reference_and_memory_is_bounded():
    monitor = PanelWeatherMonitor()
    for minute in range(100):
        t = START + timedelta(minutes=minute)
        monitor.observe(frame(t), now=t)
    assert len(monitor._frames) == 31
    t += timedelta(minutes=1)
    assert monitor.observe(replace(frame(t), generation='model2'), now=t).state == 'warming_up'


def test_learning_gate_experiment_excludes_whole_target_group():
    from balcony_solar_forecast.core.panel_weather import without_target
    reduced = without_target(frame(START), '0')
    assert [p.source for p in reduced.panels] == ['2']
    assert PanelWeatherMonitor().observe(reduced, now=START).state == 'unknown'


def test_source_dropout_cannot_create_a_common_weather_ramp():
    initial = tuple(PanelObservation(str(i), 'A' if i<2 else 'B', 'east' if i<2 else 'south',
                                    START, watts, 100) for i,watts in enumerate([50,150,50,50,150,150]))
    monitor=PanelWeatherMonitor()
    assert monitor.observe(PanelFrame(START,START,'same',initial),now=START).state=='warming_up'
    later=START+timedelta(minutes=5)
    retained=tuple(replace(initial[i],reported_at=later) for i in (0,2,3))
    result=monitor.observe(PanelFrame(later,later,'same',retained),now=later)
    assert result.state=='warming_up' and result.common_ramp is None
    later2=later+timedelta(minutes=5)
    stable=tuple(replace(p,reported_at=later2) for p in retained)
    assert monitor.observe(PanelFrame(later2,later2,'same',stable),now=later2).state=='common_damping'


def test_panel_normals_do_not_invent_diversity_at_rounding_or_north_boundaries():
    from balcony_solar_forecast.core.panel_weather import orientation_cohorts
    for angles in [(22.4,22.6),(359,1),(90,270)]:
        tilt=0 if angles==(90,270) else 70
        panels=[PanelObservation(str(i),'G','unused',START,100,100,azimuth_deg=az,tilt_deg=tilt) for i,az in enumerate(angles)]
        assert len(set(orientation_cohorts(panels).values()))==1
    panels=[PanelObservation(str(i),'G','unused',START,100,100,azimuth_deg=az,tilt_deg=70) for i,az in enumerate([0,45,90])]
    assert len(set(orientation_cohorts(panels).values()))==3
    assert orientation_cohorts(panels)==orientation_cohorts(list(reversed(panels)))
    invalid=replace(panels[0],tilt_deg=True)
    assert invalid.source not in orientation_cohorts([invalid])
