"""Test bootstrap.

The pure fetcher tests must import ``fetcher.py`` (which does relative
imports of the HA-free ``const`` and ``core.types``) WITHOUT running the
integration's root ``__init__.py`` — that module imports Home Assistant,
which is not installed on the plain-pytest path (Windows/CI without HA).

We synthesise a lightweight ``balcony_solar_forecast`` package whose
``__init__`` is empty, mapped at the real source directory, then let the
real submodules (``const``, ``core``, ``core.types``, ``fetcher``) load
under it via normal import machinery. ``store.py`` / ``coordinator.py`` /
the root ``__init__`` are NOT imported here (they need HA); their syntax is
covered by ``python -m compileall`` in CI.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_PKG = "balcony_solar_forecast"
_SRC = Path(__file__).resolve().parents[1] / "custom_components" / _PKG


def _install_pure_package() -> None:
    if _PKG in sys.modules:
        return
    # 1) Empty top-level package rooted at the real source dir (so real
    #    submodules resolve) but WITHOUT executing the HA-importing __init__.
    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_SRC)]  # type: ignore[attr-defined]
    pkg.__package__ = _PKG
    sys.modules[_PKG] = pkg

    # 2) Empty core subpackage.
    core = ModuleType(f"{_PKG}.core")
    core.__path__ = [str(_SRC / "core")]  # type: ignore[attr-defined]
    core.__package__ = f"{_PKG}.core"
    sys.modules[f"{_PKG}.core"] = core


def _load(mod_name: str, rel_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, _SRC / rel_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_install_pure_package()
# Order matters: const has no intra-package deps; core.types needs const;
# fetcher needs const + core.types.
_load(f"{_PKG}.const", "const.py")
_load(f"{_PKG}.core.types", "core/types.py")
_load(f"{_PKG}.fetcher", "fetcher.py")
