"""Start a Windows process on a desktop of its own that nobody looks at.

A Unity Editor started in `-batchmode` shows no main window, but the tests it runs can open `EditorWindow`s (a chart, a form) that appear on
the user's screen and take the keyboard and the mouse. A process started on another desktop of the same window station keeps working (it has
windows, focus and a layout, they are just not on the visible desktop), so the user's desktop stays undisturbed.

`start` returns a `DesktopProcess`, which offers what `supervise` needs of a `subprocess.Popen`: `pid`, `poll`, `wait`, `kill`, `returncode`
and the process handle. Windows only; elsewhere `start` raises `OSError`."""
from __future__ import annotations

import os
import subprocess
import uuid
from typing import Mapping, Sequence

GENERIC_ALL = 0x10000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF


class DesktopUnavailable(OSError):
    """The desktop itself could not be created (a service session, a restricted account); nothing was started."""


def available() -> bool:
    return os.name == "nt"


def _api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPCWSTR, ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInformation)]
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, user32, kernel32, StartupInfo, ProcessInformation


def _environment_block(env: Mapping[str, str]) -> str:
    items = sorted(env.items(), key=lambda item: item[0].upper())
    return "".join(f"{key}={value}\0" for key, value in items) + "\0"


class DesktopProcess:
    """The process started by `start`; it owns the process handle and the desktop until `close`."""

    def __init__(self, args: Sequence[str], process_handle: int, pid: int, desktop_handle: int) -> None:
        self.args = list(args)
        self.pid = pid
        self.returncode: int | None = None
        self._handle = process_handle  # also read by supervise.WindowsTree and _resume_process (`int(process._handle)`)
        self._desktop = desktop_handle
        self._ctypes, self._wintypes, self._user32, self._kernel32, _, _ = _api()

    def _exited(self, wait_ms: int) -> bool:
        if self.returncode is not None:
            return True
        if not self._handle:
            return False
        state = self._kernel32.WaitForSingleObject(self._handle, wait_ms)
        if state == WAIT_TIMEOUT:
            return False
        if state != WAIT_OBJECT_0:
            raise OSError(self._ctypes.get_last_error(), "WaitForSingleObject failed")
        code = self._wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(self._handle, self._ctypes.byref(code)):
            raise OSError(self._ctypes.get_last_error(), "GetExitCodeProcess failed")
        self.returncode = int(code.value)
        return True

    def poll(self) -> int | None:
        return self.returncode if self._exited(0) else None

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = INFINITE if timeout is None else max(0, int(timeout * 1000))
        if not self._exited(milliseconds):
            raise subprocess.TimeoutExpired(self.args, timeout)
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        if self.returncode is None and self._handle:
            self._kernel32.TerminateProcess(self._handle, 1)

    terminate = kill

    def close(self) -> None:
        """Release the handles. The result of `poll` stays; the desktop goes away with its last process."""
        if self._handle:
            self._exited(0)
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0
        if self._desktop:
            self._user32.CloseDesktop(self._desktop)
            self._desktop = 0


def start(argv: Sequence[str], cwd: str | None, env: Mapping[str, str] | None, log, creationflags: int) -> DesktopProcess:
    """Start `argv` on a new desktop with its standard output and error in `log` (an open binary file) and no standard input. The
    process is started with `creationflags`; pass CREATE_SUSPENDED to resume it later. Raises OSError when it cannot start."""
    if os.name != "nt":
        raise OSError("a hidden desktop exists on Windows only")
    import msvcrt

    ctypes, wintypes, user32, kernel32, StartupInfo, ProcessInformation = _api()
    name = f"valtrion-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    desktop = user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
    if not desktop:
        raise DesktopUnavailable(ctypes.get_last_error(), "CreateDesktopW failed")
    nul = open(os.devnull, "rb")
    inherited: list[int] = []
    try:
        log.flush()
        out_handle = msvcrt.get_osfhandle(log.fileno())
        in_handle = msvcrt.get_osfhandle(nul.fileno())
        for handle in (out_handle, in_handle):  # only these two are inheritable while the child starts
            if not kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT):
                raise OSError(ctypes.get_last_error(), "SetHandleInformation failed")
            inherited.append(handle)
        startup = StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = name
        startup.dwFlags = STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = in_handle, out_handle, out_handle
        information = ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(part) for part in argv]))
        block = ctypes.create_unicode_buffer(_environment_block(env)) if env is not None else None
        block_pointer = ctypes.cast(block, ctypes.c_void_p) if block is not None else None
        flags = creationflags | (CREATE_UNICODE_ENVIRONMENT if env is not None else 0)
        if not kernel32.CreateProcessW(None, command, None, None, True, flags, block_pointer, cwd, ctypes.byref(startup),
                                       ctypes.byref(information)):
            raise OSError(ctypes.get_last_error(), f"CreateProcessW failed for {argv[0]!r}")
        kernel32.CloseHandle(information.hThread)
        return DesktopProcess(argv, information.hProcess, information.dwProcessId, desktop)
    except BaseException:
        user32.CloseDesktop(desktop)
        raise
    finally:
        for handle in inherited:
            kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0)
        nul.close()
