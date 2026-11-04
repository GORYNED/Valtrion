"""Scratch copies: temporary working folders an agent asks Valtrion for, with a lifecycle Valtrion can finish on its own.

An agent that needs a working copy outside a job (a clean-checkout reproduction of a branch, a dependency's test project at a
pinned commit) creates it with `valtrion workroot scratch create`. It lives in `<work root>/scratch/<id>/` (`<id>` is the name the
agent gave plus 8 random hex digits) and carries a marker file `.valtrion-scratch.json`: who made it (the owner string, as leases
use it, and the host user), when, from which source and commit, the job or lease it depends on and when it expires. With `--sha` the
folder holds a detached checkout in `checkout/`, hanging off the project's mirror like a job's checkout.

Removal is the only dangerous step, so every removal (`release`, `sweep`, the pre-build disk cleanup) goes through the same guards:

- the folder is `<work root>/scratch/<id>` for a well-formed id: the scratch folder and the entry are real folders, not symlinks or
  junctions, and their resolved paths are exactly where they should be (nothing resolves outside the work root);
- the marker is Valtrion's (`marker` = `valtrion.scratch/1`), names the folder's id and names an owner and a host user;
- the folder belongs to the caller: on POSIX the folder's file owner is the calling user, the marker's host user is the calling user,
  and the marker's owner is the caller's (`GDV_OWNER`, else `user@host`) unless the caller passes `--any-owner`;
- nothing uses it: no held `unity-project:` lease of the folder or of a Unity project inside it, no held lease whose worktree lies
  inside it, the lease the marker depends on is not held, and no process names the folder on its command line or has its working
  directory there (Linux: `/proc`, macOS: `lsof`; Windows does not expose it). When the process list cannot be read, or it does
  not even contain this process, nothing is removed.

A sweep removes only a folder that is due (its readable expiry passed, or the job it depends on is recorded as finished or
cancelled) and in which nothing changed for the idle time; that is measured over the whole tree, and a tree that cannot be walked
within a time budget counts as in use.

The folder is first renamed to a tombstone name inside `scratch/` (on Windows this fails while a file inside is open), checked once
more, and then removed: its content first and the marker last. A removal that fails half way renames it back, so it still carries
its marker and can be released again. A symlink or junction inside the folder is removed as a link; what it points to is never
touched.

Everything else in the work root that is not one of Valtrion's folders (`jobs`, `mirrors`, `launchd`, `scratch`, `tools`) is
*unowned*: `doctor`, `workroot show` and `workroot sweep` list it (name, size, age, a lease or process that refers to it) and never
remove it. `valtrion workroot adopt PATH --yes` (the owner's confirmation) moves such a folder into `scratch/` with a marker, so the
normal expiry applies; a git repository with uncommitted changes, stashes or commits on no remote is refused unless
`--discard-unsaved-work` is given as well.
"""

from __future__ import annotations

import getpass
import math
import os
import re
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from . import artifacts, core, lease, workroot

MARKER = ".valtrion-scratch.json"
MARKER_KIND = "valtrion.scratch/1"
CHECKOUT = "checkout"
TOMBSTONE_PREFIX = ".removing-"
#: The entries of the work root that are Valtrion's own; anything else there is unowned.
RESERVED = ("jobs", "mirrors", "launchd", "scratch", "tools")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
SCRATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}-[0-9a-f]{8}")
#: How deep below a folder a Unity project (a folder with `ProjectSettings/`) is looked for, for its `unity-project:` lease.
PROJECT_DEPTH = 3
_SKIP_WHEN_LOOKING_FOR_PROJECTS = {".git", "library", "temp", "logs", "obj", "node_modules"}
_BOUNDARY = "\\/\"' \t"
#: Job states after which a job never runs again (`jobs.py`).
ENDED_JOB_STATES = ("finished", "cancelled")
#: How long the sweep may walk one folder to find its newest change; a folder not walked within it counts as in use.
IDLE_WALK_BUDGET_SEC = 30.0
#: How long `unowned` may spend measuring sizes in total; entries left over are reported without a complete size.
SIZE_BUDGET_SEC = 10.0
LSOF_TIMEOUT_SEC = 20.0

#: Returns the processes of the host (objects with `pid`, `ppid`, `path`, `command` and optionally `cwd`), or None when they
#: cannot be read.
ProcessList = Callable[[], "Sequence[Any] | None"]
#: The recorded state of a job (`queued`, `running`, `finished`, `cancelled`), or None when no record of it exists here.
JobState = Callable[[str], "str | None"]


class ScratchError(Exception):
    """A scratch operation was refused (the reason says why) or failed."""


@dataclass(frozen=True)
class Caller:
    """Who asks: the owner string (as leases record it), the host user and, on POSIX, the user id."""
    owner: str
    user: str
    uid: int | None = None


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser raises various errors without a passwd entry or USERNAME
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def caller(owner: str | None = None) -> Caller:
    """The calling process: `owner`, else `GDV_OWNER`, else `user@host` (the default owner of a lease)."""
    user = current_user()
    return Caller(owner or os.environ.get("GDV_OWNER") or f"{user}@{core.local_host_id()}", user,
                  os.getuid() if hasattr(os, "getuid") else None)


def job_state(job_id: str, home: Path | None = None) -> str | None:
    """The state recorded for `job_id` in this host's job index, or None when there is no readable record."""
    from . import jobs  # jobs imports much of the package; only these lookups need it
    try:
        document = core.read_json(jobs.find_job_directory(job_id, home) / "job.json", attempts=2)
    except (jobs.JobError, OSError, ValueError):
        return None
    state = document.get("state") if isinstance(document, dict) else None
    return state if isinstance(state, str) else None


# -- processes --------------------------------------------------------------------------------------------------------------


def parse_lsof_cwd(text: str) -> dict[int, str]:
    """Output of `lsof -a -d cwd -Fn` (`p<pid>` and `n<path>` lines): the working directory of each process."""
    found: dict[int, str] = {}
    pid = None
    for line in text.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            found[pid] = line[1:]
    return found


def _working_directories(os_name: str) -> "dict[int, str] | None":
    """pid -> working directory where the system tells it: Linux `/proc`, macOS `lsof`. None when it should but cannot be read
    (macOS without a working `lsof`); empty on Windows, which does not expose the working directory of another process."""
    if os_name == "linux":
        found = {}
        for entry in os.listdir("/proc") if os.path.isdir("/proc") else []:
            if entry.isdigit():
                try:
                    found[int(entry)] = os.readlink(f"/proc/{entry}/cwd")
                except OSError:
                    continue
        return found
    if os_name == "macos":
        try:
            done = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn"], capture_output=True, timeout=LSOF_TIMEOUT_SEC, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        text = done.stdout.decode("utf-8", "replace")
        found = parse_lsof_cwd(text)
        return found if found else None  # lsof exits 1 when some process cannot be read; an empty answer is no answer
    return {}


def system_processes() -> "list[Any] | None":
    """The host's processes with their working directory where available. The list itself comes from the Unity guard's probe
    (`ps` on POSIX, Win32_Process in UTF-8 on Windows)."""
    from . import unityguard
    os_name = core.local_os()
    listed = unityguard.SystemProbe(os_name).processes()
    if listed is None:
        return None
    cwds = _working_directories(os_name)
    if cwds is None:
        return None
    return [SimpleNamespace(pid=proc.pid, ppid=proc.ppid, path=proc.path, command=proc.command, cwd=cwds.get(proc.pid))
            for proc in listed]


# -- paths ------------------------------------------------------------------------------------------------------------------


def _folds_case() -> bool:
    return core.local_os() in ("windows", "macos")  # their default file systems ignore case


def _key(text: str) -> str:
    """`text` (a path or a name) in the form two spellings of one place share."""
    text = os.path.normcase(text)
    if os.name == "nt":
        text = text.replace("/", "\\")
    return text.lower() if _folds_case() else text


def _real(path: Path) -> str:
    return _key(os.path.realpath(path))


def is_link(path: Path) -> bool:
    """Whether `path` itself is a symlink or, on Windows, any reparse point (a junction, a mount point). Not followed."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _scratch_root(roots: workroot.WorkRoots) -> Path:
    """`<work root>/scratch`, refused when it is a link or resolves anywhere but directly below the work root."""
    folder = roots.scratch
    if is_link(folder):
        raise ScratchError(f"{folder} is a symlink or junction; scratch directories must be real folders in the work root")
    if folder.exists() and _real(folder) != os.path.join(_real(roots.root), _key("scratch")):
        raise ScratchError(f"{folder} resolves to {os.path.realpath(folder)}, outside the work root {roots.root}")
    return folder


def _check_place(base: Path, folder: Path) -> None:
    """`folder` is a real directory (not a link) that resolves to exactly `base`/its name."""
    if is_link(folder):
        raise ScratchError(f"{folder} is a symlink or junction, not a scratch directory; it is never removed")
    if not folder.exists():
        raise ScratchError(f"there is no scratch directory {folder.name} in {base}")
    if not folder.is_dir():
        raise ScratchError(f"{folder} is not a directory")
    if _real(folder) != os.path.join(_real(base), _key(folder.name)):
        raise ScratchError(f"{folder} resolves to {os.path.realpath(folder)}, outside {base}")


def locate(roots: workroot.WorkRoots, scratch_id: str) -> Path:
    """The scratch folder of `scratch_id`, after the path guards (see the module text). Raises ScratchError."""
    if not isinstance(scratch_id, str) or not SCRATCH_ID.fullmatch(scratch_id):
        raise ScratchError(f"{scratch_id!r} is not a scratch id (a name of letters, digits, '.', '_', '-' and 8 hex digits, as "
                           "`workroot scratch create` prints it)")
    base = _scratch_root(roots)
    folder = base / scratch_id
    _check_place(base, folder)
    return folder


def read_marker(folder: Path, scratch_id: str | None = None) -> dict:
    """The marker of the scratch folder `folder`, when it is Valtrion's, names `scratch_id` (default: the folder's name) and names
    an owner and a host user. Raises ScratchError."""
    path = folder / MARKER
    expected = folder.name if scratch_id is None else scratch_id
    if is_link(path) or not path.is_file():
        raise ScratchError(f"{folder} has no Valtrion scratch marker ({MARKER}): it was not made by valtrion and is never removed")
    try:
        document = core.read_json(path, attempts=2)
    except (OSError, ValueError) as error:
        raise ScratchError(f"the marker of {folder} cannot be read ({error}); it is never removed") from error
    if not isinstance(document, dict) or document.get("marker") != MARKER_KIND:
        raise ScratchError(f"the marker of {folder} is not Valtrion's ({MARKER_KIND}); it is never removed")
    if document.get("id") != expected:
        raise ScratchError(f"the marker of {folder} names the id {document.get('id')!r}, not {expected!r}; it is never removed")
    for key in ("owner", "user"):
        if not isinstance(document.get(key), str) or not document[key]:
            raise ScratchError(f"the marker of {folder} names no {key}; it is incomplete and never removed")
    return document


# -- ownership and use ------------------------------------------------------------------------------------------------------


def owner_problem(folder: Path, marker: dict, who: Caller, any_owner: bool = False) -> str | None:
    """Why `who` may not remove `folder`, or None. Another host user's folder is never removed, whatever `any_owner` says, and
    neither is one whose marker names no owner or host user."""
    if who.uid is not None:
        try:
            uid = os.lstat(folder).st_uid
        except OSError as error:
            return f"cannot be checked ({error})"
        if uid != who.uid:
            return f"belongs to another host user (file owner uid {uid}, not {who.uid}); skipped"
    user, owner = marker.get("user"), marker.get("owner")
    if not isinstance(user, str) or not user or not isinstance(owner, str) or not owner:
        return "has a marker that names no owner or host user; skipped"
    if _key(user) != _key(who.user):
        return f"was made by the host user {user}, not {who.user}; skipped"
    if not any_owner and owner != who.owner:
        return (f"belongs to {owner!r}, not {who.owner!r}; release it as that owner (GDV_OWNER) or pass --any-owner to remove "
                "another owner's scratch directory")
    return None


def project_folders(folder: Path, depth: int = PROJECT_DEPTH) -> list[Path]:
    """`folder`, its `checkout/` and every Unity project (a folder with `ProjectSettings/`) up to `depth` levels below it. Links are
    not followed; `.git` and Unity's own folders are not searched."""
    found = [folder, folder / CHECKOUT]
    level = [folder]
    for _ in range(depth):
        below: list[Path] = []
        for parent in level:
            try:
                children = list(os.scandir(parent))
            except OSError:
                continue
            for child in children:
                try:
                    if not child.is_dir(follow_symlinks=False) or is_link(Path(child.path)):
                        continue
                except OSError:
                    continue
                if child.name.lower() in _SKIP_WHEN_LOOKING_FOR_PROJECTS:
                    continue
                path = Path(child.path)
                if (path / "ProjectSettings").is_dir():
                    found.append(path)
                below.append(path)
        level = below
    return found


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def held_leases(folder: Path, marker: dict, manager: lease.LeaseManager) -> list[str]:
    """The held leases that refer to `folder`: the `unity-project:` lease of the folder or a Unity project in it, a lease whose
    worktree lies inside it, and the lease the marker depends on. A stale lease (dead holder, expired TTL) does not count."""
    wanted = {lease.unity_project_resource(path) for path in project_folders(folder)}
    depends = marker.get("lease")
    if isinstance(depends, str) and depends:
        wanted.add(depends)
    real = _real(folder)
    found = []
    for status in manager.list_all():
        if status.state != "held":
            continue
        record = status.record
        inside = bool(record and record.worktree and _inside(_real(Path(record.worktree)), real))
        if status.resource in wanted or inside:
            found.append(f"{status.resource} ({record.describe() if record else 'an unreadable lock file'})")
    return found


def _needles(folder: Path) -> list[str]:
    return sorted({_key(str(folder.absolute())), _real(folder)})


def _mentions(text: str, needles: Sequence[str]) -> bool:
    """Whether `text` (a command line) names one of the paths `needles` (or a path below one), not merely a longer sibling name."""
    hay = _key(text)
    for needle in needles:
        start = 0
        while True:
            index = hay.find(needle, start)
            if index < 0:
                break
            end = index + len(needle)
            if end == len(hay) or hay[end] in _BOUNDARY:
                return True
            start = index + 1
    return False


def _family(processes: Sequence[Any]) -> set[int]:
    """This process and its ancestors: the caller itself names the folder (`workroot adopt PATH`) and is not a user of it."""
    by_pid = {proc.pid: proc for proc in processes}
    pid, seen = os.getpid(), set()
    while pid and pid not in seen:
        seen.add(pid)
        proc = by_pid.get(pid)
        if proc is None:
            break
        pid = proc.ppid
    return seen


def using_processes(folder: Path, processes: "Sequence[Any]") -> list[int]:
    """Ids of the processes (other than this one and its ancestors) whose command line or executable names `folder` or a path in
    it, or whose working directory (when the probe knows it) lies in it."""
    needles = _needles(folder)
    mine = _family(processes)
    found = []
    for proc in processes:
        if proc.pid in mine:
            continue
        cwd = getattr(proc, "cwd", None)
        if _mentions(f"{proc.path} {proc.command}", needles) or (cwd and any(_inside(_key(cwd), needle) for needle in needles)):
            found.append(proc.pid)
    return found


class _Processes:
    """The host's process list, read at most once per operation and only when a check needs it. A list that does not contain
    this very process is incomplete (another user's view, a broken probe) and counts as unreadable."""

    def __init__(self, source: ProcessList | None) -> None:
        self._source = source  # None: `system_processes`, looked up when needed
        self._read = False
        self._value: "Sequence[Any] | None" = None

    def get(self) -> "Sequence[Any] | None":
        if not self._read:
            self._read = True
            try:
                value = (self._source or system_processes)()
            except Exception:  # noqa: BLE001 - an unreadable process list means "unknown", which keeps every folder
                value = None
            if value is not None and not any(getattr(proc, "pid", None) == os.getpid() for proc in value):
                value = None
            self._value = value
        return self._value


def activity(folder: Path, marker: dict, manager: lease.LeaseManager, processes: "_Processes") -> str | None:
    """Why `folder` is in use (a held lease, a live process, or a process list that cannot be read), or None when it is idle."""
    leases = held_leases(folder, marker, manager)
    if leases:
        return "a lease refers to it: " + "; ".join(leases)
    listed = processes.get()
    if listed is None:
        return "the process list cannot be read (or is incomplete), so a process using it cannot be ruled out"
    pids = using_processes(folder, listed)
    if pids:
        return "a live process uses it (pid " + ", ".join(str(pid) for pid in pids) + ")"
    return None


# -- create, list, release --------------------------------------------------------------------------------------------------


def _public_source(source: str | None) -> str | None:
    """`source` without credentials in a URL (`https://user:token@host/...`), for the marker."""
    return re.sub(r"(://)[^/@]+@", r"\1", source) if source else source


def new_id(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ScratchError(f"{name!r} is not a scratch name: 1-40 letters, digits, '.', '_' or '-', starting with a letter or digit")
    return f"{name}-{uuid.uuid4().hex[:8]}"


def _check_hours(hours: float) -> float:
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours <= 0:
        raise ScratchError("the lifetime of a scratch directory must be a positive number of hours")
    return float(hours)


def _marker(scratch_id: str, name: str, who: Caller, command: str, created: datetime, hours: float, **extra: Any) -> dict:
    document = {"marker": MARKER_KIND, "id": scratch_id, "name": name, "owner": who.owner, "user": who.user,
                "host": core.local_host_id(), "session": os.environ.get("GDV_SESSION") or None, "command": command,
                "createdUtc": core.format_utc(created), "expiresUtc": core.format_utc(created + timedelta(hours=hours)),
                "maxAgeHours": hours, **extra}
    return {key: value for key, value in document.items() if value is not None}


def _new_folder(roots: workroot.WorkRoots, name: str) -> tuple[Path, str]:
    base = _scratch_root(roots)
    base.mkdir(parents=True, exist_ok=True)
    base = _scratch_root(roots)  # again, now that it exists: it must resolve where it should
    scratch_id = new_id(name)
    return base / scratch_id, scratch_id


def create(roots: workroot.WorkRoots, name: str, who: Caller, *, max_age_hours: float, command: str = "workroot scratch create",
           project: str | None = None, source: str | None = None, sha: str | None = None, job: str | None = None,
           lease_resource: str | None = None, run: workroot.Runner = workroot.run_git,
           clock: Callable[[], datetime] = core.now, job_state_of: JobState | None = None) -> tuple[Path, dict]:
    """A new scratch folder `<work root>/scratch/<name>-<hex>` with its marker; with `sha` (and `project`, `source`) a detached
    checkout of that commit in its `checkout/`. `job` must be a job this host knows. Returns (folder, marker). A checkout that
    fails leaves nothing behind."""
    hours = _check_hours(max_age_hours)
    if sha is not None:
        workroot.check_sha(sha)
        if not project or not source:
            raise ScratchError("a scratch checkout (--sha) needs --project and --source or --repo")
    if lease_resource is not None:
        try:
            lease.lease_file_name(lease_resource)
        except ValueError as error:
            raise ScratchError(str(error)) from error
    if job is not None and (job_state_of or job_state)(job) is None:
        raise ScratchError(f"there is no job {job!r} on this host (valtrion job list); a scratch directory can only depend on a "
                           "job that exists")
    folder, scratch_id = _new_folder(roots, name)
    folder.mkdir(mode=0o700)
    marker = _marker(scratch_id, name, who, command, clock(), hours, project=project, source=_public_source(source), sha=sha, job=job,
                     lease=lease_resource)
    try:
        core.atomic_write_json(folder / MARKER, marker)
        if sha is not None:
            workroot.add_worktree(roots, project, source, sha, folder / CHECKOUT, run)
    except BaseException:
        _remove_contents(folder)
        workroot.prune_mirrors(roots, run)
        raise
    return folder, marker


def _expires(marker: dict) -> float | None:
    value = marker.get("expiresUtc")
    if not isinstance(value, str):
        return None
    try:
        return core.parse_utc(value).timestamp()
    except ValueError:
        return None


def listing(roots: workroot.WorkRoots, clock: Callable[[], float] = time.time) -> list[dict]:
    """Every entry of `<work root>/scratch`: the valid scratch folders with their marker facts, and the others with why they are
    not valid (those are unowned and never removed). A folder whose expiry cannot be read is `expired: false` (it never expires)."""
    try:
        base = _scratch_root(roots)
    except ScratchError as error:
        return [{"name": "scratch", "path": str(roots.scratch), "valid": False, "problem": str(error)}]
    if not base.is_dir():
        return []
    items = []
    for entry in sorted(base.iterdir()):
        try:
            folder = locate(roots, entry.name)
            marker = read_marker(folder)
        except ScratchError as error:
            items.append({"name": entry.name, "path": str(entry), "valid": False, "problem": str(error)})
            continue
        expires = _expires(marker)
        item = {"id": marker["id"], "path": str(folder), "valid": True, "expired": expires is not None and expires <= clock()}
        if expires is None:
            item["problem"] = "the marker names no readable expiry: the sweep never removes it, release it by hand"
        for key in ("owner", "user", "createdUtc", "expiresUtc", "project", "sha", "source", "job", "lease", "adoptedFrom"):
            if key in marker:
                item[key] = marker[key]
        if (folder / CHECKOUT).is_dir():
            item["checkout"] = str(folder / CHECKOUT)
        items.append(item)
    return items


def _remove_link(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # a directory symlink or junction on Windows: removes the link, never what it points to


def _remove_contents(folder: Path) -> None:
    """Remove `folder`: its content first, the marker last, then the folder. A link inside is removed as a link."""
    for child in sorted(folder.iterdir()):
        if child.name == MARKER:
            continue
        if is_link(child):
            _remove_link(child)
        else:
            artifacts._remove(child)  # noqa: SLF001 - same package; rmtree does not follow links or junctions below it
    marker = folder / MARKER
    if marker.exists() or marker.is_symlink():
        marker.unlink()
    folder.rmdir()


def _remove_checked(roots: workroot.WorkRoots, scratch_id: str) -> Path:
    """Remove the scratch folder `scratch_id`: look at its path and marker once more, rename it to a tombstone inside `scratch/` (a
    folder swapped for a link after the checks is caught by the next look; on Windows the rename fails while a file inside is
    open), look again, then remove it. A removal that fails is renamed back, so the folder keeps its id and marker."""
    folder = locate(roots, scratch_id)
    read_marker(folder)
    base = folder.parent
    tombstone = base / f"{TOMBSTONE_PREFIX}{scratch_id}-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(folder, tombstone)
    except OSError as error:
        raise ScratchError(f"{folder} cannot be moved aside for removal (a file in it may be open): {error}") from error
    try:
        _check_place(base, tombstone)
        read_marker(tombstone, scratch_id)
        _remove_contents(tombstone)
    except BaseException:
        if tombstone.exists() and not folder.exists():
            try:
                os.rename(tombstone, folder)
            except OSError:
                pass
        raise
    return folder


def release(roots: workroot.WorkRoots, scratch_id: str, who: Caller, *, any_owner: bool = False, dry_run: bool = False,
            manager: lease.LeaseManager | None = None, processes: ProcessList | None = None,
            run: workroot.Runner = workroot.run_git) -> str:
    """Remove exactly the scratch folder `scratch_id`, after every guard of the module text. Returns its path; raises ScratchError
    with the reason when it is refused."""
    folder = locate(roots, scratch_id)
    marker = read_marker(folder)
    problem = owner_problem(folder, marker, who, any_owner)
    if problem:
        raise ScratchError(f"{folder} {problem}")
    busy = activity(folder, marker, manager or lease.LeaseManager(), _Processes(processes))
    if busy:
        raise ScratchError(f"{folder} is in use: {busy}; it is kept")
    if not dry_run:
        try:
            _remove_checked(roots, scratch_id)
        except OSError as error:
            raise ScratchError(f"{folder} could not be removed completely ({error}); it keeps its marker, release it again") from error
        workroot.prune_mirrors(roots, run)
    return str(folder)


# -- sweep ------------------------------------------------------------------------------------------------------------------


@dataclass
class SweepReport:
    removed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)  # (path, why it stays)


def recent_mtime(folder: Path, depth: int = 2) -> float:
    """Newest modification time of `folder` and the entries up to `depth` levels below it (links are not followed). For reports;
    removal decisions use `newest_change`."""
    newest = 0.0
    level = [folder]
    for current in range(depth + 1):
        below: list[Path] = []
        for path in level:
            try:
                newest = max(newest, os.lstat(path).st_mtime)
            except OSError:
                continue
            if current < depth and path.is_dir() and not is_link(path):
                try:
                    below.extend(path.iterdir())
                except OSError:
                    pass
        level = below
    return newest


def newest_change(folder: Path, budget_sec: float = IDLE_WALK_BUDGET_SEC, monotonic: Callable[[], float] = time.monotonic) -> float | None:
    """Newest modification time of anything in the tree of `folder` (folders included, so a created or removed file counts; links
    are not followed). None when the walk did not finish within `budget_sec` or a folder in it cannot be read: the caller must then
    treat the folder as in use."""
    deadline = monotonic() + budget_sec
    try:
        newest = os.lstat(folder).st_mtime
    except OSError:
        return None
    pending = [str(folder)]
    while pending:
        if monotonic() > deadline:
            return None
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    newest = max(newest, info.st_mtime)
                    if entry.is_dir(follow_symlinks=False) and not is_link(Path(entry.path)):
                        pending.append(entry.path)
        except OSError:
            return None
    return newest


def due(marker: dict, active_jobs: Sequence[str], now: float, job_state_of: JobState | None = None) -> str | None:
    """Why the scratch folder of `marker` may be removed by a sweep, or None: its expiry passed, or the job it depends on is
    recorded here as ended. A marker without a readable expiry is never due (only `release` removes it), and an unknown job is not
    an ended one."""
    expires = _expires(marker)
    if expires is None:
        return None
    if expires <= now:
        return f"it expired at {marker.get('expiresUtc')}"
    job = marker.get("job")
    if isinstance(job, str) and job and job not in set(active_jobs):
        state = (job_state_of or job_state)(job)
        if state in ENDED_JOB_STATES:
            return f"its job {job} has ended ({state})"
    return None


def sweep(roots: workroot.WorkRoots, who: Caller, active_jobs: Sequence[str], idle_sec: float, *, any_owner: bool = False,
          dry_run: bool = False, manager: lease.LeaseManager | None = None, processes: ProcessList | None = None,
          clock: Callable[[], float] = time.time, run: workroot.Runner = workroot.run_git, job_state_of: JobState | None = None,
          walk_budget_sec: float = IDLE_WALK_BUDGET_SEC) -> SweepReport:
    """Remove the scratch folders that are due (`due`) and in which nothing changed for `idle_sec` (`newest_change`), under every
    guard of the module text. An active or unexpired one, one in use, one that cannot be shown idle and one of another owner
    (unless `any_owner`) or host user stay, each with its reason."""
    report = SweepReport()
    try:
        base = _scratch_root(roots)
    except ScratchError as error:
        report.kept.append((str(roots.scratch), str(error)))
        return report
    if not base.is_dir():
        return report
    listed = _Processes(processes)
    leases = manager or lease.LeaseManager()
    now = clock()
    for entry in sorted(base.iterdir()):
        try:
            folder = locate(roots, entry.name)
            marker = read_marker(folder)
        except ScratchError as error:
            report.kept.append((str(entry), f"not a valid scratch directory, never removed: {error}"))
            continue
        why = due(marker, active_jobs, now, job_state_of)
        if why is None:
            until = marker.get("expiresUtc") if _expires(marker) is not None else "never: no readable expiry"
            report.kept.append((str(folder), f"not expired (until {until})"))
            continue
        problem = owner_problem(folder, marker, who, any_owner)
        if problem:
            report.kept.append((str(folder), problem))
            continue
        changed = newest_change(folder, walk_budget_sec)
        if changed is None:
            report.kept.append((str(folder), f"{why}, but its tree could not be walked within {walk_budget_sec:g} s, so it counts as in use"))
            continue
        if changed > now - idle_sec:
            report.kept.append((str(folder), f"{why}, but something in it changed within the last {idle_sec / 3600:g} h"))
            continue
        busy = activity(folder, marker, leases, listed)
        if busy:
            report.kept.append((str(folder), f"in use: {busy}"))
            continue
        if not dry_run:
            try:
                _remove_checked(roots, folder.name)
            except (OSError, ScratchError) as error:
                report.kept.append((str(folder), f"could not be removed: {error}"))
                continue
        report.removed.append(f"{folder} (scratch, {why})")
    if report.removed and not dry_run:
        workroot.prune_mirrors(roots, run)
    return report


# -- unowned entries and adoption -------------------------------------------------------------------------------------------


def _bytes(path: Path, deadline: float) -> tuple[int, bool]:
    """Bytes under `path` (links not followed) and whether the count is complete (it stops at `deadline`, a monotonic time)."""
    if is_link(path) or not path.is_dir():
        try:
            return os.lstat(path).st_size, True
        except OSError:
            return 0, True
    total = 0
    for folder, folders, files in os.walk(path):  # os.walk does not descend into links; a junction is pruned below
        if time.monotonic() > deadline:
            return total, False
        folders[:] = [name for name in folders if not is_link(Path(folder) / name)]
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total, True


def unowned(roots: workroot.WorkRoots, manager: lease.LeaseManager | None = None, processes: ProcessList | None = None,
            clock: Callable[[], float] = time.time, sizes: bool = True, size_budget_sec: float = SIZE_BUDGET_SEC) -> list[dict]:
    """The entries of the work root that are not Valtrion's (anything beside `jobs`, `mirrors`, `launchd`, `scratch`, `tools`, and
    any entry of `scratch/` without a valid marker): name, path, size, age, and the leases and processes that refer to it. Reported
    only: nothing here removes them. `processes` is `None` in an entry when the process list cannot be read; `bytesComplete` is
    false when the size budget ran out (then `bytes` is a lower bound)."""
    if not roots.root.is_dir():
        return []
    reserved = {_key(name) for name in RESERVED}
    entries: list[tuple[str, Path, str]] = []
    for item in sorted(roots.root.iterdir()):
        if _key(item.name) in reserved and not is_link(item):
            continue
        entries.append((item.name, item, "not a Valtrion folder"))
    try:
        base = _scratch_root(roots)
    except ScratchError:
        base = None  # a linked scratch folder is itself listed above
    if base is not None and base.is_dir():
        for item in sorted(base.iterdir()):
            try:
                read_marker(locate(roots, item.name))
            except ScratchError as error:
                entries.append((f"scratch/{item.name}", item, str(error)))
    if not entries:
        return []
    leases = manager or lease.LeaseManager()
    listed = _Processes(processes).get()
    now = clock()
    deadline = time.monotonic() + size_budget_sec
    found = []
    for name, path, reason in entries:
        try:
            refers = held_leases(path, {}, leases) if path.is_dir() and not is_link(path) else []
        except OSError:
            refers = []
        size, complete = _bytes(path, deadline) if sizes else (None, False)
        found.append({
            "name": name, "path": str(path), "kind": "link" if is_link(path) else "directory" if path.is_dir() else "file",
            "bytes": size, "bytesComplete": complete, "ageHours": round(max(0.0, now - recent_mtime(path)) / 3600, 1),
            "reason": reason, "leases": refers, "processes": None if listed is None else using_processes(path, listed)})
    return found


def describe_unowned(item: dict) -> str:
    """One warning line for an unowned entry, for `doctor` and `workroot show`. It names no ready-made removal or adoption command:
    only the owner decides that."""
    size = item.get("bytes")
    if size is None:
        amount = "size not measured"
    else:
        amount = f"{'at least ' if not item.get('bytesComplete', True) else ''}{size / 1024 ** 3:.1f} GiB"
    use = []
    if item.get("leases"):
        use.append("leased: " + "; ".join(item["leases"]))
    if item.get("processes") is None:
        use.append("processes unknown")
    elif item["processes"]:
        use.append("used by pid " + ", ".join(str(pid) for pid in item["processes"]))
    state = ", ".join(use) if use else "no lease or process refers to it"
    return (f"unowned work-root entry {item['name']} ({item['kind']}, {amount}, idle {item['ageHours']:g} h, {state}): valtrion never "
            "removes it; its owner either removes it or, after checking it holds nothing to keep, confirms `valtrion workroot adopt` "
            "for it so it expires as a scratch directory")


def unsaved_work(folder: Path, run: workroot.Runner = workroot.run_git) -> list[str]:
    """What would be lost in the git repositories of `folder` (the folder itself and its direct subfolders): uncommitted or untracked
    changes, stashes, commits that are on no remote. A repository git cannot answer for counts as unsaved."""
    repos = [folder]
    try:
        repos += [child for child in sorted(folder.iterdir()) if child.is_dir() and not is_link(child)]
    except OSError as error:
        return [f"{folder} cannot be read: {error}"]
    problems = []
    for repo in repos:
        if not (repo / ".git").exists():
            continue
        checks = (("status", ["status", "--porcelain"], "uncommitted or untracked changes"),
                  ("stash", ["stash", "list"], "stashed changes"),
                  ("rev-list", ["rev-list", "--max-count=1", "HEAD", "--branches", "--not", "--remotes"], "commits that are on no remote"))
        for name, argv, meaning in checks:
            code, output = run(argv, repo)
            if code != 0:
                problems.append(f"{repo}: git {name} failed ({' '.join(output.split())[:120]})")
                break
            if output.strip():
                problems.append(f"{repo}: {meaning}")
    return problems


def adopt(roots: workroot.WorkRoots, path: Path, who: Caller, *, max_age_hours: float, name: str | None = None,
          discard_unsaved_work: bool = False, manager: lease.LeaseManager | None = None, processes: ProcessList | None = None,
          clock: Callable[[], datetime] = core.now, run: workroot.Runner = workroot.run_git) -> tuple[Path, dict]:
    """Make the unowned folder `path` (a direct entry of the work root; a relative path is taken below the work root) a scratch
    folder: move it to `scratch/<name>-<hex>` and write its marker, so the normal expiry applies. Refused for a link or junction, a
    path that resolves anywhere else, one of Valtrion's folders, another host user's folder, a folder in use and, unless
    `discard_unsaved_work`, a git repository with work that exists nowhere else (`unsaved_work`). A git worktree of a mirror is not
    adopted correctly (moving it breaks its registration); a standalone clone is."""
    hours = _check_hours(max_age_hours)
    given = Path(path)
    candidate = given if given.is_absolute() else roots.root / given
    if is_link(candidate):
        raise ScratchError(f"{candidate} is a symlink or junction; only a real folder of the work root can be adopted")
    if not candidate.is_dir():
        raise ScratchError(f"{candidate} is not a directory")
    real = _real(candidate)
    if os.path.dirname(real) != _real(roots.root):
        raise ScratchError(f"{candidate} resolves to {os.path.realpath(candidate)}, which is not a direct entry of the work root "
                           f"{roots.root}; only an unowned entry there can be adopted")
    if os.path.basename(real) in {_key(item) for item in RESERVED}:
        raise ScratchError(f"{candidate} is one of valtrion's own folders, not an unowned entry")
    if who.uid is not None and os.lstat(candidate).st_uid != who.uid:
        raise ScratchError(f"{candidate} belongs to another host user; it is not adopted")
    busy = activity(candidate, {}, manager or lease.LeaseManager(), _Processes(processes))
    if busy:
        raise ScratchError(f"{candidate} is in use: {busy}; it is not adopted")
    if not discard_unsaved_work:
        unsaved = unsaved_work(candidate, run)
        if unsaved:
            raise ScratchError(f"{candidate} holds work that exists nowhere else ({'; '.join(unsaved)}); save it first, or pass "
                               "--discard-unsaved-work to adopt it anyway (it is then removed when it expires)")
    label = name if name is not None else (core.safe_segment(candidate.name, "adopted", 40).rstrip(".") or "adopted")
    folder, scratch_id = _new_folder(roots, label)
    os.replace(candidate, folder)  # the same volume: scratch/ is inside the work root
    marker = _marker(scratch_id, label, who, "workroot adopt", clock(), hours, adoptedFrom=str(candidate))
    try:
        core.atomic_write_json(folder / MARKER, marker)
    except OSError:
        os.replace(folder, candidate)
        raise
    return folder, marker
