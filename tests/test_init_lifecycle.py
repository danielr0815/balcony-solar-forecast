"""Lifecycle tests for the integration's ``__init__`` plumbing.

Covered here: ``async_remove_entry`` — beyond deleting the persisted store it
must sweep the entry's REPAIR ISSUES. Every issue the coordinator raises is
entry-scoped with the ``_{entry_id}`` suffix (coordinator._issue_id_for), and
HA's issue registry keeps issues after the entry is gone, so without the
sweep a re-installed entry would inherit stale, unactionable warnings.

Uses fakes only (same pattern as tests/test_learning_visibility.py): a stand-in
``ir`` module whose registry is keyed ``(domain, issue_id)`` like HA's, and a
stand-in ``ForecastStore`` so no real storage is touched.
"""

from __future__ import annotations

import custom_components.balcony_solar_forecast as init_mod
from custom_components.balcony_solar_forecast.const import DOMAIN


class _FakeIssueRegistry:
    """``ir.async_get(hass)`` return value: issues keyed ``(domain, issue_id)``."""

    def __init__(self, issues: dict[tuple[str, str], object]) -> None:
        self.issues = dict(issues)
        self.deleted: list[tuple[str, str]] = []

    def delete(self, domain: str, issue_id: str) -> None:
        self.deleted.append((domain, issue_id))
        self.issues.pop((domain, issue_id), None)


class _FakeIr:
    """The ``homeassistant.helpers.issue_registry`` module, reduced to what
    ``async_remove_entry`` uses."""

    def __init__(self, registry: _FakeIssueRegistry) -> None:
        self._registry = registry

    def async_get(self, hass) -> _FakeIssueRegistry:
        return self._registry

    def async_delete_issue(self, hass, domain: str, issue_id: str) -> None:
        self._registry.delete(domain, issue_id)


class _FakeStore:
    """``ForecastStore`` stand-in recording the remove call."""

    def __init__(self, hass, entry_id: str) -> None:
        self.entry_id = entry_id
        self.removed = False

    async def async_remove(self) -> None:
        self.removed = True


class _FakeHass:
    pass


class _FakeEntry:
    entry_id = "entry42"


async def test_remove_entry_deletes_store_and_own_repair_issues(monkeypatch):
    """Only THIS entry's issues go: another entry's issues of our domain and
    same-suffix issues of a foreign domain must survive."""
    issues: dict[tuple[str, str], object] = {
        (DOMAIN, "actual_entity_missing_entry42"): object(),
        (DOMAIN, "learning_stalled_dead_channel_entry42"): object(),
        (DOMAIN, "actual_entity_missing_otherentry"): object(),
        ("other_integration", "some_issue_entry42"): object(),
    }
    registry = _FakeIssueRegistry(issues)
    store_holder: dict[str, _FakeStore] = {}

    def _store(hass, entry_id):
        store_holder["store"] = _FakeStore(hass, entry_id)
        return store_holder["store"]

    monkeypatch.setattr(init_mod, "ForecastStore", _store)
    # raising=False: against the pre-fix code there is no ``ir`` reference yet —
    # the RED state must be the missing sweep (assertions below), not a patch
    # error.
    monkeypatch.setattr(init_mod, "ir", _FakeIr(registry), raising=False)

    await init_mod.async_remove_entry(_FakeHass(), _FakeEntry())

    assert store_holder["store"].removed is True
    assert registry.deleted == [
        (DOMAIN, "actual_entity_missing_entry42"),
        (DOMAIN, "learning_stalled_dead_channel_entry42"),
    ]
    assert set(registry.issues) == {
        (DOMAIN, "actual_entity_missing_otherentry"),
        ("other_integration", "some_issue_entry42"),
    }
