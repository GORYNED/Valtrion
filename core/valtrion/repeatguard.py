"""The repeat guard of `valtrion pipeline`, `run` and `build`: an identical start within a window is refused, naming the earlier job.

Off by default. With `host.json` `"repeatGuard": {"windowSec": N}` (N > 0) a start whose inputs are the same as those of a start
less than N seconds ago is refused before a job exists, with the id, state and status of the earlier job; `--allow-repeat` starts it
anyway. The inputs are the pipeline as it will run (including the commit named with `--sha`), the content of every plan and
pre-launch file of its run stages, the options that change what runs (stage selection, `--resume-from`, `--keep-going`, the result
cache mode, the managed checkout), the job limits (`--timeout-sec`, `--stall-sec`, `--lease-wait`, `--disk-wait`), how the job is
started (`--domain`, `--foreground`), the project folder, its commit, and for each uncommitted change its path, size and modification time. Any change of these is a new input and is never refused. Records live in `<valtrion home>/repeat-guard/<key>.json` and are
removed by `prune` once they are older than a day beyond any window; two identical starts in the same instant may both pass.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from . import core
from .compare import GitRunner
from .gitstate import read_state

FOLDER = "repeat-guard"
#: Payload keys that change what a pipeline job does (and so are inputs of the guard).
_PAYLOAD_INPUTS = ("selection", "resumeFrom", "keepGoing", "resultCache", "shaPinned", "checkoutId", "checkoutRoot", "unityPath")


@dataclass(frozen=True)
class Previous:
    job_id: str
    started_utc: str
    age_sec: float
    state: str | None
    status: str | None

    def message(self, window_sec: float) -> str:
        where = f"{self.state}" + (f", {self.status}" if self.status else "") if self.state else "state unknown"
        return (f"an identical start {self.age_sec:.0f} s ago is job {self.job_id} ({where}); nothing it depends on changed and the "
                f"repeat window is {window_sec:g} s (host.json repeatGuard.windowSec). Change an input, wait, or pass --allow-repeat")


def guard_root(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / FOLDER


def _file_digest(path: Path) -> str:
    try:
        return core.sha256_file(path)
    except OSError:
        return "missing"


def fingerprint(pipeline: Mapping[str, Any], payload: Mapping[str, Any], base: Path, project_dir: Path | None,
                git: GitRunner, request: Mapping[str, Any] | None = None) -> str:
    """The key of a start: SHA-256 over the inputs listed in the module text. `request` holds the job limits that are not in the
    payload (`--timeout-sec`, `--stall-sec`, `--lease-wait`, `--disk-wait`) and how the job is started (`--domain`, `--foreground`):
    a rerun with a longer timeout, or in the `gui` domain after a `NEEDS_PERMISSION` in the `user` domain, is a changed input."""
    files = []
    for stage in pipeline.get("stages", []):
        if stage.get("plan"):
            plan = Path(stage["plan"])
            files.append(["plan", stage["plan"], _file_digest(plan if plan.is_absolute() else base / plan)])
        for entry in stage.get("prelaunchFiles") or ():
            local = Path(str(entry.get("local", "")))
            files.append(["prelaunch", str(entry.get("local")), _file_digest(local if local.is_absolute() else base / local)])
    tree: dict[str, Any] = {"folder": None}
    if project_dir is not None:
        state = read_state(project_dir, git)
        top_code, top = git(project_dir, ["rev-parse", "--show-toplevel"])
        top_dir = Path(top.strip()) if top_code == 0 and top.strip() else project_dir
        changes = []
        for relative in sorted(state.changed):
            try:
                info = (top_dir / relative).stat()
                changes.append([relative, info.st_size, info.st_mtime_ns])
            except OSError:
                changes.append([relative, "missing"])
        tree = {"folder": os.path.normcase(str(project_dir.resolve())), "sha": state.sha, "known": state.known, "changes": changes}
    material = {"pipeline": pipeline, "payload": {key: payload.get(key) for key in _PAYLOAD_INPUTS}, "files": files, "tree": tree,
                "request": dict(sorted((request or {}).items()))}
    return core.sha256_bytes(json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _job_state(job_id: str, home: Path | None) -> tuple[str | None, str | None]:
    try:
        from .jobs import JobStore, find_job_directory
        job = JobStore(find_job_directory(job_id, home)).read_job()
        return job.get("state"), job.get("status")
    except Exception:  # noqa: BLE001 - the state only improves the message
        return None, None


def check(key: str, window_sec: float, home: Path | None = None, now: Callable = core.now) -> Previous | None:
    """The earlier identical start within `window_sec`, or None (also when the window is 0 or the record is unreadable)."""
    if window_sec <= 0:
        return None
    path = guard_root(home) / f"{key}.json"
    try:
        record = core.read_json(path, attempts=2)
        started = core.parse_utc(record["startedUtc"])
        job_id = str(record["jobId"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (now() - started).total_seconds()
    if age < 0 or age >= window_sec:
        return None
    state, status = _job_state(job_id, home)
    return Previous(job_id, record["startedUtc"], age, state, status)


def record(key: str, job_id: str, home: Path | None = None, clock: Callable[[], str] = core.utc_now) -> None:
    """Remember a start (a failed write only loses the guard for this start)."""
    try:
        core.atomic_write_json(guard_root(home) / f"{key}.json", {"jobId": job_id, "startedUtc": clock()})
    except OSError:
        pass


def prune(window_sec: float, home: Path | None = None, now: Callable = core.now) -> list[str]:
    """Remove records older than the window plus a day; returns the removed keys."""
    folder = guard_root(home)
    removed: list[str] = []
    if not folder.is_dir():
        return removed
    limit = timedelta(seconds=max(0.0, window_sec)) + timedelta(days=1)
    for path in folder.glob("*.json"):
        try:
            started = core.parse_utc(core.read_json(path, attempts=1)["startedUtc"])
            old = now() - started > limit
        except (OSError, ValueError, KeyError, TypeError):
            old = True
        if old:
            try:
                path.unlink()
                removed.append(path.stem)
            except OSError:
                pass
    return removed
