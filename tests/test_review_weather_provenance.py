"""Live coordinator provenance exposes failures outside the HTTP fetch path."""

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from tests.helpers.weather import _coord  # noqa: E402


@pytest.mark.parametrize("provider_error,expected", [
    (None, "Weather is older than the physics fallback horizon"),
    ("provider offline", "provider offline"),
])
def test_failed_update_reports_provider_error_or_coordinator_exception(provider_error, expected):
    coord = _coord()
    coord.data = {"status": "fresh"}
    coord.last_update_success = False
    coord.last_exception = UpdateFailed("Weather is older than the physics fallback horizon")
    coord._last_error = provider_error

    provenance = coord.forecast_provenance

    assert provenance["last_error"] == expected
    assert provenance["available"] is False
    assert provenance["source_status"] == "unavailable"


def test_success_does_not_resurrect_a_previous_coordinator_exception():
    coord = _coord()
    coord.data = {"status": "fresh"}
    coord.last_update_success = True
    coord.last_exception = UpdateFailed("old failure")
    assert coord.forecast_provenance["last_error"] is None
