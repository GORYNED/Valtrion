"""Shared helpers of the host core: Valtrion home, UTC time, atomic file writes, hashing, host identity and process liveness."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME_ENV = "VALTRION_HOME"
#: Set by the job runner for the work it does in-process: the launchd domain of the job (`gui/501`), `launchd` when the job is a launchd
#: agent whose domain is not recorded yet, empty when it has none (a foreground job). Absent outside a job.
JOB_DOMAIN_ENV = "GDV_JOB_LAUNCHD_DOMAIN"

_UTC = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$")
_UNSAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9._:-]+")
_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")

# A file that a reader holds open cannot be replaced on Windows; the writer retries briefly instead of failing.
_REPLACE_ATTEMPTS = 20
_REPLACE_PAUSE_SEC = 0.025


def valtrion_home() -> Path:
    """Machine-local state root (hosts, registry, leases, job index); `VALTRION_HOME` overrides it for tests and isolated runs."""
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else Path.home() / ".valtrion"


def now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{moment.microsecond // 1000:03d}Z"


def utc_now() -> str:
    return format_utc(now())


def parse_utc(text: str) -> datetime:
    match = _UTC.match(text)
    if match is None:
        raise ValueError(f"not a UTC timestamp: {text!r}")
    year, month, day, hour, minute, second, fraction = match.groups()
    microsecond = int((fraction or "0")[:6].ljust(6, "0"))
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), microsecond, tzinfo=timezone.utc)


def safe_identifier(text: str, fallback: str = "x", limit: int = 100) -> str:
    """Make `text` a valid contract identifier: letters, digits and `._:-`, starting with a letter or digit."""
    cleaned = _UNSAFE_IDENTIFIER.sub("-", text).lstrip("._:-")[:limit]
    return cleaned or fallback


def safe_segment(text: str, fallback: str = "x", limit: int = 100) -> str:
    """Make `text` one portable path segment (no colon, which Windows forbids in file names)."""
    cleaned = _UNSAFE_SEGMENT.sub("-", text).lstrip("._-")[:limit]
    return cleaned or fallback


def local_os() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def local_host_id() -> str:
    return safe_identifier(socket.gethostname().lower(), "local-host")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write through a temporary file in the same folder, then rename over the target; a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_PAUSE_SEC)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def to_json(document: Any) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def atomic_write_json(path: Path, document: Any) -> None:
    atomic_write_text(path, to_json(document))


def read_json(path: Path, attempts: int = 10) -> Any:
    """Read a JSON file that another process may be replacing; a brief sharing violation or torn read is retried."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise
        except (PermissionError, json.JSONDecodeError, UnicodeDecodeError) as error:
            last = error
            time.sleep(_REPLACE_PAUSE_SEC)
    assert last is not None
    raise last


def append_line(path: Path, line: str) -> int:
    """Append one line with a single write (append mode keeps concurrent lines whole) and return the new file size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        return handle.tell()


def process_start_token(pid: int) -> str | None:
    """A value that identifies this incarnation of a process id: its start time. Two processes that reuse a pid differ in it, so it
    tells the runner a job recorded from an unrelated process that now owns the pid. None when it cannot be read."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in (created, exited, kernel, user))):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    import subprocess
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.strip()
    return text or None


def process_alive(pid: int) -> bool:
    """Whether a process with this id exists. Used for the holder check of leases; the TTL covers a reused id."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: the process exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
