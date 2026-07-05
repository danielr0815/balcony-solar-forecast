"""Pytest bootstrap for the pure forecast core.

The integration root ``custom_components/balcony_solar_forecast/__init__.py``
imports Home Assistant (it is the HA glue entry point). Importing any core
submodule the normal way (``import balcony_solar_forecast.core.horizon``)
would execute that root ``__init__`` first and fail with
``ModuleNotFoundError: homeassistant`` outside a HA checkout.

The core itself imports NOTHING from Home Assistant (SPEC §4 hard invariant),
so we make it importable for bare pytest by registering
``balcony_solar_forecast`` as a *namespace-style* package object whose
``__path__`` points at the real directory, WITHOUT running its ``__init__``.
Submodule imports (``.const``, ``.core.horizon``, ...) then resolve straight
to their files. This touches only the test process; the shipped package is
unchanged.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_CUSTOM_COMPONENTS = Path(__file__).resolve().parents[2] / "custom_components"
_PKG_DIR = _CUSTOM_COMPONENTS / "balcony_solar_forecast"


def _register_namespace_package(name: str, path: Path) -> None:
    """Register ``name`` as an empty namespace package rooted at ``path``.

    Skips executing any ``__init__.py``; only the search path is set so
    ``import name.submodule`` finds the submodule files.
    """
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]  # marks it as a package for submodule lookup
    mod.__package__ = name
    sys.modules[name] = mod


# ``custom_components`` is already a PEP-420 namespace package (no __init__).
# We only need to shadow the ``balcony_solar_forecast`` root so its HA-importing
# __init__ never runs during pure-core tests.
if str(_CUSTOM_COMPONENTS) not in sys.path:
    sys.path.insert(0, str(_CUSTOM_COMPONENTS))

_register_namespace_package("balcony_solar_forecast", _PKG_DIR)
_register_namespace_package("balcony_solar_forecast.core", _PKG_DIR / "core")
