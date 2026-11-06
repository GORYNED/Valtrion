"""How valtrion reaches a host: the local machine or another machine over SSH.

Local and remote work share one path. A job always runs through the same supervisor on the host that owns the resources; the
local case only skips the transport. A transport runs short, bounded commands (usually `<recorded valtrion command> ...` on the target
host) and copies files; it never keeps a session open for a job, so a dropped connection does not change a job's outcome.
"""

from __future__ import annotations

import abc
import os
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import constants


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command. `timed_out` is set when the deadline killed it; `returncode` is then None."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def status(self) -> str:
        """A timed-out command is TIMEOUT, a non-zero exit is FAIL; the caller decides when a failure means BLOCKED."""
        if self.timed_out:
            return constants.TIMEOUT
        return constants.PASS if self.returncode == 0 else constants.FAIL


#: Starts one process; replaced in tests. Receives argv, timeout seconds, working directory and environment.
Runner = Callable[[Sequence[str], int, "str | None", "Mapping[str, str] | None"], CommandResult]


def run_process(argv: Sequence[str], timeout_sec: int, cwd: str | None = None, env: Mapping[str, str] | None = None,
                stdin: int | None = None) -> CommandResult:
    """Run a process with a hard deadline."""
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_sec, cwd=cwd,
            env=dict(env) if env else None, stdin=stdin, check=False, creationflags=constants.NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        return CommandResult(None, _text(error.stdout), _text(error.stderr), True)
    except OSError as error:
        return CommandResult(127, "", str(error))
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _text(value: object) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


#: ssh itself failed (connect, auth, dropped session); the remote command never ran or its result is unknown.
SSH_FAILURE = 255

#: What one finished call says about the host. `reachable`: the host answered and the command ran (whatever its own exit code).
#: `tool-missing`: the host answered but the program is not there. `transient`: ssh or the network failed in a way that usually
#: passes (retry). `unreachable`: a definitive verdict, with the evidence in `Verdict.detail`.
REACHABLE, TOOL_MISSING, TRANSIENT, UNREACHABLE = "reachable", "tool-missing", "transient", "unreachable"

#: ssh messages that no retry can fix (a retry only delays the verdict, and repeated bad logins can lock the account out).
_PERMANENT_SSH = re.compile(
    r"permission denied|host key verification failed|remote host identification has changed|too many authentication failures"
    r"|no matching (?:host key|key exchange|cipher|mac)|bad configuration option|unsupported option|no such identity", re.IGNORECASE)
#: ssh messages about a stale or broken connection-sharing socket: the next attempt must not use it.
_MUX_SSH = re.compile(r"mux_client_|mux_master|control ?socket|controlpath|master refused", re.IGNORECASE)
#: A shell (or cmd.exe) saying the program does not exist, as opposed to the program failing.
_NOT_FOUND = re.compile(r"command not found|is not recognized as an internal or external command", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """The reading of one result: `state` (one of the constants above), the evidence line, and how many attempts produced it."""

    state: str
    detail: str = ""
    attempts: int = 1

    @property
    def reachable(self) -> bool:
        return self.state in (REACHABLE, TOOL_MISSING)


def evidence(result: CommandResult) -> str:
    """The last non-empty line of stderr (else stdout), cut to 200 characters; what a human needs to see why a call failed."""
    lines = [line.strip() for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def is_permanent_ssh_failure(result: CommandResult, any_exit: bool = False) -> bool:
    """`any_exit`: also for a non-255 exit (scp reports a refused login with exit 1)."""
    return (any_exit or result.returncode == SSH_FAILURE) and bool(_PERMANENT_SSH.search(result.stderr or ""))


def is_connection_state_failure(result: CommandResult) -> bool:
    return result.returncode == SSH_FAILURE and bool(_MUX_SSH.search(result.stderr or ""))


def classify(result: CommandResult, attempts: int = 1) -> Verdict:
    """Read one result of a call through ssh. A non-zero exit of the remote command is NOT a reachability failure."""
    if result.timed_out:
        return Verdict(TRANSIENT, "no answer before the deadline", attempts)
    if result.returncode == SSH_FAILURE:
        if is_permanent_ssh_failure(result):
            return Verdict(UNREACHABLE, f"ssh refused the connection: {evidence(result)}", attempts)
        return Verdict(TRANSIENT, f"ssh failed: {evidence(result) or 'no message'}", attempts)
    if result.returncode is None:
        return Verdict(TRANSIENT, "the call ended without an exit code", attempts)
    if is_tool_missing(result):
        return Verdict(TOOL_MISSING, evidence(result) or f"exit {result.returncode}", attempts)
    return Verdict(REACHABLE, "", attempts)


def is_tool_missing(result: CommandResult) -> bool:
    """The host answered and its shell says the program is not there (exit 126/127, or cmd.exe's message with exit 1)."""
    return result.returncode in (126, 127) or (bool(result.returncode) and bool(_NOT_FOUND.search(result.stderr or "")))


def final_verdict(result: CommandResult, attempts: int = 1) -> Verdict:
    """The verdict once retries are over: a failure that is still transient is now definitive, with the last error as evidence."""
    verdict = classify(result, attempts)
    if verdict.state == TRANSIENT:
        what = "did not answer" if result.timed_out else verdict.detail
        suffix = f" (after {attempts} attempts)" if attempts > 1 else ""
        return Verdict(UNREACHABLE, f"{what}{suffix}", attempts)
    return verdict


_CMD_SAFE = re.compile(r"^[A-Za-z0-9_./:\\@+-]+$")


def quote_cmd(argument: str) -> str:
    """Quote one argument for the cmd.exe that OpenSSH on Windows uses to run the remote command.

    Inside double quotes cmd.exe treats `& | < > ^ ( )` literally, so any other argument is wrapped in them. What cannot be made
    literal is refused with ValueError instead of being interpreted: `%` (expanded even inside quotes), `"` and line breaks."""
    if any(character in argument for character in '%"\r\n\x00'):
        raise ValueError(f"argument {argument!r} contains a character (% \" or a line break) that cmd.exe on the host would interpret")
    if argument and _CMD_SAFE.match(argument):
        return argument
    # A backslash before the closing quote would escape it.
    return '"' + re.sub(r"(\\+)$", r"\1\1", argument) + '"'


class Transport(abc.ABC):
    """Reaches one host. Implementations: `LocalTransport` and `SshTransport`."""

    #: `local` or `ssh`, as recorded in `host.transport` of a result.
    kind: str

    @abc.abstractmethod
    def run(self, command: Sequence[str], timeout_sec: int, cwd: str | None = None) -> CommandResult:
        """Run a command on the host with a hard deadline."""

    @abc.abstractmethod
    def put(self, local: Path, remote: str, timeout_sec: int) -> CommandResult:
        """Copy a local file to the host."""

    @abc.abstractmethod
    def get(self, remote: str, local: Path, timeout_sec: int) -> CommandResult:
        """Copy a file or folder from the host; artifacts keep an identical layout under the caller's artifacts root."""


class LocalTransport(Transport):
    kind = "local"

    def __init__(self, runner: Runner = run_process) -> None:
        self._runner = runner

    def run(self, command: Sequence[str], timeout_sec: int, cwd: str | None = None) -> CommandResult:
        return self._runner(command, timeout_sec, cwd, None)

    def put(self, local: Path, remote: str, timeout_sec: int) -> CommandResult:
        return self._copy(Path(local), Path(remote))

    def get(self, remote: str, local: Path, timeout_sec: int) -> CommandResult:
        return self._copy(Path(remote), Path(local))

    @staticmethod
    def _copy(source: Path, destination: Path) -> CommandResult:
        import shutil
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
        except OSError as error:
            return CommandResult(1, "", str(error))
        return CommandResult(0)


class SshTransport(Transport):
    """OpenSSH in batch mode: never prompts, bounded connect time, no agent forwarding."""

    kind = "ssh"

    def __init__(self, target: str, connect_timeout_sec: int = 10, runner: Runner = run_process, posix: bool = True,
                 control_dir: Path | None = None, multiplex: bool | None = None) -> None:
        if not target:
            raise ValueError("an ssh transport needs a target")
        if target.startswith("-"):
            raise ValueError("an ssh target must not start with '-' (it would be read as an option)")
        if connect_timeout_sec < 1:
            raise ValueError("connect timeout must be positive")
        self.target = target
        #: False for a Windows host whose default shell is not POSIX: commands then use absolute paths and no `cd`.
        self.posix = posix
        self.connect_timeout_sec = connect_timeout_sec
        self._runner = runner
        #: Keep an open pipe as the standard input of the remote command for the length of `run`. ssh forwards it, so the command on the
        #: host sees end of input when this process dies or ssh drops, which a command without a terminal cannot learn otherwise.
        self.lifeline = False
        self.LIFELINE_BEAT_SEC = 5.0
        #: The folder for the multiplexing control sockets. Connection sharing needs a POSIX ssh client (the Windows OpenSSH client
        #: has no ControlMaster), so it is on by default only there and only when a folder is given.
        self.control_dir = control_dir
        self.multiplex = (os.name != "nt") if multiplex is None else multiplex
        self._sharing_reset = False

    def reset_connection(self) -> None:
        """Stop using connection sharing for the following calls of this transport (a stale or broken control socket otherwise fails
        every call the same way). A new transport shares again."""
        if self.multiplex:
            self._sharing_reset = True

    def _options(self) -> list[str]:
        options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout_sec}", "-o", "ForwardAgent=no",
                   # A dead peer is noticed within 15 s x 4 instead of hanging a call until its own timeout.
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
        if self._sharing_reset:
            # After a broken or stale control socket: this connection stands alone and cannot be sent to a dead master.
            options += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
            return options
        socket_path = self._control_path()
        if socket_path:
            # One persistent connection per host: later short calls reuse it instead of paying the handshake each time.
            options += ["-o", "ControlMaster=auto", "-o", "ControlPersist=10m", "-o", f"ControlPath={socket_path}"]
        return options

    def _control_path(self) -> str | None:
        if not self.multiplex or self.control_dir is None:
            return None
        try:
            self.control_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.control_dir, 0o700)
        except OSError:
            return None
        # %C is a 40-character hash of the connection; a unix socket path is limited to about 104 characters.
        path = str(self.control_dir / "cm-%C")
        if any(character.isspace() for character in path):
            return None  # ssh_config would read the value as several tokens; skip sharing rather than fail every call
        return path if len(path) - 2 + 40 <= 100 else None

    def ssh_argv(self, command: Sequence[str], cwd: str | None = None) -> list[str]:
        # POSIX hosts get shell quoting; a Windows host's default shell is cmd.exe, which quotes with double quotes.
        remote = shlex.join(command) if self.posix else " ".join(quote_cmd(part) for part in command)
        if cwd:
            if not self.posix:
                raise ValueError("a working directory needs a POSIX shell on the host; use absolute paths instead")
            # A leading ~/ must stay unquoted or the remote shell would not expand it to the home folder.
            folder = "~/" + shlex.quote(cwd[2:]) if cwd.startswith("~/") else shlex.quote(cwd)
            remote = f"cd {folder} && {remote}"
        return ["ssh", *self._options(), self.target, "--", remote]

    def run(self, command: Sequence[str], timeout_sec: int, cwd: str | None = None) -> CommandResult:
        try:
            argv = self.ssh_argv(command, cwd)
        except ValueError as error:
            return CommandResult(2, "", f"refused to send the command to {self.target}: {error}")
        if not self.lifeline:
            return self._runner(argv, timeout_sec + self.connect_timeout_sec, None, None)
        read_end, write_end = os.pipe()  # the write end stays open exactly as long as this call runs
        done = threading.Event()

        def beat() -> None:
            # One byte at once and then every few seconds: the host trusts the end of its input as a sign of a lost caller only after it
            # has received a byte, so a host whose input was never a live pipe (closed, NUL) is not mistaken for a lost caller.
            while True:
                try:
                    os.write(write_end, b".")
                except OSError:
                    return
                if done.wait(self.LIFELINE_BEAT_SEC):
                    return

        beater = threading.Thread(target=beat, name="valtrion-ssh-lifeline", daemon=True)
        beater.start()
        try:
            return self._runner(argv, timeout_sec + self.connect_timeout_sec, None, None, read_end)  # type: ignore[call-arg]
        finally:
            done.set()
            beater.join(timeout=2)
            os.close(read_end)
            os.close(write_end)

    @staticmethod
    def _scp_path(remote: str) -> str:
        """scp talks SFTP to the server, which expects a Windows drive path as `/C:/dir/file`; a backslash path is not found."""
        if len(remote) > 2 and remote[0].isalpha() and remote[1] == ":" and remote[2] in ("\\", "/"):
            return "/" + remote.replace("\\", "/")
        return remote

    def put(self, local: Path, remote: str, timeout_sec: int) -> CommandResult:
        argv = ["scp", "-r", *self._options(), str(local), f"{self.target}:{self._scp_path(remote)}"]
        return self._runner(argv, timeout_sec, None, None)

    def get(self, remote: str, local: Path, timeout_sec: int) -> CommandResult:
        argv = ["scp", "-r", *self._options(), f"{self.target}:{self._scp_path(remote)}", str(local)]
        return self._runner(argv, timeout_sec, None, None)
