"""`valtrion hosts sync`: the same core version on every SSH host, installed from a wheel built from this installation.

For a host the recorded `valtrionCommand` is asked for its version. A host that already runs this version is left alone; otherwise the
wheel is pushed through the transport and installed into a virtual environment under `~/.valtrion/core/<version>` on that host. The
absolute path of the `valtrion` command in it is recorded in hosts.json, so non-interactive SSH sessions never depend on PATH.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import __version__
from .config import HostEntry
from .transport import SSH_FAILURE, CommandResult, Transport, final_verdict

STEP_TIMEOUT_SEC = 120
INSTALL_TIMEOUT_SEC = 600
_VERSION = re.compile(r"^valtrion (\d+\.\d+\.\d+\S*) \(contract \d+\)\s*$", re.MULTILINE)

OK, UPDATED, BLOCKED, UNREACHABLE = "ok", "updated", "BLOCKED", "unreachable"


@dataclass(frozen=True)
class SyncRow:
    host: str
    status: str
    version: str | None = None
    command: str | None = None
    reason: str = ""

    def document(self) -> dict:
        return {"host": self.host, "status": self.status, "version": self.version, "command": self.command, "reason": self.reason}


def parse_version_output(text: str) -> str | None:
    """The version from `valtrion --version` (`valtrion X.Y.Z (contract N)` on a line of its own). A banner or profile output around it
    is ignored, and a banner line that only starts with `valtrion X.Y.Z` is not mistaken for it; the last matching line wins."""
    matches = _VERSION.findall(text)
    return matches[-1] if matches else None


def remote_version(transport: Transport, command: str) -> tuple[str | None, CommandResult]:
    result = transport.run([command, "--version"], STEP_TIMEOUT_SEC)
    return (parse_version_output(result.stdout) if result.ok else None), result


#: Tried in this order when a host has no `python`: a non-interactive SSH session has no PATH worth trusting, and `python3` there is
#: often the system interpreter, which may be older than 3.10.
POSIX_PYTHON_CANDIDATES = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "python3")
WINDOWS_PYTHON_CANDIDATES = ("python",)
_PROBE = "import os,sys;print('valtrion-home='+os.path.expanduser('~'));print('valtrion-py310='+str(sys.version_info>=(3,10)))"


def parse_python_probe(text: str) -> tuple[str, bool] | None:
    """(home folder, Python is 3.10+) from the probe output, or None. Lines of a login banner or profile around the two marked lines
    are ignored; the marker-less form (home line, then True or False) is still read."""
    home = recent = None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("valtrion-home="):
            home = line[len("valtrion-home="):]
        elif line.startswith("valtrion-py310="):
            recent = line[len("valtrion-py310="):] == "True"
    if home and recent is not None:
        return home, recent
    if len(lines) >= 2 and lines[-1] in ("True", "False"):
        return lines[-2], lines[-1] == "True"
    return None


def python_candidates(entry: HostEntry) -> tuple[str, ...]:
    if entry.python:
        return (entry.python,)
    return WINDOWS_PYTHON_CANDIDATES if entry.os == "windows" else POSIX_PYTHON_CANDIDATES


def find_python(entry: HostEntry, transport: Transport) -> tuple[str | None, CommandResult, str]:
    """(the first candidate that runs Python 3.10+, the last probe result, the last candidate tried). A host that does not answer
    stops the search at once."""
    result, python = CommandResult(127, "", "no candidate"), ""
    for python in python_candidates(entry):
        result = transport.run([python, "-c", _PROBE], STEP_TIMEOUT_SEC)
        if result.timed_out or result.returncode == SSH_FAILURE:
            return None, result, python
        parsed = parse_python_probe(result.stdout) if result.ok else None
        if parsed and parsed[1]:
            return python, result, python
    return None, result, python


def _join(entry: HostEntry, *parts: str) -> str:
    separator = "\\" if entry.os == "windows" else "/"
    return separator.join(part.rstrip("/\\") if index < len(parts) - 1 else part for index, part in enumerate(parts))


def venv_command(entry: HostEntry, home: str, version: str) -> tuple[str, str, str]:
    """(virtual environment folder, its python, its valtrion command) on a host whose home folder is `home`."""
    root = _join(entry, home, ".valtrion", "core", version)
    scripts = "Scripts" if entry.os == "windows" else "bin"
    suffix = ".exe" if entry.os == "windows" else ""
    return root, _join(entry, root, scripts, "python" + suffix), _join(entry, root, scripts, "valtrion" + suffix)


def _failed(host: str, step: str, result: CommandResult) -> SyncRow:
    if result.timed_out or result.returncode == SSH_FAILURE:
        return SyncRow(host, UNREACHABLE, reason=f"{step}: the host did not answer ({final_verdict(result).detail})")
    detail = (result.stderr or result.stdout).strip().splitlines()
    return SyncRow(host, BLOCKED, reason=f"{step} failed" + (f": {detail[-1]}" if detail else f" (exit {result.returncode})"))


def sync_host(entry: HostEntry, transport: Transport, wheel: Path, record: Callable[[str, str, str], None], version: str = __version__) -> SyncRow:
    """Bring one SSH host to `version`. `record(host id, command, python)` stores the command and Python paths (hosts.json) once verified."""
    if entry.valtrion_command:
        found, result = remote_version(transport, entry.valtrion_command)
        if found == version:
            return SyncRow(entry.id, OK, found, entry.valtrion_command, "already at this version")
        if result.timed_out or result.returncode == SSH_FAILURE:
            verdict = final_verdict(result)
            return SyncRow(entry.id, UNREACHABLE, reason=f"the host did not answer ({verdict.detail})")
    python, probe, tried = find_python(entry, transport)
    if python is None:
        if probe.timed_out or probe.returncode == SSH_FAILURE:
            return _failed(entry.id, f"{tried} on the host", probe)
        tried = ", ".join(python_candidates(entry))
        hint = ("remove `python` from the host in hosts.json to probe again, or record another with `valtrion hosts add --python <absolute path>`"
                if entry.python else "install Python 3.10 or newer there and record its absolute path with `valtrion hosts add --python <absolute path>`")
        return SyncRow(entry.id, BLOCKED, reason=f"no usable Python 3.10+ on the host (tried {tried}); {hint}")
    home = (parse_python_probe(probe.stdout) or ("", False))[0]  # find_python returns a candidate only when this parsed
    venv, venv_python, command = venv_command(entry, home, version)
    incoming = _join(entry, home, ".valtrion", "incoming")
    remote_wheel = _join(entry, incoming, wheel.name)
    steps: Sequence[tuple[str, Callable[[], CommandResult]]] = (
        ("create the incoming folder", lambda: transport.run([python, "-c", "import os,sys;os.makedirs(sys.argv[1],exist_ok=True)", incoming], STEP_TIMEOUT_SEC)),
        ("copy the wheel", lambda: transport.put(wheel, remote_wheel, INSTALL_TIMEOUT_SEC)),
        ("create the virtual environment", lambda: transport.run([python, "-m", "venv", venv], INSTALL_TIMEOUT_SEC)),
        ("install the wheel", lambda: transport.run(
            [venv_python, "-m", "pip", "install", "--force-reinstall", "--no-deps", "--disable-pip-version-check", remote_wheel], INSTALL_TIMEOUT_SEC)),
    )
    for step, action in steps:
        result = action()
        if not result.ok:
            return _failed(entry.id, step, result)
    found, result = remote_version(transport, command)
    if found != version:
        return SyncRow(entry.id, BLOCKED, found, command, f"after the install the host reports {found or 'no version'}, expected {version}")
    record(entry.id, command, python)
    return SyncRow(entry.id, UPDATED, found, command, "installed")


def render(rows: Sequence[SyncRow], local_version: str = __version__) -> str:
    lines = [f"local core {local_version}"]
    for row in rows:
        detail = f"{row.version or '-'}  {row.command or '-'}" + (f"  ({row.reason})" if row.reason else "")
        lines.append(f"  {row.host:<20} {row.status:<12} {detail}")
    return "\n".join(lines)


def all_synced(rows: Sequence[SyncRow]) -> bool:
    return all(row.status in (OK, UPDATED) for row in rows)
