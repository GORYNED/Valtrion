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

import sys
from typing import Mapping

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
