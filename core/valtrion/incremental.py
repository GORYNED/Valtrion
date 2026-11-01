"""The persistent build folder of a managed checkout: what makes the second build after a small change take minutes.

A job normally exports a fresh player into its work folder and deletes it, so IL2CPP C++ and Xcode compile everything every time.
A managed checkout instead keeps `<valtrion home>/incremental/<project>/<checkout id>/` with the exported project (`export/`, written
by Unity with Append, so only changes are exported) and Xcode's `DerivedData/`; the IL2CPP build cache lives in the checkout's
`Library/`, which a checkout reset keeps. The folder is used only by a build that holds the checkout's `unity-project:` lease.

The folder is reused while its *key* is unchanged. The key covers the toolchain (Unity, Xcode), the build profile with its settings
in `validation/device/profiles.json` and the device the build is signed for; any change, or a request (`--clean`), makes the next
build start from nothing. A build that was killed half way (timeout, stall) forgets the key, so the next one starts clean too.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import core

STATE = "state.json"
EXPORT = "export"
DERIVED_DATA = "DerivedData"
OWNER = "owner.json"


def root_of(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / "incremental"


def folder_of(home: Path | None, project: str, checkout_id: str) -> Path:
    return root_of(home) / core.safe_segment(project, "project", 60) / core.safe_segment(checkout_id, "checkout", 40)


@dataclass
class Plan:
    folder: Path
    key: str
    clean: bool = False  # a clean rebuild was asked for


@dataclass
class Prepared:
    export: Path
    derived: Path
    append: bool  # Unity exports on top of the previous export
    note: str  # what the report says about it


def prepare(plan: Plan, lease_resource: str | None = None) -> Prepared:
    """Make `plan.folder` ready for a build: keep what the key allows, remove the rest. The state is removed until the build
    succeeds (`commit`), so a build that dies leaves a folder the next build does not trust."""
    folder = plan.folder
    folder.mkdir(parents=True, exist_ok=True)
    if lease_resource:
        core.atomic_write_json(folder / OWNER, {"leaseResource": lease_resource})
    try:
        state = core.read_json(folder / STATE)
    except (OSError, ValueError):
        state = None
    previous = state.get("key") if isinstance(state, dict) else None
    export, derived = folder / EXPORT, folder / DERIVED_DATA
    if plan.clean:
        reason = "a clean rebuild was requested"
    elif previous is None:
        reason = "first build here" if not export.exists() else "the last build here did not finish"
    elif previous != plan.key:
        reason = "the toolchain, build profile or signing changed"
    else:
        reason = None
    if reason:
        discard(folder)
    try:
        (folder / STATE).unlink()
    except OSError:
        pass
    export.mkdir(parents=True, exist_ok=True)
    reused = reason is None and any(export.glob("*.xcodeproj"))
    note = "reused the previous export and DerivedData" if reused else f"clean build ({reason or 'no previous export'})"
    return Prepared(export, derived, reused, note)


def commit(plan: Plan) -> None:
    """The build succeeded: the folder may be reused by a build with the same key."""
    core.atomic_write_json(plan.folder / STATE, {"key": plan.key, "updatedUtc": core.utc_now()})


def discard(folder: Path) -> int:
    """Remove the export and DerivedData of `folder` (the next build there starts clean); returns the bytes freed."""
    freed = 0
    for name in (EXPORT, DERIVED_DATA, STATE):
        path = folder / name
        if path.is_dir():
            freed += _size(path)
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    return freed


def _size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def folders(home: Path | None = None) -> list[Path]:
    """Every persistent build folder: `<root>/<project>/<checkout id>`."""
    base = root_of(home)
    if not base.is_dir():
        return []
    return sorted(path for project in base.iterdir() if project.is_dir() for path in project.iterdir() if path.is_dir())
