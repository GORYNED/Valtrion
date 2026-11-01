"""Artifact layout and cleanup.

Layout: `<artifactsRoot>/<project>/<sha8>/<runId>/{job.json, events.jsonl, result.json, console.log, ...}`. The caller's artifacts
root receives pulled artifacts with the identical relative layout.

Cleanup: a run removes its temporary builds, exports and intermediates after it passes, or
after it fails once results and diagnostics are saved. Only paths the run itself registered as temporary are removed, and never
user data, another task's working copy, a baseline or a retained artifact.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterable

from . import constants, core

TEMP_REGISTRY = "temp-paths.json"
RETAINED_MARKER = ".retained"
RUN_MARKERS = ("job.json", "result.json", "events.jsonl")


@dataclass(frozen=True)
class RunLayout:
    root: Path
    project: str
    sha8: str
    run_id: str

    @property
    def relative(self) -> Path:
        return Path(core.safe_segment(self.project), core.safe_segment(self.sha8), core.safe_segment(self.run_id))

    @property
    def directory(self) -> Path:
        return self.root / self.relative

    @property
    def job_json(self) -> Path:
        return self.directory / "job.json"

    @property
    def events(self) -> Path:
        return self.directory / "events.jsonl"

    @property
    def result(self) -> Path:
        return self.directory / "result.json"

    @property
    def console(self) -> Path:
        return self.directory / "console.log"

    @property
    def spec(self) -> Path:
        return self.directory / "spec.json"

    @property
    def cancel(self) -> Path:
        return self.directory / "cancel.requested"


def sha8_of(sha: str | None) -> str:
    return sha[:8] if sha and len(sha) >= 8 else "unversioned"


# -- temporary paths --------------------------------------------------------------------------------------------------------


def register_temp(layout: RunLayout, path: Path) -> None:
    """Record that this run created `path` and may remove it when the run ends."""
    registry = layout.directory / TEMP_REGISTRY
    try:
        paths = core.read_json(registry)
    except FileNotFoundError:
        paths = []
    entry = str(Path(path).resolve())
    if entry not in paths:
        paths.append(entry)
        core.atomic_write_json(registry, paths)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_worktree(path: Path) -> bool:
    """A folder with a `.git` entry is a working copy (this or another task's); it is never removed."""
    return (path / ".git").exists()


def protection_reason(path: Path, protected: Iterable[Path], artifacts_root: Path) -> str | None:
    """Why `path` must not be removed, or None. Protected paths are baselines and retained artifacts; a path is refused when it
    equals, contains or lies inside one of them."""
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved == resolved.parent or resolved == home or resolved in home.parents:
        return "it is a filesystem root or a parent of the home folder"
    if resolved == artifacts_root.resolve() or resolved in artifacts_root.resolve().parents:
        return "it is the artifacts root or a parent of it"
    # A temporary export may live inside the run's own working copy, so only a path that is itself a working copy is refused.
    if is_worktree(resolved):
        return "it is a working copy"
    return _overlap(resolved, protected)


def _overlap(resolved: Path, protected: Iterable[Path]) -> str | None:
    for item in protected:
        item = item.resolve()
        if resolved == item or _is_within(resolved, item) or _is_within(item, resolved):
            return f"it overlaps the protected path {item}"
    return None


def _make_writable(function: Callable, target: str, _error) -> None:
    os.chmod(target, stat.S_IWRITE)
    function(target)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_make_writable)
        else:
            shutil.rmtree(path, onerror=_make_writable)
    elif path.exists() or path.is_symlink():
        path.unlink()


@dataclass(frozen=True)
class CleanupReport:
    removed: tuple[str, ...] = ()
    kept: tuple[tuple[str, str], ...] = ()  # (path, reason)
    skipped_reason: str | None = None  # cleanup did not run at all


def cleanup_run(layout: RunLayout, protected: Iterable[Path] = (), keep_on_fail: bool = False, status: str | None = None,
                allowed_roots: Iterable[Path] | None = None) -> CleanupReport:
    """Remove the temporary paths this run registered. Refuses unless results are saved, and keeps anything protected.
    With `allowed_roots`, a registered path must lie in the run folder or under one of them (the registry file is not trusted
    to name arbitrary folders)."""
    try:
        registered = core.read_json(layout.directory / TEMP_REGISTRY)
    except FileNotFoundError:
        return CleanupReport()  # the run registered nothing temporary
    if not registered:
        return CleanupReport()
    if not layout.result.exists():
        return CleanupReport(skipped_reason="results are not saved yet (result.json is missing)")
    if keep_on_fail and status not in (None, constants.PASS):
        return CleanupReport(skipped_reason=f"kept for diagnosis after {status}")
    protected_paths = list(protected)
    retained = layout.directory / RETAINED_MARKER
    if retained.exists():
        protected_paths.append(layout.directory)
    removed: list[str] = []
    kept: list[tuple[str, str]] = []
    run_directory = layout.directory.resolve()
    for entry in registered:
        path = Path(entry)
        if not path.exists() and not path.is_symlink():
            continue
        resolved_entry = path.resolve()
        roots = None if allowed_roots is None else [layout.directory.resolve(), *(Path(r).resolve() for r in allowed_roots)]
        if roots is not None and not any(_is_within(resolved_entry, root) for root in roots):
            kept.append((entry, "it is outside the run folder and the allowed temporary roots"))
            continue
        # The run's own results and logs are never temporary, even if registered by mistake.
        reason = protection_reason(path, [*protected_paths, layout.result, layout.console, layout.job_json, layout.events], layout.root)
        if reason is None and path.resolve() == run_directory:
            reason = "it is the run folder itself"
        if reason is not None:
            kept.append((entry, reason))
            continue
        try:
            _remove(path)
            removed.append(entry)
        except OSError as error:
            kept.append((entry, f"could not be removed: {error}"))
    core.atomic_write_json(layout.directory / TEMP_REGISTRY, [item for item, _ in kept])
    return CleanupReport(tuple(removed), tuple(kept))


# -- retention --------------------------------------------------------------------------------------------------------------


def mark_retained(directory: Path) -> None:
    (directory / RETAINED_MARKER).write_text("retained by `valtrion retain`; `valtrion gc` never removes this run\n", encoding="utf-8")


def iter_runs(root: Path) -> list[Path]:
    """Run folders: exactly `<root>/<project>/<sha8>/<runId>` and holding job, result or event files."""
    runs: list[Path] = []
    if not root.is_dir():
        return runs
    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        for sha in sorted(p for p in project.iterdir() if p.is_dir()):
            for run in sorted(p for p in sha.iterdir() if p.is_dir()):
                if any((run / marker).exists() for marker in RUN_MARKERS):
                    runs.append(run)
    return runs


def _run_is_active(run: Path) -> bool:
    try:
        document = core.read_json(run / "job.json")
    except (OSError, ValueError):
        return False
    return document.get("state") in ("queued", "running")


def gc(root: Path, keep_days: float, dry_run: bool = False, protected: Iterable[Path] = (),
       clock: Callable[[], float] = time.time) -> CleanupReport:
    """Remove run folders older than `keep_days`, except active, retained and protected ones. Age is the newest file time."""
    if keep_days < 0:
        raise ValueError("keep-days cannot be negative")
    cutoff = clock() - timedelta(days=keep_days).total_seconds()
    protected_paths = list(protected)
    removed: list[str] = []
    kept: list[tuple[str, str]] = []
    for run in iter_runs(root):
        if (run / RETAINED_MARKER).exists():
            kept.append((str(run), "retained"))
            continue
        if _run_is_active(run):
            kept.append((str(run), "job still active"))
            continue
        newest = max((p.stat().st_mtime for p in [run, *run.iterdir()]), default=0.0)
        if newest > cutoff:
            continue
        reason = _overlap(run.resolve(), protected_paths)
        if reason is not None:
            kept.append((str(run), reason))
            continue
        if not dry_run:
            try:
                _remove(run)
            except OSError as error:
                kept.append((str(run), f"could not be removed: {error}"))
                continue
        removed.append(str(run))
    if not dry_run:
        for project in [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []:
            for sha in [p for p in project.iterdir() if p.is_dir()]:
                try:
                    if not any(sha.iterdir()):
                        sha.rmdir()
                except OSError:
                    pass  # a job was created in it meanwhile
            try:
                if not any(project.iterdir()):
                    project.rmdir()
            except OSError:
                pass
    return CleanupReport(tuple(removed), tuple(kept))
