"""`valtrion hosts sync`: the same core version on every SSH host, installed from a wheel built from this installation.

For a host the recorded `valtrionCommand` is asked for its version. A host that already runs this version (and does not use a
versioned path) is left alone; otherwise the wheel is pushed through the transport and installed

- with pipx when the host has it: one environment that pipx replaces in place, and the short shim pipx exposes (for example
  `<home>/.local/bin/valtrion`) is the recorded command; or
- when it has not, into a virtual environment `~/.valtrion/core/<version>` with a stable shim `~/.valtrion/bin/valtrion` that always
  points to the newest version.

The absolute path of the command is recorded in hosts.json, so non-interactive SSH sessions never depend on PATH. Version folders that
no process uses are removed; one a running job uses stays, and a pipx environment that a running job uses is not replaced.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import __version__
from .config import HostEntry
from .transport import SSH_FAILURE, CommandResult, Transport, final_verdict, is_tool_missing

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
    method: str | None = None

    def document(self) -> dict:
        return {"host": self.host, "status": self.status, "version": self.version, "command": self.command, "reason": self.reason,
                "method": self.method}


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


def shim_command(entry: HostEntry, home: str) -> str:
    """The stable command of the virtual-environment fallback: a symlink (a `.cmd` file on Windows) that follows the newest version."""
    return _join(entry, home, ".valtrion", "bin", "valtrion.cmd" if entry.os == "windows" else "valtrion")


def pipx_command(entry: HostEntry, bin_dir: str) -> str:
    return _join(entry, bin_dir, "valtrion.exe" if entry.os == "windows" else "valtrion")


_VERSIONED = re.compile(r"[\\/]core[\\/][^\\/]+[\\/](bin|Scripts)[\\/]valtrion(\.exe)?$")


def is_versioned_command(command: str) -> bool:
    """A command inside `core/<version>/`: it changes with every release, so a sync replaces it with a stable one."""
    return bool(_VERSIONED.search(command))


def _failed(host: str, step: str, result: CommandResult) -> SyncRow:
    if result.timed_out or result.returncode == SSH_FAILURE:
        return SyncRow(host, UNREACHABLE, reason=f"{step}: the host did not answer ({final_verdict(result).detail})")
    detail = (result.stderr or result.stdout).strip().splitlines()
    return SyncRow(host, BLOCKED, reason=f"{step} failed" + (f": {detail[-1]}" if detail else f" (exit {result.returncode})"))


# -- pipx -------------------------------------------------------------------------------------------------------------------


def pipx_candidates(entry: HostEntry, python: str, home: str) -> tuple[tuple[str, ...], ...]:
    """Ways to start pipx on the host, most specific first: as a module of the chosen Python, next to it, then the usual places."""
    candidates: list[tuple[str, ...]] = [(python, "-m", "pipx")]
    if entry.os == "windows":
        candidates.append(("pipx",))
        return tuple(candidates)
    if python.startswith("/"):
        candidates.append((python.rsplit("/", 1)[0] + "/pipx",))
    for path in ("/opt/homebrew/bin/pipx", "/usr/local/bin/pipx", _join(entry, home, ".local", "bin", "pipx"), "pipx"):
        if (path,) not in candidates:
            candidates.append((path,))
    return tuple(candidates)


def find_pipx(entry: HostEntry, transport: Transport, python: str, home: str) -> tuple[str, ...] | None:
    for candidate in pipx_candidates(entry, python, home):
        result = transport.run([*candidate, "--version"], STEP_TIMEOUT_SEC)
        if result.timed_out or result.returncode == SSH_FAILURE:
            return None
        if result.ok:
            return candidate
    return None


def _pipx_value(transport: Transport, pipx: tuple[str, ...], name: str) -> str | None:
    result = transport.run([*pipx, "environment", "--value", name], STEP_TIMEOUT_SEC)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.ok else []
    return lines[-1] if lines else None


# -- the host-side helper ---------------------------------------------------------------------------------------------------

#: Copied to the host and run with the host's Python (`prune`, `link`, `busy`, `path`). It lists processes with `ps` (POSIX) or
#: PowerShell (Windows); when that fails it changes nothing, so a folder is never removed on a guess.
HOST_SCRIPT = r'''import os, shutil, subprocess, sys


def processes():
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"]
    else:
        argv = ["ps", "-axww", "-o", "args="]
    done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=60)
    if done.returncode != 0:
        raise SystemExit("cannot list processes: " + (done.stderr.strip() or "exit %d" % done.returncode))
    return done.stdout.lower() if os.name == "nt" else done.stdout


def in_use(text, folder):
    return (folder.lower() if os.name == "nt" else folder) in text


def prune(core, keep):
    if not os.path.isdir(core):
        return
    names = [n for n in sorted(os.listdir(core)) if os.path.isdir(os.path.join(core, n)) and n not in keep]
    if not names:
        return
    text = processes()
    for name in names:
        folder = os.path.join(core, name)
        if in_use(text, folder):
            print("busy=" + name)
            continue
        shutil.rmtree(folder)
        print("removed=" + name)


def link(path, target):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.name == "nt":
        with open(path, "w", newline="") as handle:
            handle.write('@echo off\r\n"%s" %%*\r\n' % target)
        return
    temporary = path + ".new"
    if os.path.lexists(temporary):
        os.remove(temporary)
    os.symlink(target, temporary)
    os.replace(temporary, path)


def busy(folder):
    if os.path.isdir(folder) and in_use(processes(), folder):
        print("busy")


def add_path(rc, directory):
    line = 'export PATH="%s:$PATH"  # valtrion' % directory
    rc = os.path.expanduser(rc)
    existing = open(rc, encoding="utf-8").read() if os.path.exists(rc) else ""
    if line in existing:
        print("present")
        return
    with open(rc, "a", encoding="utf-8") as handle:
        handle.write(("" if not existing or existing.endswith("\n") else "\n") + line + "\n")
    print("added")


mode, rest = sys.argv[1], sys.argv[2:]
if mode == "prune":
    prune(rest[0], set(rest[1:]))
elif mode == "link":
    link(rest[0], rest[1])
elif mode == "busy":
    busy(rest[0])
elif mode == "path":
    add_path(rest[0], rest[1])
else:
    raise SystemExit("unknown mode " + mode)
'''


def _put_script(entry: HostEntry, transport: Transport, python: str, home: str) -> tuple[list[str] | None, CommandResult]:
    """Copy the helper to the host's incoming folder; returns the command prefix that runs it."""
    with tempfile.TemporaryDirectory(prefix="valtrion-host-") as folder:
        local = Path(folder) / "valtrion_host.py"
        local.write_text(HOST_SCRIPT, encoding="utf-8")
        remote_path = _join(entry, home, ".valtrion", "incoming", local.name)
        result = transport.put(local, remote_path, INSTALL_TIMEOUT_SEC)
    return ([python, remote_path] if result.ok else None), result


def prune_versions(entry: HostEntry, transport: Transport, python: str, home: str, keep: Sequence[str]) -> str:
    """Remove `core/<version>` folders not in `keep` that no process uses. Best effort: returns a sentence for the report."""
    script, _ = _put_script(entry, transport, python, home)
    if script is None:
        return "old versions were not cleaned up (could not copy the helper)"
    result = transport.run([*script, "prune", _join(entry, home, ".valtrion", "core"), *keep], STEP_TIMEOUT_SEC)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return "old versions were not cleaned up" + (f" ({detail[-1]})" if detail else "")
    marks = [line.strip() for line in result.stdout.splitlines() if "=" in line]
    removed = [m.split("=", 1)[1] for m in marks if m.startswith("removed=")]
    busy = [m.split("=", 1)[1] for m in marks if m.startswith("busy=")]
    parts = ([f"removed old versions {', '.join(removed)}"] if removed else []) + ([f"kept {', '.join(busy)} (in use)"] if busy else [])
    return "; ".join(parts)


def _joined(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def sync_host(entry: HostEntry, transport: Transport, wheel: Path, record: Callable[[str, str, str], None], version: str = __version__) -> SyncRow:
    """Bring one SSH host to `version`. `record(host id, command, python)` stores the command and Python paths (hosts.json) once verified."""
    if entry.valtrion_command:
        found, result = remote_version(transport, entry.valtrion_command)
        if found == version and not is_versioned_command(entry.valtrion_command):
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
    incoming = _join(entry, home, ".valtrion", "incoming")
    remote_wheel = _join(entry, incoming, wheel.name)
    for step, action in (
        ("create the incoming folder", lambda: transport.run([python, "-c", "import os,sys;os.makedirs(sys.argv[1],exist_ok=True)", incoming], STEP_TIMEOUT_SEC)),
        ("copy the wheel", lambda: transport.put(wheel, remote_wheel, INSTALL_TIMEOUT_SEC)),
    ):
        result = action()
        if not result.ok:
            return _failed(entry.id, step, result)
    pipx = find_pipx(entry, transport, python, home)
    if pipx is not None:
        return _sync_with_pipx(entry, transport, python, home, pipx, remote_wheel, record, version)
    return _sync_with_venv(entry, transport, python, home, remote_wheel, record, version)


def _sync_with_pipx(entry: HostEntry, transport: Transport, python: str, home: str, pipx: tuple[str, ...], remote_wheel: str,
                    record: Callable[[str, str, str], None], version: str) -> SyncRow:
    venvs = _pipx_value(transport, pipx, "PIPX_LOCAL_VENVS")
    if venvs:  # pipx replaces its environment; not under a job that runs from it
        script, _ = _put_script(entry, transport, python, home)
        if script is not None:
            probe = transport.run([*script, "busy", _join(entry, venvs, "valtrion")], STEP_TIMEOUT_SEC)
            if probe.ok and "busy" in probe.stdout.split():
                return SyncRow(entry.id, BLOCKED, method="pipx", reason="a running job uses the pipx environment of valtrion, so it is "
                               "not replaced under it; run `valtrion hosts sync` again when the job has ended")
    result = transport.run([*pipx, "install", "--force", "--python", python, remote_wheel], INSTALL_TIMEOUT_SEC)
    if not result.ok:
        return _failed(entry.id, "install the wheel with pipx", result)
    bin_dir = _pipx_value(transport, pipx, "PIPX_BIN_DIR") or _join(entry, home, ".local", "bin")
    command = pipx_command(entry, bin_dir)
    found, result = remote_version(transport, command)
    if found != version:
        return SyncRow(entry.id, BLOCKED, found, command, f"after the install the host reports {found or 'no version'}, expected {version}", "pipx")
    record(entry.id, command, python)
    cleanup = prune_versions(entry, transport, python, home, ())
    return SyncRow(entry.id, UPDATED, found, command, _joined("installed with pipx", cleanup), "pipx")


def _sync_with_venv(entry: HostEntry, transport: Transport, python: str, home: str, remote_wheel: str,
                    record: Callable[[str, str, str], None], version: str) -> SyncRow:
    venv, venv_python, versioned = venv_command(entry, home, version)
    for step, action in (
        ("create the virtual environment", lambda: transport.run([python, "-m", "venv", venv], INSTALL_TIMEOUT_SEC)),
        ("install the wheel", lambda: transport.run(
            [venv_python, "-m", "pip", "install", "--force-reinstall", "--no-deps", "--disable-pip-version-check", remote_wheel], INSTALL_TIMEOUT_SEC)),
    ):
        result = action()
        if not result.ok:
            return _failed(entry.id, step, result)
    found, result = remote_version(transport, versioned)
    if found != version:
        return SyncRow(entry.id, BLOCKED, found, versioned, f"after the install the host reports {found or 'no version'}, expected {version}", "venv")
    command = shim_command(entry, home)
    script, copied = _put_script(entry, transport, python, home)
    if script is None:
        return _failed(entry.id, "copy the helper", copied)
    result = transport.run([*script, "link", command, versioned], STEP_TIMEOUT_SEC)
    if not result.ok:
        return _failed(entry.id, "point the stable command to this version", result)
    found, result = remote_version(transport, command)
    if found != version:
        return SyncRow(entry.id, BLOCKED, found, command, f"the stable command reports {found or 'no version'}, expected {version}", "venv")
    record(entry.id, command, python)
    cleanup = prune_versions(entry, transport, python, home, (version,))
    return SyncRow(entry.id, UPDATED, found, command, _joined("installed in a virtual environment (pipx is not available)", cleanup), "venv")


# -- PATH of non-interactive sessions ---------------------------------------------------------------------------------------


def _folder_of(entry: HostEntry, command: str) -> str:
    return command.rsplit("\\", 1)[0] if entry.os == "windows" else command.rsplit("/", 1)[0]


def path_fix(entry: HostEntry, command: str) -> str:
    """The one-line fix that makes `valtrion` resolvable by name in a non-interactive SSH session."""
    folder = _folder_of(entry, command)
    if entry.os == "macos":
        return f"on the host run: echo 'export PATH=\"{folder}:$PATH\"' >> ~/.zshenv   (or run `valtrion hosts sync --ensure-path`)"
    if entry.os == "linux":
        return f"on the host add `export PATH=\"{folder}:$PATH\"` at the top of ~/.bashrc, above any interactive-shell guard"
    return f"on the host add {folder} to the user PATH (`pipx ensurepath` does it for the pipx folder)"


def path_check(entry: HostEntry, transport: Transport, version: str = __version__) -> dict | None:
    """Whether `valtrion` is found by plain name in a non-interactive SSH session of the host (what a script on the host does).
    None when the host has no recorded command or does not answer; otherwise {"status", "reason", "action"}."""
    if not entry.valtrion_command:
        return None
    result = transport.run(["valtrion", "--version"], STEP_TIMEOUT_SEC)
    if result.timed_out or result.returncode == SSH_FAILURE:
        return None
    if is_tool_missing(result):
        return {"status": "warning", "reason": "`valtrion` is not found by name in a non-interactive SSH session on this host "
                f"(the recorded command {entry.valtrion_command} works, but scripts that run `valtrion` do not find it)",
                "action": path_fix(entry, entry.valtrion_command)}
    found = parse_version_output(result.stdout) if result.ok else None
    if found != version:
        return {"status": "warning", "reason": f"`valtrion` by name on this host is {found or 'not answering'}, not {version}",
                "action": "remove the other installation or put the folder of the recorded command first in PATH"}
    return {"status": "ok", "reason": "`valtrion` is found by name", "action": ""}


def ensure_path(entry: HostEntry, transport: Transport, command: str) -> str:
    """The explicit opt-in (`hosts sync --ensure-path`): append the PATH line for the command's folder to the file a non-interactive
    shell reads. macOS only (`~/.zshenv`); never done without the flag. Returns a sentence for the report."""
    if entry.os != "macos":
        return f"--ensure-path is only done on macOS; {path_fix(entry, command)}"
    python, probe, _ = find_python(entry, transport)
    if python is None:
        return "PATH not changed (no usable Python on the host)"
    home = (parse_python_probe(probe.stdout) or ("", False))[0]
    script, _ = _put_script(entry, transport, python, home)
    if script is None:
        return "PATH not changed (could not copy the helper)"
    result = transport.run([*script, "path", "~/.zshenv", _folder_of(entry, command)], STEP_TIMEOUT_SEC)
    if not result.ok:
        return "PATH not changed (the helper failed)"
    return "PATH line already in ~/.zshenv" if "present" in result.stdout else "added the PATH line to ~/.zshenv"


def render(rows: Sequence[SyncRow], local_version: str = __version__) -> str:
    lines = [f"local core {local_version}"]
    for row in rows:
        detail = f"{row.version or '-'}  {row.command or '-'}" + (f"  ({row.reason})" if row.reason else "")
        lines.append(f"  {row.host:<20} {row.status:<12} {detail}")
    return "\n".join(lines)


def all_synced(rows: Sequence[SyncRow]) -> bool:
    return all(row.status in (OK, UPDATED) for row in rows)
