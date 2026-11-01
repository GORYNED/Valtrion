"""Free disk space of a build host: clean up before a build when space is low, and say so before the toolchain fails.

`host.json` `disk.minFreeGiB` turns this on (without it nothing here runs and builds start as before). Before a `build` stage the
pipeline checks the free space of the work root's volume; below the threshold it removes, in this order, what no job needs:
abandoned job folders of the work root (`workroot.sweep`; a running job, a retained run and a protected path are never touched),
run folders older than `disk.keepDaysUnderPressure` (`artifacts.gc`; active, retained and `--protect`ed runs are kept) and
published builds beyond the build-cache retention (`buildcache.gc`). Still below the threshold afterwards: the build is `BLOCKED`
with the numbers and the largest consumers, and no toolchain starts.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import artifacts, buildcache, config, jobs, workroot

GIB = 1024 ** 3
#: Under pressure a job folder untouched for this long counts as abandoned (the usual sweep waits `workroot.STALE_JOB_DIRECTORY_SEC`).
PRESSURE_STALE_SEC = 3600.0
DEFAULT_KEEP_DAYS_UNDER_PRESSURE = 2.0


def free_bytes(path: Path) -> int:
    """Free bytes of the volume that holds `path` (or its nearest existing parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def folder_bytes(path: Path) -> int:
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def _gib(value: float) -> str:
    return f"{value / GIB:.1f} GiB"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: str | None = None
    removed: tuple[str, ...] = ()
    free_before: int = 0
    free_after: int = 0


def largest_consumers(settings: config.HostSettings, home: Path | None, limit: int = 3) -> list[tuple[str, int]]:
    """The biggest things the host keeps, for the BLOCKED reason: job folders of the work root, then the three top-level roots."""
    items: list[tuple[str, int]] = []
    roots = settings.roots
    if roots.jobs.is_dir():
        for folder in roots.jobs.iterdir():
            if folder.is_dir():
                items.append((str(folder), folder_bytes(folder)))
    items.append((f"{settings.artifacts_root} (run folders)", folder_bytes(settings.artifacts_root) if settings.artifacts_root.is_dir() else 0))
    cache = buildcache.builds_root(home)
    items.append((f"{cache} (build cache)", folder_bytes(cache) if cache.is_dir() else 0))
    return sorted(items, key=lambda item: item[1], reverse=True)[:limit]


def ensure_free(settings: config.HostSettings, home: Path | None, protected: Sequence[str] = (),
                free: Callable[[Path], int] | None = None) -> Outcome:
    """Make sure the work root's volume has `settings.min_free_gib` free; clean up when it has not. Returns `ok` when there is enough
    space (or no threshold is set), otherwise the BLOCKED reason. `free` is injectable for tests (a fake volume)."""
    if not settings.min_free_gib:
        return Outcome(True)
    free = free or free_bytes
    need = settings.min_free_gib * GIB
    try:
        before = free(settings.work_root)
    except OSError:
        return Outcome(True)  # the free space cannot be read: do not stop a build over a check that cannot be made
    if before >= need:
        return Outcome(True, free_before=before, free_after=before)
    removed: list[str] = []
    errors: list[str] = []
    protected_paths = [Path(item) for item in protected]
    active = jobs.active_job_ids(home)
    steps: list[tuple[str, Callable[[], Iterable[str]]]] = [
        ("abandoned job folders", lambda: workroot.sweep(settings.roots, active, PRESSURE_STALE_SEC,
                                                         keep=lambda job_id: jobs.kept_work_reason(job_id, home))),
        ("expired run folders", lambda: artifacts.gc(settings.artifacts_root, settings.keep_days_under_pressure,
                                                    protected=protected_paths).removed),
        ("expired builds", lambda: buildcache.gc(buildcache.builds_root(home)).removed)]
    for name, step in steps:
        try:  # a locked file (Windows) or a folder that vanished meanwhile must not stop the others, nor fail the stage
            removed.extend(step())
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {type(error).__name__}: {error}")
    try:
        after = free(settings.work_root)
    except OSError:
        after = before
    if after >= need:
        return Outcome(True, removed=tuple(removed), free_before=before, free_after=after)
    consumers = "; ".join(f"{path}: {_gib(size)}" for path, size in largest_consumers(settings, home))
    problems = f"; cleanup problems: {'; '.join(errors)}" if errors else ""
    return Outcome(False, f"only {_gib(after)} free on the volume of {settings.work_root}, below the {settings.min_free_gib:g} GiB "
                          f"minimum (host.json disk.minFreeGiB) after removing {len(removed)} abandoned or expired item(s); "
                          f"largest: {consumers}{problems}", tuple(removed), before, after)


def release_paths(paths: Iterable[Path]) -> tuple[int, list[str]]:
    """Remove build output that was published or installed; returns (bytes freed, errors). A missing path is fine."""
    freed, errors = 0, []
    for path in paths:
        try:
            if not path.exists() and not path.is_symlink():
                continue
            size = folder_bytes(path) if path.is_dir() else path.stat().st_size
            artifacts._remove(path)  # noqa: SLF001 - same package
            freed += size
        except OSError as error:
            errors.append(f"{path}: {error}")
    return freed, errors
