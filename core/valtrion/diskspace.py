"""Free disk space of a build host: clean up before a build when space is low, and say so before the toolchain fails.

`host.json` `disk.minFreeGiB` turns this on (without it nothing here runs and builds start as before). Before a `build` stage the
pipeline checks the free space of the work root's volume; below the threshold it removes, in this order, what no job needs:
abandoned job folders of the work root (`workroot.sweep`; a running job, a retained run and a protected path are never touched),
expired idle scratch copies (`scratch.sweep`; one in use or of another host user is never touched), run folders older than `disk.keepDaysUnderPressure` (`artifacts.gc`; active, retained and `--protect`ed runs are kept) and
published builds beyond the build-cache retention (`buildcache.gc`). Still below the threshold afterwards: the build is `BLOCKED`
with the numbers and the largest consumers, and no toolchain starts. With a wait (`disk.waitSec`, `--disk-wait`) the pipeline first
waits, before it takes the project and Editor leases, up to the limit for the space to appear (`wait_for_space`).
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import artifacts, buildcache, config, core, incremental, jobs, lease, scratch, workroot

GIB = 1024 ** 3
#: Under pressure a job folder untouched for this long counts as abandoned (the usual sweep waits `workroot.STALE_JOB_DIRECTORY_SEC`).
PRESSURE_STALE_SEC = 3600.0
DEFAULT_KEEP_DAYS_UNDER_PRESSURE = 2.0
#: A job that waits for free space checks (and cleans up) this often.
POLL_SEC = 30.0


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


#: Written into a job's run folder when it publishes, reuses or installs a build: the key that job still needs.
BUILD_KEY_FILE = "build-key.json"


def remember_build(run_folder: Path, key: str) -> None:
    """Record, in the run folder of a job, the published build it depends on, so that no cleanup removes it while the job is active."""
    keys = _read_keys(run_folder)
    if key not in keys:  # a pipeline may use several builds (an install by key, then a build): every one stays protected
        keys.append(key)
    try:
        core.atomic_write_json(run_folder / BUILD_KEY_FILE, {"keys": keys})
    except OSError:
        pass  # bookkeeping only: the one-hour guard of the build cache still applies


def _read_keys(run_folder: Path) -> list[str]:
    try:
        document = core.read_json(run_folder / BUILD_KEY_FILE, attempts=2)
        found = document["keys"] if "keys" in document else [document["key"]]  # `key` is the form of 0.8.2
        return [str(item) for item in found]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def active_build_keys(home: Path | None = None, active: Sequence[str] | None = None) -> list[str]:
    """Keys of the published builds that jobs which are still active depend on (built and waiting for the device, or installing by key).
    `active` is the list of active job ids when the caller already has it (finding it reads the whole job index)."""
    keys: list[str] = []
    for job_id in (jobs.active_job_ids(home) if active is None else active):
        try:
            keys.extend(_read_keys(jobs.find_job_directory(job_id, home)))
        except jobs.JobError:
            continue
    return keys


def _gib(value: float) -> str:
    return f"{value / GIB:.1f} GiB"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: str | None = None
    removed: tuple[str, ...] = ()
    free_before: int = 0
    free_after: int = 0
    cancelled: bool = False  # the wait for space ended because the job was cancelled


def largest_consumers(settings: config.HostSettings, home: Path | None, limit: int = 3) -> list[tuple[str, int]]:
    """The biggest things the host keeps, for the BLOCKED reason: job folders of the work root, then the three top-level roots."""
    items: list[tuple[str, int]] = []
    roots = settings.roots
    if roots.jobs.is_dir():
        for folder in roots.jobs.iterdir():
            if folder.is_dir():
                items.append((str(folder), folder_bytes(folder)))
    if roots.scratch.is_dir() and not scratch.is_link(roots.scratch):
        for folder in roots.scratch.iterdir():
            if folder.is_dir() and not scratch.is_link(folder):
                items.append((f"{folder} (scratch)", folder_bytes(folder)))
    items.append((f"{settings.artifacts_root} (run folders)", folder_bytes(settings.artifacts_root) if settings.artifacts_root.is_dir() else 0))
    cache = buildcache.builds_root(home)
    items.append((f"{cache} (build cache)", folder_bytes(cache) if cache.is_dir() else 0))
    for folder in incremental.folders(home):
        items.append((f"{folder} (incremental build)", folder_bytes(folder)))
    return sorted(items, key=lambda item: item[1], reverse=True)[:limit]


def ensure_free(settings: config.HostSettings, home: Path | None, protected: Sequence[str] = (),
                free: Callable[[Path], int] | None = None, keep_incremental: bool = False) -> Outcome:
    """Make sure the work root's volume has `settings.min_free_gib` free; clean up when it has not. Returns `ok` when there is enough
    space (or no threshold is set), otherwise the BLOCKED reason. `free` is injectable for tests (a fake volume). With
    `keep_incremental` the persistent build folders are left alone (a job that is only waiting for space does not hold its checkout's
    lease, so its own folder would look idle)."""
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
        # Expired scratch copies of any owner of this host user (the owner turned this cleanup on with disk.minFreeGiB); one in use,
        # unexpired or of another host user stays, as in `workroot sweep`.
        ("expired scratch directories", lambda: scratch.sweep(settings.roots, scratch.caller(), active, PRESSURE_STALE_SEC, any_owner=True,
                                                              manager=lease.LeaseManager(home or core.valtrion_home())).removed),
        ("expired run folders", lambda: artifacts.gc(settings.artifacts_root, settings.keep_days_under_pressure,
                                                    protected=protected_paths).removed),
        ("expired builds", lambda: buildcache.gc(buildcache.builds_root(home), protect=active_build_keys(home, active),
                                                  **settings.build_cache.limits()).removed)]
    if not keep_incremental:
        # Last: the next build there compiles everything again, so it goes only when nothing else was enough.
        steps.append(("idle incremental build folders", lambda: release_idle_incremental(home)))
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


def releasable_bytes(settings: config.HostSettings, home: Path | None, own_job_id: str) -> int:
    """Bytes in the work folders of the other active jobs: what the volume gets back when they all end (their build output and
    export are removed with the job). Run folders, published builds and persistent build folders stay, so they are not counted."""
    total = 0
    for job_id in jobs.active_job_ids(home):
        folder = settings.roots.jobs / job_id
        if job_id != own_job_id and folder.is_dir():
            total += folder_bytes(folder)
    return total


def wait_for_space(settings: config.HostSettings, home: Path | None, protected: Sequence[str], wait_sec: float, own_job_id: str,
                   on_wait: Callable[[Outcome, float], None] | None = None, cancelled: Callable[[], bool] = lambda: False,
                   free: Callable[[Path], int] | None = None, poll_sec: float | None = None) -> Outcome:
    """Like `ensure_free`, but when the space is still short after the cleanup, wait up to `wait_sec` for it, cleaning up and
    checking again every `poll_sec`. Ends at once, without waiting, when the space could not suffice even if every other active job
    finished (`releasable_bytes`) and the idle persistent build folders were released: waiting cannot help then. Those folders are
    not removed here (the job holds no lease on its own checkout yet): when they could close the gap, the outcome is `ok` and the
    check under the leases, which releases them, has the last word. `on_wait(outcome, waited_sec)` is called before each pause."""
    clock = time.monotonic
    started = clock()
    need = (settings.min_free_gib or 0) * GIB
    removed: list[str] = []
    interval = POLL_SEC if poll_sec is None else poll_sec
    while True:
        if cancelled():
            return Outcome(False, "cancelled by request", tuple(removed), cancelled=True)
        outcome = ensure_free(settings, home, protected, free, keep_incremental=True)
        removed.extend(outcome.removed)
        if outcome.ok:
            return Outcome(True, None, tuple(removed), outcome.free_before, outcome.free_after)
        waited = clock() - started
        if outcome.free_after + idle_incremental_bytes(home) >= need:
            return Outcome(True, None, tuple(removed), outcome.free_before, outcome.free_after)
        coming = releasable_bytes(settings, home, own_job_id)
        if outcome.free_after + coming < need:
            return Outcome(False, f"{outcome.reason}; waiting cannot help: even when every other active job ends ({_gib(coming)} in "
                                  "their work folders) the volume stays below the minimum", tuple(removed), outcome.free_before, outcome.free_after)
        if waited >= wait_sec:
            return Outcome(False, f"{outcome.reason}; no space appeared within the {wait_sec:g} s wait (host.json disk.waitSec, "
                                  "--disk-wait)", tuple(removed), outcome.free_before, outcome.free_after)
        if on_wait is not None:
            on_wait(outcome, waited)
        pause_until = clock() + min(interval, max(0.01, wait_sec - waited))
        while clock() < pause_until and not cancelled():  # a cancel is noticed within a second, not after a whole poll
            time.sleep(max(0.0, min(1.0, pause_until - clock())))


def idle_incremental_folders(home: Path | None) -> list[Path]:
    """The persistent build folders whose checkout nobody builds in right now (their `unity-project:` lease is free). A folder that
    cannot be shown to be idle is not listed."""
    manager = lease.LeaseManager(home or core.valtrion_home())
    found: list[Path] = []
    for folder in incremental.folders(home):
        try:
            resource = core.read_json(folder / incremental.OWNER).get("leaseResource")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(resource, str) and manager.status(resource).state == "free":
            found.append(folder)
    return found


def idle_incremental_bytes(home: Path | None) -> int:
    return sum(folder_bytes(folder) for folder in idle_incremental_folders(home))


def release_idle_incremental(home: Path | None) -> list[str]:
    """Remove the export and DerivedData of the persistent build folders whose checkout nobody builds in right now; the next build
    there starts clean."""
    return [f"{folder} (incremental build)" for folder in idle_incremental_folders(home) if incremental.discard(folder)]


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
