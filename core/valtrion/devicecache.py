"""The copies of installed apps that Apple's device service keeps on a Mac, and how many of them valtrion leaves there.

`xcrun devicectl device install app` (the `install` step of the `ios` platform) makes the device service store a copy of every
installed app, so that the next install of the same app sends only what changed. The copies live outside valtrion, in
`~/Library/Containers/com.apple.CoreDevice.CoreDeviceService/Data/Library/Caches/AppInstallationBinaryDeltas/<app id>/<id>`, one folder per
install, about the size of the app, and the service never removes them. A host that installs builds many times a day fills the
volume with them. Everything here is a cache: a removed copy only means that the next install of that app sends the whole app once.

`prune` keeps the newest `keep` copies of each app (by modification time) and removes the rest. The folder belongs to the macOS user
and sits in a protected container: a process without access (no Full Disk Access) reads nothing, and the report then says so
instead of failing. Other systems have no such folder.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Names a folder other than the macOS default (tests, an unusual setup). An empty value turns the cache off.
CACHE_ENV = "VALTRION_DEVICE_INSTALL_CACHE"
DEFAULT_KEEP = 2
_RELATIVE = Path("Library/Containers/com.apple.CoreDevice.CoreDeviceService/Data/Library/Caches/AppInstallationBinaryDeltas")


def default_root() -> Path | None:
    """The folder of the device service's copies, or None where there is none (not macOS, or `CACHE_ENV` is empty)."""
    named = os.environ.get(CACHE_ENV)
    if named is not None:
        return Path(named).expanduser() if named.strip() else None
    if sys.platform != "darwin":
        return None
    return Path.home() / _RELATIVE


def present(root: Path | None) -> bool:
    """Whether the folder is there; a folder that cannot be looked at (no Full Disk Access) counts as there, so that the caller reports it."""
    if root is None:
        return False
    try:
        return root.is_dir()
    except OSError:
        return True


def _tree_bytes(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if not path.is_dir():
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def _newest(path: Path) -> float:
    try:
        newest = path.lstat().st_mtime
    except OSError:
        return 0.0
    if path.is_dir() and not path.is_symlink():
        for folder, folders, files in os.walk(path):
            for name in (*folders, *files):
                try:
                    newest = max(newest, os.lstat(os.path.join(folder, name)).st_mtime)
                except OSError:
                    pass
    return newest


@dataclass(frozen=True)
class Copy:
    app: str
    path: Path
    modified: float


@dataclass
class Listing:
    copies: list[Copy] = field(default_factory=list)
    error: str | None = None  # the folder exists but cannot be read (no Full Disk Access), or another OS error


def listing(root: Path | None) -> Listing:
    """The copies under `root`: one per entry of an app's folder. An absent folder is an empty listing, an unreadable one carries the error."""
    found = Listing()
    if root is None:
        return found
    try:
        apps = sorted(item for item in root.iterdir() if item.is_dir() and not item.is_symlink())
    except FileNotFoundError:
        return found
    except PermissionError:
        found.error = f"{root} cannot be read: the process needs Full Disk Access (macOS Privacy & Security)"
        return found
    except OSError as error:
        found.error = f"{root} cannot be read: {error}"
        return found
    for app in apps:
        try:
            items = list(app.iterdir())
        except OSError as error:
            found.error = f"{app} cannot be read: {error}"
            continue
        found.copies.extend(Copy(app.name, item, _newest(item)) for item in items)
    return found


def size_bytes(root: Path | None) -> int:
    return _tree_bytes(root) if present(root) else 0


@dataclass
class Report:
    removed: list[str] = field(default_factory=list)
    kept: int = 0
    freed: int = 0
    errors: list[str] = field(default_factory=list)


def _inside(root: Path, path: Path) -> bool:
    """Whether the entry itself (a link is judged by where it sits, not by where it points) lies below `root`."""
    try:
        place = path.parent.resolve(strict=False) / path.name
        return place.is_relative_to(root.resolve(strict=False)) and place != root.resolve(strict=False)
    except OSError:
        return False


def prune(root: Path | None, keep: int | None = DEFAULT_KEEP, dry_run: bool = False) -> Report:
    """Remove all but the `keep` newest copies of each app under `root`. `keep` None turns it off; 0 removes every copy. Nothing outside
    `root` is touched (a link is never followed), and a copy that cannot be removed is reported, not fatal."""
    report = Report()
    if root is None or keep is None:
        return report
    found = listing(root)
    if found.error:
        report.errors.append(found.error)
    by_app: dict[str, list[Copy]] = {}
    for copy in found.copies:
        by_app.setdefault(copy.app, []).append(copy)
    for copies in by_app.values():
        copies.sort(key=lambda item: (item.modified, item.path.name), reverse=True)
        report.kept += min(keep, len(copies))
        for copy in copies[keep:]:
            if not _inside(root, copy.path):
                report.errors.append(f"{copy.path} is outside {root}: not removed")
                continue
            size = _tree_bytes(copy.path)
            if not dry_run:
                try:
                    if copy.path.is_dir() and not copy.path.is_symlink():
                        shutil.rmtree(copy.path)
                    else:
                        copy.path.unlink()
                except OSError as error:
                    report.errors.append(f"{copy.path}: {type(error).__name__}: {error}")
                    report.kept += 1
                    continue
            report.removed.append(str(copy.path))
            report.freed += size
    return report
