"""Supervisor and watchdog: run one step as a process with a hard timeout and stall detectors, and kill its whole process tree.

Progress is judged from signals, never from the process merely being alive: growth of the log or of watched files, a
`gdv.heartbeat/1` file whose `seq` advances, and (opt-in) CPU time of the process tree. No progress for the stall window is STALL;
exceeding the hard timeout is TIMEOUT. In both cases the whole tree is killed (a Windows Job Object, a POSIX process group) and
the step never leaves processes behind. The defaults are 10 minutes, 3 for a short operation; a plan may
narrow them but not disable them, so a window of zero or less is refused.
"""

from __future__ import annotations

import abc
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import constants, core, hiddendesktop, nowindow

DEFAULT_STALL_SEC = 600.0
SHORT_STALL_SEC = 180.0
_MARKER = re.compile(rb"^GDV-RESULT (PASS|FAIL|BLOCKED|NEEDS_PERMISSION|STALL|TIMEOUT)\b[ \t]*(.*?)\r?$", re.MULTILINE)
_MARKER_TAIL_BYTES = 64 * 1024
_SCAN_LIMIT = 4000  # files visited when sizing a watched folder


@dataclass(frozen=True)
class Watchdog:
    """Limits and progress signals of one supervised step."""

    timeout_sec: float
    stall_sec: float = DEFAULT_STALL_SEC
    progress_files: tuple[Path, ...] = ()
    heartbeat: Path | None = None
    #: Whether growth of the step's own log counts as progress. Default: yes, unless a heartbeat is the authority
    #: (an application that spams its log while stuck must still stall).
    log_counts: bool | None = None
    #: Opt-in: CPU time of the process tree counts as progress (for example a native compile that logs nothing).
    cpu_counts: bool = False
    poll_sec: float = 0.25
    tick_sec: float = 15.0
    cancel_file: Path | None = None

    def __post_init__(self) -> None:
        if self.timeout_sec <= 0 or self.stall_sec <= 0:
            raise ValueError("a watchdog needs a positive timeout and stall window; they cannot be disabled")
        if self.poll_sec <= 0 or self.tick_sec <= 0:
            raise ValueError("poll and tick intervals must be positive")

    @property
    def counts_log(self) -> bool:
        return self.log_counts if self.log_counts is not None else self.heartbeat is None


@dataclass
class SuperviseResult:
    status: str
    reason: str | None
    returncode: int | None
    elapsed_sec: float
    idle_sec: float
    pid: int | None = None
    marker: str | None = None
    cancelled: bool = False

    @property
    def exit_code(self) -> int:
        return constants.exit_code_for_status(self.status)


# -- process trees ------------------------------------------------------------------------------------------------------------


class ProcessTree(abc.ABC):
    """The set of processes started by one step: measure their CPU, tell whether any is alive, kill them all."""

    @abc.abstractmethod
    def cpu_seconds(self) -> float: ...

    @abc.abstractmethod
    def alive(self) -> bool: ...

    @abc.abstractmethod
    def kill(self) -> None: ...

    def close(self) -> None:
        """Release handles. On Windows closing the job also kills whatever is still in it."""


class PosixTree(ProcessTree):
    def __init__(self, process: subprocess.Popen, grace_sec: float = 3.0) -> None:
        self._process = process
        self._group = process.pid  # started with start_new_session=True, so the group id is the leader's pid
        self._grace = grace_sec

    def cpu_seconds(self) -> float:
        try:
            output = subprocess.run(["ps", "-axo", "pgid=,time="], capture_output=True, text=True, timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return 0.0
        total = 0.0
        for line in output.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == str(self._group):
                total += _parse_cpu_time(parts[1])
        return total

    def alive(self) -> bool:
        try:
            os.killpg(self._group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # A group whose only members are zombies of our own child is gone once the child is reaped.
        return self._process.poll() is None or _group_has_members(self._group)

    def kill(self) -> None:
        for sig, wait in ((signal.SIGTERM, self._grace), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(self._group, sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if self._process.poll() is not None and not _group_has_members(self._group):
                    return
                time.sleep(0.05)


def _group_has_members(group: int) -> bool:
    try:
        output = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == str(group) and not parts[1].startswith("Z"):
            return True
    return False


def _parse_cpu_time(text: str) -> float:
    """`[[dd-]hh:]mm:ss[.ff]` as printed by ps."""
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        days = int(day_text) if day_text.isdigit() else 0
    seconds = 0.0
    try:
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
    except ValueError:
        return 0.0
    return days * 86400 + seconds


class WindowsTree(ProcessTree):
    """A Job Object: every process the step starts (children, grandchildren) belongs to it and dies with it."""

    def __init__(self, process: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._process = process
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._job = kernel32.CreateJobObjectW(None, None)
        if not self._job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

        self._accounting_type = Accounting
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(self._job)
            raise OSError(error, "SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(self._job, int(process._handle)):  # noqa: SLF001 - Popen exposes no public handle
            error = ctypes.get_last_error()
            kernel32.CloseHandle(self._job)
            raise OSError(error, "AssignProcessToJobObject failed")

    def _accounting(self):
        info = self._accounting_type()
        ok = self._kernel32.QueryInformationJobObject(
            self._job, 1, self._ctypes.byref(info), self._ctypes.sizeof(info), None)
        return info if ok else None

    def cpu_seconds(self) -> float:
        info = self._accounting()
        return 0.0 if info is None else (info.TotalUserTime + info.TotalKernelTime) / 1e7

    def alive(self) -> bool:
        info = self._accounting()
        return info is not None and info.ActiveProcesses > 0

    def kill(self) -> None:
        if self._job:
            self._kernel32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        if self._job:
            self._kernel32.CloseHandle(self._job)
            self._job = None


def _resume_process(process: subprocess.Popen) -> None:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(int(process._handle))  # noqa: SLF001
    if status != 0:
        raise OSError(f"NtResumeProcess failed with status {status:#x}")


_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000  # a console step started by a detached runner must not open a window


def spawn(argv: Sequence[str], cwd: str | None, env: Mapping[str, str] | None, log, tree_grace_sec: float = 3.0,
          hidden_desktop: bool = False):
    """Start the step with its output in `log` and attach it to a process tree. Raises OSError when it cannot start. `hidden_desktop`
    (Windows only, else ignored): the step runs on a desktop of its own, so the windows it opens never show on the user's screen."""
    environment = dict(env) if env is not None else None
    if os.name == "nt":
        # Started suspended, put into the Job Object, then resumed, so no descendant can ever escape the job.
        flags = _CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED | _CREATE_NO_WINDOW
        process = None
        if hidden_desktop:
            try:
                process = hiddendesktop.start(argv, cwd, environment, log, flags)
            except hiddendesktop.DesktopUnavailable:
                process = None  # no desktop can be created here: run on the current one rather than not at all
        if process is None:
            process = subprocess.Popen(
                list(argv), cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            tree: ProcessTree = WindowsTree(process)
            _resume_process(process)
        except BaseException:
            process.kill()
            process.wait()
            raise
        return process, tree
    process = subprocess.Popen(
        list(argv), cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return process, PosixTree(process, tree_grace_sec)


# -- progress ---------------------------------------------------------------------------------------------------------------


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    visited = 0
    for root, _, files in os.walk(path):
        for name in files:
            visited += 1
            if visited > _SCAN_LIMIT:
                return total
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _heartbeat_seq(path: Path) -> int | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    seq = document.get("seq") if isinstance(document, dict) else None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


class ProgressTracker:
    """Remembers when a progress signal last advanced. `poll` returns whether any signal advanced since the last poll."""

    def __init__(
            self, watchdog: Watchdog, log_path: Path | None, tree: ProcessTree | None = None,
            monotonic: Callable[[], float] = time.monotonic) -> None:
        self._watchdog = watchdog
        self._log_path = log_path
        self._tree = tree
        self._monotonic = monotonic
        self._sizes: dict[Path, int] = {}
        self._seq: int | None = None
        self._cpu = 0.0
        self.last_progress = monotonic()
        self.poll()
        self.last_progress = monotonic()

    def poll(self) -> bool:
        advanced = False
        watched = list(self._watchdog.progress_files)
        if self._watchdog.counts_log and self._log_path is not None:
            watched.append(self._log_path)
        for path in watched:
            size = _path_size(path)
            if size > self._sizes.get(path, 0):
                advanced = True
            self._sizes[path] = size
        if self._watchdog.heartbeat is not None:
            seq = _heartbeat_seq(self._watchdog.heartbeat)
            if seq is not None and (self._seq is None or seq > self._seq):
                advanced = True
                self._seq = seq
        if self._watchdog.cpu_counts and self._tree is not None:
            cpu = self._tree.cpu_seconds()
            if cpu - self._cpu >= 0.05:
                advanced = True
            self._cpu = max(self._cpu, cpu)
        if advanced:
            self.last_progress = self._monotonic()
        return advanced

    def idle_sec(self) -> float:
        return self._monotonic() - self.last_progress


# -- supervision ------------------------------------------------------------------------------------------------------------

EventSink = Callable[[str, str, dict], None]  # (event type, message, data)


def read_marker(log_path: Path) -> tuple[str, str] | None:
    """The last `GDV-RESULT <status> <message>` line in the tail of the log, if the application wrote one."""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _MARKER_TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    found = _MARKER.findall(tail)
    if not found:
        return None
    status, message = found[-1]
    return status.decode(), message.decode("utf-8", "replace")


def classify_exit(
        returncode: int, marker: tuple[str, str] | None, contract_exit_codes: bool = False,
        exit_codes: Mapping[int, str] | None = None) -> tuple[str, str | None]:
    """Status of a process that exited by itself. A terminal line written by the application is stricter than the exit code."""
    if marker is not None and marker[0] != constants.PASS:
        return marker[0], marker[1] or f"the application reported {marker[0]}"
    if returncode == 0:
        return constants.PASS, None
    if exit_codes and returncode in exit_codes:
        status = exit_codes[returncode]
        return status, None if status == constants.PASS else f"exit code {returncode}"
    if contract_exit_codes:
        if returncode == constants.EXIT_BLOCKED:
            return constants.BLOCKED, f"exit code {returncode} (a prerequisite is missing)"
        if returncode == constants.EXIT_STALL:
            return constants.TIMEOUT, f"exit code {returncode} (the step reported a stall or timeout)"
    return constants.FAIL, f"exit code {returncode}"


def supervise(
        argv: Sequence[str], watchdog: Watchdog, log_path: Path, cwd: str | None = None, env: Mapping[str, str] | None = None,
        on_event: EventSink | None = None, contract_exit_codes: bool = False, exit_codes: Mapping[int, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        hidden_desktop: bool = False) -> SuperviseResult:
    """Run `argv` under the watchdog; its output goes to `log_path`. Always returns, and never leaves processes behind. `hidden_desktop`:
    see `spawn`."""
    emit = on_event or (lambda *_: None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    with open(log_path, "ab") as log:
        try:
            process, tree = spawn(argv, cwd, env, log, hidden_desktop=hidden_desktop)
        except OSError as error:
            return SuperviseResult(constants.BLOCKED, f"cannot start {argv[0]!r}: {error}", None, 0.0, 0.0)
        tracker = ProgressTracker(watchdog, log_path, tree, monotonic)
        emit("log", f"started pid {process.pid}", {"pid": process.pid, "argv": list(argv)})
        next_tick = started + watchdog.tick_sec
        status: str | None = None
        reason: str | None = None
        cancelled = False
        try:
            while True:
                if process.poll() is not None:
                    break
                tracker.poll()
                now = monotonic()
                elapsed = now - started
                if watchdog.cancel_file is not None and watchdog.cancel_file.exists():
                    status, reason, cancelled = constants.BLOCKED, "cancelled by request", True
                elif elapsed >= watchdog.timeout_sec:
                    status, reason = constants.TIMEOUT, f"no result within the {watchdog.timeout_sec:g} s timeout"
                    emit("timeout.detected", reason, {"elapsedSec": round(elapsed, 3)})
                elif tracker.idle_sec() >= watchdog.stall_sec:
                    status = constants.STALL
                    reason = f"no progress for {tracker.idle_sec():.1f} s (stall window {watchdog.stall_sec:g} s)"
                    emit("stall.detected", reason, {"idleSec": round(tracker.idle_sec(), 3), "elapsedSec": round(elapsed, 3)})
                if status is not None:
                    tree.kill()
                    break
                if now >= next_tick:
                    emit("progress", "running", {"elapsedSec": round(elapsed, 1), "idleSec": round(tracker.idle_sec(), 1)})
                    next_tick = now + watchdog.tick_sec
                sleep(watchdog.poll_sec)
            returncode = process.wait(timeout=30) if status is None else _reap(process)
            # The step may exit while a child it started is still running; the step owns that child, so it ends with the step.
            if tree.alive():
                tree.kill()
        finally:
            if process.poll() is None:
                tree.kill()
                _reap(process)
            tree.close()
            release = getattr(process, "close", None)  # a hidden-desktop process owns its handles
            if release is not None:
                release()
    elapsed = monotonic() - started
    marker = read_marker(log_path)
    if status is None:
        status, reason = classify_exit(returncode, marker, contract_exit_codes, exit_codes)
    return SuperviseResult(
        status, reason, returncode, elapsed, tracker.idle_sec(), process.pid, marker[0] if marker else None, cancelled)


def _reap(process: subprocess.Popen) -> int | None:
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        return None


@dataclass
class BoundedResult:
    status: str
    reason: str | None
    returncode: int | None
    output: str
    elapsed_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == constants.PASS


def run_bounded(
        argv: Sequence[str], timeout_sec: float, stall_sec: float | None = None, cwd: str | None = None,
        env: Mapping[str, str] | None = None, log_path: Path | None = None, tail_bytes: int | None = None,
        **watchdog_options) -> BoundedResult:
    """Run a command under the watchdog and return its output (only the last `tail_bytes` bytes when that is given). Adapters use this
    for every external tool call (`devicectl copy`, `adb`, `xcodebuild`) so that no call can hang the run."""
    own_log = log_path is None
    if own_log:
        handle, name = tempfile.mkstemp(prefix="gdv-step-", suffix=".log")
        os.close(handle)
        log_path = Path(name)
    assert log_path is not None
    watchdog = Watchdog(timeout_sec, stall_sec if stall_sec is not None else min(timeout_sec, SHORT_STALL_SEC), **watchdog_options)
    outcome = supervise(argv, watchdog, log_path, cwd, env)
    try:
        if tail_bytes is None:
            output = log_path.read_bytes().decode("utf-8", "replace")
        else:
            with open(log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - tail_bytes))
                output = handle.read().decode("utf-8", "replace")
    except OSError:
        output = ""
    if own_log:
        try:
            log_path.unlink()
        except OSError:
            pass
    return BoundedResult(outcome.status, outcome.reason, outcome.returncode, output, outcome.elapsed_sec)


def call_bounded(function: Callable[[], object], timeout_sec: float):
    """Safety net for an in-process adapter call: returns `(True, value)`, or `(False, None)` when it exceeds the timeout.
    The call cannot be interrupted, so its thread is abandoned; adapters should prefer `run_bounded` for external tools."""
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = function()
        except BaseException as error:  # noqa: BLE001 - re-raised in the caller
            box["error"] = error

    thread = threading.Thread(target=target, name="valtrion-bounded-call", daemon=True)
    thread.start()
    thread.join(timeout_sec)
    if thread.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return True, box.get("value")


def kill_process_tree(pid: int) -> bool:
    """Kill a process and its descendants by pid (used to stop a runner that ignores a cancel request)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=30,
                                **nowindow.OPTIONS)
        return result.returncode == 0
    try:
        os.killpg(pid, signal.SIGKILL)  # the runner leads its own session
        return True
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False
