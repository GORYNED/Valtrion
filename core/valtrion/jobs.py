"""Detached jobs: `valtrion job start|status|watch|cancel`.

A job runs in its own process (the runner) on the host that owns the resources, so a dropped SSH connection or a closed terminal
never changes its outcome. State is `job.json` (`gdv.job/1`, rewritten atomically) plus `events.jsonl` (`gdv.event/1`,
append-only) in the run folder `<artifactsRoot>/<project>/<sha8>/<runId>/`. A watcher reads these from a byte offset and judges
progress from the age of the last event, never from the liveness of its own connection.

Detaching: Windows uses `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` (and breaks away from a parent Job Object when allowed);
macOS uses a one-shot launchd agent, in the `user` domain or the `gui` domain for steps that need the login keychain; other
systems use `setsid`.
"""

from __future__ import annotations

import json
import math
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import artifacts, constants, core, lease, remote, supervise, workroot
from .artifacts import RunLayout

EVENT_TYPES = (
    "job.started", "job.finished", "step.started", "step.finished", "progress", "stall.detected", "timeout.detected",
    "lease.acquired", "lease.released", "log")
JOB_KINDS = ("run", "pipeline", "build", "install", "collect")
DEFAULT_LEASE_TTL_SEC = 120
STARTUP_WAIT_SEC = 15.0
ADAPTER_LOG_FOLDERS = ("logs", "build-logs")  # written beside the build output by the macOS and Windows (logs) and iOS (build-logs) adapters
STARTUP_GRACE_SEC = 120.0  # a job younger than this with no verified runner yet is still starting
_FLUSH_INTERVAL_SEC = 1.0


class JobError(Exception):
    """A permanent job problem (unknown job, unreadable state); not a transport failure."""


# -- store ------------------------------------------------------------------------------------------------------------------


def index_path(job_id: str, home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / "jobs" / f"{core.safe_segment(job_id)}.json"


def find_job_directory(job_id: str, home: Path | None = None) -> Path:
    try:
        return Path(core.read_json(index_path(job_id, home))["dir"])
    except (OSError, ValueError, KeyError) as error:
        raise JobError(f"unknown job {job_id!r}") from error


def work_cleanup_blocker(spec: Mapping[str, Any]) -> str | None:
    """Why the job's work directories must stay (same rules for the runner's own cleanup and for `cancel`), or None. A command
    job's diagnostics are in the run folder by construction (console log and events); a pipeline job must have saved its
    result first, as `artifacts.cleanup_run` requires of the temporary paths it removes."""
    layout = RunLayout(Path(spec["artifactsRoot"]), spec["project"], spec["sha8"], spec["jobId"])
    if not spec.get("cleanup", True):
        return "cleanup is disabled for this job"
    if (layout.directory / artifacts.RETAINED_MARKER).exists():
        return "the run is retained"
    if spec["payload"]["type"] == "pipeline" and not layout.result.exists():
        return "results are not saved yet (result.json is missing)"
    base = workroot.WorkRoots(Path(spec["workRoot"])).job(spec["jobId"]).base
    for item in spec.get("protectedPaths", []):
        if artifacts._overlap(base.resolve(), [Path(item)]) is not None:
            return f"they overlap the protected path {item}"
    return None


def save_adapter_logs(spec: Mapping[str, Any]) -> str | None:
    """Platform adapters write their build logs to a `logs` folder beside the build output, which is the job directory when the
    build lives in the work root. Copy it to the run folder (same relative name, `logs`) before the directory goes; returns a
    message when the copy failed, else None."""
    base = workroot.WorkRoots(Path(spec["workRoot"])).job(spec["jobId"]).base
    run_folder = RunLayout(Path(spec["artifactsRoot"]), spec["project"], spec["sha8"], spec["jobId"]).directory
    for name in ADAPTER_LOG_FOLDERS:
        if not (base / name).is_dir():
            continue
        try:
            shutil.copytree(base / name, run_folder / name, dirs_exist_ok=True)
        except OSError as error:
            return f"build logs could not be saved to the run folder: {error}"
    return None


def kept_work_reason(job_id: str, home: Path | None = None) -> str | None:
    """Why the job's work directories are kept on purpose (retained run, protected path, cleanup off, unsaved pipeline result);
    None for an unknown job or one with nothing to keep. The sweep consults this before removing a directory. For an inbox folder
    (a caller's files, named by the job that was started with it) the reason is that a running job still reads it."""
    if job_id.startswith(workroot.INBOX_PREFIX):
        for active in active_job_ids(home):
            try:
                if JobStore(find_job_directory(active, home)).read_spec()["payload"].get("inbox") == job_id:
                    return "a running job reads it"
            except (JobError, OSError, KeyError):
                continue
        return None
    try:
        spec = JobStore(find_job_directory(job_id, home)).read_spec()
        reason = work_cleanup_blocker(spec) if spec.get("workRoot") else None
        # Unsaved results only hold the directories back for the hours the sweep waits anyway; they are not kept for ever.
        return None if reason and reason.startswith("results are not saved") else reason
    except (JobError, OSError, KeyError):
        return None


def active_job_ids(home: Path | None = None, now: Callable[[], datetime] = core.now) -> list[str]:
    """Ids of jobs whose runner is verifiably alive, or that were created moments ago and are still starting (their work
    directories must not be swept). A job left `running` by a killed runner or a reboot is not active."""
    folder = (home or core.valtrion_home()) / "jobs"
    active = []
    for entry in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            directory = Path(core.read_json(entry)["dir"])
            job = core.read_json(directory / "job.json")
            if job.get("state") not in ("queued", "running"):
                continue
            if _runner_is_ours(JobStore(directory), job.get("pid")):
                active.append(entry.stem)
            elif (now() - core.parse_utc(job["updatedUtc"])).total_seconds() < STARTUP_GRACE_SEC:
                active.append(entry.stem)
        except (OSError, ValueError, KeyError):
            continue
    return active


def new_job_id(label: str, sha8: str, moment: datetime | None = None) -> str:
    stamp = (moment or core.now()).strftime("%Y%m%dT%H%M%S") + "Z"
    return core.safe_identifier(f"{stamp}-{core.safe_segment(label, 'job', 40)}-{sha8}")


def job_document(
        job_id: str, kind: str, state: str, status: str | None, reason: str | None, created: str, updated: str, host: str,
        command: Sequence[str], stall_sec: float, timeout_sec: float, last_progress: str, events_offset: int, pid: int | None = None,
        result_path: str | None = None, phase: str | None = None) -> dict:
    document: dict[str, Any] = {
        "schema": "gdv.job/1", "jobId": job_id, "kind": kind, "state": state, "status": status, "reason": reason,
        "createdUtc": created, "updatedUtc": updated, "host": host, "command": [part for part in command if part] or ["-"],
        "watchdog": {"stallSec": max(1, math.ceil(stall_sec)), "timeoutSec": max(1, math.ceil(timeout_sec)), "lastProgressUtc": last_progress},
        "eventsOffset": events_offset}
    if pid:
        document["pid"] = pid
    if result_path:
        document["resultPath"] = result_path
    if phase:
        document["phase"] = phase
    return document


class JobStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.job_json = directory / "job.json"
        self.events = directory / "events.jsonl"
        self.spec = directory / "spec.json"
        self.result = directory / "result.json"
        self.console = directory / "console.log"
        self.cancel = directory / "cancel.requested"

    def read_job(self) -> dict:
        try:
            return core.read_json(self.job_json)
        except (OSError, ValueError) as error:
            raise JobError(f"cannot read {self.job_json}: {error}") from error

    def read_spec(self) -> dict:
        try:
            return core.read_json(self.spec)
        except (OSError, ValueError) as error:
            raise JobError(f"cannot read {self.spec}: {error}") from error


class EventLog:
    """The single writer of events.jsonl for a job: the runner process."""

    def __init__(self, store: JobStore, job_id: str, clock: Callable[[], str] = core.utc_now) -> None:
        self._store = store
        self._job_id = job_id
        self._clock = clock
        self._lock = threading.Lock()
        try:
            self._seq = sum(1 for _ in open(store.events, "rb"))
        except FileNotFoundError:
            self._seq = 0
        self.offset = store.events.stat().st_size if store.events.exists() else 0
        self.last_utc = clock()

    def emit(self, event_type: str, message: str | None = None, step: str | None = None, status: str | None = None,
             data: Mapping[str, Any] | None = None) -> int:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r}")
        with self._lock:
            document: dict[str, Any] = {"schema": "gdv.event/1", "seq": self._seq, "utc": self._clock(), "jobId": self._job_id, "type": event_type}
            if step:
                document["step"] = step
            if status:
                document["status"] = status
            if message:
                document["message"] = message
            if data:
                document["data"] = dict(data)
            self._seq += 1
            self.offset = core.append_line(self._store.events, json.dumps(document, separators=(",", ":"), ensure_ascii=False))
            self.last_utc = document["utc"]
            return self.offset


def read_events(path: Path, from_offset: int, max_bytes: int = 256 * 1024) -> tuple[list[str], int]:
    """Whole lines from `from_offset`; the offset after the last whole line returned (a partial line is left for next time).
    A line longer than the chunk is read by growing the chunk, so the watcher never sticks in front of it."""
    limit = 64 * 1024 * 1024
    while True:
        try:
            with open(path, "rb") as handle:
                handle.seek(max(0, from_offset))
                chunk = handle.read(max_bytes)
        except FileNotFoundError:
            return [], max(0, from_offset)
        end = chunk.rfind(b"\n")
        if end >= 0 or len(chunk) < max_bytes or max_bytes >= limit:
            break
        max_bytes *= 4
    if end < 0:
        return [], max(0, from_offset)
    lines = [line.decode("utf-8", "replace") for line in chunk[: end + 1].splitlines() if line.strip()]
    return lines, max(0, from_offset) + end + 1


def last_event_utc(path: Path) -> str | None:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 8192))
            tail = handle.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            return json.loads(line)["utc"]
        except (ValueError, KeyError, TypeError):
            continue
    return None


def tail_info(directory: Path, from_offset: int, root: Path | None = None, now: Callable[[], datetime] = core.now) -> dict:
    """What a watcher needs in one short call: new events, the job state and the age of the last event (host clock)."""
    store = JobStore(directory)
    job = store.read_job()
    lines, next_offset = read_events(store.events, from_offset)
    newest = max(filter(None, [last_event_utc(store.events), job["updatedUtc"], job["createdUtc"]]), key=core.parse_utc)
    age = max(0.0, (now() - core.parse_utc(newest)).total_seconds())
    info: dict[str, Any] = {
        "jobId": job["jobId"], "state": job["state"], "status": job["status"], "reason": job["reason"],
        "stallSec": job["watchdog"]["stallSec"], "timeoutSec": job["watchdog"]["timeoutSec"], "pid": job.get("pid"),
        "lines": lines, "nextOffset": next_offset, "ageSec": round(age, 3), "serverUtc": core.format_utc(now()), "dir": str(directory)}
    if root is not None:
        try:
            info["relDir"] = directory.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return info


# -- runner (the detached process) ------------------------------------------------------------------------------------------


@dataclass
class JobContext:
    """What a payload may use while the runner executes it."""

    store: JobStore
    events: EventLog
    spec: dict
    job_id: str
    flush: Callable[[], None]
    cancelled: Callable[[], bool]
    layout: RunLayout
    acquire: Callable[[Mapping[str, Any]], "str | None"]
    home: Path | None = None
    state: dict = field(default_factory=dict)
    work: workroot.JobDirs | None = None  # the job's disposable directories under the work root, when it has one
    #: Leases the job took and could no longer renew (taken over, or removed by hand). Work that needed them must not continue.
    lost_leases: Callable[[], list[str]] = field(default=lambda: [])
    #: Give back one lease taken with `acquire` (`{resource}` as it was named there); returns whether it was held and released.
    release: Callable[[Mapping[str, Any]], bool] = field(default=lambda item: False)
    #: The reason a resource is held by someone else right now (a live lease), or None when it is free; takes nothing.
    probe: Callable[[str], "str | None"] = field(default=lambda resource: None)
    #: Record what the job is doing (`waiting-build`, `building`, `waiting-device`, `on-device`) in job.json and as an event;
    #: None clears it.
    set_phase: Callable[["str | None"], None] = field(default=lambda phase: None)


class _Runner:
    def __init__(self, directory: Path, home: Path | None, leases: lease.LeaseManager | None) -> None:
        self.store = JobStore(directory)
        self.home = home
        self.spec = self.store.read_spec()
        self.job_id = self.spec["jobId"]
        self.events = EventLog(self.store, self.job_id)
        self.leases = leases or lease.LeaseManager(home, host_id=self.spec["host"]["id"])
        self.created = self.store.read_job()["createdUtc"]
        watchdog = self.spec["watchdog"]
        self.stall_sec = float(watchdog["stallSec"])
        self.timeout_sec = float(watchdog["timeoutSec"])
        self.last_progress = core.utc_now()
        self.state, self.status, self.reason = "running", None, None
        self._flushed = 0.0
        self.keeper: lease.LeaseKeeper | None = None
        self._waited_sec = 0.0
        self._wait_since: float | None = None
        self.phase: str | None = None
        #: The lease each `acquire_one` resource name resolved to (a counted resource becomes one of its slots), for `release_one`.
        self.taken: dict[str, str] = {}
        #: Resources the job was started with (`spec.leases`): held for the whole job, never released by a stage.
        self.pinned: set[str] = set()

    def set_phase(self, phase: str | None) -> None:
        """What the job is doing now, in job.json (`phase`) and as a `progress` event with `data.phase`. Cleared with None."""
        if phase == self.phase:
            return
        self.phase = phase
        self.last_progress = core.utc_now()
        self.events.emit("progress", f"phase: {phase}" if phase else "phase cleared", data={"phase": phase})
        self.flush(force=True)

    def lease_wait_elapsed(self) -> float:
        """Seconds this job has spent acquiring leases (a wait in progress included). The pipeline timeout does not count them: a wait
        is bounded by `--lease-wait` alone, and work that has not started cannot use up the time meant for it."""
        since = self._wait_since
        return self._waited_sec + (time.monotonic() - since if since is not None else 0.0)

    def command_environment(self) -> dict | None:
        """The payload's own `env` (which replaces the environment, as before) or, for a job with a work root, this process's
        environment plus the `GDV_*` job directories; None leaves the child with the runner's environment."""
        explicit = self.spec["payload"].get("env")
        if explicit is not None:
            return explicit
        extra = self.spec.get("jobEnv")
        return {**os.environ, **extra} if extra else None

    def command(self) -> list[str]:
        payload = self.spec["payload"]
        return list(payload["argv"]) if payload["type"] == "command" else ["pipeline", str(payload["file"])]

    def flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._flushed < _FLUSH_INTERVAL_SEC:
            return
        self._flushed = time.monotonic()
        result = str(self.store.result) if self.store.result.exists() else None
        core.atomic_write_json(self.store.job_json, job_document(
            self.job_id, self.spec["kind"], self.state, self.status, self.reason, self.created, core.utc_now(),
            self.spec["host"]["id"], self.command(), self.stall_sec, self.timeout_sec, self.last_progress, self.events.offset,
            os.getpid(), result, self.phase if self.state == "running" else None))

    def on_supervise_event(self, event_type: str, message: str, data: dict) -> None:
        if event_type in ("progress", "log"):
            self.last_progress = core.utc_now()
        self.events.emit(event_type if event_type in EVENT_TYPES else "log", message, data=data)
        self.flush()

    def acquire_leases(self) -> str | None:
        """Take every lease the job needs; returns the BLOCKED reason (naming the holder) or None."""
        for item in self.spec.get("leases", []):
            reason = self.acquire_one(item)
            if reason:
                return f"BLOCKED: {reason}"
            self.pinned.add(item["resource"])
        return None

    def acquire_one(self, item: Mapping[str, Any]) -> str | None:
        """Take one more lease (`{resource}` or `{resource, slots}`) for the rest of the job; returns the reason it is blocked."""
        ttl = int(self.spec.get("leaseTtlSec", DEFAULT_LEASE_TTL_SEC))
        owner, pid = self.spec.get("owner") or "valtrion", os.getpid()
        if self.keeper is None:
            self.keeper = lease.LeaseKeeper(self.leases, self.job_id, pid, max(1.0, ttl / 3), owner)
            self.keeper.start()
        wait = float(self.spec.get("leaseWaitSec", 0))
        purpose = self.spec.get("purpose") or f"valtrion job {self.job_id}"
        resource = item["resource"]

        def waiting(reason: str, waited_sec: float) -> None:
            self.events.emit("log", lease.waiting_message(reason, waited_sec), data={"resource": resource, "holder": reason, "waitedSec": round(waited_sec, 1)})

        slots = item.get("slots")
        if slots is None and resource == lease.UNITY_EDITOR_RESOURCE:
            try:
                slots = lease.unity_editor_capacity(self.home)  # the host's total, so every claimant counts the same slots
            except ValueError as error:  # a broken host.json blocks the job like the CLI does
                return str(error)
        self._wait_since = time.monotonic()  # the time spent here is a wait for someone else: it does not count against the timeout
        try:
            if slots is not None:
                outcome = self.leases.acquire_slot(resource, int(slots), owner, self.job_id, ttl, purpose, pid, wait_sec=wait, on_wait=waiting)
            else:
                outcome = self.leases.acquire(resource, owner, self.job_id, ttl, purpose, pid, wait_sec=wait, on_wait=waiting)
        finally:
            self._waited_sec += time.monotonic() - self._wait_since
            self._wait_since = None
        if not outcome.acquired:
            return outcome.reason
        self.keeper.add(outcome.lease.resource)
        self.taken[resource] = outcome.lease.resource
        self.events.emit("lease.acquired", outcome.lease.resource, data={
            "resource": outcome.lease.resource, "reclaimedStale": outcome.reclaimed.describe() if outcome.reclaimed else None})
        return None

    def release_one(self, item: Mapping[str, Any]) -> bool:
        """Give back one lease taken with `acquire_one` (named as there) before the job ends, so the next job can take it. The
        others stay held. Returns False when this job does not hold it."""
        if item["resource"] in self.pinned:
            return False  # requested for the whole job (`--lease`): the stage does not give it back
        actual = self.taken.pop(item["resource"], None)
        if actual is None or self.keeper is None:
            return False
        released = self.keeper.release_one(actual)
        if released is None:
            return False
        self.events.emit("lease.released", actual, data={"resource": actual, "ok": bool(released)})
        return bool(released)

    def probe_lease(self, resource: str) -> str | None:
        """Why `resource` cannot be taken right now (a live holder other than this job), or None. Takes nothing."""
        status = self.leases.status(resource)
        if status.state != "held" or status.record is None:
            return None
        return None if status.record.session == self.job_id else f"{resource} is {status.record.describe()}"

    def lost_leases(self) -> list[str]:
        return list(self.keeper.lost) if self.keeper is not None else []

    def release_leases(self) -> None:
        if self.keeper is None:
            return
        self.keeper.stop()
        held = self.keeper.held()
        failed = self.keeper.release_all()
        for resource in held:
            self.events.emit("lease.released", resource, data={"resource": resource, "ok": resource not in failed})
        if self.keeper.lost:
            self.events.emit("log", "a lease was lost while the job ran: " + ", ".join(self.keeper.lost))

    def execute(self) -> None:
        payload = self.spec["payload"]
        watchdog = self.spec["watchdog"]
        if payload["type"] == "command":
            options = dict(
                progress_files=tuple(Path(p) for p in watchdog.get("progressFiles", [])),
                heartbeat=Path(watchdog["heartbeat"]) if watchdog.get("heartbeat") else None,
                log_counts=watchdog.get("logCounts"), cpu_counts=bool(watchdog.get("cpuCounts")),
                poll_sec=float(watchdog.get("pollSec", 0.25)), tick_sec=float(watchdog.get("tickSec", 15.0)),
                cancel_file=self.store.cancel)
            self.events.emit("step.started", " ".join(payload["argv"])[:300], step="command")
            outcome = supervise.supervise(
                payload["argv"], supervise.Watchdog(self.timeout_sec, self.stall_sec, **options), self.store.console,
                payload.get("cwd"), self.command_environment(), self.on_supervise_event, bool(self.spec.get("contractExitCodes")))
            self.events.emit("step.finished", outcome.reason, step="command", status=outcome.status,
                             data={"returncode": outcome.returncode, "elapsedSec": round(outcome.elapsed_sec, 3)})
            if outcome.cancelled:
                self.state, self.status, self.reason = "cancelled", None, outcome.reason
            else:
                self.status, self.reason = outcome.status, outcome.reason
            return
        if payload["type"] == "pipeline":
            from . import pipeline
            context = JobContext(
                self.store, self.events, self.spec, self.job_id, self.flush, lambda: self.store.cancel.exists(),
                RunLayout(Path(self.spec["artifactsRoot"]), self.spec["project"], self.spec["sha8"], self.job_id), self.acquire_one,
                self.home, work=self.work_dirs(), lost_leases=self.lost_leases, release=self.release_one, probe=self.probe_lease,
                set_phase=self.set_phase)
            with _PipelineWatch(self) as guard:
                outcome = pipeline.run_pipeline_job(context)
            if guard.timed_out:
                self.status, self.reason = constants.TIMEOUT, f"the pipeline did not finish within the {self.timeout_sec:g} s timeout"
                self._mark_result_timeout()
            elif outcome.cancelled:
                self.state, self.status, self.reason = "cancelled", None, "cancelled by request"
            else:
                self.status, self.reason = outcome.status, outcome.reason
            return
        raise JobError(f"unknown payload type {payload['type']!r}")

    def work_roots(self) -> workroot.WorkRoots | None:
        return workroot.WorkRoots(Path(self.spec["workRoot"])) if self.spec.get("workRoot") else None

    def work_dirs(self) -> workroot.JobDirs | None:
        roots = self.work_roots()
        return roots.job(self.job_id) if roots else None

    def _mark_result_timeout(self) -> None:
        """The timeout raised the cancel request, so the stage wrote BLOCKED; the result must say what really ended the run."""
        try:
            document = core.read_json(self.store.result)
        except (OSError, ValueError):
            return
        if document.get("status") in (constants.BLOCKED, constants.PASS):
            document["status"], document["reason"] = constants.TIMEOUT, self.reason
            core.atomic_write_json(self.store.result, document)

    def finish(self) -> None:
        if self.state == "running":
            self.state = "finished"
        self.last_progress = core.utc_now()
        self.events.emit("job.finished", self.reason, status=self.status or "BLOCKED", data={"state": self.state})
        self.flush(force=True)

    def cleanup(self) -> None:
        if not self.spec.get("cleanup", True):
            return
        layout = RunLayout(Path(self.spec["artifactsRoot"]), self.spec["project"], self.spec["sha8"], self.job_id)
        import tempfile
        payload = self.spec["payload"]
        roots = [tempfile.gettempdir(), *self.spec.get("tempRoots", [])]
        if self.spec.get("workRoot"):
            roots.append(str(workroot.WorkRoots(Path(self.spec["workRoot"])).jobs))
        if payload.get("projectDir"):
            roots.append(payload["projectDir"])
        self.cleanup_work()
        report = artifacts.cleanup_run(layout, [Path(p) for p in self.spec.get("protectedPaths", [])], status=self.status,
                                       allowed_roots=roots)
        if report.removed or report.kept or report.skipped_reason:
            self.events.emit("log", "cleanup", data={"removed": list(report.removed), "kept": [list(item) for item in report.kept],
                                                      "skipped": report.skipped_reason})
            self.flush(force=True)


    def cleanup_work(self) -> None:
        """Remove everything the job created under the work root (checkout, builds, exports, DerivedData, temp). Results and logs
        are in the run folder under the artifacts root, saved before this point, so a failed job is cleaned too."""
        roots = self.work_roots()
        if roots is None:
            return
        kept = work_cleanup_blocker(self.spec)
        if kept:
            self.events.emit("log", f"work directories kept: {kept}", data={"kept": str(roots.job(self.job_id).base)})
            return
        problem = save_adapter_logs(self.spec)
        if problem:
            self.events.emit("log", problem)
        try:  # the tree is complete here (Library, package cache, build output): the one place the real longest path is known
            for warning in workroot.checkout_warnings(roots.job(self.job_id).checkout):
                self.events.emit("log", warning, data={"warning": "path-length"})
        except OSError:
            pass
        try:
            removed = workroot.cleanup_job(roots, self.job_id, inbox=self.spec["payload"].get("inbox"))
        except (OSError, workroot.WorkRootError) as error:
            self.events.emit("log", f"work cleanup failed: {error}")
            return
        if removed:
            self.events.emit("log", "work cleanup", data={"removed": removed})
            self.flush(force=True)


class _PipelineWatch:
    """Liveness and the hard timeout of a pipeline job. A pipeline is not a supervised process, so a thread pulses a `progress`
    event (this is what a watcher judges the job's age by, through a long silent build or lease wait) and enforces `timeoutSec`
    by raising the cancel request, which every stage and the device poll honour."""

    def __init__(self, runner: "_Runner") -> None:
        self._runner = runner
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="valtrion-pipeline-watch", daemon=True)
        self.timed_out = False

    def __enter__(self) -> "_PipelineWatch":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        runner = self._runner
        tick = float(runner.spec["watchdog"].get("tickSec", 15.0))
        started = next_pulse = time.monotonic()
        while not self._stop.wait(0.25):
            now = time.monotonic()
            working = now - started - runner.lease_wait_elapsed()
            if working >= runner.timeout_sec and not self.timed_out:
                self.timed_out = True
                runner.events.emit("timeout.detected", f"no result within the {runner.timeout_sec:g} s timeout")
                try:
                    runner.store.cancel.write_text("timeout\n", encoding="utf-8")
                except OSError:
                    pass
            if now >= next_pulse:
                next_pulse = now + tick
                runner.last_progress = core.utc_now()
                runner.events.emit("progress", "pipeline running", data={"elapsedSec": round(now - started, 1), "workingSec": round(working, 1)})
                runner.flush(force=True)


def run_job(directory: Path, home: Path | None = None, leases: lease.LeaseManager | None = None) -> int:
    """Entry of the detached runner. Always leaves job.json in a final state and releases every lease it took."""
    runner = _Runner(directory, home, leases)
    _install_cancel_signals(runner.store.cancel)
    core.atomic_write_json(directory / "runner.json", {"pid": os.getpid(), "token": core.process_start_token(os.getpid())})
    runner.events.emit("job.started", f"pid {os.getpid()} on {runner.spec['host']['id']}",
                       data={"host": runner.spec["host"], "kind": runner.spec["kind"]})
    runner.flush(force=True)
    try:
        blocked = runner.acquire_leases()
        if blocked:
            runner.status, runner.reason = constants.BLOCKED, blocked[len("BLOCKED: "):]
        else:
            runner.execute()
    except BaseException as error:  # noqa: BLE001 - the job must always reach a final state
        runner.status, runner.reason = constants.FAIL, f"runner error: {type(error).__name__}: {error}"
        runner.events.emit("log", runner.reason)
    finally:
        try:
            runner.release_leases()
            # Before the final state, so a watcher that stops at `finished` has seen every event, cleanup included.
            runner.cleanup()
        except BaseException as error:  # noqa: BLE001 - cleanup must never stop the job from reaching its final state
            runner.events.emit("log", f"cleanup failed: {type(error).__name__}: {error}")
        finally:
            runner.finish()
    _remove_launchd_agent(runner.spec)
    return constants.exit_code_for_status(runner.status) if runner.status else constants.EXIT_BLOCKED


def _install_cancel_signals(cancel_file: Path) -> None:
    def request_cancel(*_: object) -> None:
        try:
            cancel_file.write_text("signal\n", encoding="utf-8")
        except OSError:
            pass

    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, request_cancel)
            except (ValueError, OSError):
                pass


def _remove_launchd_agent(spec: Mapping[str, Any]) -> None:
    agent = spec.get("launchd")
    if not agent:
        return
    uid = os.getuid() if hasattr(os, "getuid") else 0
    _retire_launchd_files(agent.get("files") or [], spec)
    targets = [agent["domain"]] if agent.get("domain") else [f"user/{uid}", f"gui/{uid}"]
    for target in targets:
        # Fire and forget: bootout signals the very process that calls it and waits for it to exit, so it must not be awaited here.
        subprocess.Popen(["launchctl", "bootout", f"{target}/{agent['label']}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def _retire_launchd_files(files: Sequence[str], spec: Mapping[str, Any]) -> None:
    """Keep the runner's log with the run (it is a diagnostic), then remove the job definition and log from the work root."""
    if not files:
        return
    plist, log = Path(files[0]), Path(files[1])
    try:
        run_folder = RunLayout(Path(spec["artifactsRoot"]), spec["project"], spec["sha8"], spec["jobId"]).directory
        if log.is_file():
            shutil.copyfile(log, run_folder / "runner.log")
    except (OSError, KeyError):
        pass
    for item in (plist, log):
        try:
            item.unlink()
        except OSError:
            pass


# -- starting ---------------------------------------------------------------------------------------------------------------


@dataclass
class JobRequest:
    kind: str
    project: str
    sha: str | None
    label: str
    payload: dict
    host: dict  # {id, transport, selectedBy}
    stall_sec: float = supervise.DEFAULT_STALL_SEC
    timeout_sec: float = 3600
    progress_files: tuple[str, ...] = ()
    heartbeat: str | None = None
    log_counts: bool | None = None
    cpu_counts: bool = False
    poll_sec: float = 0.25
    tick_sec: float = 15.0
    contract_exit_codes: bool = False
    leases: tuple[dict, ...] = ()
    lease_wait_sec: float = 0
    lease_ttl_sec: int = DEFAULT_LEASE_TTL_SEC
    owner: str = ""
    purpose: str = ""
    cleanup: bool = True
    protected_paths: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)


def create_job(request: JobRequest, artifacts_root: Path, home: Path | None = None,
               work_roots: workroot.WorkRoots | None = None) -> tuple[RunLayout, dict]:
    """Create the run folder with spec.json and a queued job.json, and index the job id. Nothing is started yet. With
    `work_roots` the job also gets its directories under the work root and the `GDV_*` variables that name them."""
    if request.kind not in JOB_KINDS:
        raise JobError(f"unknown job kind {request.kind!r}")
    if request.stall_sec <= 0 or request.timeout_sec <= 0:
        raise JobError("stall and timeout windows must be positive; they cannot be disabled")
    sha8 = artifacts.sha8_of(request.sha)
    base = new_job_id(request.label, sha8)
    job_id, counter = base, 1
    while True:
        layout = RunLayout(artifacts_root, request.project, sha8, job_id)
        layout.directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            layout.directory.mkdir()  # atomic: two starts in the same second cannot both own one folder
            break
        except FileExistsError:
            counter += 1
            job_id = f"{base}-{counter}"
    spec = {
        "jobId": job_id, "kind": request.kind, "project": layout.relative.parts[0], "sha8": sha8, "sha": request.sha,
        "artifactsRoot": str(artifacts_root), "host": request.host, "payload": request.payload,
        "watchdog": {"stallSec": request.stall_sec, "timeoutSec": request.timeout_sec, "progressFiles": list(request.progress_files),
                     "heartbeat": request.heartbeat, "logCounts": request.log_counts, "cpuCounts": request.cpu_counts,
                     "pollSec": request.poll_sec, "tickSec": request.tick_sec},
        "contractExitCodes": request.contract_exit_codes, "leases": list(request.leases), "leaseWaitSec": request.lease_wait_sec,
        "leaseTtlSec": request.lease_ttl_sec, "owner": request.owner, "purpose": request.purpose, "cleanup": request.cleanup,
        "protectedPaths": list(request.protected_paths), **request.extra}
    if work_roots is not None:
        dirs = work_roots.job(job_id)
        dirs.create()
        spec["workRoot"] = str(work_roots.root)
        spec["jobEnv"] = dirs.environment(work_roots, layout.directory)
    core.atomic_write_json(layout.spec, spec)
    stamp = core.utc_now()
    command = request.payload["argv"] if request.payload["type"] == "command" else ["pipeline", str(request.payload["file"])]
    core.atomic_write_json(layout.job_json, job_document(
        job_id, request.kind, "queued", None, None, stamp, stamp, request.host["id"], command, request.stall_sec, request.timeout_sec,
        stamp, 0))
    core.atomic_write_json(index_path(job_id, home), {"dir": str(layout.directory)})
    return layout, spec


def runner_argv(directory: Path, cli_root: Path | None = None, python: str | None = None) -> list[str]:
    if cli_root is None:
        return remote.local_cli_argv(["job", "_run", str(directory)])
    return [python or sys.executable, "-I", "-c", remote.LAUNCHER, str(cli_root), "job", "_run", str(directory)]


def stable_python(os_name: str, protected: Callable[..., str | None] = workroot.protected_location) -> str:
    """The interpreter a background job runs: the one that runs this CLI (the pipx or venv interpreter it was installed with, so
    the Python version is the one `valtrion` requires, never the system one). On macOS it must not lie in a folder the operating
    system protects: a background job there would be asked for folder access that nobody can answer."""
    if os_name == "macos":
        for path in (Path(sys.executable), Path(sys.prefix)):
            reason = protected(path, os_name)
            if reason:
                raise JobError(f"the interpreter {sys.executable} lies {reason}; install valtrion with pipx or `python tools/install.py` "
                               "into a folder outside Documents, Desktop, Downloads and cloud storage")
    return sys.executable


_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def launch_detached(
        directory: Path, os_name: str | None = None, domain: str = "auto", spec: dict | None = None,
        run_command: Callable[[Sequence[str]], "tuple[int, str]"] | None = None,
        work_roots: workroot.WorkRoots | None = None) -> dict:
    """Start the runner so that it outlives this process. Returns `{pid}` or `{launchd: {domain, label}}`. With `work_roots` a
    macOS background job runs the CLI staged under the work root, with the interpreter that runs this CLI and a working directory, plist
    and log there too, so that it reads nothing from a folder the operating system protects."""
    os_name = os_name or core.local_os()
    if os_name == "macos" and work_roots is not None:
        argv = runner_argv(directory, workroot.stage_runtime(work_roots, remote.CLI_ROOT), stable_python(os_name))
    else:
        argv = runner_argv(directory)
    log = directory / "runner.log"
    if os_name == "macos":
        return _launch_launchd(argv, directory, log, domain, spec, run_command, work_roots)
    with open(log, "ab") as handle:
        if os_name == "windows":
            base = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
            try:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, cwd=directory,
                                           creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
            except OSError:  # the parent's Job Object does not allow breakaway
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, cwd=directory,
                                           creationflags=base, close_fds=True)
        else:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, cwd=directory,
                                       start_new_session=True, close_fds=True)
    _DETACHED.append(process)  # referenced so a runner that outlives its handle does not raise a ResourceWarning at collection
    return {"pid": process.pid}


_DETACHED: list[subprocess.Popen] = []


def launchd_plist(argv: Sequence[str], directory: Path, log: Path, label: str, environ: Mapping[str, str] | None = None) -> bytes:
    environment = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    for name in (core.HOME_ENV,):
        if environ and environ.get(name):
            environment[name] = environ[name]
    return plistlib.dumps({
        "Label": label, "ProgramArguments": list(argv), "WorkingDirectory": str(directory), "RunAtLoad": True,
        "StandardOutPath": str(log), "StandardErrorPath": str(log), "EnvironmentVariables": environment})


def _launch_launchd(argv, directory, log, domain, spec, run_command, work_roots=None) -> dict:
    if domain not in ("auto", "user", "gui"):
        raise JobError(f"unknown launchd domain {domain!r}; use auto, user or gui")
    run = run_command or (lambda command: _run(command))
    uid = os.getuid() if hasattr(os, "getuid") else 0
    label = workroot.LAUNCHD_LABEL_PREFIX + core.safe_segment(directory.name, "job", 80)
    plist = directory / "launchd.plist"
    working = directory
    files: list[str] = []
    if work_roots is not None:
        # Everything launchd touches for this job lives under the work root; the run folder is only written by the runner itself.
        work_roots.launchd.mkdir(parents=True, exist_ok=True)
        plist, log, working = work_roots.launchd / f"{label}.plist", work_roots.launchd / f"{label}.log", work_roots.launchd
        files = [str(plist), str(log)]
    plist.write_bytes(launchd_plist(argv, working, log, label, os.environ))
    # `auto` prefers the user domain, which an SSH session without a login may not have, and falls back to the GUI domain.
    if spec is not None:
        # Before bootstrapping: the agent starts at load and reads spec.json at once, and its cleanup needs the label.
        spec["launchd"] = {"label": label, **({"files": files} if files else {})}
        core.atomic_write_json(directory / "spec.json", spec)
    failures = []
    for candidate in (("user", "gui") if domain == "auto" else (domain,)):
        target = f"{candidate}/{uid}"
        code, output = run(["launchctl", "bootstrap", target, str(plist)])
        if code == 0:
            break
        failures.append(f"launchctl bootstrap {target} failed: {output[:200]}")
    else:
        hint = "; the gui domain needs a logged-in user (owner action)" if domain in ("auto", "gui") else ""
        raise JobError("; ".join(failures) + hint)
    if spec is not None:
        spec["launchd"] = {"domain": target, "label": label, **({"files": files} if files else {})}
        core.atomic_write_json(directory / "spec.json", spec)
    return {"launchd": {"domain": target, "label": label}}


def _run(command: Sequence[str]) -> tuple[int, str]:
    done = subprocess.run(list(command), capture_output=True, text=True, timeout=60, check=False)
    return done.returncode, (done.stdout + done.stderr).strip()


def wait_started(directory: Path, timeout_sec: float = STARTUP_WAIT_SEC, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout_sec
    store = JobStore(directory)
    while time.monotonic() < deadline:
        if store.events.exists():
            return True
        sleep(0.1)
    return False


def _mentions_android(request: JobRequest) -> bool:
    """Whether the job's command names Android (a `-buildTarget Android`, an `android` build script): a heuristic for the warning only."""
    argv = request.payload.get("argv") or []
    return any("android" in str(item).lower() for item in argv)


def start_job(request: JobRequest, artifacts_root: Path, home: Path | None = None, os_name: str | None = None, domain: str = "auto",
              foreground: bool = False, wait_sec: float = STARTUP_WAIT_SEC, work_roots: workroot.WorkRoots | None = None) -> dict:
    """Create and start a job; returns as soon as the runner is up. Foreground runs the job in this process (tests, debugging)."""
    layout, spec = create_job(request, artifacts_root, home, work_roots)
    info = {"jobId": spec["jobId"], "dir": str(layout.directory), "relDir": layout.relative.as_posix(), "host": request.host["id"]}
    if work_roots is not None:
        warnings = workroot.start_warnings(work_roots.root, spec["jobId"], os_name, _mentions_android(request))  # the same text `valtrion doctor` gives
        if warnings:
            info["warnings"] = warnings
    if foreground:
        info["exitCode"] = run_job(layout.directory, home)
        return info
    if os_name == "macos" or (os_name is None and core.local_os() == "macos"):
        launched = launch_detached(layout.directory, "macos", domain, spec, work_roots=work_roots)
    else:
        launched = launch_detached(layout.directory, os_name)
    info.update(launched)
    if not wait_started(layout.directory, wait_sec):
        raise JobError(f"the runner of {spec['jobId']} did not start within {wait_sec:g} s (see {layout.directory / 'runner.log'}, or the .log named after the job label in the work root's launchd folder)")
    return info


# -- cancel -----------------------------------------------------------------------------------------------------------------


def cancel_job(directory: Path, grace_sec: float = 15.0, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Ask the runner to stop (it kills the step's tree and releases its leases). A runner that ignores the request for
    `grace_sec`, or one that is already dead, is dealt with directly. Idempotent."""
    store = JobStore(directory)
    job = store.read_job()
    if job["state"] in ("finished", "cancelled"):
        return {"jobId": job["jobId"], "state": job["state"], "note": "already ended"}
    store.cancel.write_text(f"requested {core.utc_now()}\n", encoding="utf-8")
    pid = job.get("pid") if _runner_is_ours(store, job.get("pid")) else None
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if store.read_job()["state"] in ("finished", "cancelled"):
            return {"jobId": job["jobId"], "state": store.read_job()["state"], "note": "stopped by the runner"}
        if not pid or not core.process_alive(pid):
            break
        sleep(0.25)
    forced = False
    if pid and core.process_alive(pid) and _runner_is_ours(store, pid):
        forced = supervise.kill_process_tree(pid)
    final = store.read_job()
    if final["state"] not in ("finished", "cancelled"):
        # The runner is gone and left the job open; close it so no watcher waits for it, and free what it held.
        _close_orphaned(store, final)
    return {"jobId": job["jobId"], "state": "cancelled", "note": "runner was killed" if forced else "runner was already gone"}


def _runner_is_ours(store: JobStore, pid: int | None) -> bool:
    """Whether `pid` is still the runner of this job and not an unrelated process that reused the number. The runner records its
    start time; a process is stopped only when that matches, so a dead runner's pid is never used against someone else's process."""
    if not pid or not core.process_alive(pid):
        return False
    try:
        recorded = core.read_json(store.directory / "runner.json")
    except (OSError, ValueError):
        return False
    return recorded.get("pid") == pid and recorded.get("token") is not None and recorded.get("token") == core.process_start_token(pid)


def _close_orphaned(store: JobStore, job: dict) -> None:
    spec = store.read_spec()
    events = EventLog(store, job["jobId"])
    manager = lease.LeaseManager(host_id=spec["host"]["id"])
    for item in manager.list_all():
        if item.record and item.record.session == job["jobId"]:
            manager.release(item.resource, job["jobId"], item.record.pid)
            events.emit("lease.released", item.resource, data={"resource": item.resource, "ok": True})
    if spec.get("workRoot"):
        kept = work_cleanup_blocker(spec)
        if kept:
            events.emit("log", f"work directories kept: {kept}")
        else:
            problem = save_adapter_logs(spec)
            if problem:
                events.emit("log", problem)
            try:
                workroot.cleanup_job(workroot.WorkRoots(Path(spec["workRoot"])), job["jobId"], inbox=spec["payload"].get("inbox"))
            except (OSError, workroot.WorkRootError) as error:
                events.emit("log", f"work cleanup failed: {error}")
    _remove_launchd_agent(spec)  # the dead runner never got to retire its own definition and log
    events.emit("job.finished", "cancelled; the runner was gone", status=constants.BLOCKED, data={"state": "cancelled"})
    job = dict(job)
    job.update({"state": "cancelled", "status": None, "reason": "cancelled by request", "updatedUtc": core.utc_now(), "eventsOffset": events.offset})
    core.atomic_write_json(store.job_json, job)


# -- watching ---------------------------------------------------------------------------------------------------------------


@dataclass
class WatchOutcome:
    status: str
    reason: str | None
    offset: int
    state: str | None = None
    info: dict | None = None

    @property
    def exit_code(self) -> int:
        return constants.exit_code_for_status(self.status)


def watch(
        fetch: Callable[[int], "dict | None"], emit: Callable[[str], None], offset: int = 0, stall_sec: float | None = None,
        poll_sec: float = 2.0, unreachable_sec: float = 1800.0, sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic) -> WatchOutcome:
    """Follow a job to its end. `fetch(offset)` returns a `tail_info` dict, or None when the host cannot be reached (a dropped
    connection); it raises JobError for a permanent problem. Stall is judged from the age of the last event on the host, so
    an unreachable host never counts as a stall; a host unreachable for `unreachable_sec` is BLOCKED (the job keeps running and
    can be resumed from the returned offset)."""
    unreachable_since: float | None = None
    while True:
        try:
            info = fetch(offset)
        except JobError as error:
            return WatchOutcome(constants.BLOCKED, str(error), offset)
        if info is None:
            if unreachable_since is None:
                unreachable_since = monotonic()
            if monotonic() - unreachable_since >= unreachable_sec:
                return WatchOutcome(constants.BLOCKED, f"the host was unreachable for {unreachable_sec:g} s; the job keeps running, "
                                    f"resume with --from {offset}", offset)
            sleep(poll_sec)
            continue
        unreachable_since = None
        for line in info["lines"]:
            emit(line)
        offset = info["nextOffset"]
        if info["state"] == "finished":
            return WatchOutcome(info["status"] or constants.FAIL, info["reason"], offset, "finished", info)
        if info["state"] == "cancelled":
            return WatchOutcome(constants.BLOCKED, info["reason"] or "cancelled", offset, "cancelled", info)
        window = stall_sec if stall_sec is not None else float(info["stallSec"])
        if info["ageSec"] >= window and not info["lines"]:
            return WatchOutcome(constants.STALL, f"no event for {info['ageSec']:.0f} s (stall window {window:g} s); the runner may be dead",
                                offset, info["state"], info)
        sleep(poll_sec)
