"""Nightly decisions stay date-keyed, retryable and quarantined on collapse."""
from datetime import date
from itertools import product

from balcony_solar_forecast.core.training_plan import TrainingContext, plan_training


def test_exhaustive_boolean_contract_and_freeze_order():
    day = date(2026, 9, 30)
    for trained, issued, actual, collapse, frozen in product([False, True], [False, True],
            [False, True], [False, True], [None, '2026-09-29', '2026-10-01', '2026-10-02']):
        context = TrainingContext(day, trained, issued, actual, collapse, frozen)
        result = plan_training(context)
        assert result.rollback_snapshot == (not trained)
        assert result.mark_consumed == (not trained and issued and actual)
        assert result.train_geometric == result.train_empirical == (not trained and not collapse)
        if trained:
            assert not result.freeze_changed and result.freeze_date == frozen
        elif collapse:
            assert result.freeze_date == '2026-10-01'
        elif frozen in ('2026-09-29', '2026-10-01'):
            assert result.freeze_date is None
        else:
            assert result.freeze_date == frozen
