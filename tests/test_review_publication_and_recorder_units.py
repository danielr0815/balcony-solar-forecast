"""Publication requires live weather coverage and measured DC in proven units."""

from datetime import timedelta

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.balcony_solar_forecast import coordinator as coord_mod
from scripts.validation import capture_lead_time as capture
from tests.helpers.weather import NOW, _om_payload
from tests.test_capture_lead_time import DAY, PLAN, START
from tests.test_review_weather_warmstart import _warm_coordinator


@pytest.mark.parametrize(("delay_in", "seconds_after_end"), [("executor", 0), ("presenter", 1)])
async def test_expired_at_publication_discards_candidate_without_mutating_result_context(
    monkeypatch, delay_in, seconds_after_end,
):
    slot_end = NOW.replace(hour=10, minute=15)
    start_clock = slot_end - timedelta(seconds=1)
    publish_clock = slot_end + timedelta(seconds=seconds_after_end)
    payload = _om_payload(start_iso=slot_end.isoformat(), n_quarters=1)
    coord = _warm_coordinator(monkeypatch, payload, timedelta(minutes=20))
    coord._last_fetched_at = start_clock - timedelta(minutes=20)
    now = start_clock
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: now)
    weather = coord._cached_weather()
    coord._last_result = await coord._compute(weather, start_clock)
    context_names = (
        "_last_result", "_result_provenance", "_result_reference_context",
        "_result_inverter_eta", "_result_capture_context", "_correction_source",
    )
    previous = {name: getattr(coord, name) for name in context_names}
    audit_calls = []
    monkeypatch.setattr(coord, "_track_headline_revisions", lambda *args: audit_calls.append(args))
    run = coord.hass.async_add_executor_job
    build = coord._build_data

    async def delayed_run(*args):
        nonlocal now
        result = await run(*args)
        now = publish_clock
        return result

    def delayed_build(*args, **kwargs):
        nonlocal now
        result = build(*args, **kwargs)
        now = publish_clock
        return result

    if delay_in == "executor":
        monkeypatch.setattr(coord.hass, "async_add_executor_job", delayed_run)
    else:
        monkeypatch.setattr(coord, "_build_data", delayed_build)

    with pytest.raises(UpdateFailed, match="no usable future intervals"):
        await coord._async_update_data()
    assert all(getattr(coord, name) is value for name, value in previous.items())
    assert audit_calls == []


@pytest.mark.parametrize(("second_unit", "second_class", "expected_wh"), [
    ("W", "power", 250.0),
    ("kW", "power", 250.0),
    ("A", "electric_current", None),
    ("A", "power", None),
    (None, None, None),
])
def test_recorder_statistics_require_power_unit_metadata(monkeypatch, second_unit, second_class, expected_wh):
    plan = {**PLAN, "targets": [START.isoformat()]}
    binding = capture._binding_sha256(plan)
    after = START + 2 * capture.HOUR
    monkeypatch.setattr(capture, "_now", lambda: after)
    monkeypatch.setattr(capture, "_fetch_entry", lambda *args: {"dc_bindings_sha256": binding})
    requests = []

    class StubRecorder:
        def __init__(self, *args):
            pass

        def connect(self):
            pass

        def close(self):
            pass

        def call(self, request):
            requests.append(request)
            if request["type"] == "recorder/get_statistics_metadata":
                return [
                    {"statistic_id": "sensor.dc_a", "unit_class": "power",
                     "statistics_unit_of_measurement": "W"},
                    {"statistic_id": "sensor.dc_b", "unit_class": second_class,
                     "statistics_unit_of_measurement": second_unit},
                ]
            assert request["units"] == {"power": "W"}
            # The Recorder normalizes genuine power to the requested W.
            # For other classes it can still return numeric means in source units.
            return {
                name: [{"start": (START + timedelta(minutes=5 * i)).timestamp() * 1000, "mean": value}
                       for i in range(12)]
                for name, value in (("sensor.dc_a", 150.0), ("sensor.dc_b", 100.0))
            }

    monkeypatch.setattr(capture, "MiniWS", StubRecorder)
    series, acquired, verified = capture._fetch_measurements(object(), "https://ha.example", "synthetic", plan, [START])
    snapshot = capture.actual_capture(plan, "digest", DAY, series, captured_at=acquired, dc_bindings_sha256=verified)
    assert snapshot["records"][0]["actual_wh"] == expected_wh
    if expected_wh is None:
        assert snapshot["records"][0]["missing_modules"] == ["B"]
    else:
        assert snapshot["records"][0]["missing_modules"] == []
