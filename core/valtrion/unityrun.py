"""`valtrion unity run`: one Unity batch-mode command under Valtrion's leases and supervision, the same on Windows, macOS and Linux.

The run holds the lease on the project folder and then one slot of the host's `unity-editor` capacity (the same total every other claimant
counts) from before Unity starts until its process tree is gone, renews both while it waits and while Unity runs, and gives them back in
reverse order on every exit. Unity runs under the watchdog: a hard timeout, a stall window that follows the growth of the `-logFile`, and a
lost lease each end in killing the whole process tree. The last line printed on stdout is `STATUS <name>`.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Sequence, TextIO

from . import config, constants, core, lease, supervise
from .providers.unity import support

DEFAULT_LEASE_WAIT_SEC = 1800.0
DEFAULT_STALL_SEC = 600.0
LEASE_TTL_SEC = 300
LOG_TAIL_BYTES = 256 * 1024
CONSOLE_TAIL_BYTES = 64 * 1024

#: Texts in the tail of a Unity log that turn exit code 1 from a failed run into a missing prerequisite (an Editor that could not
#: open the project or has no license). Compared in lower case.
BLOCKED_LOG_MARKERS = (
    "another unity instance is running with this project open",
    "cannot open the same project",
    "no valid unity editor license",
    "unable to activate",
    "license activation failed",
    "license activation error",
)
#: Exit codes of a Unity batch-mode process: 0 ok, 2 tests failed, 3 the run could not start (for example a compile error).
UNITY_EXIT_CODES = {0: constants.PASS, 2: constants.FAIL, 3: constants.FAIL}


class Interrupted(BaseException):
    """SIGTERM, SIGBREAK or SIGHUP reached the command: unwind through the `finally` blocks that stop Unity and give the leases back."""


def option_value(args: Sequence[str], name: str) -> str | None:
    """The value after the last `-name` (Unity options are case-insensitive), or None."""
    found = None
    for position, item in enumerate(args[:-1]):
        if item.lower() == name.lower():
            found = args[position + 1]
    return found


def has_option(args: Sequence[str], *prefixes: str) -> bool:
    return any(item.lower().startswith(tuple(prefix.lower() for prefix in prefixes)) for item in args)


def _absolute(text: str) -> Path:
    return Path(text).expanduser().absolute()  # relative paths mean what they mean to Unity: from the working directory


def _result(out: TextIO, status: str, reason: str | None) -> int:
    if reason:
        print(reason, file=out)
    print(f"STATUS {status}", file=out)
    out.flush()
    return constants.exit_code_for_status(status)


def _explicit_editor(path: str) -> tuple[str | None, str | None]:
    candidate = _absolute(path)
    if candidate.is_dir() and candidate.suffix == ".app":
        candidate = candidate / "Contents" / "MacOS" / "Unity"
    if not candidate.is_file():
        return None, f"the Unity executable {candidate} does not exist"
    return str(candidate), None


def log_tail(path: Path | None, limit: int = LOG_TAIL_BYTES) -> str:
    if path is None:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def blocked_by_log(tail: str) -> str | None:
    lowered = tail.lower()
    for marker in BLOCKED_LOG_MARKERS:
        if marker in lowered:
            return marker
    return None


def classify(result: supervise.SuperviseResult, log: Path | None) -> tuple[str, str | None]:
    """Status of the finished run. Exit code 1 is BLOCKED when the log names a project or license problem, else FAIL."""
    status, reason = result.status, result.reason
    if status == constants.FAIL and result.returncode == 1:
        marker = blocked_by_log(log_tail(log))
        if marker is not None:
            return constants.BLOCKED, f"Unity exited with code 1 and its log says: {marker}"
    return status, reason


class _Handlers:
    """Turns termination signals into `Interrupted` for the duration of the run (only possible on the main thread)."""

    def __init__(self) -> None:
        self._saved: dict[int, object] = {}

    def __enter__(self) -> "_Handlers":
        names = ("SIGTERM", "SIGBREAK", "SIGHUP")
        for name in names:
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                self._saved[number] = signal.signal(number, self._raise)
            except (ValueError, OSError):  # not the main thread, or a signal this platform cannot catch
                pass
        return self

    @staticmethod
    def _raise(number: int, frame: object) -> None:
        raise Interrupted(f"signal {number}")

    def __exit__(self, *_: object) -> None:
        for number, previous in self._saved.items():
            try:
                signal.signal(number, previous)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):
                pass


def run(
        unity_args: Sequence[str], project_dir: Path | None = None, unity: str | None = None,
        lease_wait_sec: float = DEFAULT_LEASE_WAIT_SEC, timeout_sec: float = 0.0, stall_sec: float = DEFAULT_STALL_SEC,
        purpose: str = "", home: Path | None = None, out: TextIO | None = None, err: TextIO | None = None,
        ttl_sec: int = LEASE_TTL_SEC, poll_sec: float = 1.0, cancel_poll_sec: float = 1.0) -> int:
    """Run Unity with `unity_args`; returns the exit code of the final status (already printed as the last stdout line)."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = list(unity_args)

    # -- the project, the Editor and the Accelerator ---------------------------------------------------------------------
    given = option_value(args, "-projectPath")
    if given is not None:
        project = _absolute(given)
    elif project_dir is not None:
        project = project_dir.expanduser().absolute()
        args += ["-projectPath", str(project)]
    else:
        return _result(out, constants.BLOCKED, "no Unity project: pass -projectPath, --project-dir, set GDV_UNITY_PROJECT or add a valtrion.json")
    if not project.is_dir():
        return _result(out, constants.BLOCKED, f"the Unity project folder {project} does not exist")

    if unity:
        editor, problem = _explicit_editor(unity)
    else:
        found, problem = support.find_editor(support.project_unity_version(project), core.local_os(), os.environ)
        editor = found["path"] if found else None
    if editor is None:
        return _result(out, constants.BLOCKED, problem)

    try:
        if not has_option(args, "-EnableCacheServer", "-cacheServer"):
            args += config.unity_accelerator_args(home)
        capacity = lease.unity_editor_capacity(home)
        manager = lease.LeaseManager(home)
        project_resource = lease.unity_project_resource(project)
    except (config.ConfigError, ValueError, OSError) as error:
        return _result(out, constants.BLOCKED, str(error))

    log_option = option_value(args, "-logFile")
    log_file = _absolute(log_option) if log_option and log_option != "-" else None

    # -- leases: project first, then one editor slot, both within one shared wait budget -------------------------------------
    owner = os.environ.get("GDV_OWNER") or f"{_user()}@{core.local_host_id()}"
    pid = os.getpid()
    session = f"unity-run-{pid}"
    what = purpose or "valtrion unity run"
    deadline = time.monotonic() + max(0.0, lease_wait_sec)
    work = Path(tempfile.mkdtemp(prefix="valtrion-unity-"))
    keeper = lease.LeaseKeeper(manager, session, pid, ttl_sec / 3, owner)
    held: list[str] = []
    stop_watching = threading.Event()

    def waiting(reason: str, waited_sec: float) -> None:
        print(lease.waiting_message(reason, waited_sec), file=err)

    try:
        with _Handlers():
            try:
                taken = manager.acquire(project_resource, owner, session, ttl_sec, what, pid, wait_sec=max(0.0, deadline - time.monotonic()),
                                        poll_sec=poll_sec, on_wait=waiting)
                if not taken.acquired:
                    return _result(out, constants.BLOCKED, taken.reason)
                held.append(project_resource)
                keeper.add(project_resource)
                keeper.start()  # the project lease is renewed while this run waits for a slot
                slot = manager.acquire_slot(lease.UNITY_EDITOR_RESOURCE, capacity, owner, session, ttl_sec, what, pid,
                                            wait_sec=max(0.0, deadline - time.monotonic()), poll_sec=poll_sec, on_wait=waiting)
                if not slot.acquired:
                    return _result(out, constants.BLOCKED, slot.reason)
                held.append(slot.lease.resource)
                keeper.add(slot.lease.resource)
                if keeper.lost:
                    return _result(out, constants.BLOCKED, f"the lease(s) {', '.join(keeper.lost)} were lost before Unity started")

                console = work / "console.log"
                cancel = work / "cancel"
                watchdog = supervise.Watchdog(
                    timeout_sec if timeout_sec > 0 else float("inf"),
                    stall_sec if stall_sec > 0 and log_file is not None else float("inf"),
                    progress_files=(log_file,) if log_file is not None else (), log_counts=False, cancel_file=cancel)

                def watch_leases() -> None:
                    while not stop_watching.wait(cancel_poll_sec):
                        if keeper.lost:
                            cancel.touch()
                            return

                watcher = threading.Thread(target=watch_leases, name="valtrion-unity-lease-watch", daemon=True)
                watcher.start()
                outcome = supervise.supervise([editor, *args], watchdog, console, None, None, exit_codes=UNITY_EXIT_CODES)
                stop_watching.set()
                watcher.join(timeout=5)
                if outcome.cancelled or keeper.lost:
                    status, reason = constants.BLOCKED, f"the lease(s) {', '.join(keeper.lost)} were lost while Unity ran; Unity was stopped"
                else:
                    status, reason = classify(outcome, log_file or console)
                text = log_tail(console, CONSOLE_TAIL_BYTES)
                if text:
                    err.write(text if text.endswith("\n") else text + "\n")
                    err.flush()
                return _result(out, status, reason)
            except Interrupted as stop:
                return _result(out, constants.BLOCKED, f"interrupted ({stop}); Unity was stopped and the leases released")
            except KeyboardInterrupt:
                return _result(out, constants.BLOCKED, "interrupted; Unity was stopped and the leases released")
    finally:
        stop_watching.set()
        keeper.stop()
        for resource in reversed(held):  # the slot first, then the project
            try:
                manager.release(resource, session, pid)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)


def _user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name (a service account) must not stop a run
        return "unknown"
