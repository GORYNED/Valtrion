"""The persistent build folder of a managed checkout: what makes the second build after a small change take minutes.

A job normally exports a fresh player into its work folder and deletes it, so IL2CPP C++ and Xcode compile everything every time.
A managed checkout instead keeps `<valtrion home>/incremental/<project>/<checkout id>/` with the exported project (`export/`, written
by Unity with Append, so only changes are exported) and Xcode's `DerivedData/`; the IL2CPP build cache lives in the checkout's
`Library/`, which a checkout reset keeps. The folder is used only by a build that holds the checkout's `unity-project:` lease.

The folder is reused while its *key* is unchanged. The key covers the toolchain (Unity, Xcode), the build profile with its settings
in `validation/device/profiles.json` and the device the build is signed for; any change, or a request (`--clean`), makes the next
build start from nothing. A build that was killed half way (timeout, stall) forgets the key, so the next one starts clean too.

Unity's Append export writes every generated source again, with a fresh mtime and mostly the same bytes, and the native builds
(Bee for IL2CPP, Xcode) compare mtimes. So `export-manifest.json` keeps path -> size, mtime and content hash of the export, and
after each export `settle` gives a file that was rewritten with the same bytes its old mtime back.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from . import core

STATE = "state.json"
EXPORT = "export"
DERIVED_DATA = "DerivedData"
OWNER = "owner.json"
MANIFEST = "export-manifest.json"  # beside the export, never in it: path -> size, mtime, hash of the last settled export


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


def _hash_file(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=20)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _walk(export: Path):
    """Every regular file of the export as (relative POSIX path, path, stat); symlinks are left alone."""
    for current, _dirs, names in os.walk(export):
        for name in names:
            path = Path(current) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield path.relative_to(export).as_posix(), path, info


def load_manifest(folder: Path) -> dict[str, list]:
    """path -> [size, mtime_ns, hash] of the export as the last build left it; empty when there is none or it cannot be read."""
    try:
        data = core.read_json(folder / MANIFEST)
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {path: entry for path, entry in files.items() if isinstance(entry, list) and len(entry) == 3}


def snapshot(folder: Path, export: Path) -> dict[str, list]:
    """The export before Unity runs: path -> [size, mtime_ns, hash]. A file whose size and mtime equal the manifest keeps its hash
    (nothing read), any other file is hashed, so a manifest that is out of date is repaired rather than trusted."""
    known = load_manifest(folder)
    result: dict[str, list] = {}
    for rel, path, info in _walk(export):
        entry = known.get(rel)
        if entry is not None and entry[0] == info.st_size and entry[1] == info.st_mtime_ns:
            result[rel] = entry
            continue
        try:
            result[rel] = [info.st_size, info.st_mtime_ns, _hash_file(path)]
        except OSError:
            continue
    return result


def settle(folder: Path, export: Path, before: dict[str, list]) -> dict:
    """Run after the export step. A file Unity wrote again with the same bytes gets its previous mtime back (so Bee, IL2CPP and
    Xcode, which compare mtimes, do not rebuild it); changed and new files keep their new mtime; files that are gone drop out of
    the manifest. Writes the manifest of the export as it is now and returns the counts (overall, by top-level folder and by
    extension) for the report."""
    after: dict[str, list] = {}
    groups: dict[str, dict[str, int]] = {}
    by_ext: dict[str, dict[str, int]] = {}

    def count(rel: str, kind: str, size: int) -> None:
        top = rel.split("/", 1)[0] if "/" in rel else "(root)"
        ext = os.path.splitext(rel)[1].lower() or "(none)"
        for table, key in ((groups, top), (by_ext, ext)):
            row = table.setdefault(key, {})
            row[kind] = row.get(kind, 0) + 1
            row[kind + "Bytes"] = row.get(kind + "Bytes", 0) + size

    present: set[str] = set()
    for rel, path, info in _walk(export):
        present.add(rel)
        old = before.get(rel)
        try:
            if old is not None and old[0] == info.st_size and old[1] == info.st_mtime_ns:
                after[rel] = old  # not touched at all
                count(rel, "untouched", info.st_size)
                continue
            digest = _hash_file(path)
            if old is None:
                after[rel] = [info.st_size, info.st_mtime_ns, digest]
                count(rel, "new", info.st_size)
            elif old[0] == info.st_size and digest == old[2]:
                os.utime(path, ns=(info.st_atime_ns, old[1]))  # rewritten with the same bytes: the old mtime is the truth
                after[rel] = old
                count(rel, "restored", info.st_size)
            else:
                after[rel] = [info.st_size, info.st_mtime_ns, digest]
                count(rel, "changed", info.st_size)
        except OSError:
            count(rel, "unreadable", info.st_size)  # still on disk; the next build hashes it again
    for rel, old in before.items():
        if rel not in present:
            count(rel, "deleted", old[0])
    try:
        core.atomic_write_json(folder / MANIFEST, {"files": after, "updatedUtc": core.utc_now()})
    except OSError:
        pass  # the next build hashes the export again
    totals: dict[str, int] = {}
    for row in groups.values():
        for key, value in row.items():
            totals[key] = totals.get(key, 0) + value
    return {"total": totals, "byFolder": groups, "byExtension": by_ext}


def summary(stats: dict) -> str:
    total = stats["total"]
    parts = [f"{total.get(kind, 0)} {kind}" for kind in ("untouched", "restored", "changed", "new", "deleted")]
    return ", ".join(parts) + f"; {total.get('restoredBytes', 0) // (1 << 20)} MiB had the same bytes and kept their mtime"


def commit(plan: Plan) -> None:
    """The build succeeded: the folder may be reused by a build with the same key."""
    core.atomic_write_json(plan.folder / STATE, {"key": plan.key, "updatedUtc": core.utc_now()})


def discard(folder: Path) -> int:
    """Remove the export and DerivedData of `folder` (the next build there starts clean); returns the bytes freed."""
    freed = 0
    for name in (EXPORT, DERIVED_DATA, STATE, MANIFEST):
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
