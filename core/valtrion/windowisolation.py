"""Keep the windows that tests open off the user's screen on macOS.

A Unity Editor started in `-batchmode` shows no main window, but a test can open an `EditorWindow` (a chart, a form, a utility window, a
popup), and on macOS that window appears over the user's work, flickers and now and then takes the keyboard. macOS has no second desktop
inside one login session, so `unity run` cannot start the Editor "somewhere else" as it does on Windows (`hiddendesktop`). It asks the Editor
to keep its own windows out of sight instead: it gives it the environment variable `GDV_ISOLATE_WINDOWS=1`, and the Editor part of the Unity
package (`Valtrion.Unity.Editor.WindowIsolation`) then forbids the Editor process to become the active application and makes every window of
the process transparent and deaf to the mouse before it is shown. The windows are still created and drawn, so the tests run in full.

Needs the Unity package of the project to contain that assembly; without it the variable does nothing. The same setting as on Windows
(`--[no-]hidden-desktop`, `host.json` `hiddenDesktop`) turns it on and off."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

ENVIRONMENT_VARIABLE = "GDV_ISOLATE_WINDOWS"


def applies() -> bool:
    """Whether this system hides test windows by asking the Editor (macOS)."""
    return sys.platform == "darwin"


def environment(base: Mapping[str, str], enabled: bool) -> dict[str, str]:
    """`base` for the Editor: with the variable set to 1 when `enabled` and this is macOS, otherwise without it (a value inherited from
    the caller's environment must not switch the isolation on against the setting)."""
    result = {key: value for key, value in base.items() if key != ENVIRONMENT_VARIABLE}
    if enabled and applies():
        result[ENVIRONMENT_VARIABLE] = "1"
    return result


PACKAGE = "com.goryned.valtrion.unity"
MINIMUM_PACKAGE = (0, 13, 0)
_TAG = re.compile(r"unity-v(\d+)\.(\d+)\.(\d+)")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _old(version: tuple[int, ...]) -> str:
    return (f"window isolation: the project has {PACKAGE} {'.'.join(map(str, version))}; version "
            f"{'.'.join(map(str, MINIMUM_PACKAGE))} or newer hides test windows")


def note(project: Path, unity_args: Sequence[str]) -> str | None:
    """A line for the user when the isolation cannot work as asked, else None. It needs `-batchmode` and the Unity package 0.13 or newer
    in the project. The package is looked for as an embedded folder (`Packages/<name>/package.json`, its `version` counts) and as a
    dependency of `Packages/manifest.json` (a Git tag `unity-vX.Y.Z` counts; a local path or a commit can only be assumed new enough)."""
    if "-batchmode" not in unity_args:
        return "window isolation: Unity is not started with -batchmode, so the Editor ignores it and test windows may show on the screen"
    embedded = project / "Packages" / PACKAGE / "package.json"
    try:
        version = _VERSION.match(str(json.loads(embedded.read_text(encoding="utf-8")).get("version", "")))
    except (OSError, ValueError, AttributeError):
        version = None
    if version:
        numbers = tuple(int(part) for part in version.groups())
        return _old(numbers) if numbers < MINIMUM_PACKAGE else None
    try:
        dependencies = json.loads((project / "Packages" / "manifest.json").read_text(encoding="utf-8")).get("dependencies", {})
    except (OSError, ValueError, AttributeError):
        return None  # not a project we can read; Unity will complain by itself
    if not isinstance(dependencies, dict) or PACKAGE not in dependencies:
        return f"window isolation: the project does not use {PACKAGE}; test windows may show on the screen"
    tag = _TAG.search(str(dependencies[PACKAGE]))
    if tag:
        numbers = tuple(int(part) for part in tag.groups())
        if numbers < MINIMUM_PACKAGE:
            return _old(numbers)
    return None
