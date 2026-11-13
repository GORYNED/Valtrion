"""Running a Unity Editor task on another host while this host's Editor slots are busy.

A host has a fixed number of `unity-editor` slots. When all are taken, a new Editor run (a pipeline on the Editor platform, or
`unity run`) waits in the local queue even if another configured host is idle. With offloading, the run goes to that host over SSH.

Both sides opt in. The sending host marks a host in `hosts.json` (`"offload": true`); the receiving host accepts work and names the kinds
in its own `host.json` (`offload.accept`, `offload.kinds`). Nothing here changes the commands, the result or the events of a run.

- *Free slots are a hint, the owner's lease decides.* `lease slots` tells whether a host has a free slot and nobody waiting for it. The
  remote run asks for the slot with no wait: a host that is busy by then answers `BLOCKED` at once and the run goes to this host's
  queue. A slot is never over-booked. While the run looks for a free host (at most `offload.settleSec`) it holds no place in the local
  waiting line; it joins the line at the end when no host takes it.
- *Code.* The host checks out a commit of its managed checkout (`host.json` `projects`). A commit that is not on the project's remote and
  uncommitted changes travel as a git bundle over SSH (`codesync.py`); nothing is pushed. Uncommitted Git LFS files are refused.
- *One job here.* The run is an ordinary local job: `job status|tail|cancel` work as before, the events of the remote job are copied into it, and
  its result is the remote result with the transfer written into `host.tools` (`offloadFrom`, `offloadReason`, `offloadSnapshot`,
  `offloadBaseSha`). `selectedBy` is unchanged.
- *Failure.* Before the remote run has started, any failure puts the run back into the local queue. When the host is lost after it started,
  the run is reported `BLOCKED` (the result is unknown), or run once more here when `offload.rerunLocal` allows it.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__, codesync, compare, config, constants, core, doctor, jobs, lease, remote, submit
from .config import HostEntry
from .transport import evidence

#: Written into the run folder of a job that follows a run on another host.
MARKER = "offload.json"
SLOTS_TIMEOUT_SEC = 45
#: What ssh says when it never reached the host (the start call cannot have created a job there).
NOT_STARTED = re.compile(r"connect to host|connection refused|no route to host|could not resolve|timed out|permission denied|"
                         r"host key verification failed|network is unreachable|banner exchange|kex_exchange", re.IGNORECASE)
POLL_SEC = 5.0
#: Reasons of a run that ended `BLOCKED` only because the host's slots (or its Editor guard or project lease) were taken a moment before.
SLOT_BUSY = re.compile(
    r"(?:all \d+ slots of unity-editor are leased|unity-editor(?::slot\d+)? is (?:leased|held|next for)|is next for .+ who has waited longer"
    r"|unity-project:[0-9a-f]+ is (?:leased|held)|the project folder .+ is at .+, not the job's commit|a Unity Editor runs outside any lease|MiB of memory are free)", re.IGNORECASE)


class OffloadError(Exception):
    """The run cannot be placed on the host; the message is the reason (the run goes back to the local queue)."""


class StartUncertain(OffloadError):
    """The call that starts the run on the host ended without an answer after it may have reached it: a job may be running there."""


def slot_busy(reason: str | None) -> bool:
    return bool(reason) and SLOT_BUSY.search(reason) is not None


# -- what a host tells about itself ---------------------------------------------------------------------------------------------------


def host_report(home: Path | None = None) -> dict:
    """`valtrion lease slots`: the Editor slots of this host and what a sender needs to know before it sends work here."""
    capacity = lease.unity_editor_capacity(home)
    manager = lease.LeaseManager(home)
    offload = config.load_offload_settings(home)
    projects = [project.name for project in config.load_host_settings(home).projects]
    return {"host": core.local_host_id(), "os": core.local_os(), "version": __version__,
            "unityEditor": manager.slot_report(lease.UNITY_EDITOR_RESOURCE, capacity),
            "unityVersions": [editor["version"] for editor in doctor.find_editors(core.local_os())],
            "offload": {"accept": offload.accept, "kinds": list(offload.kinds), "maxBundleMiB": offload.max_bundle_mib}, "projects": projects}


def free_slots(report: Mapping[str, Any]) -> int:
    """Slots a newcomer could take now: none while somebody waits for them (the waiter is first in line)."""
    slots = report.get("unityEditor") or {}
    return int(slots.get("free", 0)) if not slots.get("waiting") else 0


def ineligible(report: Mapping[str, Any], kind: str, unity_version: str | None, names: Sequence[str]) -> tuple[str | None, str | None]:
    """(why the host cannot take the work, None) or (None, the name of its managed project)."""
    offload = report.get("offload") or {}
    if not offload.get("accept"):
        return "does not accept offloaded work (host.json offload.accept)", None
    if kind not in (offload.get("kinds") or ()):
        return f"does not accept offloaded work of kind {kind}", None
    if report.get("version") != __version__:
        return f"runs valtrion {report.get('version')}, this is {__version__}; run `valtrion hosts sync`", None
    if unity_version and unity_version != "unknown" and unity_version not in (report.get("unityVersions") or ()):
        return f"Unity {unity_version} is not installed", None
    known = report.get("projects") or ()
    project = next((name for name in names if name in known), None)
    if project is None:
        return f"has no managed project {' or '.join(repr(name) for name in names)} (host.json projects)", None
    return None, project


@dataclass(frozen=True)
class Target:
    entry: HostEntry
    report: dict
    project: str


def survey(entries: Sequence[HostEntry], remote_for: Callable[[HostEntry], remote.Remote], kind: str, unity_version: str | None,
           names: Sequence[str]) -> tuple[list[Target], list[str]]:
    """Ask every offload host for its slots. Returns the hosts that can take this work (free or not) and, for the others, why not."""
    targets: list[Target] = []
    reasons: list[str] = []
    for entry in sorted(entries, key=lambda item: item.id):
        try:
            session = remote_for(entry)
            result, document = session.call_json(["lease", "slots"], SLOTS_TIMEOUT_SEC)
        except (ValueError, OSError) as error:
            reasons.append(f"{entry.id}: {error}")
            continue
        if document is None:
            reasons.append(f"{entry.id}: {remote.describe_probe_failure(result, session.attempts)}")
            continue
        why, project = ineligible(document, kind, unity_version, names)
        if why:
            reasons.append(f"{entry.id}: {why}")
        else:
            targets.append(Target(entry, document, project or ""))
    return targets, reasons


# -- the receiving host: take the code in ----------------------------------------------------------------------------------------------


def accept_import(project: str, sha: str, bundle: Path | None, kind: str, home: Path | None = None,
                  git: compare.GitRunner = compare.run_git, slow_git: compare.GitRunner | None = None) -> dict:
    """`valtrion offload import`: choose a free managed checkout of `project` and make `sha` available in it (from the bundle, else from
    the project's remote). Returns `{checkoutId, sha, project, folder}`; `OffloadError` says why the work cannot be taken."""
    from . import checkout as checkouts

    settings = config.load_offload_settings(home)
    if not settings.accept:
        raise OffloadError("this host does not accept offloaded work (host.json offload.accept)")
    if kind not in settings.kinds:
        raise OffloadError(f"this host does not accept offloaded work of kind {kind}")
    managed = config.load_host_settings(home).project(project)
    if managed is None:
        raise OffloadError(f"host.json declares no project {project!r}")
    manager = lease.LeaseManager(home)
    order = sorted(managed.checkouts, key=lambda item: item.id != managed.default)
    chosen = next((item for item in order if manager.status(lease.unity_project_resource(managed.project_dir(item))).state != "held"), None)
    if chosen is None:
        if bundle is not None:
            try:
                bundle.unlink()
            except OSError:
                pass
        raise OffloadError(f"every checkout of project {project} is in use")
    if bundle is not None:
        try:
            size = bundle.stat().st_size
            if size > settings.max_bundle_mib * 1024 * 1024:
                raise OffloadError(f"the bundle is {size // (1024 * 1024)} MiB, more than offload.maxBundleMiB ({settings.max_bundle_mib})")
            problem = codesync.import_bundle(chosen.path, bundle, sha, git, slow_git, managed.remote)
        except OSError as error:
            raise OffloadError(f"the bundle cannot be read: {error}") from error
        finally:
            try:
                bundle.unlink()
            except OSError:
                pass
    else:
        problem = checkouts.preflight(chosen.path, sha, managed.remote, git, slow_git)
    if problem:
        raise OffloadError(problem)
    return {"checkoutId": chosen.id, "sha": sha, "project": project, "folder": str(chosen.path)}


# -- the sending host ------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    checkout_id: str
    sha: str  # the commit the host checks out
    base_sha: str  # the commit the result names
    dirty: bool
    note: str


def prepare(target: Target, project_dir: Path, remote_for: Callable[[HostEntry], remote.Remote], settings: config.OffloadSettings,
            git: compare.GitRunner = compare.run_git, kind: str = "editor",
            snapshot: Callable[..., codesync.Snapshot] = codesync.snapshot) -> Prepared:
    """Make the code of `project_dir` available on the host: take a snapshot, send the bundle when one is needed, and let the host pick a
    checkout. `codesync.SnapshotError` (an uncommitted LFS file, a bundle that is too large) and `OffloadError` end the attempt."""
    with tempfile.TemporaryDirectory(prefix="valtrion-offload-") as staging:
        taken = snapshot(project_dir, git, Path(staging), settings.max_bundle_mib * 1024 * 1024)
        session = remote_for(target.entry)
        arguments = ["offload", "import", "--project", target.project, "--sha", taken.sha, "--kind", kind]
        if taken.bundle is not None:
            name = f"1-{taken.sha[:12]}.bundle"
            try:
                inbox = submit.push_files(session, target.entry, [(taken.bundle, name)])
            except submit.SubmitError as error:
                raise OffloadError(str(error)) from error
            arguments += ["--bundle", submit.join_path(inbox, name, target.entry.os)]
        result, document = session.call_json(arguments, 900)
    if document is None or "checkoutId" not in document:
        raise OffloadError(evidence(result) or f"host {target.entry.id} did not accept the code (exit {result.returncode})")
    if taken.dirty:
        note = f"uncommitted changes ({taken.changed} path(s)) sent as a bundle on top of {taken.base_sha[:12]}"
    elif taken.bundle is not None:
        note = f"{taken.unpushed} commit(s) not on the project's remote sent as a bundle"
    else:
        note = "commit fetched by the host from the project's remote"
    return Prepared(document["checkoutId"], taken.sha, taken.base_sha, taken.dirty, note)


@dataclass
class Outcome:
    status: str
    reason: str | None
    cancelled: bool = False
    #: Set when the run is to be run again here: the host was lost after the remote run started.
    rerun: bool = False


@dataclass
class Placement:
    """What a job knows about the run it may offload (`spec.offload`)."""
    kind: str
    project_dir: Path
    names: list[str]
    unity_version: str | None = None
    force: bool = False  # `--offload`: a refusal ends the run `BLOCKED` instead of going back to the local queue
    extra: dict = field(default_factory=dict)

    @staticmethod
    def from_spec(spec: Mapping[str, Any]) -> "Placement":
        return Placement(spec["kind"], Path(spec["projectDir"]), list(spec["names"]), spec.get("unityVersion"), bool(spec.get("force")),
                         dict(spec.get("extra") or {}))


class Offloader:
    """Places one run (see the module text). Everything that touches the outside world is passed in, so tests drive it with fakes."""

    def __init__(self, placement: Placement, *, emit: Callable[..., Any], cancelled: Callable[[], bool], directory: Path,
                 forward: Callable[[HostEntry, Target, Prepared, str], dict], settings: config.OffloadSettings | None = None,
                 local_room: Callable[[], bool] | None = None, entries: Sequence[HostEntry] | None = None,
                 remote_for: Callable[[HostEntry], remote.Remote] | None = None, home: Path | None = None,
                 git: compare.GitRunner = compare.run_git, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, snapshot: Callable[..., codesync.Snapshot] = codesync.snapshot,
                 pull: Callable[..., Any] = remote.pull_run, transport_for: Callable[[HostEntry], Any] | None = None) -> None:
        self.placement = placement
        self.emit = emit
        self.cancelled = cancelled
        self.directory = directory
        self.forward = forward
        self.home = home
        self.settings = settings or config.load_offload_settings(home)
        self.entries = list(entries) if entries is not None else [e for e in config.load_hosts(home) if e.kind == "ssh" and e.offload]
        self._remote_for = remote_for or (lambda entry: remote.remote_for(entry, None))
        self._transport_for = transport_for or remote.make_transport
        self._local_room = local_room or self._default_local_room
        self.git = git
        self.sleep = sleep
        self.monotonic = monotonic
        self.snapshot = snapshot
        self.pull = pull
        self.refused: list[str] = []
        self._announced = False
        self._given_up: set[str] = set()  # hosts that refused for a reason that waiting does not change

    def _default_local_room(self) -> bool:
        report = lease.LeaseManager(self.home).slot_report(lease.UNITY_EDITOR_RESOURCE, lease.unity_editor_capacity(self.home))
        return free_slots({"unityEditor": report}) > 0

    def log(self, message: str, **data: Any) -> None:
        self.emit("log", message, data={"offload": data} if data else None)

    # -- placement ---------------------------------------------------------------------------------------------------------------------

    def run(self) -> Outcome | None:
        """None: run here (the caller goes on with the local queue). An `Outcome`: the run was done on another host, or cannot go on."""
        if not self.entries or self._local_room():
            return None
        deadline = self.monotonic() + (self.settings.settle_sec if not self.placement.force else max(self.settings.settle_sec, 60.0))
        while True:
            if self.cancelled():
                return None
            targets, reasons = survey(self.entries, self._remote_for, self.placement.kind, self.placement.unity_version, self.placement.names)
            if not targets:
                if reasons and not self._announced:
                    self.log("no host can take this run: " + "; ".join(reasons), considered=reasons)
                    self._announced = True
                return self._refusal("; ".join(reasons)) if self.placement.force else None
            for target in (t for t in targets if free_slots(t.report) > 0 and t.entry.id not in self._given_up):
                outcome = self._try(target)
                if outcome is not None:
                    return outcome
                if self.refused and self.placement.force and self.refused[-1].startswith("BLOCKED"):
                    return Outcome(constants.BLOCKED, self.refused[-1][len("BLOCKED: "):])
            if self.monotonic() >= deadline or self._local_room():
                return self._refusal("no host has a free Editor slot") if self.placement.force else None
            self.sleep(min(POLL_SEC, max(0.0, deadline - self.monotonic())))

    def _refusal(self, reason: str) -> Outcome:
        return Outcome(constants.BLOCKED, f"--offload: {reason}")

    def _try(self, target: Target) -> Outcome | None:
        entry = target.entry
        self.log(f"local Editor slots are busy; trying host {entry.id}", host=entry.id, free=free_slots(target.report))
        try:
            prepared = prepare(target, self.placement.project_dir, self._remote_for, self.settings, self.git, self.placement.kind, self.snapshot)
        except codesync.SnapshotError as error:
            self.refused.append(f"BLOCKED: {error}")
            self.log(f"the code cannot be sent: {error}", host=entry.id, refused=str(error))
            self._given_up.update(t.entry.id for t in [target])
            self.entries = []  # the same code is refused by every host
            return None
        except (OffloadError, ValueError, OSError) as error:
            self.refused.append(str(error))
            self.log(f"host {entry.id} cannot take the run: {error}", host=entry.id, refused=str(error))
            if not slot_busy(str(error)) and "in use" not in str(error):
                self._given_up.add(entry.id)
            return None
        reason = "every local Editor slot was busy"
        try:
            started = self.forward(entry, target, prepared, reason)
        except StartUncertain as error:
            return self._lost(entry, None, f"the start on host {entry.id} was not answered ({error})")
        except OffloadError as error:
            self.refused.append(str(error))
            self.log(f"host {entry.id} did not start the run: {error}", host=entry.id, refused=str(error))
            self._given_up.add(entry.id)
            return None
        self.log(f"running on host {entry.id} as job {started['jobId']}: {prepared.note}", host=entry.id, job=started["jobId"], note=prepared.note)
        try:  # read by the dashboard: this job is only following a run on another host
            core.atomic_write_json(self.directory / MARKER, {"host": entry.id, "job": started["jobId"], "note": prepared.note, "reason": reason})
        except OSError:
            pass
        try:
            return self._follow(entry, started)
        except Exception as error:  # noqa: BLE001 - the remote run exists: whatever goes wrong here must not start it a second time
            return self._lost(entry, started["jobId"], f"following job {started['jobId']} on host {entry.id} failed: {type(error).__name__}: {error}")

    def _cancel(self, entry: HostEntry, job_id: str) -> bool:
        try:
            return self._remote_for(entry).call(["job", "cancel", job_id], 60).ok
        except (ValueError, OSError):
            return False

    def _lost(self, entry: HostEntry, job_id: str | None, reason: str) -> Outcome:
        """The run may exist on the host and its end was not seen: cancel it when its id is known, then run it again here when allowed."""
        if job_id is not None:
            self._cancel(entry, job_id)
        if self.settings.rerun_local and not self.cancelled():
            self.log(f"{reason}; the run is run again here", host=entry.id, lost=True)
            return Outcome(constants.BLOCKED, reason, rerun=True)
        where = f"check `valtrion job status {job_id} --host {entry.id}`" if job_id else f"check host {entry.id} with `valtrion lease slots --host {entry.id}`"
        return Outcome(constants.BLOCKED, f"{reason}, so the result is unknown; {where}")

    # -- following the remote job --------------------------------------------------------------------------------------------------------

    def _follow(self, entry: HostEntry, started: Mapping[str, Any]) -> Outcome | None:
        job_id = started["jobId"]
        session = self._remote_for(entry)
        cancel_sent = [False]

        def cancel_remote() -> None:
            if not cancel_sent[0]:
                cancel_sent[0] = self._cancel(entry, job_id)  # a cancel that did not get through is tried again

        def fetch(offset: int) -> dict | None:
            if self.cancelled():
                cancel_remote()
            result = session.call(["job", "tail", job_id, "--from", str(offset)], 60)
            if result.timed_out or result.returncode in (remote.SSH_FAILURE, None):
                return None
            if not result.ok:
                raise jobs.JobError((result.stderr or result.stdout).strip()[:300] or "the host rejected the request")
            return remote.extract_json_object(result.stdout)

        def mirror(line: str) -> None:
            self._mirror(line, entry.id, job_id)

        outcome = jobs.watch(fetch, mirror, 0, None, 2.0, self.settings.lost_sec, self.sleep, self.monotonic)
        if outcome.state not in ("finished", "cancelled"):
            return self._lost(entry, job_id, f"host {entry.id} was lost while the run was in progress ({outcome.reason})")
        info = outcome.info or {}
        pulled = self.directory / "remote"
        clean_pulled(self.directory)  # nothing of an earlier attempt may be taken for this one
        document = None
        if info.get("dir"):
            copied = self.pull(self._transport_for(entry), info["dir"], pulled)
            if copied.ok:
                document = self._read_result(pulled / "result.json")
            else:
                self.log(f"the run folder of job {job_id} could not be collected: {(copied.stderr or '').strip()[:200]}", host=entry.id)
        status, reason = outcome.status, outcome.reason
        if document is not None:
            status, reason = document.get("status", status), document.get("reason", reason)
        if status == constants.BLOCKED and slot_busy(reason) and outcome.state == "finished":
            self.log(f"host {entry.id} had no free slot after all ({reason}); the run stays in the local queue", host=entry.id, raced=True)
            clean_pulled(self.directory)
            return None
        if document is not None:
            core.atomic_write_json(self.directory / "result.json", document)
        return Outcome(status or constants.FAIL, reason, cancelled=outcome.state == "cancelled")

    @staticmethod
    def _read_result(path: Path) -> dict | None:
        try:
            document = core.read_json(path)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _mirror(self, line: str, host: str, job: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("type") in ("job.started", "job.finished"):
            return  # the local job has its own start and end
        kind = event.get("type") if event.get("type") in jobs.EVENT_TYPES else "log"
        data = dict(event.get("data") or {})
        data["remote"] = {"host": host, "job": job, "utc": event.get("utc")}
        message = event.get("message")
        self.emit(kind, f"[{host}] {message}" if message else None, step=event.get("step"), status=event.get("status"), data=data)


def clean_pulled(directory: Path) -> None:
    shutil.rmtree(directory / "remote", ignore_errors=True)
