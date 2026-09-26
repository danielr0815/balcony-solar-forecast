"""Late labels must not overwrite a newer health verdict during catch-up."""

from datetime import date
from types import SimpleNamespace

from custom_components.balcony_solar_forecast._channel_health import (
    _record_actuals_outcome,
)


def test_older_acceptance_keeps_newer_discard_verdict():
    health = {"discard_streak": 3, "last_discard_day": "2026-07-05",
              "last_discard_reason": "low_coverage", "last_accepted_day": "2026-07-01"}
    writes = []
    coord = SimpleNamespace(_store=SimpleNamespace(
        get_learning_health=lambda: health, set_learning_health=writes.append),
        _delete_repair_issue=lambda _: None)
    _record_actuals_outcome(coord, date(2026, 7, 3), accepted=True)
    assert writes == [], "an older filled gap cannot clear the newer outage verdict"


def test_older_discard_keeps_newer_success_verdict():
    health = {"discard_streak": 0, "last_discard_day": None,
              "last_accepted_day": "2026-07-05"}
    writes = []
    coord = SimpleNamespace(_store=SimpleNamespace(
        get_learning_health=lambda: health, set_learning_health=writes.append,
        get_issued=lambda _: {"status": "fresh"}),
        _last_actuals_dropout={"reason": "low_coverage", "modules": ["M1"]},
        _delete_repair_issue=lambda _: None)
    _record_actuals_outcome(coord, date(2026, 7, 3), accepted=False)
    assert writes == [], "an old unresolved gap cannot become today's new outage"


def test_late_eta_outlier_cannot_revive_cleared_watchdog():
    from custom_components.balcony_solar_forecast._channel_health import (
        _record_eta_calibration_outcome,
    )

    health = {"eta_oob_streak": 0, "eta_oob_last_day": None,
              "eta_last_checked_day": "2026-07-05"}
    writes = []
    coord = SimpleNamespace(_store=SimpleNamespace(
        get_learning_health=lambda: health, set_learning_health=writes.append,
        get_issued=lambda _: {"status": "fresh"}),
        _delete_repair_issue=lambda _: None)
    _record_eta_calibration_outcome(coord, date(2026, 7, 3), median_ratio=0.5)
    assert writes == [], "historical catch-up must not revive a newer cleared eta warning"
