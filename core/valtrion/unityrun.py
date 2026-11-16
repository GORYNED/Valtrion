"""`valtrion unity run`: one Unity batch-mode command under Valtrion's leases and supervision, the same on Windows, macOS and Linux.

The run holds the lease on the project folder and then one slot of the host's `unity-editor` capacity (the same total every other claimant
counts) from before Unity starts until its process tree is gone, renews both while it waits and while Unity runs, and gives them back in
reverse order on every exit. Unity runs under the watchdog: a hard timeout, a stall window that follows the growth of the `-logFile`, and a
lost lease each end in killing the whole process tree. The last line printed on stdout is `STATUS <name>`.

An Edit Mode or Play Mode test run with a key (`unitycache`) is answered from the host's result cache when an identical run passed
before, under the project lease and without an Editor slot or Unity; while it runs, the lease `unity-result:<key>` tells other sessions that the key is being
computed, and they wait for its result instead of starting a second identical Editor run.
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

from . import config, constants, core, lease, lfs, progresswindow, supervise, testprogress, unitycache, unityguard, windowisolation
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


def with_host_defaults(args: Sequence[str], project: Path, home: Path | None = None) -> list[str]:
    """The command as Unity will run it, made explicit: a relative `-testResults` becomes the path under the project folder (Unity resolves
    it against `-projectPath`, not the working directory), and a command without `-buildTarget` gets `unityBuildTarget` of host.json when
    it is set. Both are what Unity would do anyway; written out, the result cache can key the run."""
    result = list(args)
    for position, item in enumerate(result[:-1]):
        if item.lower() == "-testresults":
            value = result[position + 1]
            if value and not value.startswith("-") and not Path(value).expanduser().is_absolute():
                result[position + 1] = str(Path(project) / value)
    # Only a test run: a build (`-executeMethod`) chooses its platform itself.
    if any(item.lower() == "-runtests" for item in result) and not has_option(result, "-buildTarget", "-activeBuildProfile"):
        target = config.unity_build_target(home)
        if target:
            result += ["-buildTarget", target]
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


def default_purpose(args: Sequence[str], project: Path) -> str:
    """What the leases say a run is for when the caller gives no `--purpose`: what the run does and, for an agent's session, the name of
    its working copy (`Unity tests (EditMode) · task-ab12cd`), which stands for the session on the dashboard."""
    title, _ = progresswindow.describe_run(args, None)
    label = core.worktree_label(project) or core.worktree_label(Path.cwd())
    return f"{title} · {label}" if label else title


def run(
        unity_args: Sequence[str], project_dir: Path | None = None, unity: str | None = None,
        lease_wait_sec: float = DEFAULT_LEASE_WAIT_SEC, timeout_sec: float = 0.0, stall_sec: float = DEFAULT_STALL_SEC,
        purpose: str = "", home: Path | None = None, out: TextIO | None = None, err: TextIO | None = None,
        ttl_sec: int = LEASE_TTL_SEC, poll_sec: float = 1.0, cancel_poll_sec: float = 1.0,
        probe: unityguard.Probe | None = None, guard_poll_sec: float | None = None, lifeline: int | None = None,
        lfs_materialize: bool | None = None, lfs_call: lfs.LfsCall | None = None, lfs_free: Callable[[Path], int] | None = None,
        hidden_desktop: bool | None = None, progress_window: bool | None = None,
        prepare: Callable[[], str | None] | None = None, cache: unitycache.Plan | None = None, read_cache: bool = True) -> int:
    """Run Unity with `unity_args`; returns the exit code of the final status (already printed as the last stdout line).
    `lifeline`: a file descriptor whose end of input means the caller is gone (the standard input of a command run over SSH); the run is then
    interrupted like by Ctrl+C. `lfs_materialize`: run the Git LFS preflight (None: `lfs.materialize` of host.json, default on);
    `lfs_call` and `lfs_free` replace `git lfs` and the free-space probe in tests. `hidden_desktop`: start Unity on a desktop of its own
    (None: `hiddenDesktop` of host.json, default off here; the command line turns it on by default): on Windows a desktop of its own, on
    macOS the Editor hides its own windows (`windowisolation`; needs the Unity package), so test windows do not appear on the screen.
    `progress_window`: show one window with the progress of the run (same rules, `progressWindow`). `prepare`: called with the project
    lease held, before the LFS preflight and the Editor slot (an offloaded run resets its checkout to the commit here); it returns None or
    the reason the run is `BLOCKED`. Its time does not count against the lease wait. `cache`: the result cache key of the command
    (`cache_plan`; None: no result cache); `read_cache` False (`--no-cache`) runs Unity without looking the key up or waiting for
    another session that computes it, and still stores a passing result. An offloaded run (`prepare`) is never cached."""
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
    # An offloaded run (`prepare`) finds the folder and its Unity version only after the checkout has been reset to the commit.
    if prepare is None and not project.is_dir():
        return _result(out, constants.BLOCKED, f"the Unity project folder {project} does not exist")
    try:
        args = with_host_defaults(args, project, home)
    except config.ConfigError as error:
        return _result(out, constants.BLOCKED, str(error))

    editor, problem = None, None
    if unity:
        editor, problem = _explicit_editor(unity)
    elif prepare is None:
        found, problem = support.find_editor(support.project_unity_version(project), core.local_os(), os.environ)
        editor = found["path"] if found else None
    if editor is None and (unity or prepare is None):
        return _result(out, constants.BLOCKED, problem)

    if prepare is not None or cache is None or not cache.key:
        cache = None
    cache_root = unitycache.cache_root(home)

    try:
        if not has_option(args, "-EnableCacheServer", "-cacheServer"):
            args += config.unity_accelerator_args(home)
        guard_settings = unityguard.load_settings(home)
        materialize = config.lfs_materialize(home) if lfs_materialize is None else lfs_materialize
        on_hidden_desktop = config.hidden_desktop(home) if hidden_desktop is None else hidden_desktop
        show_progress = config.progress_window(home) if progress_window is None else progress_window
        if on_hidden_desktop and windowisolation.applies():
            problem = windowisolation.note(project, args)
            if problem:
                _say(err, problem)
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
    what = purpose or default_purpose(args, project)
    deadline = time.monotonic() + max(0.0, lease_wait_sec)
    work = Path(tempfile.mkdtemp(prefix="valtrion-unity-"))
    progress_file = testprogress.path_for(session, home)
    progress_file.unlink(missing_ok=True)  # one of an earlier run that had this process id says nothing about this run
    keeper = lease.LeaseKeeper(manager, session, pid, ttl_sec / 3, owner)
    #: Every lease this run may hold, in the order taken. A name is listed before its acquire call starts, so an interrupt between the
    #: moment a lease is created and the moment the run notes it still finds it at cleanup (releasing a lease of another holder does nothing).
    may_hold: list[str] = []
    stop_watching = threading.Event()
    handlers = _Handlers()
    guard_poll = guard_poll_sec if guard_poll_sec is not None else max(poll_sec, GUARD_POLL_SEC)

    def waiting(reason: str, waited_sec: float) -> None:
        _say(err, lease.waiting_message(reason, waited_sec))

    #: The result cache markers this run may hold: they only tell other sessions that a key is being computed, so losing one must
    #: not stop Unity.
    markers: set[str] = set()

    def lost_leases() -> list[str]:
        return [resource for resource in keeper.lost if resource not in markers]

    def hold_marker(resource: str) -> None:
        may_hold.append(resource)
        markers.add(resource)

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
                if prepare is not None:
                    prepare_started = time.monotonic()
                    problem = prepare()
                    deadline += time.monotonic() - prepare_started
                    if problem:
                        return _result(out, constants.BLOCKED, problem)
                    if not project.is_dir():
                        return _result(out, constants.BLOCKED, f"the Unity project folder {project} does not exist")
                    if editor is None:
                        found, problem = support.find_editor(support.project_unity_version(project), core.local_os(), os.environ)
                        if found is None:
                            return _result(out, constants.BLOCKED, problem)
                        editor = found["path"]
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
                if cache is not None:  # under the project lease: the project, then the marker of the key, then the slot
                    marker_started = time.monotonic()
                    answered, cache = _cache_step(cache, hold_marker, manager, keeper, owner, session, pid, ttl_sec, what, project,
                                                  read_cache, max(0.0, deadline - time.monotonic()), poll_sec, waiting, cache_root, out, err)
                    if answered is not None:
                        return answered
                    deadline += time.monotonic() - marker_started  # waiting for another session leaves the slot its own wait
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
                if lost_leases():
                    return _result(out, constants.BLOCKED, f"the lease(s) {', '.join(lost_leases())} were lost before Unity started")

                console = work / "console.log"
                cancel = work / "cancel"
                before = _log_state(log_file)
                results_before = _log_state(cache.result_path) if cache is not None else None
                watchdog = supervise.Watchdog(
                    timeout_sec if timeout_sec > 0 else float("inf"),
                    stall_sec if stall_sec > 0 and log_file is not None else float("inf"),
                    progress_files=(log_file,) if log_file is not None else (), log_counts=False, cancel_file=cancel)

                def watch_leases() -> None:
                    while not stop_watching.wait(cancel_poll_sec):
                        if lost_leases():
                            cancel.touch()
                            return

                watcher = threading.Thread(target=watch_leases, name="valtrion-unity-lease-watch", daemon=True)
                watcher.start()
                title, detail = progresswindow.describe_run(args, project)
                window = progresswindow.start(log_file or console, title, detail, show_progress, progress_file)
                # The Editor part of the Unity package writes how far the tests have come into this file (dashboard, progress window).
                unity_environment = {**windowisolation.environment(os.environ, on_hidden_desktop), testprogress.ENVIRONMENT_VARIABLE: str(progress_file)}
                outcome = None
                try:
                    outcome = supervise.supervise([editor, *args], watchdog, console, None, unity_environment, exit_codes=UNITY_EXIT_CODES,
                                                  hidden_desktop=on_hidden_desktop)
                finally:
                    if window is not None and outcome is None:  # an interrupt: there is no result to show
                        window.finish(constants.BLOCKED, "interrupted")
                stop_watching.set()
                watcher.join(timeout=5)
                lost = f"the lease(s) {', '.join(lost_leases())} were lost while Unity ran"
                if outcome.cancelled:
                    status, reason = constants.BLOCKED, f"{lost}; Unity was stopped"
                else:
                    # A log from an earlier run (Unity failed before it opened the file) says nothing about this one.
                    status, reason = classify(outcome, log_file if _log_is_new(log_file, before) else console)
                    if lost_leases():  # Unity had already ended: its own result stands, with the note
                        reason = f"{reason}; note: {lost}" if reason else f"note: {lost}"
                if window is not None:
                    window.finish(status, reason)
                text = log_tail(console, CONSOLE_TAIL_BYTES)
                if text:
                    _say(err, text.rstrip("\n"))
                if cache is not None and status == constants.PASS:
                    _say(err, keep_result(cache, cache_root, _log_is_new(cache.result_path, results_before)))
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
                progress_file.unlink(missing_ok=True)
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


def cache_plan(unity_args: Sequence[str], project_dir: Path | None = None, unity: str | None = None,
               home: Path | None = None) -> unitycache.Plan:
    """The result cache key of a command, found the way `run` finds the project and the Editor; a plan without a key says why."""
    args = normalize_assembly_names(unity_args)
    given = option_value(args, "-projectPath")
    if given is not None:
        project = _absolute(given)
    elif project_dir is not None:
        project = Path(project_dir).expanduser().absolute()
    else:
        return unitycache.no_key("no Unity project", args)
    if not project.is_dir():
        return unitycache.no_key(f"the Unity project folder {project} does not exist", args)
    try:
        args = with_host_defaults(args, project, home)
    except config.ConfigError as error:
        return unitycache.no_key(str(error), args)
    if unity:
        editor, problem = _explicit_editor(unity)
    else:
        found, problem = support.find_editor(support.project_unity_version(project), core.local_os(), os.environ)
        editor = found["path"] if found else None
    if editor is None:
        return unitycache.no_key(problem or "no Unity Editor", args)
    return unitycache.plan(args, project, editor)


def answer_hit(hit: unitycache.Hit, plan: unitycache.Plan, out: TextIO) -> int:
    """Answer a command from the cache: write the stored result file where the command asks for it and report `PASS`."""
    try:
        if plan.result_path is None:
            raise OSError("the command names no -testResults file")
        plan.result_path.parent.mkdir(parents=True, exist_ok=True)
        plan.result_path.write_bytes(hit.text.encode("utf-8"))  # the text as Unity wrote it (no newline translation)
    except OSError as error:
        return _result(out, constants.BLOCKED, f"the cached result of key {hit.key} cannot be written to -testResults: {error}")
    line = f"cached: PASS of key {hit.key} stored {hit.stored_utc}; Unity was not started (--no-cache runs it)"
    log = option_value(plan.args, "-logFile")
    if log and log != "-":  # a log left by an earlier run must not be read as the log of this one
        try:
            path = _absolute(log)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((line + "\n").encode("utf-8"))
        except OSError:
            pass
    return _result(out, constants.PASS, line)


def keep_result(plan: unitycache.Plan, root: Path, result_is_new: bool) -> str:
    """Store the result of a passing run under its key; returns the line that says what happened."""
    short = (plan.key or "")[:12]
    if not result_is_new:
        return f"cache: not stored (key {short}): the -testResults file was not written by this run"
    again = _replan(plan)
    if again.key != plan.key:
        return f"cache: not stored (key {short}): {unitycache.changed_inputs(plan, again)}"
    text, why = unitycache.read_result(plan.result_path) if plan.result_path is not None else (None, "no -testResults file")
    if text is None:
        return f"cache: not stored (key {short}): {why}"
    try:
        stored, why = unitycache.store(root, plan, text)
    except OSError as error:
        stored, why = None, f"it cannot be written ({error})"
    if stored is None:
        return f"cache: not stored (key {short}): {why}"
    return f"cache: stored PASS under key {plan.key}"


def _replan(plan: unitycache.Plan) -> unitycache.Plan:
    """The key of the same command computed again from the working copy as it is now."""
    if plan.project is None or not plan.editor:
        return unitycache.no_key(plan.reason or "no project or Editor", plan.args)
    return unitycache.plan(plan.args, plan.project, plan.editor)


def answer_from_cache(plan: unitycache.Plan, home: Path | None = None, out: TextIO | None = None) -> int | None:
    """Answer a command from the cache before it is placed (offloaded or queued here): only with the project lease free and taken
    for the moment it takes (a run in progress in the same folder may be writing the same result and log files), with the key
    computed again under it. Returns the exit code, or None when the command must run (or wait) as usual."""
    out = out or sys.stdout
    root = unitycache.cache_root(home)
    if not plan.key or plan.project is None or unitycache.lookup(root, plan, touch=False)[0] is None:
        return None
    pid = os.getpid()
    session = f"unity-run-{pid}"
    try:
        manager = lease.LeaseManager(home)
        resource = lease.unity_project_resource(plan.project)
    except (config.ConfigError, ValueError, OSError):
        return None
    handlers = _Handlers()
    with handlers:  # SIGTERM, SIGHUP, Ctrl+C: the lease is given back like in `run`
        try:
            try:
                taken = manager.acquire(resource, os.environ.get("GDV_OWNER") or f"{_user()}@{core.local_host_id()}", session,
                                        LEASE_TTL_SEC, "result cache lookup", pid, worktree=str(plan.project), wait_sec=0)
                if not taken.acquired:
                    return None
                current = _replan(plan)
                hit = unitycache.lookup(root, current)[0] if current.key else None
                return answer_hit(hit, current, out) if hit is not None else None
            except (ValueError, OSError):
                return None
            except Interrupted as stop:
                return _result(out, constants.BLOCKED, f"interrupted ({stop}); the leases were released")
            except KeyboardInterrupt:
                return _result(out, constants.BLOCKED, "interrupted; the leases were released")
        finally:
            handlers.cleaning = True
            for _attempt in range(2):
                try:
                    manager.release(resource, session, pid)  # releasing a lease this run does not hold changes nothing
                    break
                except Interrupted:
                    continue
            handlers.finished.set()


def _cache_step(cache: unitycache.Plan, hold: Callable[[str], None], manager: lease.LeaseManager, keeper: lease.LeaseKeeper, owner: str,
                session: str, pid: int, ttl_sec: int, what: str, project: Path, read_cache: bool, wait_sec: float, poll_sec: float,
                waiting: Callable[[str, float], None], cache_root: Path, out: TextIO, err: TextIO) -> tuple[int | None, unitycache.Plan | None]:
    """With the project lease held: compute the key again (the working copy may have changed while the run waited), answer a hit, and
    take the lease that says this key is being computed. Another live session that holds it is waited for (within the lease wait), and
    then the key is computed again and looked up; when the wait runs out, and with `--no-cache` at once, the run goes on without the
    lease. Returns (the exit code when the run is answered from the cache, else None; the plan that is current, None without a key)."""
    current = _replan(cache)
    if not current.key:
        _say(err, f"cache: not cached: {current.reason}")
        return None, None
    if current.key != cache.key:
        _say(err, f"cache: the inputs changed since the command started; the key is now {current.key}")
    cache = current
    if read_cache:
        hit, _ = unitycache.lookup(cache_root, cache)
        if hit is not None:
            return answer_hit(hit, cache, out), cache

    def purpose(plan: unitycache.Plan) -> str:
        return f"{what} · result cache {(plan.key or '')[:12]}"

    marker = cache.marker or ""
    hold(marker)
    taken = manager.try_acquire(marker, owner, session, ttl_sec, purpose(cache), pid, worktree=str(project))
    if taken.acquired:
        keeper.add(marker)
        if read_cache:  # another session may have stored the result a moment ago
            hit, _ = unitycache.lookup(cache_root, cache)
            if hit is not None:
                return answer_hit(hit, cache, out), cache
        return None, cache
    holder = taken.holder.describe() if taken.holder is not None else "another session"
    if not read_cache:
        _say(err, f"cache: key {cache.key} is being computed by another session ({holder}); --no-cache: Unity runs anyway")
        return None, cache
    _say(err, f"cache: key {cache.key} is being computed by another session: {holder}; waiting up to {wait_sec:.0f} s for its result")
    taken = manager.acquire(marker, owner, session, ttl_sec, purpose(cache), pid, worktree=str(project), wait_sec=wait_sec,
                            poll_sec=poll_sec, on_wait=waiting)
    if not taken.acquired:
        _say(err, f"cache: the other session has not finished key {cache.key} within the wait ({taken.reason}); Unity runs here")
        return None, cache
    keeper.add(marker)
    again = _replan(cache)  # the working copy may have changed during the wait
    if again.key != cache.key:
        keeper.release_one(marker)
        if not again.key:
            _say(err, f"cache: not cached: {again.reason}")
            return None, None
        _say(err, f"cache: the inputs changed while the run waited; the key is now {again.key}")
        cache = again
        hold(cache.marker or "")
        again_taken = manager.try_acquire(cache.marker or "", owner, session, ttl_sec, purpose(cache), pid, worktree=str(project))
        if again_taken.acquired:
            keeper.add(cache.marker or "")
        else:
            holder = again_taken.holder.describe() if again_taken.holder is not None else "another session"
            _say(err, f"cache: key {cache.key} is being computed by another session ({holder}); not waiting again")
    hit, _ = unitycache.lookup(cache_root, cache)
    if hit is not None:
        return answer_hit(hit, cache, out), cache
    return None, cache


def _say(stream: TextIO, text: str) -> None:
    try:
        print(text, file=stream)
        stream.flush()
    except (BrokenPipeError, ValueError):  # the reader is gone; nothing to tell
        pass


INPUT_POLL_SEC = 0.2


def _watch_input(fd: int, stop: threading.Event, note: Callable[[str], None] | None = None,
                 ended: Callable[[], None] | None = None) -> None:
    """Wait for the end of the caller's input, then interrupt the main thread like Ctrl+C: the caller (an SSH client) is gone, so the
    run stops Unity and gives its leases back. Used only when the caller said so (`--stop-on-stdin-eof`).

    The caller sends a byte at once and every few seconds. The end of the input counts as a lost caller only after at least one byte
    has arrived: an input that was never a live pipe (closed, /dev/null, NUL behind a wrapper) ends at once and says nothing about the
    caller, so the lifeline is then switched off with a note and the run goes on.

    It reads the raw descriptor, never `sys.stdin`: a thread blocked inside the buffered reader holds its lock, and finalizing the
    interpreter then aborts the process. On POSIX it polls with a timeout and ends when `stop` is set; Windows cannot poll a pipe, so
    there the thread blocks in `os.read` (a raw call, harmless at exit).

    `ended` is called when the caller is gone (default: interrupt the main thread)."""
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
    if ended is not None:
        ended()
    else:
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
