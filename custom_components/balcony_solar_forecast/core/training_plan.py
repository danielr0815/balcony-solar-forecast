"""Explicit nightly decisions; scheduling and persistence stay in HA glue."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True, slots=True)
class TrainingContext:
    day: date
    already_trained: bool
    has_issued: bool
    has_actuals: bool
    collapsed: bool
    frozen_date: str | None


@dataclass(frozen=True, slots=True)
class TrainingPlan:
    rollback_snapshot: bool
    train_geometric: bool
    train_empirical: bool
    mark_consumed: bool
    freeze_changed: bool
    freeze_date: str | None


def plan_training(context: TrainingContext) -> TrainingPlan:
    """Preserve the scheduler's idempotence, collapse and delayed-label contract."""
    if context.already_trained:
        return TrainingPlan(False, False, False, False, False, context.frozen_date)
    next_day = (context.day+timedelta(days=1)).isoformat()
    frozen = context.frozen_date
    if context.collapsed:
        frozen = next_day
    elif frozen is not None and frozen <= next_day:
        frozen = None
    return TrainingPlan(True, not context.collapsed, not context.collapsed,
                        context.has_issued and context.has_actuals,
                        frozen != context.frozen_date, frozen)
