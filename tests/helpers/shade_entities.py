"""Shared shade-entity fakes; no imports from other test modules."""
from __future__ import annotations

from datetime import date


class _FakeEntry:
    entry_id = "abc123"


def _bare(cls, coordinator, **attrs):
    """Instantiate an entity bypassing HA's CoordinatorEntity.__init__."""
    obj = cls.__new__(cls)
    obj.coordinator = coordinator
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


class _ShadeCoordinator:
    """Minimal stand-in exposing exactly the shade-profile surface the three
    entities read/write. ``builder`` selects the ``build_shade_profile`` shape:
    ``"return"`` returns ``profile``, ``"raise"`` raises, ``"missing"`` omits
    the attribute entirely (the getattr fallback path)."""

    def __init__(
        self,
        *,
        plane_names=(),
        module="",
        sp_date=None,
        profile=None,
        builder="return",
    ) -> None:
        self.entry = _FakeEntry()
        self.last_update_success = True
        self._plane_names = list(plane_names)
        self.shade_profile_module = module
        self.shade_profile_date = sp_date
        self._profile = profile
        self.module_set: list[str] = []
        self.date_set: list[date] = []
        if builder == "return":
            self.build_shade_profile = lambda: self._profile
        elif builder == "raise":
            def _boom():
                raise RuntimeError("diagram build blew up")

            self.build_shade_profile = _boom
        # builder == "missing": leave the attribute unset (getattr -> None).

    def shade_profile_plane_names(self) -> list[str]:
        return list(self._plane_names)

    def set_shade_profile_module(self, module: str) -> None:
        self.module_set.append(module)
        self.shade_profile_module = module

    def set_shade_profile_date(self, day: date) -> None:
        self.date_set.append(day)
        self.shade_profile_date = day
