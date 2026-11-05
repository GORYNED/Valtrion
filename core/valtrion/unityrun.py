"""`valtrion unity run`: one Unity batch-mode command under Valtrion's leases and supervision, the same on Windows, macOS and Linux.

The run holds the lease on the project folder and then one slot of the host's `unity-editor` capacity (the same total every other claimant
counts) from before Unity starts until its process tree is gone, renews both while it waits and while Unity runs, and gives them back in
reverse order on every exit. Unity runs under the watchdog: a hard timeout, a stall window that follows the growth of the `-logFile`, and a
lost lease each end in killing the whole process tree. The last line printed on stdout is `STATUS <name>`.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import sys
import tempfile
import _thread
import threading
import time
from pathlib import Path
from typing import Callable, Sequence, TextIO

from . import config, constants, core, lease, lfs, supervise, unityguard
from .providers.unity import support

DEFAULT_LEASE_WAIT_SEC = 1800.0
DEFAULT_STALL_SEC = 600.0
LEASE_TTL_SEC = 300
LOG_TAIL_BYTES = 256 * 1024
CONSOLE_TAIL_BYTES = 64 * 1024
#: Seconds between two looks at the process list and free memory while the run waits for them (a CIM query on Windows is not cheap).
GUARD_POLL_SEC = unityguard.GUARD_POLL_SEC

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


def normalize_assembly_names(args: Sequence[str]) -> list[str]:
    """Unity's `-assemblyNames` takes names separated by `;`. A united list from `valtrion select` (`{assemblies}`) is comma separated
    (a `;` would end the command in a shell), so the value of `-assemblyNames` is rewritten with `;`. Only this option: the
    names of `-testFilter` may hold commas (test parameters)."""
    result = list(args)
    for position, item in enumerate(result[:-1]):
        if item.lower() == "-assemblynames":
            names = [name.strip() for name in re.split(r"[;,]", result[position + 1]) if name.strip()]
            result[position + 1] = ";".join(names)
    return result


def _absolute(text: str) -> Path:
    return Path(text).expanduser().absolute()  # relative paths mean what they mean to Unity: from the working directory


def _result(out: TextIO, status: str, reason: str | None) -> int:
    try:
        if reason:
            print(reason, file=out)
        print(f"STATUS {status}", file=out)
        out.flush()
    except (BrokenPipeError, ValueError):  # the reader is gone (a dropped SSH session); the work is over either way
        pass
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


#: Windows console events that end the process: Ctrl+Break, the console window (or an SSH/ConPTY session) closing, log off, shutdown.
#: Ctrl+C is left to Python (`KeyboardInterrupt`). Windows ends the process a few seconds after a handler returns for these.
CONSOLE_STOP_EVENTS = frozenset({1, 2, 5, 6})
CONSOLE_CLEANUP_WAIT_SEC = 4.0


def console_stop_handler(interrupt: Callable[[], None], finished: threading.Event,
                         wait_sec: float = CONSOLE_CLEANUP_WAIT_SEC) -> Callable[[int], bool]:
    """The Windows console control handler: on a stop event interrupt the main thread (which kills Unity and releases the leases)
    and hold the handler until that is done or the wait ends. Returns whether it handled the event."""
    def handle(event: int) -> bool:
        if event not in CONSOLE_STOP_EVENTS:
            return False
        interrupt()
        finished.wait(wait_sec)
        return True

    return handle


def install_console_handler(handler: Callable[[int], bool]) -> Callable[[], None]:  # pragma: no cover - needs Windows
    """Register `handler` with SetConsoleCtrlHandler; returns the function that removes it."""
    import ctypes
    from ctypes import wintypes

    routine_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    routine = routine_type(lambda event: bool(handler(int(event))))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleCtrlHandler.argtypes = [routine_type, wintypes.BOOL]
    if not kernel32.SetConsoleCtrlHandler(routine, True):
        raise OSError(ctypes.get_last_error(), "SetConsoleCtrlHandler failed")

    def remove() -> None:
        kernel32.SetConsoleCtrlHandler(routine, False)

    _CONSOLE_ROUTINES.append(routine)  # never dropped: Windows may still be running the callback after the registration is removed
    return remove


_CONSOLE_ROUTINES: list = []


class _Handlers:
    """Turns termination signals into `Interrupted` for the duration of the run (only possible on the main thread). On Windows it also
    handles the console events that carry no Python signal (window or session closing). `finished` is set by the caller once the
    leases are released, which lets a waiting console handler return."""

    def __init__(self, windows: bool | None = None, install: Callable[[Callable[[int], bool]], Callable[[], None]] = install_console_handler,
                 interrupt: Callable[[], None] = _thread.interrupt_main) -> None:
        self._saved: dict[int, object] = {}
        self._windows = os.name == "nt" if windows is None else windows
        self._install = install
        self._interrupt = interrupt
        self._remove: Callable[[], None] | None = None
        self.finished = threading.Event()
        #: Set while the leases are being given back: a further signal then changes nothing (cleanup is not interrupted).
        self.cleaning = False
        #: Set by the first signal that raised `Interrupted`.
        self.fired = False

    def __enter__(self) -> "_Handlers":
        if self._windows:
            try:
                self._remove = self._install(console_stop_handler(self._interrupt, self.finished))
            except OSError:  # no console to hook (a service); the signals below still apply
                self._remove = None
        # SIGINT too: `_thread.interrupt_main` (the end of the caller's input, a Windows console event) arrives through it.
        names = ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP")
        for name in names:
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                self._saved[number] = signal.signal(number, self._raise)
            except (ValueError, OSError):  # not the main thread, or a signal this platform cannot catch
                pass
        return self

    def _raise(self, number: int, frame: object) -> None:
        # The first signal unwinds the run; every later one (also while its `except` body or the release is running) changes nothing.
        if self.cleaning or self.fired:
            return
        self.fired = True
        raise Interrupted(f"signal {number}")

    def __exit__(self, *_: object) -> None:
        if self._remove is not None:
            self._remove()
            self._remove = None
        for number, previous in self._saved.items():
            try:
                signal.signal(number, previous)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):
                pass


def run(
        unity_args: Sequence[str], project_dir: Path | None = None, unity: str | None = None,
        lease_wait_sec: float = DEFAULT_LEASE_WAIT_SEC, timeout_sec: float = 0.0, stall_sec: float = DEFAULT_STALL_SEC,
        purpose: str = "", home: Path | None = None, out: TextIO | None = None, err: TextIO | None = None,
        ttl_sec: int = LEASE_TTL_SEC, poll_sec: float = 1.0, cancel_poll_sec: float = 1.0,
        probe: unityguard.Probe | None = None, guard_poll_sec: float | None = None, lifeline: int | None = None,
        lfs_materialize: bool | None = None, lfs_call: lfs.LfsCall | None = None, lfs_free: Callable[[Path], int] | None = None) -> int:
    """Run Unity with `unity_args`; returns the exit code of the final status (already printed as the last stdout line).
    `lifeline`: a file descriptor whose end of input means the caller is gone (the standard input of a command run over SSH); the run is then
    interrupted like by Ctrl+C. `lfs_materialize`: run the Git LFS preflight (None: `lfs.materialize` of host.json, default on);
    `lfs_call` and `lfs_free` replace `git lfs` and the free-space probe in tests."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = normalize_assembly_names(unity_args)

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
        guard_settings = unityguard.load_settings(home)
        materialize = config.lfs_materialize(home) if lfs_materialize is None else lfs_materialize
        lfs_cache = lfs.cache_root(config.load_host_settings(home).work_root) if materialize else None

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
    #: Every lease this run may hold, in the order taken. A name is listed before its acquire call starts, so an interrupt between the
    #: moment a lease is created and the moment the run notes it still finds it at cleanup (releasing a lease of another holder does nothing).
    may_hold: list[str] = []
    stop_watching = threading.Event()
    handlers = _Handlers()
    guard_poll = guard_poll_sec if guard_poll_sec is not None else max(poll_sec, GUARD_POLL_SEC)

    def waiting(reason: str, waited_sec: float) -> None:
        _say(err, lease.waiting_message(reason, waited_sec))

    input_watcher: threading.Thread | None = None
    with handlers:
        if lifeline is not None:  # after the handlers: the end of the input must arrive as an `Interrupted`, not as a bare Ctrl+C
            input_watcher = threading.Thread(target=_watch_input, args=(lifeline, stop_watching, lambda text: _say(err, text)), name="valtrion-unity-input-watch", daemon=True)
            input_watcher.start()
        try:
            try:
                may_hold.append(project_resource)
                taken = manager.acquire(project_resource, owner, session, ttl_sec, what, pid, worktree=str(project),
                                        wait_sec=max(0.0, deadline - time.monotonic()), poll_sec=poll_sec, on_wait=waiting)
                if not taken.acquired:
                    return _result(out, constants.BLOCKED, taken.reason)
                keeper.add(project_resource)
                keeper.start()  # the project lease is renewed while this run waits for a slot
                if lfs_cache is not None:
                    # Under the project lease (no other run writes this tree) and before the slot (a download holds no Editor slot).
                    # Its time does not count against the lease wait: a long download must not leave the slot wait no budget.
                    preflight_started = time.monotonic()
                    preflight = lfs.materialize(project, lfs_cache, lfs=lfs_call or lfs.run_lfs, free=lfs_free or lfs.diskspace.free_bytes,
                                                say=lambda text: _say(err, text))
                    deadline += time.monotonic() - preflight_started
                    if preflight.pointers:
                        _say(err, f"lfs: {len(preflight.materialized)} of {preflight.pointers} pointer file(s) materialized "
                                  f"({preflight.from_cache} from the shared cache, {preflight.from_repository} from the repository's LFS store, "
                                  f"{preflight.fetched} fetched)")
                    if preflight.pointers or not preflight.ok:
                        _say(out, "LFS-PREFLIGHT " + json.dumps(preflight.describe(), sort_keys=True))
                    if not preflight.ok:
                        return _result(out, constants.BLOCKED, preflight.reason)
                may_hold += [lease.slot_resource(lease.UNITY_EDITOR_RESOURCE, number) for number in range(1, capacity + 1)]
                # The slot is taken only once no Editor runs outside a lease and enough memory is free (the gate), all within the same
                # wait budget: a run that waits for such an Editor holds the project lease but leaves the slot to others.
                gate = unityguard.Gate(manager, guard_settings, probe, guard_poll, lambda text: _say(err, text))
                slot = manager.acquire_slot(lease.UNITY_EDITOR_RESOURCE, capacity, owner, session, ttl_sec, what, pid,
                                            wait_sec=max(0.0, deadline - time.monotonic()), poll_sec=poll_sec, on_wait=waiting,
                                            precondition=gate)
                if not slot.acquired:
                    return _result(out, constants.BLOCKED, slot.reason)
                keeper.add(slot.lease.resource)
                if keeper.lost:
                    return _result(out, constants.BLOCKED, f"the lease(s) {', '.join(keeper.lost)} were lost before Unity started")

                console = work / "console.log"
                cancel = work / "cancel"
                before = _log_state(log_file)
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
                lost = f"the lease(s) {', '.join(keeper.lost)} were lost while Unity ran"
                if outcome.cancelled:
                    status, reason = constants.BLOCKED, f"{lost}; Unity was stopped"
                else:
                    # A log from an earlier run (Unity failed before it opened the file) says nothing about this one.
                    status, reason = classify(outcome, log_file if _log_is_new(log_file, before) else console)
                    if keeper.lost:  # Unity had already ended: its own result stands, with the note
                        reason = f"{reason}; note: {lost}" if reason else f"note: {lost}"
                text = log_tail(console, CONSOLE_TAIL_BYTES)
                if text:
                    _say(err, text.rstrip("\n"))
                return _result(out, status, reason)
            except Interrupted as stop:
                return _result(out, constants.BLOCKED, f"interrupted ({stop}); Unity was stopped and the leases released")
            except KeyboardInterrupt:
                return _result(out, constants.BLOCKED, "interrupted; Unity was stopped and the leases released")
        finally:
            def clean_up() -> None:
                handlers.cleaning = True  # signals during the release change nothing; the handlers stay until the end of the block
                stop_watching.set()
                for resource in reversed(may_hold):  # the slot first, then the project; under the keeper's lock, so no renewal recreates one
                    keeper.add(resource)
                    keeper.release_one(resource)
                keeper.stop()
                shutil.rmtree(work, ignore_errors=True)

            for _attempt in range(2):  # a signal that lands before `cleaning` is set interrupts the first attempt; the second is complete
                try:
                    clean_up()
                    break
                except Interrupted:
                    continue
            handlers.finished.set()
            if input_watcher is not None and os.name != "nt":
                # POSIX: the watcher ends by itself within one poll, so it is never left to interpreter finalization. Windows cannot
                # poll a pipe; there the daemon thread is left (it is blocked in a raw read, which is harmless) and nothing is waited for.
                input_watcher.join(timeout=2 * INPUT_POLL_SEC + 1)


def _say(stream: TextIO, text: str) -> None:
    try:
        print(text, file=stream)
        stream.flush()
    except (BrokenPipeError, ValueError):  # the reader is gone; nothing to tell
        pass


INPUT_POLL_SEC = 0.2


def _watch_input(fd: int, stop: threading.Event, note: Callable[[str], None] | None = None) -> None:
    """Wait for the end of the caller's input, then interrupt the main thread like Ctrl+C: the caller (an SSH client) is gone, so the
    run stops Unity and gives its leases back. Used only when the caller said so (`--stop-on-stdin-eof`).

    The caller sends a byte at once and every few seconds. The end of the input counts as a lost caller only after at least one byte
    has arrived: an input that was never a live pipe (closed, /dev/null, NUL behind a wrapper) ends at once and says nothing about the
    caller, so the lifeline is then switched off with a note and the run goes on.

    It reads the raw descriptor, never `sys.stdin`: a thread blocked inside the buffered reader holds its lock, and finalizing the
    interpreter then aborts the process. On POSIX it polls with a timeout and ends when `stop` is set; Windows cannot poll a pipe, so
    there the thread blocks in `os.read` (a raw call, harmless at exit)."""
    armed = False
    try:
        while not stop.is_set():
            if os.name != "nt":
                ready, _, _ = select.select([fd], [], [], INPUT_POLL_SEC)
                if not ready:
                    continue
            if os.read(fd, 4096):
                armed = True
                continue
            break  # the end of the input
        else:
            return
    except (OSError, ValueError):  # a closed or invalid descriptor is the end of the input as well
        pass
    if stop.is_set():
        return
    if not armed:
        if note is not None:
            note("note: the input of this command is not a live pipe, so the end of the caller's connection cannot be noticed; "
                 "the run goes on (it stops at the end of Unity or --timeout-sec)")
        return
    _thread.interrupt_main()


def _log_state(path: Path | None) -> tuple[int, int] | None:
    try:
        info = path.stat() if path is not None else None
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns) if info is not None else None


def _log_is_new(path: Path | None, before: tuple[int, int] | None) -> bool:
    """Whether the log was written after `before` (taken when the run started): a missing file before counts as new once it exists."""
    now = _log_state(path)
    return now is not None and now != before


def _user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name (a service account) must not stop a run
        return "unknown"
