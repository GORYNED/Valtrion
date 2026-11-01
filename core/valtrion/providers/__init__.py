"""Registry of platform adapters.

An adapter module (`valtrion.providers.ios`, `android`, `windows`, `macos`, `editor`) calls `register(platform, factory)` when it is
imported. `load_known()` imports the modules that exist and ignores the ones that do not; a failure inside an existing module is
reported by name instead of hidden. Tests register fakes with `register` and remove them with `unregister`.
"""

from __future__ import annotations

import importlib
from typing import Callable

from ..adapter import PlatformAdapter

KNOWN = ("editor", "windows", "macos", "ios", "android")

_FACTORIES: dict[str, Callable[[], PlatformAdapter]] = {}
_ERRORS: dict[str, str] = {}


def register(platform: str, factory: Callable[[], PlatformAdapter]) -> None:
    _FACTORIES[platform] = factory


def unregister(platform: str) -> None:
    _FACTORIES.pop(platform, None)


def load_known() -> None:
    for name in KNOWN:
        if name in _FACTORIES:
            continue
        module = f"{__name__}.{name}"
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as error:
            if error.name != module:  # the adapter exists but one of its imports is missing
                _ERRORS[name] = f"{type(error).__name__}: {error}"
        except Exception as error:  # noqa: BLE001 - one broken adapter must not hide the others
            _ERRORS[name] = f"{type(error).__name__}: {error}"


def get(platform: str) -> PlatformAdapter | None:
    load_known()
    factory = _FACTORIES.get(platform)
    return factory() if factory else None


def platforms() -> list[str]:
    load_known()
    return sorted(_FACTORIES)


def load_errors() -> dict[str, str]:
    return dict(_ERRORS)
