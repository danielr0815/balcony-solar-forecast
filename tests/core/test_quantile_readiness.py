"""Night slots and repeated/undated labels cannot inflate reported readiness."""
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from balcony_solar_forecast.core.quantile_readiness import (
    dated_evidence_days,
    positive_slot_readiness,
)
from balcony_solar_forecast.core.types import QuantileState


def test_positive_slots_count_partial_cold_and_trained_separately():
    result = positive_slot_readiness([
        (datetime(2026, 9, 1, 0, tzinfo=UTC), 0, False),
        (datetime(2026, 9, 1, 8, tzinfo=UTC), 100, True),
        (datetime(2026, 9, 1, 9, tzinfo=UTC), 100, False),
        (datetime(2026, 9, 2, 8, tzinfo=UTC), 100, False),
        (datetime(2026, 9, 3, 8, tzinfo=UTC), 100, True),
        (datetime(2026, 9, 3, 9, tzinfo=UTC), float('nan'), True),
    ], tz=UTC)
    assert result['2026-09-01'] == {'positive_slots': 2, 'trained_slots': 1,
                                    'trained_fraction': .5, 'state': 'partial'}
    assert result['2026-09-02']['state'] == 'cold'
    assert result['2026-09-03']['state'] == 'trained'
    assert positive_slot_readiness([], tz=UTC) == {}


def test_local_date_boundary_and_dated_days_do_not_claim_independence():
    slot = datetime(2026, 9, 1, 23, tzinfo=UTC)
    assert '2026-09-02' in positive_slot_readiness([(slot, 1, True)], tz=ZoneInfo('Europe/Berlin'))
    state = QuantileState(bins={'a': [['2026-09-01', 1]]*20+[['', 1], 1, ['bad', 1],
                               ['2026-12-01', 1], ['2025-01-01', 1], ['2026-09-02', float('nan')]],
                               'b': [['2026-09-01', 1], ['2026-09-03', 1]]})
    assert dated_evidence_days(state, date(2026, 9, 30)) == 2
