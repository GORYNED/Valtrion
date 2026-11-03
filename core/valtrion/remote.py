"""Reaching a host: retrying transport, remote valtrion invocation, host probing with a short cache, artifact pull.

Every remote call is short, idempotent and bounded (connect and total timeout, retried with backoff). A call that fails or a
connection that drops never changes a job's outcome: the job runs detached on the host and the caller reconnects and reads its
state again. Local and remote share one path; the local transport only skips SSH.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import core, doctor
from .adapter import DeviceInfo
from .config import HostEntry
from .resolver import HostCapabilities, HostUnavailable
from .transport import (
    SSH_FAILURE, TOOL_MISSING, UNREACHABLE, CommandResult, LocalTransport, Runner, SshTransport, Transport, evidence, final_verdict,
    is_connection_state_failure, is_permanent_ssh_failure, is_tool_missing, run_process)

LAUNCHER = "import sys; sys.path.insert(0, sys.argv[1]); from valtrion.cli import main; sys.exit(main(sys.argv[2:]))"
CLI_ROOT = Path(__file__).resolve().parent.parent
HOST_CACHE_TTL_SEC = 300

#: Attempts and pauses of `RetryingTransport`: a call is tried up to 5 times with 1, 2, 4 and 8 s between tries (15 s of pauses),
#: and no new try starts once `RETRY_BUDGET_SEC` has passed since the first one. The bound on one call is therefore the budget plus
#: one call's own deadline (`timeout + connect timeout`); it is never unbounded.
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_SEC = (1.0, 2.0, 4.0, 8.0)
RETRY_BUDGET_SEC = 240.0


def local_cli_argv(args: Sequence[str]) -> list[str]:
    """Run this very CLI in a new interpreter in isolated mode (no working directory or PYTHONPATH on sys.path)."""
    return [sys.executable, "-I", "-c", LAUNCHER, str(CLI_ROOT), *args]


class RetryingTransport(Transport):
    """Wraps a transport and retries calls that failed in the transport itself, with growing pauses. Only idempotent commands
    may be sent through it: a timed-out call may have executed.

    What is retried: a timeout, and exit 255 (ssh itself failed) unless the message says no retry can help (authentication refused,
    host key mismatch, bad ssh option). A remote command's own non-zero exit is never retried. A message about a broken connection
    sharing socket resets sharing on the inner transport first, so the retry does not meet the same dead socket. `last_attempts` is the
    number of tries the latest call took."""

    def __init__(self, inner: Transport, attempts: int = RETRY_ATTEMPTS, backoff_sec: Sequence[float] = RETRY_BACKOFF_SEC,
                 sleep: Callable[[float], None] = time.sleep, budget_sec: float = RETRY_BUDGET_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.kind = inner.kind
        self._inner = inner
        self._attempts = max(1, attempts)
        self._backoff = tuple(backoff_sec) or (1.0,)
        self._sleep = sleep
        self._budget = budget_sec
        self._clock = clock
        self.retries = 0
        self.last_attempts = 1

    @staticmethod
    def _transport_failure(result: CommandResult, copy: bool) -> bool:
        if result.timed_out:
            return True
        if is_permanent_ssh_failure(result, any_exit=copy):
            return False
        return result.returncode != 0 if copy else result.returncode == SSH_FAILURE

    def _attempt(self, call: Callable[[], CommandResult], copy: bool) -> CommandResult:
        started = self._clock()
        self.last_attempts = 1
        result = call()
        for attempt in range(1, self._attempts):
            if not self._transport_failure(result, copy):
                return result
            pause = self._backoff[min(attempt - 1, len(self._backoff) - 1)]
            if self._clock() - started + pause >= self._budget:
                return result
            if is_connection_state_failure(result):
                reset = getattr(self._inner, "reset_connection", None)
                if callable(reset):
                    reset()
            self.retries += 1
            self._sleep(pause)
            self.last_attempts = attempt + 1
            result = call()
        return result

    def run(self, command: Sequence[str], timeout_sec: int, cwd: str | None = None) -> CommandResult:
        return self._attempt(lambda: self._inner.run(command, timeout_sec, cwd), copy=False)

    def put(self, local: Path, remote: str, timeout_sec: int) -> CommandResult:
        return self._attempt(lambda: self._inner.put(local, remote, timeout_sec), copy=True)

    def get(self, remote: str, local: Path, timeout_sec: int) -> CommandResult:
        return self._attempt(lambda: self._inner.get(remote, local, timeout_sec), copy=True)


def make_transport(entry: HostEntry, runner: Runner = run_process, retry: bool = True, sleep: Callable[[float], None] = time.sleep,
                   lifeline: bool = False) -> Transport:
    if entry.kind == "local":
        return LocalTransport(runner)
    # Control sockets live in the valtrion state folder, never under ~/Documents or a synced folder.
    base = SshTransport(entry.ssh_target or "", entry.connect_timeout_sec, runner, posix=entry.os != "windows",
                        control_dir=core.valtrion_home() / "ssh")
    base.lifeline = lifeline
    return RetryingTransport(base, sleep=sleep) if retry else base


@dataclass
class Remote:
    """Runs valtrion commands on a host. For an SSH host the prefix is the absolute path of the `valtrion` command recorded by
    `valtrion hosts sync`, so a non-interactive session needs no PATH; `argv_prefix` replaces it in tests."""

    transport: Transport
    argv_prefix: tuple[str, ...]
    cwd: str | None = None

    def call(self, args: Sequence[str], timeout_sec: int = 60) -> CommandResult:
        return self.transport.run([*self.argv_prefix, *args], timeout_sec, self.cwd)

    @property
    def attempts(self) -> int:
        """How many tries the latest call took (1 for a transport that does not retry)."""
        return getattr(self.transport, "last_attempts", 1)

    def call_json(self, args: Sequence[str], timeout_sec: int = 60) -> tuple[CommandResult, dict | None]:
        result = self.call(args, timeout_sec)
        if not result.ok:
            return result, None
        return result, extract_json_object(result.stdout)


def extract_json_object(text: str) -> dict | None:
    """The first JSON object in `text`. A login shell may print a banner, a message of the day or profile output around what the
    command wrote; none of that is part of the answer, so it is skipped instead of failing the parse."""
    decoder = json.JSONDecoder()
    position = text.find("{")
    while position != -1:
        try:
            document, _ = decoder.raw_decode(text, position)
        except ValueError:
            position = text.find("{", position + 1)
            continue
        if isinstance(document, dict):
            return document
        position = text.find("{", position + 1)
    return None


class RemotePathError(ValueError):
    """A remote project path that cannot be a path on the host: it was rewritten on its way through the local shell."""


#: What MSYS conversion really produces from a POSIX path `/x`: the folder Git for Windows is installed in, or an MSYS2/Cygwin root,
#: followed by the path. A plain folder called `Git`, `msys64` or `cygwin64` that holds the user's own files is not evidence of it.
_SHELL_ROOT = re.compile(r"^[A-Za-z]:[\\/](?:Program Files(?: \(x86\))?[\\/]Git|msys\d*|cygwin\d*)[\\/](?P<rest>.*)$", re.IGNORECASE)
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
#: First folders of a POSIX file system; a path below the shell's root that starts with one of them was most likely typed as `/<name>/...`.
_POSIX_TOP = frozenset({"usr", "users", "home", "opt", "tmp", "mnt", "volumes", "etc", "var", "private"})


def rewritten_by_local_shell(path: str, host_os: str) -> bool:
    """True when `path` looks like a POSIX path of the host that the local shell converted (MSYS path conversion):
    `/Users/me/x` reaches the command as `C:/Program Files/Git/Users/me/x`. For a host that is not Windows any drive path is suspect;
    for a Windows host only a path below the shell's own root whose next folder is a POSIX top-level folder is."""
    if host_os != "windows":
        return bool(_DRIVE_PATH.match(path))
    match = _SHELL_ROOT.match(path)
    return bool(match) and re.split(r"[\\/]", match.group("rest"), maxsplit=1)[0].casefold() in _POSIX_TOP


_WARNED: set[str] = set()


def remote_path_problem(path: str, host_os: str) -> str | None:
    """The message for a remote path that the local shell rewrote, else None."""
    if rewritten_by_local_shell(path, host_os):
        return (f"the remote path {path!r} looks rewritten by the local shell (MSYS path conversion); "
                "set GDV_REMOTE_PROJECT or use a script")
    return None


def check_remote_path(entry: HostEntry, path: str | None) -> None:
    """A remote project path that the local shell rewrote: `RemotePathError` for a host that is not Windows (the path cannot exist
    there); for a Windows host it may be real, so the message is only printed."""
    problem = remote_path_problem(path, entry.os) if path and entry.kind != "local" else None
    if problem is None:
        return
    if entry.os == "windows":
        if problem not in _WARNED:  # once per process, however many remote calls a command makes
            _WARNED.add(problem)
            print(f"valtrion: warning: {problem}", file=sys.stderr)
        return
    raise RemotePathError(problem)


def remote_for(entry: HostEntry, project_dir: str | None, transport: Transport | None = None) -> Remote:
    """The valtrion entry point on `entry`: this CLI for the local host, the recorded `valtrionCommand` for an SSH host (run in
    `project_dir` when given)."""
    check_remote_path(entry, project_dir)
    transport = transport or make_transport(entry)
    if entry.kind == "local":
        return Remote(transport, tuple(local_cli_argv([])))
    if not entry.valtrion_command:
        raise ValueError(f"host {entry.id} has no recorded valtrion command; run `valtrion hosts sync`")
    return Remote(transport, (entry.valtrion_command,), project_dir or None)


# -- probing hosts for the resolver -------------------------------------------------------------------------------------------


def devices_from_report(report: dict) -> list[DeviceInfo]:
    devices = []
    for item in report.get("devices", []):
        if item.get("online", True) is False:  # kept in the registry for its settings, but the last discovery did not find it
            continue
        try:
            devices.append(DeviceInfo(
                item["id"], item["platform"], item["class"], item.get("evidence", "physical"), item.get("model", ""), item.get("os", ""),
                tuple(item.get("capabilities", ())), item.get("exclusive", True)))
        except (KeyError, ValueError):
            continue
    return devices


def make_probe(
        local_report: Callable[[], dict], remote_factory: Callable[[HostEntry], Remote | None], entries: dict[str, HostEntry],
        home: Path | None = None, clock: Callable[[], float] = time.time) -> Callable:
    """A resolver probe: the local host is probed in process, a remote one by `valtrion doctor --json` over its transport. A remote
    report is cached briefly so one run does not probe the same host repeatedly (only a successful report is cached, never a failure).
    A host that cannot be used yields a `HostUnavailable` whose reason says which case it is: the host did not answer (with the last
    error and the number of tries), the host answered but the valtrion command is missing, or the host answered but `doctor` failed."""
    cache_dir = (home or core.valtrion_home()) / "hostcache"

    def probe(host) -> HostCapabilities | HostUnavailable | None:
        entry = entries.get(host.id)
        if host.transport_kind == "local" or entry is None:
            report = local_report()
        else:
            report = _cached(cache_dir, host.id, clock)
            if report is None:
                try:
                    remote = remote_factory(entry)
                except RemotePathError as error:
                    return HostUnavailable(str(error))
                if remote is None:
                    return None
                result, document = remote.call_json(["doctor", "--json"], 90)
                if document is None:
                    return HostUnavailable(describe_probe_failure(result, remote.attempts))
                report = document
                core.atomic_write_json(cache_dir / f"{core.safe_segment(host.id)}.json", {"savedAt": clock(), "report": report})
        return doctor.to_host_capabilities(report, devices_from_report(report))

    return probe


def describe_probe_failure(result: CommandResult, attempts: int = 1) -> str:
    """Why `valtrion doctor --json` gave no report, keeping apart a host that did not answer, a host without the command, and a
    command that ran but did not produce a report."""
    verdict = final_verdict(result, attempts)
    if verdict.state == UNREACHABLE:
        return f"unreachable ({verdict.detail})"
    if verdict.state == TOOL_MISSING:
        return f"reachable, but the valtrion command is missing on the host ({verdict.detail}); run `valtrion hosts sync`"
    if result.ok:
        return "reachable, but `valtrion doctor --json` printed no JSON report"
    return f"reachable, but `valtrion doctor --json` failed (exit {result.returncode})" + (f": {evidence(result)}" if evidence(result) else "")


def _cached(cache_dir: Path, host_id: str, clock: Callable[[], float]) -> dict | None:
    try:
        saved = core.read_json(cache_dir / f"{core.safe_segment(host_id)}.json")
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or clock() - float(saved.get("savedAt", 0)) > HOST_CACHE_TTL_SEC:
        return None
    report = saved.get("report")
    return report if isinstance(report, dict) else None


def pull_run(transport: Transport, remote_dir: str, local_dir: Path, timeout_sec: int = 600) -> CommandResult:
    """Copy a run folder's contents into the caller's artifacts root, same relative layout. Idempotent: a repeated pull
    overwrites with identical files."""
    local_dir.mkdir(parents=True, exist_ok=True)
    if re.match(r"^[A-Za-z]:[\\/]", remote_dir):
        remote_dir = remote_dir.replace("\\", "/")  # scp takes forward slashes in a Windows path (backslashes are eaten by the remote shell)
    return transport.get(remote_dir.rstrip("/\\") + "/.", local_dir, timeout_sec)
