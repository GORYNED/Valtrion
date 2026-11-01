"""What the Editor and Windows adapters share: project profiles, finding the Editor, tolerant file copies and exit results.

A project describes how it is built and run in `<project>/validation/device/profiles.json`:

    { "schema": "gdv.profiles/1",
      "profiles": { "Validation": {
        "windows": { "executeMethod": "My.Editor.Build.BuildWindows", "productName": "My Validation",
                     "companyName": "My Validation", "scriptingBackend": "mono" },
        "editor":  { "runner": "../../tools/validation/run-unity-tests.py", "planAssembly": "My.Validation.Tests" } } } }

`executeMethod` is the consumer's build hook. It receives `-buildOutput <exe>`, `-gdvProductName`, `-gdvCompanyName`,
`-gdvBackend` (the profile's `scriptingBackend`) and `-gdvDevelopment`, applies the identity for that one build and restores the project settings afterwards.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ... import constants, core, doctor, supervise

PROFILES_RELATIVE = Path("validation") / "device" / "profiles.json"
_VERSION = re.compile(r"m_EditorVersion:\s*(\S+)")
_COMPILE_ERROR = re.compile(r"error CS\d+|Scripts have compiler errors|Compilation failed")
_COPY_ATTEMPTS = 5
_COPY_PAUSE_SEC = 0.05


class ProfileError(Exception):
    """profiles.json exists but is unreadable or malformed. A missing file is not an error; it means no hook is configured."""


def load_profiles(project: Path) -> dict[str, Any]:
    """The `profiles` map of the project's profiles.json (profile name -> platform -> settings); {} when there is no file."""
    path = project / PROFILES_RELATIVE
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ProfileError(f"cannot read {path}: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != "gdv.profiles/1" or not isinstance(document.get("profiles"), dict):
        raise ProfileError(f"{path} must be a gdv.profiles/1 document with a \"profiles\" object (profile name, then platform, then settings)")
    return document["profiles"]


def platform_profile(profiles: dict[str, Any], profile: str | None, platform: str) -> tuple[str | None, dict | None]:
    """(profile name, settings) for the platform: the named profile, else (when none is named) the first profile that has one."""
    if profile is not None:
        entry = (profiles.get(profile) or {}).get(platform)
        return profile, entry if isinstance(entry, dict) else None
    for name in sorted(profiles):
        entry = (profiles[name] or {}).get(platform)
        if isinstance(entry, dict):
            return name, entry
    return None, None


def project_unity_version(project: Path) -> str | None:
    try:
        text = (project / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION.search(text)
    return match.group(1) if match else None


def project_identity(project: Path) -> tuple[str | None, str | None]:
    """(companyName, productName) the project itself builds with, read from ProjectSettings.asset."""
    try:
        text = (project / "ProjectSettings" / "ProjectSettings.asset").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    company = re.search(r"^\s*companyName:\s*(.*?)\s*$", text, re.MULTILINE)
    product = re.search(r"^\s*productName:\s*(.*?)\s*$", text, re.MULTILINE)
    return (company.group(1) if company else None, product.group(1) if product else None)


def find_editor(version: str | None, os_name: str, environ: Mapping[str, str] | None = None) -> tuple[dict | None, str | None]:
    """The installed Editor that matches the project's version, or why there is none."""
    if not version:
        return None, "the project's Unity version cannot be read from ProjectSettings/ProjectVersion.txt"
    editors = doctor.find_editors(os_name, dict(environ) if environ is not None else None)
    for editor in editors:
        if editor["version"] == version:
            return editor, None
    installed = ", ".join(e["version"] for e in editors) or "none"
    return None, f"Unity {version} is not installed (installed: {installed})"


def editor_module_folder(editor: Mapping[str, Any], name: str) -> Path | None:
    """The PlaybackEngines folder of a module, for example `windowsstandalonesupport`, when it is installed."""
    root = Path(editor["path"]).parent
    for engines in (root / "Data" / "PlaybackEngines", root.parent / "PlaybackEngines"):
        if engines.is_dir():
            for entry in engines.iterdir():
                if entry.name.lower() == name.lower():
                    return entry
    return None


def compile_errors(log: Path) -> bool:
    try:
        return bool(_COMPILE_ERROR.search(log.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def copy_tolerant(source: Path, destination: Path) -> str | None:
    """Copy a file or folder that an application may be writing. A file that stays locked is skipped and named in the return
    value; a missing source is reported, never created. Returns None when everything was copied."""
    if not source.exists():
        return f"{source} does not exist"
    skipped: list[str] = []
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        return None if _copy_file(source, destination) else f"{source.name} is locked"
    destination.mkdir(parents=True, exist_ok=True)
    for root, _, files in os.walk(source):
        relative = Path(root).relative_to(source)
        (destination / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            if not _copy_file(Path(root) / name, destination / relative / name):
                skipped.append(str(relative / name))
    return f"locked and skipped: {', '.join(skipped[:5])}" if skipped else None


def _copy_file(source: Path, destination: Path) -> bool:
    for attempt in range(_COPY_ATTEMPTS):
        try:
            shutil.copyfile(source, destination)
            return True
        except FileNotFoundError:
            return True  # replaced between listing and copying; the next poll sees the new file
        except PermissionError:
            if attempt == _COPY_ATTEMPTS - 1:
                return False
            time.sleep(_COPY_PAUSE_SEC)
    return False


def safe_relative(remote: str) -> Path | None:
    """A device-side path must stay inside the data root: relative, no `..`, no drive."""
    if not remote or remote.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", remote):
        return None
    parts = [part for part in re.split(r"[\\/]+", remote) if part]
    if not parts or any(part in (".", "..") for part in parts):
        return None
    return Path(*parts)


def latest_run_folder(out: Path) -> Path | None:
    runs = [p for p in out.glob("*") if p.is_dir()] if out.is_dir() else []
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def write_exit_result(
        out: Path, status: str, reason: str, steps: Sequence[Mapping[str, Any]] = (), unverified: Sequence[str] = (),
        folder_name: str = "adapter") -> Path:
    """Record how a process ended when the harness left no result: the adapter writes a `result.json` in the style of the
    harness next to its heartbeat, into the latest run folder or, when there is none, into `<out>/<folder_name>`."""
    folder = latest_run_folder(out) or out / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    document = {"status": status, "reason": None if status == constants.PASS else reason, "steps": list(steps), "metrics": [],
                "unverified": list(unverified), "writtenBy": "valtrion-adapter"}
    core.atomic_write_json(folder / "result.json", document)
    return folder / "result.json"


_TOOL_STATUS = re.compile(rb"^STATUS (PASS|FAIL|BLOCKED|NEEDS_PERMISSION|STALL|TIMEOUT)\s*$", re.MULTILINE)


def read_tool_status(log: Path) -> tuple[str, str] | None:
    """The `STATUS <status>` line a build tool prints last, with the nonblank line before it as the reason."""
    try:
        with open(log, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 64 * 1024))
            tail = handle.read()
    except OSError:
        return None
    found = list(_TOOL_STATUS.finditer(tail))
    if not found:
        return None
    last = found[-1]
    lines = [line.strip() for line in tail[:last.start()].decode("utf-8", "replace").splitlines() if line.strip()]
    return last.group(1).decode(), lines[-1] if lines else ""


def exit_status(
        returncode: int | None, marker: tuple[str, str] | None, tool_status: tuple[str, str] | None = None) -> tuple[str, str | None]:
    """Status of a process that ended without a harness result: its own `GDV-RESULT` line, a build tool's `STATUS` line, else
    the exit code (never a PASS, because a run without a result proved nothing)."""
    if marker is not None and marker[0] != constants.PASS:
        return marker[0], marker[1] or f"the application reported {marker[0]}"
    if tool_status is not None and tool_status[0] != constants.PASS:
        return tool_status[0], tool_status[1] or f"the runner reported {tool_status[0]}"
    if returncode is None:
        return constants.FAIL, "the process ended and its exit code is unknown"
    if returncode == 0:
        return constants.FAIL, "the process exited normally but the harness wrote no result"
    return constants.FAIL, f"the process exited with code {returncode} without a result"


def run_unity(
        argv: Sequence[str], log: Path, timeout_sec: float, stall_sec: float, cwd: str | None = None) -> supervise.SuperviseResult:
    """Run an Editor command under the watchdog; CPU time of the tree counts as progress (an import or compile logs little)."""
    watchdog = supervise.Watchdog(timeout_sec, stall_sec, progress_files=(log,), cpu_counts=True)
    return supervise.supervise(list(argv), watchdog, log.with_name(log.name + ".console"), cwd)


def is_host_windows() -> bool:
    return core.local_os() == "windows"


def interactive_desktop() -> bool | None:
    """Whether this process runs on an interactive window station (a logged-in desktop). A job started from an SSH session or a
    service does not: a rendering player started there has no screen to draw on and never reports. None when it cannot be told."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetProcessWindowStation.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    station = user32.GetProcessWindowStation()
    if not station:
        return None
    name = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    if not user32.GetUserObjectInformationW(station, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):  # UOI_NAME
        return None
    return name.value == "WinSta0"
