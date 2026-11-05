"""One small window that shows how a `valtrion unity run` is going, instead of nothing (the Editor runs on a hidden desktop) or a flicker of
windows (the tests open them).

`unity run` starts it (`start`) in a process of its own, next to the Unity it supervises, and tells it when the run ended (`Handle.finish`). The
window follows the Unity log: the phase (loading the project, running tests, shutting down), the time, the last line and, at the end, the
status. It is always on top but never takes the focus, closes itself a few seconds after the end, and closes at once when the run is gone.

It is only a view: whatever fails here (no desktop to show on, no Tk, a log that cannot be read) ends the window and never the run.
Windows only. The test counter ("120 of 361") is not shown: Unity's batch log carries no line per test."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from . import testprogress

POLL_MS = 500
CLOSE_AFTER_END_MS = 4000
READ_LIMIT = 1 << 20

#: (text in a log line, phase it announces); a later line wins, so the phase follows the log.
PHASE_MARKERS = (
    ("[Project] Loading completed", "Project loaded"),
    ("Running tests for ExecutionSettings", "Running tests"),
    ("Build completed with a result of", "Build finished"),
    ("Cleanup mono", "Shutting down"),
)
START_PHASE = "Starting Unity"
LOADING_PHASE = "Loading the project (import, compile)"
STATUS_COLORS = {"PASS": "#1a7f37", "FAIL": "#cf222e"}
OTHER_STATUS_COLOR = "#9a6700"


class PhaseTracker:
    """Reads the log in pieces as it grows and keeps the current phase and the last line worth showing."""

    def __init__(self) -> None:
        self.phase = START_PHASE
        self.last_line = ""
        self._carry = ""

    def feed(self, text: str) -> None:
        lines = (self._carry + text).split("\n")
        self._carry = lines.pop()  # the last piece may be half a line
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if self.phase == START_PHASE:
                self.phase = LOADING_PHASE
            if not stripped.startswith("##utp"):
                self.last_line = stripped
            for marker, phase in PHASE_MARKERS:
                if marker in stripped:
                    self.phase = phase


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _option(args: Sequence[str], name: str) -> str | None:
    lowered = name.lower()
    for index, value in enumerate(args[:-1]):
        if value.lower() == lowered:
            return args[index + 1]
    return None


def describe_run(unity_args: Sequence[str], project: Path | None) -> tuple[str, str]:
    """(title, detail) of a Unity batch run, from its arguments."""
    lowered = [value.lower() for value in unity_args]
    if "-runtests" in lowered:
        title = f"Unity tests ({_option(unity_args, '-testPlatform') or 'EditMode'})"
        detail = _option(unity_args, "-assemblyNames") or _option(unity_args, "-testFilter") or "all tests"
    elif "-executemethod" in lowered:
        title = "Unity batch method"
        detail = _option(unity_args, "-executeMethod") or ""
    elif "-buildtarget" in lowered:
        title = "Unity build"
        detail = _option(unity_args, "-buildTarget") or ""
    else:
        title = "Unity batch run"
        detail = ""
    if project is not None:
        detail = f"{project.name}: {detail}" if detail else project.name
    return title, detail


class Handle:
    """What `unity run` keeps of a window it started."""

    def __init__(self, process: subprocess.Popen, done_file: Path) -> None:
        self._process = process
        self._done_file = done_file

    def finish(self, status: str, reason: str | None = None) -> None:
        """Tell the window how the run ended. It shows that for a few seconds and closes. Never raises."""
        try:
            if self._process.poll() is not None:  # the window never opened or was closed: nobody will read the file
                self._done_file.unlink(missing_ok=True)
                return
            partial = self._done_file.with_suffix(".tmp")
            partial.write_text(json.dumps({"status": status, "reason": reason or ""}), encoding="utf-8")
            os.replace(partial, self._done_file)
        except OSError:
            pass


def start(log_file: Path, title: str, detail: str, enabled: bool = True, progress_file: Path | None = None) -> Handle | None:
    """Open the window for a run whose Unity log is `log_file`. `progress_file` is where the Unity package reports "N of M" tests
    (see `testprogress`): the window then shows a counter and a real bar. None when it is off, not on Windows or cannot be started.
    Never raises."""
    if not enabled or os.name != "nt":
        return None
    try:
        from . import remote

        descriptor, name = tempfile.mkstemp(prefix="valtrion-progress-", suffix=".json")
        os.close(descriptor)
        done_file = Path(name)
        done_file.unlink()  # the file appears when the run ends
        argv = remote.local_cli_argv([
            "unity", "_progress", "--log", str(log_file), "--done", str(done_file), "--parent-pid", str(os.getpid()),
            "--title", title, "--detail", detail, *(["--progress", str(progress_file)] if progress_file else [])])
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000200, close_fds=True)  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        return Handle(process, done_file)
    except (OSError, ValueError, ImportError):
        return None


# -- the window process --------------------------------------------------------------------------------------------------------


def _process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(handle)


def _read_new(log_file: Path, offset: int) -> tuple[str, int]:
    try:
        size = log_file.stat().st_size
        if size < offset:  # Unity starts its log anew
            offset = 0
        with open(log_file, "rb") as handle:
            handle.seek(offset)
            data = handle.read(READ_LIMIT)
    except OSError:
        return "", offset
    return data.decode("utf-8", "replace"), offset + len(data)


def _without_activation(root) -> None:
    """Never take the focus from what the user is typing into, and keep the window out of the taskbar."""
    import ctypes

    user32 = ctypes.WinDLL("user32")
    window = user32.GetParent(root.winfo_id()) or root.winfo_id()
    style = user32.GetWindowLongW(window, -20)
    user32.SetWindowLongW(window, -20, style | 0x08000000 | 0x00000080)  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW


def _bottom_right(root, width: int, height: int) -> str:
    import ctypes
    from ctypes import wintypes

    area = wintypes.RECT()
    if ctypes.WinDLL("user32").SystemParametersInfoW(48, 0, ctypes.byref(area), 0):  # SPI_GETWORKAREA: the screen without the taskbar
        return f"{width}x{height}+{area.right - width - 16}+{area.bottom - height - 16}"
    return f"{width}x{height}+{root.winfo_screenwidth() - width - 16}+{root.winfo_screenheight() - height - 64}"


def test_counter_text(progress: dict) -> str:
    """"120 of 361 tests" and, when there are some, the failed ones."""
    text = f"{progress['done']} of {progress['total']} tests"
    return f"{text} · {progress['failed']} failed" if progress["failed"] else text


def run_window(log_file: Path, done_file: Path, parent_pid: int, title: str, detail: str, progress_file: Path | None = None) -> int:
    """Show the window until the run ends (or goes away). Returns 0; any failure to show it also returns 0."""
    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
    except Exception:  # noqa: BLE001 - no Tk or no desktop to show on: the run does not need the window
        done_file.unlink(missing_ok=True)
        return 0
    try:
        root.title("Valtrion")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.geometry(_bottom_right(root, 420, 150))
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        header = ttk.Label(frame, text=title, font=("Segoe UI", 11, "bold"))
        header.pack(anchor="w")
        ttk.Label(frame, text=detail, foreground="#57606a", wraplength=390).pack(anchor="w")
        phase_label = ttk.Label(frame, text=START_PHASE)
        phase_label.pack(anchor="w", pady=(8, 0))
        bar = ttk.Progressbar(frame, mode="indeterminate", length=390)
        bar.pack(fill="x", pady=4)
        bar.start(25)
        line_label = ttk.Label(frame, text="", foreground="#57606a", wraplength=390)
        line_label.pack(anchor="w")
        root.update_idletasks()
        try:
            _without_activation(root)
        except Exception:  # noqa: BLE001 - a window that may take the focus is still better than none
            pass

        tracker = PhaseTracker()
        started = time.monotonic()
        state = {"offset": 0, "ended": False}

        def close() -> None:
            done_file.unlink(missing_ok=True)
            root.destroy()

        def tick() -> None:
            text, state["offset"] = _read_new(log_file, state["offset"])
            if text:
                tracker.feed(text)
            elapsed = format_elapsed(time.monotonic() - started)
            try:
                ended = json.loads(done_file.read_text(encoding="utf-8")) if done_file.exists() else None
            except (OSError, ValueError):
                ended = None
            if ended is not None and not state["ended"]:
                state["ended"] = True
                status = str(ended.get("status", ""))
                bar.stop()
                bar.configure(mode="determinate", value=100)
                phase_label.configure(text=f"{status}  ({elapsed})", foreground=STATUS_COLORS.get(status, OTHER_STATUS_COLOR),
                                      font=("Segoe UI", 10, "bold"))
                line_label.configure(text=str(ended.get("reason", "")))
                root.after(CLOSE_AFTER_END_MS, close)
                return
            if not _process_alive(parent_pid):
                close()
                return
            counted = testprogress.read(progress_file) if progress_file else None
            if counted is not None and counted["total"] > 0:  # the Unity package reports the tests: a real bar and a counter
                if str(bar.cget("mode")) != "determinate":
                    bar.stop()
                    bar.configure(mode="determinate")
                bar.configure(maximum=counted["total"], value=counted["done"])
                phase_label.configure(text=f"{test_counter_text(counted)}  ({elapsed})")
                line_label.configure(text=(counted["current"] or tracker.last_line)[:160])
            else:
                phase_label.configure(text=f"{tracker.phase}  ({elapsed})")
                line_label.configure(text=tracker.last_line[:160])
            root.after(POLL_MS, tick)

        root.protocol("WM_DELETE_WINDOW", close)
        root.after(POLL_MS, tick)
        root.mainloop()
    except Exception:  # noqa: BLE001 - a view never fails the run
        done_file.unlink(missing_ok=True)
    return 0
