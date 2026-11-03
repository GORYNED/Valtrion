"""Checks before a Unity process starts: no Unity Editor runs outside a lease, and enough memory is free.

An Editor that a crashed run left behind, or one started by hand, holds a project open and memory without holding a lease, so a
leased run would collide with it or starve. The check reads the process list (`ps` on macOS and Linux, PowerShell and CIM on Windows) and
the free memory (`vm_stat`, `/proc/meminfo`, CIM) through a `Probe`, which the tests replace. A process is inside a lease when it or
one of its ancestors is the holder process of a live lease. What cannot be read is reported as a note and never blocks.

`host.json` setting `unityGuard`: `{"orphanEditors": true, "minFreeMemoryMiB": 2048}` (the values shown are the defaults;
`orphanEditors: false` and `minFreeMemoryMiB: 0` turn the checks off).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import config, core, lease

DEFAULT_MIN_FREE_MEMORY_MIB = 2048.0
_COMMAND_LIMIT = 100


@dataclass(frozen=True)
class GuardSettings:
    orphan_editors: bool = True
    min_free_memory_mib: float = DEFAULT_MIN_FREE_MEMORY_MIB


OFF = GuardSettings(False, 0.0)


def load_settings(home: Path | None = None, default_on: bool = True) -> GuardSettings:
    """The `unityGuard` setting of host.json. Without it the checks are `default_on` (on for `unity run`; the build stage and the Editor
    provider pass False, so existing builds change only on a host that opts in with a `unityGuard` object)."""
    base = home or core.valtrion_home()
    absent = GuardSettings() if default_on else OFF
    try:
        document = core.read_json(base / "host.json")
    except FileNotFoundError:
        return absent
    except (OSError, ValueError) as error:
        raise config.ConfigError(f"cannot read {base / 'host.json'}: {error}") from error
    value = document.get("unityGuard") if isinstance(document, dict) else None
    if value is None:
        return absent
    if not isinstance(value, dict):
        raise config.ConfigError("host.json: unityGuard must be an object")
    unknown = sorted(set(value) - {"orphanEditors", "minFreeMemoryMiB"})
    if unknown:
        raise config.ConfigError(f"host.json: unityGuard has unknown keys: {', '.join(unknown)}")
    orphans = value.get("orphanEditors", True)
    if not isinstance(orphans, bool):
        raise config.ConfigError("host.json: unityGuard.orphanEditors must be true or false")
    memory = value.get("minFreeMemoryMiB", DEFAULT_MIN_FREE_MEMORY_MIB)
    if isinstance(memory, bool) or not isinstance(memory, (int, float)) or not math.isfinite(memory) or memory < 0:
        raise config.ConfigError("host.json: unityGuard.minFreeMemoryMiB must be a non-negative number (0 = off)")
    return GuardSettings(orphans, float(memory))


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    #: The executable when the system names it (Windows), else the whole command line (ps).
    path: str
    command: str


# -- parsing what the system prints -------------------------------------------------------------------------------------------

_PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(.*?)\s*$")


def parse_ps(text: str) -> list[Proc]:
    """Output of `ps -axo pid=,ppid=,command=`."""
    found = []
    for line in text.splitlines():
        match = _PS_LINE.match(line)
        if match:
            found.append(Proc(int(match.group(1)), int(match.group(2)), match.group(3), match.group(3)))
    return found


def parse_cim_processes(text: str) -> list[Proc]:
    """Output of `Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json`
    (an array, or one object when only one process matched). A banner or a BOM around the JSON is skipped."""
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        return []
    try:
        document, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return []
    items = document if isinstance(document, list) else [document]
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pid, ppid = int(item["ProcessId"]), int(item.get("ParentProcessId") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        path = item.get("ExecutablePath") or ""
        found.append(Proc(pid, ppid, str(path), str(item.get("CommandLine") or path)))
    return found


def parse_vm_stat(text: str) -> float | None:
    """Free memory in MiB from `vm_stat`: free, inactive and speculative pages (what the system can hand out without paging)."""
    size = re.search(r"page size of (\d+) bytes", text)
    if size is None:
        return None
    pages = 0
    seen = False
    for label in ("free", "inactive", "speculative"):
        match = re.search(rf"^Pages {label}:\s+(\d+)\.?\s*$", text, re.MULTILINE)
        if match:
            pages += int(match.group(1))
            seen = True
    return pages * int(size.group(1)) / (1024 * 1024) if seen else None


def parse_meminfo(text: str) -> float | None:
    """Free memory in MiB from /proc/meminfo: `MemAvailable`, else free + buffers + cached."""
    values = {key: int(number) for key, number in re.findall(r"^(\w+):\s+(\d+)\s*kB", text, re.MULTILINE)}
    if "MemAvailable" in values:
        return values["MemAvailable"] / 1024
    if "MemFree" in values:
        return (values["MemFree"] + values.get("Buffers", 0) + values.get("Cached", 0)) / 1024
    return None


def parse_cim_memory(text: str) -> float | None:
    """`FreePhysicalMemory` (KB) as PowerShell prints it."""
    match = re.search(r"\d+", text or "")
    return int(match.group(0)) / 1024 if match else None


# -- recognising an Editor ----------------------------------------------------------------------------------------------------

_POSIX_EDITOR = re.compile(r"(?:/Unity\.app/Contents/MacOS/Unity|/Editor/Unity)(?:\s|$)")
_WINDOWS_EDITOR = re.compile(r'(?:^|[\\/])Unity\.exe$', re.IGNORECASE)


def _first_word(command: str) -> str:
    """The program of a Windows command line: the quoted first part, else up to the first space."""
    text = command.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text[1:]
    return text.split(None, 1)[0] if text else ""


def _posix_program(command: str) -> str | None:
    """The program a `ps` line starts with, or None when the line is some other program that only mentions an Editor (`sh -c ".../Unity"`,
    a queued `valtrion unity run --unity .../Unity`). The program is an absolute path that may contain spaces, but no further part of it
    starts a new word with `/`, `-` or a quote."""
    match = _POSIX_EDITOR.search(command)
    if match is None or not command.startswith("/"):
        return None
    prefix = command[:match.start()]
    if any(word.startswith(("/", "-", '"', "'")) for word in prefix.split()[1:]) or '"' in prefix or "'" in prefix:
        return None
    return command[:match.end()].rstrip()


def is_unity_editor(proc: Proc, os_name: str) -> bool:
    """A Unity Editor (also its asset-import workers, which are the same executable), not the Hub, a helper or a program that only has
    the Editor's path among its arguments. Only the program is looked at, never the arguments."""
    if os_name == "windows":
        return bool(_WINDOWS_EDITOR.search(proc.path or _first_word(proc.command)))
    return _posix_program(proc.command) is not None


def editors_outside_leases(processes: Iterable[Proc], protected_pids: Iterable[int], os_name: str) -> list[Proc]:
    """Editors none of whose ancestors (nor itself) is in `protected_pids`: the holder processes of live leases and this process."""
    table = {proc.pid: proc for proc in processes}
    protected = set(protected_pids)
    outside = []
    for proc in table.values():
        if not is_unity_editor(proc, os_name):
            continue
        seen: set[int] = set()
        current: Proc | None = proc
        covered = False
        while current is not None and current.pid not in seen:
            if current.pid in protected:
                covered = True
                break
            seen.add(current.pid)
            current = table.get(current.ppid)
        if not covered:
            outside.append(proc)
    return sorted(outside, key=lambda item: item.pid)


# -- the system -----------------------------------------------------------------------------------------------------------------

Runner = Callable[[Sequence[str], float], "str | None"]
#: Longest a single `ps`, `vm_stat` or PowerShell call may take (`SystemProbe.timeout_sec` lowers it when the wait budget is short).
PROBE_TIMEOUT_SEC = 30.0


def run_text(argv: Sequence[str], timeout_sec: float = PROBE_TIMEOUT_SEC) -> str | None:
    try:
        result = subprocess.run(list(argv), capture_output=True, timeout=timeout_sec, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8-sig", "replace")


class Probe:
    """What the check needs from the machine. `processes` and `free_memory_mib` return None when they cannot be read."""

    os_name: str

    def processes(self) -> list[Proc] | None:
        raise NotImplementedError

    def free_memory_mib(self) -> float | None:
        raise NotImplementedError


class SystemProbe(Probe):
    def __init__(self, os_name: str | None = None, runner: Runner = run_text, meminfo: Path = Path("/proc/meminfo")) -> None:
        self.os_name = os_name or core.local_os()
        self._run = runner
        self.timeout_sec = PROBE_TIMEOUT_SEC
        self._meminfo = meminfo

    def _powershell(self, command: str) -> str | None:
        shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return self._run([shell, "-NoProfile", "-NonInteractive", "-Command", command], self.timeout_sec)

    def processes(self) -> list[Proc] | None:
        if self.os_name == "windows":
            text = self._powershell("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine "
                                    "| ConvertTo-Json -Compress")
            return None if text is None else parse_cim_processes(text)
        text = self._run(["ps", "-axo", "pid=,ppid=,command="], self.timeout_sec)
        return None if text is None else parse_ps(text)

    def free_memory_mib(self) -> float | None:
        if self.os_name == "windows":
            text = self._powershell("(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory")
            return None if text is None else parse_cim_memory(text)
        if self.os_name == "macos":
            text = self._run(["vm_stat"], self.timeout_sec)
            return None if text is None else parse_vm_stat(text)
        try:
            return parse_meminfo(self._meminfo.read_text(encoding="utf-8"))
        except OSError:
            return None


#: Seconds between two looks at the process list and free memory while a run waits for them.
GUARD_POLL_SEC = 5.0
#: Makes the probe when a caller names none. A seam for tests of the callers (the build stage, the Editor provider), which must not
#: depend on the Editors that happen to run on the machine.
probe_factory: Callable[[], Probe] = SystemProbe


def blocked_reason(
        manager: lease.LeaseManager, home: Path | None, wait_sec: float, say: Callable[[str], None] | None = None,
        abort: Callable[[], str | None] | None = None, probe: Probe | None = None) -> str | None:
    """The settings of `host.json` and `wait_until_clear` in one call, for a caller that holds its leases already: None to go on, else the
    reason to end BLOCKED (a bad `unityGuard` setting is only a note: the checks are skipped)."""
    try:
        settings = load_settings(home, default_on=False)  # opt in with a unityGuard object in host.json
    except config.ConfigError as error:
        # A setting that cannot be read must not newly block builds that never asked for the check: it is a note, and no check is made.
        if say is not None:
            say(f"note: the pre-start Unity checks were skipped: {error}")
        return None
    return wait_until_clear(manager, settings, probe, wait_sec, GUARD_POLL_SEC, say, abort)


def wait_until_clear(
        manager: lease.LeaseManager, settings: GuardSettings, probe: Probe | None, wait_sec: float, poll_sec: float = GUARD_POLL_SEC,
        say: Callable[[str], None] | None = None, abort: Callable[[], str | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> str | None:
    """Wait, at most `wait_sec`, until no Unity Editor runs outside a lease and enough memory is free; None when clear, else the reason.
    The caller holds its own leases meanwhile (so its Editor is below a lease holder, never an orphan). `abort` returns a reason to stop
    at once (a lost lease). `say` receives the notices (a note about a check that cannot be made, and a waiting line every 30 s)."""
    probe = probe or probe_factory()
    deadline = monotonic() + max(0.0, wait_sec)
    noted: set[str] = set()
    last_notice: float | None = None
    while True:
        why = abort() if abort is not None else None
        if why:
            return why
        holders = [item.record.pid for item in manager.list_all() if item.state == "held" and item.record is not None]
        if isinstance(probe, SystemProbe):
            probe.timeout_sec = max(5.0, min(PROBE_TIMEOUT_SEC, deadline - monotonic()))  # a probe never outlasts the wait by much
        problem, notes = check(probe, settings, holders)
        for note in notes:
            if note not in noted:
                noted.add(note)
                if say is not None:
                    say(f"note: {note}")
        if problem is None:
            return None
        now = monotonic()
        if now >= deadline:
            return problem
        if say is not None and (last_notice is None or now - last_notice >= lease.WAIT_NOTICE_SEC):
            last_notice = now
            say(f"waiting (both leases are held): {problem}")
        sleep(min(poll_sec, max(0.0, deadline - now)))


def check(probe: Probe, settings: GuardSettings, protected_pids: Iterable[int]) -> tuple[str | None, list[str]]:
    """(reason the run must wait or be BLOCKED, or None; notes about checks that could not be made)."""
    notes: list[str] = []
    reasons: list[str] = []
    if settings.orphan_editors:
        processes = probe.processes()
        if processes is None:
            notes.append("the process list could not be read; Unity Editors outside a lease were not checked")
        else:
            orphans = editors_outside_leases(processes, set(protected_pids) | {os.getpid()}, probe.os_name)
            if orphans:
                listed = "; ".join(f"pid {p.pid} ({p.command[:_COMMAND_LIMIT]})" for p in orphans[:5])
                more = f" and {len(orphans) - 5} more" if len(orphans) > 5 else ""
                reasons.append(f"a Unity Editor runs outside any lease: {listed}{more}; stop it, or set host.json unityGuard.orphanEditors to false")
    if settings.min_free_memory_mib > 0:
        free = probe.free_memory_mib()
        if free is None:
            notes.append("the free memory could not be read; it was not checked")
        elif free < settings.min_free_memory_mib:
            reasons.append(f"only {free:.0f} MiB of memory are free, {settings.min_free_memory_mib:.0f} MiB are needed "
                           "(host.json unityGuard.minFreeMemoryMiB)")
    return ("; ".join(reasons) or None), notes
