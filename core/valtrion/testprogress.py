"""How far a Unity test run has come, as the Unity side reports it.

`valtrion unity run` gives the Editor the path of a file in the environment variable `GDV_PROGRESS_FILE`; the Editor part of the Unity
package (`Valtrion.Unity.Editor.Progress`) keeps it up to date with the number of tests, how many are done, passed, failed and skipped and
the test that runs now (`gdv.testprogress/1`). The dashboard and the progress window read it. Without the Unity package there is no file
and the views show a moving bar instead of a counter."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import core

SCHEMA = "gdv.testprogress/1"
ENVIRONMENT_VARIABLE = "GDV_PROGRESS_FILE"
FOLDER = "progress"


def folder(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / FOLDER


def path_for(session: str, home: Path | None = None) -> Path:
    """The progress file of one `unity run` (its lease session, `unity-run-<pid>`)."""
    return folder(home) / f"{core.safe_segment(session)}.json"


def read(path: Path) -> dict[str, Any] | None:
    """The progress in `path` as numbers that can be shown, or None when there is no readable, sensible file."""
    try:
        document = core.read_json(path, attempts=2)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return None
    try:
        total, done = int(document["total"]), int(document["done"])
        result = {
            "state": str(document.get("state") or ""), "total": total, "done": done, "passed": int(document.get("passed", 0)),
            "failed": int(document.get("failed", 0)), "skipped": int(document.get("skipped", 0)), "current": str(document.get("current") or ""),
            "utc": str(document.get("utc") or ""), "startedUtc": str(document.get("startedUtc") or ""),
            "mainUtc": str(document.get("mainUtc") or "")}
    except (KeyError, TypeError, ValueError):
        return None
    if total < 0 or done < 0:
        return None
    result["fraction"] = min(1.0, done / total) if total else None
    return result
