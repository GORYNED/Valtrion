"""`valtrion hosts sync`: the same core version on every SSH host, installed from a wheel built from this installation.

For a host the recorded `valtrionCommand` is asked for its version. A host that already runs this version (and does not use a
versioned path) is left alone; otherwise the wheel is pushed through the transport and installed

- with pipx when the host has it: one environment that pipx replaces in place, and the short shim pipx exposes (for example
  `<home>/.local/bin/valtrion`) is the recorded command; or
- when it has not, into a virtual environment `~/.valtrion/core/<version>` with a stable shim `~/.valtrion/bin/valtrion` that always
  points to the newest version.

The absolute path of the command is recorded in hosts.json, so non-interactive SSH sessions never depend on PATH. Version folders that
no process uses are removed; one a running job uses stays, and a pipx environment that a running job uses is not replaced. The process
list is read in a UTF-8 locale; when it cannot be read faithfully (it fails, is not UTF-8, is empty, or does not show the helper's own
arguments) every version stays.
"""

from __future__ import annotations

import re
import shlex
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

#: Copied to the host and run with the host's Python (`prune`, `link`, `relink`, `busy`, `path`). Nothing is removed unless the host is
#: provably idle (no job whose runner is alive, no lease whose holder is alive, no process using the folder); when any of that cannot
#: be established it changes nothing. It is self-contained (it runs on the host, without the valtrion package): the UTF-8 locale of the
#: process listing mirrors `unityguard.utf8_environment`.
HOST_SCRIPT = r'''import json, os, re, shutil, subprocess, sys, time

VERSION = re.compile(r"^\d+\.\d+\.\d+$")
#: Tried in this order; the first one the C library accepts is used, else the last one.
UTF8_LOCALES = ("en_US.UTF-8", "C.UTF-8")
POSIX_LISTING = ["ps", "-axww", "-o", "pid=,ppid=,args="]
WINDOWS_LISTING = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-CimInstance Win32_Process | ForEach-Object { [string]$_.ProcessId + [char]9 + [string]$_.ParentProcessId + [char]9 + $_.CommandLine }"]
LINE = re.compile(r"^\s*(\d+)\s+(\d+)(?:\s(.*))?$")
VIS = re.compile(rb"\\?M(?:-([\x20-\x7e])|\^([@-_?]))")
ESCAPE = re.compile(rb"\\(?:x([0-9a-fA-F]{2})|([0-3][0-7]{2}))")


class Unreadable(Exception):
    """The process list cannot be trusted; nothing may be removed."""


def utf8_environment():
    """The environment of the process listing on POSIX: `LC_ALL` set to a UTF-8 locale. Over SSH or under launchd the locale is often
    `C`, and then ps prints a non-ASCII command line vis-encoded (macOS: `M-P...`) or with `?` (procps), which matches no path."""
    if os.name == "nt":
        return None
    import locale
    chosen = UTF8_LOCALES[-1]
    try:
        saved = locale.setlocale(locale.LC_CTYPE)
        for name in UTF8_LOCALES:
            try:
                locale.setlocale(locale.LC_CTYPE, name)
            except locale.Error:
                continue
            chosen = name
            break
        locale.setlocale(locale.LC_CTYPE, saved)
    except locale.Error:
        pass
    environment = dict(os.environ)
    environment["LC_ALL"] = chosen
    return environment


def fold(text):
    return text.lower() if os.name == "nt" else text


def decoded_forms(command):
    r"""The command line as printed, and as it reads with vis (`M-x`, `M^X`) and `\xNN`/`\NNN` escapes undone (each kept only when the
    result is valid UTF-8)."""
    forms = [command]
    data = command.encode("utf-8", "surrogateescape")
    vis = VIS.sub(lambda m: bytes([0x80 | m.group(1)[0]]) if m.group(1) else bytes([0xFF if m.group(2) == b"?" else 0x80 | (m.group(2)[0] ^ 0x40)]), data)
    escaped = ESCAPE.sub(lambda m: bytes([int(m.group(1), 16) if m.group(1) else int(m.group(2), 8)]), data)
    for candidate in (vis, escaped):
        try:
            forms.append(candidate.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    return list(dict.fromkeys(fold(form) for form in forms))


def vis(data, slash):
    out = []
    for byte in data:
        if byte == 0x5C:
            out.append("\\\\" if slash else "\\")
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            low = byte & 0x7F
            code = "^" + ("?" if low == 0x7F else chr(low + 0x40)) if low < 0x20 or low == 0x7F else "-" + chr(low)
            out.append(("\\" if slash else "") + ("M" if byte & 0x80 else "") + code)
    return "".join(out)


def encoded_forms(value):
    r"""`value` as it is, and as ps or lsof print it outside a UTF-8 locale (vis with and without backslash, `\xNN`, `\NNN`)."""
    data = value.encode("utf-8", "surrogateescape")
    forms = {value, vis(data, False), vis(data, True)}
    for pattern in ("\\x%02x", "\\x%02X", "\\%03o"):
        forms.add("".join(chr(byte) if 0x20 <= byte <= 0x7E else pattern % byte for byte in data))
    return [fold(form) for form in forms if form]


def mentions(command, value, strip=()):
    """Whether the command line names `value` in any form; the forms of `strip` are taken out of it first, only where they stand as
    whole words (so a program path that starts with one of them still counts)."""
    wanted = encoded_forms(value)
    removed = sorted({form for item in strip if item for form in encoded_forms(item)}, key=len, reverse=True)
    for form in decoded_forms(command):
        for item in removed:
            form = re.sub("(?<![^\\s\"'=])" + re.escape(item) + "(?![^\\s\"'])", " ", form)
        if any(item in form for item in wanted):
            return True
    return False


def processes():
    """{pid: (ppid, command line)} of every process. Raises Unreadable when the listing fails, is not UTF-8, is empty, or does not
    show this helper with its own arguments in a form `mentions` reads (then a path with the same characters would match nothing)."""
    nt = os.name == "nt"
    options = {"creationflags": 0x08000000} if nt else {"env": utf8_environment()}  # CREATE_NO_WINDOW: the caller's code page stays
    try:
        done = subprocess.run(WINDOWS_LISTING if nt else POSIX_LISTING, capture_output=True, timeout=60, **options)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unreadable("cannot list processes: %s" % error)
    if done.returncode != 0:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()
        raise Unreadable("cannot list processes: " + (detail.splitlines()[-1] if detail else "exit %d" % done.returncode))
    try:
        text = (done.stdout or b"").decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Unreadable("the process list is not valid UTF-8")
    table, last = {}, None
    for line in text.splitlines():
        match = LINE.match(line)
        if match:
            last = int(match.group(1))
            table[last] = (int(match.group(2)), match.group(3) or "")
        elif last is not None and line.strip():
            table[last] = (table[last][0], table[last][1] + "\n" + line)  # a command line with a line break
    if not table:
        raise Unreadable("the process list is empty")
    me = table.get(os.getpid())
    if me is None:
        raise Unreadable("the process list does not show this helper (pid %d)" % os.getpid())
    unread = [arg for arg in sys.argv[1:] if arg and not mentions(me[1], arg)]
    if unread:
        raise Unreadable("the process list does not show this helper's own argument %s in a readable form (locale %s)"
                         % (ascii(unread[0]), (utf8_environment() or {}).get("LC_ALL", "default")))
    return table


def helper_family(table):
    """This helper and the shells that started it (whose command line names this script): they name the folders as arguments only."""
    family, pid = set(), os.getpid()
    while pid in table and pid not in family:
        if pid != os.getpid() and not mentions(table[pid][1], sys.argv[0]):
            break
        family.add(pid)
        pid = table[pid][0]
    return family


def in_use(table, folder):
    family = helper_family(table)
    return any(mentions(command, folder, sys.argv[1:] if pid in family else ()) for pid, (_, command) in table.items())


def alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        done = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise SystemExit("cannot check process %d" % pid)
        return str(pid) in done.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def activity(valtrion):
    """Why the host is not idle (a job whose runner is alive or just created, a lease whose holder is alive); None when it is."""
    homes = [valtrion]
    other = os.environ.get("VALTRION_HOME")
    if other and os.path.abspath(other) != os.path.abspath(valtrion):
        homes.append(other)
    for home in homes:
        jobs = os.path.join(home, "jobs")
        for name in sorted(os.listdir(jobs)) if os.path.isdir(jobs) else []:
            index = read(os.path.join(jobs, name)) if name.endswith(".json") else None
            if not isinstance(index, dict) or "dir" not in index:
                continue
            path = os.path.join(index["dir"], "job.json")
            job = read(path)
            if not isinstance(job, dict) or job.get("state") not in ("queued", "running"):
                continue
            starting = not job.get("pid") and time.time() - os.path.getmtime(path) < 600
            if starting or (job.get("pid") and alive(job["pid"])):
                return "job " + name[:-5] + " is " + job["state"]
        for root, _, files in os.walk(os.path.join(home, "leases")):
            for name in files:
                record = read(os.path.join(root, name))
                if isinstance(record, dict) and record.get("pid") and alive(record["pid"]):
                    return "lease " + str(record.get("resource", name)) + " is held"
    return None


def link_target(path):
    if os.path.islink(path):
        return os.path.realpath(path)
    cmd = path + ".cmd"
    if os.path.isfile(cmd):
        match = re.search(r'^"(.*)" %\*', open(cmd, encoding="utf-8", errors="replace").read(), re.M)
        return match.group(1) if match else ""
    return ""


def inside(folder, path):
    folder, path = os.path.normcase(os.path.realpath(folder)), os.path.normcase(os.path.realpath(path))
    return path == folder or path.startswith(folder + os.sep)


def prune(valtrion, below, protect):
    """Remove `core/<X.Y.Z>` folders strictly older than `below` that nothing protects or uses. Names that are not plain versions,
    links, newer versions, the target of the stable link and the folders of `protect` commands are never touched."""
    core = os.path.join(valtrion, "core")
    key = lambda name: tuple(int(part) for part in name.split("."))
    names = sorted(os.listdir(core)) if os.path.isdir(core) else []
    target = link_target(os.path.join(valtrion, "bin", "valtrion"))
    candidates = []
    for name in names:
        folder = os.path.join(core, name)
        if not VERSION.match(name) or os.path.islink(folder) or not os.path.isdir(folder) or key(name) >= key(below):
            continue
        if any(inside(folder, path) for path in [target, *protect] if path):
            continue
        candidates.append(name)
    if not candidates:
        return
    reason = activity(valtrion)
    if reason:
        print("skipped=" + reason)
        return
    try:
        table = processes()
    except Unreadable as error:
        print("unreadable=%s" % error)  # every version stays
        return
    for name in candidates:
        folder = os.path.join(core, name)
        if in_use(table, folder):
            print("busy=" + name)
            continue
        try:
            shutil.rmtree(folder)
            print("removed=" + name)
        except OSError as error:
            print("failed=%s: %s" % (name, error))


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


def relink(path, target):
    if os.path.lexists(path):
        link(path, target)
        print("relinked")


def busy(valtrion, folder):
    reason = activity(valtrion)
    if reason:
        print("busy=" + reason)
    elif os.path.isdir(folder):
        try:
            table = processes()
        except Unreadable as error:
            raise SystemExit(str(error))  # the caller then replaces nothing
        if in_use(table, folder):
            print("busy=a process uses " + folder)


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
    prune(rest[0], rest[1], rest[2:])
elif mode == "link":
    link(rest[0], rest[1])
elif mode == "relink":
    relink(rest[0], rest[1])
elif mode == "busy":
    busy(rest[0], rest[1])
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


def prune_versions(entry: HostEntry, transport: Transport, python: str, home: str, below: str, protect: Sequence[str]) -> str:
    """Remove `core/<X.Y.Z>` folders older than `below` that nothing uses (see HOST_SCRIPT). Best effort: returns a sentence for the
    report; whatever cannot be shown to be unused stays."""
    script, _ = _put_script(entry, transport, python, home)
    if script is None:
        return "old versions were not cleaned up (could not copy the helper)"
    result = transport.run([*script, "prune", _join(entry, home, ".valtrion"), below, *protect], STEP_TIMEOUT_SEC)
    marks = [line.strip() for line in result.stdout.splitlines() if "=" in line]
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return "old versions were not cleaned up" + (f" ({detail[-1]})" if detail else "")

    def values(prefix: str) -> list[str]:
        return [m.split("=", 1)[1] for m in marks if m.startswith(prefix + "=")]

    parts = []
    if values("skipped"):
        parts.append(f"old versions were kept: {values('skipped')[0]}")
    if values("unreadable"):
        parts.append(f"old versions were kept: the process list could not be read ({values('unreadable')[0]})")
    if values("removed"):
        parts.append(f"removed old versions {', '.join(values('removed'))}")
    if values("busy"):
        parts.append(f"kept {', '.join(values('busy'))} (a process uses it)")
    if values("failed"):
        parts.append("could not remove " + "; ".join(values("failed")))
    return "; ".join(parts)


def _joined(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def sync_host(entry: HostEntry, transport: Transport, wheel: Path, record: Callable[[str, str, str], None], version: str = __version__,
              prune_pipx: bool = False) -> SyncRow:
    """Bring one SSH host to `version`. `record(host id, command, python)` stores the command and Python paths (hosts.json) once verified.
    On a host with pipx, old `core/<version>` folders are removed only with `prune_pipx`."""
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
        return _sync_with_pipx(entry, transport, python, home, pipx, remote_wheel, record, version, prune_pipx)
    return _sync_with_venv(entry, transport, python, home, remote_wheel, record, version)


def _sync_with_pipx(entry: HostEntry, transport: Transport, python: str, home: str, pipx: tuple[str, ...], remote_wheel: str,
                    record: Callable[[str, str, str], None], version: str, prune_pipx: bool) -> SyncRow:
    # pipx replaces its environment, so nothing is changed unless the host is shown to be idle.
    venvs = _pipx_value(transport, pipx, "PIPX_LOCAL_VENVS") or _join(entry, home, ".local", "pipx", "venvs")
    script, copied = _put_script(entry, transport, python, home)
    if script is None:
        return SyncRow(entry.id, BLOCKED, method="pipx", reason="cannot tell whether a job is running (could not copy the helper); nothing was replaced")
    probe = transport.run([*script, "busy", _join(entry, home, ".valtrion"), _join(entry, venvs, "valtrion")], STEP_TIMEOUT_SEC)
    if not probe.ok:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        return SyncRow(entry.id, BLOCKED, method="pipx", reason="cannot tell whether a job is running" + (f" ({detail[-1]})" if detail else "")
                       + "; nothing was replaced")
    busy = [line.split("=", 1)[1] for line in probe.stdout.splitlines() if line.startswith("busy=")]
    if busy:
        return SyncRow(entry.id, BLOCKED, method="pipx", reason=f"the host is not idle ({busy[0]}), so the pipx environment of valtrion "
                       "is not replaced under it; run `valtrion hosts sync` again when it is")
    result = transport.run([*pipx, "install", "--force", "--python", python, remote_wheel], INSTALL_TIMEOUT_SEC)
    if not result.ok:
        return _failed(entry.id, "install the wheel with pipx", result)
    bin_dir = _pipx_value(transport, pipx, "PIPX_BIN_DIR") or _join(entry, home, ".local", "bin")
    command = pipx_command(entry, bin_dir)
    found, result = remote_version(transport, command)
    if found != version:
        why = ("pipx did not create that command; check `pipx environment` on the host" if found is None and is_tool_missing(result)
               else f"the host reports {found or 'no version'}, expected {version}")
        return SyncRow(entry.id, BLOCKED, found, command, f"pipx replaced its environment, but {why}; the recorded command was not changed and "
                       "may not work until pipx install --force is run on the host by hand", "pipx")
    record(entry.id, command, python)
    stable = shim_command(entry, home)
    transport.run([*script, "relink", stable, command], STEP_TIMEOUT_SEC)  # a link left from the fallback must not point at a removed version
    cleanup = prune_versions(entry, transport, python, home, version, (command,)) if prune_pipx else ""
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
    cleanup = prune_versions(entry, transport, python, home, version, (command,))
    return SyncRow(entry.id, UPDATED, found, command, _joined("installed in a virtual environment (pipx is not available)", cleanup), "venv")


# -- PATH of non-interactive sessions ---------------------------------------------------------------------------------------


def _folder_of(entry: HostEntry, command: str) -> str:
    return command.rsplit("\\", 1)[0] if entry.os == "windows" else command.rsplit("/", 1)[0]


def path_fix(entry: HostEntry, command: str) -> str:
    """The one-line fix that makes `valtrion` resolvable by name in a non-interactive SSH session."""
    folder = _folder_of(entry, command)
    line = shlex.quote(f'export PATH="{folder}:$PATH"')
    if entry.os == "macos":
        return f"on the host run: echo {line} >> ~/.zshenv   (or run `valtrion hosts sync --ensure-path`)"
    if entry.os == "linux":
        return f"on the host add the line {line} at the top of ~/.bashrc, above any interactive-shell guard"
    return f"on the host add {folder} to the user PATH (`pipx ensurepath` does it for the pipx folder)"


def path_check(entry: HostEntry, transport: Transport, version: str = __version__) -> dict | None:
    """Whether `valtrion` is found by plain name in a non-interactive SSH session of the host (what a script on the host does).
    None when the host has no recorded command or does not answer; otherwise {"status", "reason", "action"}. Exit 127 and the shell's
    own "not found" message mean not found; on Windows a cmd.exe "not recognized" message does too, but an exit 1 without it is the
    program's own failure, reported as such."""
    if not entry.valtrion_command:
        return None
    result = transport.run(["valtrion", "--version"], STEP_TIMEOUT_SEC)
    if result.timed_out or result.returncode == SSH_FAILURE:
        return None
    if result.returncode == 126:
        return {"status": "warning", "reason": "`valtrion` is found by name on this host but cannot be run (exit 126, no permission?)",
                "action": "check the permissions of the first `valtrion` in the host's PATH"}
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
