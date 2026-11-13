"""`valtrion unity run` on another host while this host's `unity-editor` slots are busy (see `offload.py` for the design).

The sending side (`UnityOffload`) looks at the Unity arguments, asks the offload hosts for a free slot, sends the code and runs the same
command on the host in the foreground (like `--host`, with the lifeline and no retry). The receiving side (`receive`) is the hidden
`--offload-*` options of `unity run`: it puts the run on the checkout that `offload import` filled and resets it to the commit once the
project lease is held.

Arguments are rewritten conservatively (`plan`): `-projectPath` becomes a token that the receiver replaces with its checkout's Unity
project folder, `-logFile`, `-testResults` and `-editorTestsResultFile` are redirected into a folder the receiver owns and copied back
afterwards, and an argument that names a path of this machine is a reason not to offload at all.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from . import checkout as checkouts
from . import codesync, compare, config, constants, lease, offload, remote, submit, unityrun
from .config import HostEntry
from .providers.unity import support
from .transport import SSH_FAILURE, evidence, is_tool_missing

KIND = "unity-run"
#: Replaces the value of `-projectPath` on the wire; the receiver puts its checkout's Unity project folder in its place.
PROJECT_TOKEN = "valtrion-offload-project"
#: Starts the reason of a host that refused the run before Unity started (it is not a result of the run).
REFUSED = "offload refused: "
POLL_SEC = offload.POLL_SEC
COPY_TIMEOUT_SEC = 300

_FLAGS = frozenset({"-batchmode", "-nographics", "-quit", "-runtests"})
#: Options whose value is a name, not a path: it is passed as it is and not looked at for paths.
_NAME_OPTIONS = frozenset({"-executemethod", "-testfilter", "-assemblynames", "-testplatform", "-testcategory"})
_OUTPUT_OPTIONS = frozenset({"-logfile", "-testresults", "-editortestsresultfile"})
_PATH_LIKE = re.compile(r"^(?:[\\/]|~|[A-Za-z]:)")
#: What ssh says when it never got as far as running the command.
_NOT_STARTED = re.compile(r"connect to host|could not resolve|connection refused|connection timed out|no route to host|network is unreachable"
                          r"|permission denied|host key verification|kex_exchange_identification|could not be established", re.IGNORECASE)
_STATUS = re.compile(r"^STATUS (\S+)\s*$")


class NotOffloadable(Exception):
    """The Unity arguments cannot be sent to another host; the message is the reason."""


@dataclass(frozen=True)
class _Output:
    """A place in the arguments for the path of an output file on the host."""
    index: int


@dataclass(frozen=True)
class Plan:
    items: tuple  # str or _Output
    outputs: tuple  # (the local file the caller named, as the caller wrote it)

    def render(self, out_dir: str, os_name: str) -> tuple[list[str], list[tuple[str, Path]]]:
        """(the arguments for the host, [(file on the host, local file to copy it to)]) with the output files inside `out_dir`."""
        arguments: list[str] = []
        copies: list[tuple[str, Path]] = []
        for item in self.items:
            if isinstance(item, _Output):
                local = self.outputs[item.index]
                name = submit.last_segment(str(local)) or "output"
                on_host = submit.join_path(out_dir, f"{item.index + 1}-{name}", os_name)
                arguments.append(on_host)
                copies.append((on_host, local))
            else:
                arguments.append(item)
        return arguments, copies


def _path_like(text: str) -> bool:
    return bool(_PATH_LIKE.match(text)) or ".." in text


def plan(unity_args: Sequence[str]) -> Plan:
    """Rewrite the Unity arguments for another host, or `NotOffloadable` with the reason (see the module text)."""
    items: list = []
    outputs: list[Path] = []
    has_project = False
    position = 0
    while position < len(unity_args):
        item = unity_args[position]
        low = item.lower()
        known = low == "-projectpath" or low in _OUTPUT_OPTIONS or low in _NAME_OPTIONS or low in ("-buildtarget", "-activebuildprofile")
        value = None
        if known:
            if position + 1 >= len(unity_args):
                raise NotOffloadable(f"{item} has no value")
            value = unity_args[position + 1]
            position += 1
        position += 1
        if low == "-projectpath":
            has_project = True
            items += [item, PROJECT_TOKEN]
        elif low in _OUTPUT_OPTIONS:
            if low == "-logfile" and value == "-":
                items += [item, value]
            else:
                outputs.append(Path(value).expanduser().absolute())
                items += [item, _Output(len(outputs) - 1)]
        elif low == "-buildtarget":
            if value.lower() != "standalone":
                raise NotOffloadable(f"-buildTarget {value} would switch the platform of the host's checkout (only -buildTarget Standalone is sent)")
            items += [item, value]
        elif low == "-activebuildprofile":
            raise NotOffloadable("-activeBuildProfile names a build profile of this project, not of the host's checkout")
        elif low in _NAME_OPTIONS:
            items += [item, value]
        elif low in _FLAGS:
            items.append(item)
        else:
            for part in (item, item.split("=", 1)[1] if "=" in item else ""):
                if part and _path_like(part):
                    raise NotOffloadable(f"the argument {item!r} looks like a path of this machine")
            items.append(item)
    if not has_project:
        items += ["-projectPath", PROJECT_TOKEN]
    return Plan(tuple(items), tuple(outputs))


def local_project(unity_args: Sequence[str], project_dir: Path | None) -> Path | None:
    """The Unity project this run opens: `-projectPath`, else `--project-dir` (or what the caller resolved)."""
    given = unityrun.option_value(unity_args, "-projectPath")
    chosen = Path(given) if given else project_dir
    return chosen.expanduser().absolute() if chosen is not None else None


def project_names(project_dir: Path, git: compare.GitRunner = compare.run_git) -> list[str]:
    """The names a host may know the project by: its folder, the folder of its git repository, the repository's name at `origin`."""
    names = [project_dir.name]
    code, output = git(project_dir, ["rev-parse", "--show-toplevel"])
    if code == 0 and output.strip():
        names.append(submit.last_segment(output.strip()))
    code, output = git(project_dir, ["remote", "get-url", "origin"])
    if code == 0 and output.strip():
        name = submit.last_segment(output.strip())
        names.append(name[: -len(".git")] if name.endswith(".git") else name)
    return list(dict.fromkeys(name for name in names if name and name not in (".", "..")))


def command_line(raw_argv: Sequence[str]) -> list[str]:
    """The caller's own `unity run` command line up to `--`, without the options that name this machine or are set here."""
    line = list(raw_argv)
    line = line[: line.index("--")] if "--" in line else line
    for name in ("--project-dir", "--lease-wait", "--host", "--remote-project", "--unity"):
        line = submit.drop_option(line, name)
    return [item for item in line if item not in ("--offload", "--no-offload", "--stop-on-stdin-eof")]


def parse_status(stdout: str) -> tuple[str | None, str | None]:
    """(status, reason) from what `unity run` printed: the last `STATUS <name>` line and the line before it."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    for position in range(len(lines) - 1, -1, -1):
        found = _STATUS.match(lines[position])
        if found:
            before = lines[position - 1] if position > 0 and not lines[position - 1].startswith(("STATUS ", "LFS-PREFLIGHT ")) else None
            return found.group(1), before
    return None, None


# -- the sending host ------------------------------------------------------------------------------------------------------------------


@dataclass
class _Attempt:
    code: int | None = None  # the run was done on the host: its exit code
    busy: bool = False
    refused: str | None = None
    lost: str | None = None
    problem: str | None = None  # the code cannot be sent at all (codesync.SnapshotError)


class UnityOffload:
    """Places one `unity run`. Everything that touches the outside world is passed in, so tests drive it with fakes."""

    def __init__(self, unity_args: Sequence[str], raw_argv: Sequence[str], project_dir: Path, *, entries: Sequence[HostEntry],
                 settings: config.OffloadSettings, remote_for: Callable[..., remote.Remote], local_room: Callable[[], bool],
                 timeout_sec: float, force: bool = False, git: compare.GitRunner = compare.run_git,
                 snapshot: Callable[..., codesync.Snapshot] = codesync.snapshot, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, local_host: str = "", unity: str | None = None,
                 err: TextIO | None = None) -> None:
        self.unity = unity
        self.unity_args = list(unity_args)
        self.raw_argv = list(raw_argv)
        self.project_dir = project_dir
        self.entries = list(entries)
        self.settings = settings
        self._remote_for = remote_for
        self._local_room = local_room
        self.timeout_sec = timeout_sec
        self.force = force
        self.git = git
        self.snapshot = snapshot
        self.sleep = sleep
        self.monotonic = monotonic
        self.local_host = local_host
        self._err = err
        self.busy: list[str] = []
        self.refusals: list[str] = []
        self._given_up: set[str] = set()

    # -- output --------------------------------------------------------------------------------------------------------------------------

    def _say(self, text: str) -> None:
        print(text, file=self._err or sys.stderr)

    def _blocked(self, reason: str) -> int:
        self._say(f"valtrion unity: BLOCKED: {reason}")
        return constants.EXIT_BLOCKED

    def _plain(self, entry: HostEntry) -> remote.Remote:
        return self._remote_for(entry, retry=True, lifeline=False)

    # -- placement -----------------------------------------------------------------------------------------------------------------------

    def run(self) -> int | None:
        """None: run here (the caller goes on with the local queue). A number: the exit code of the command."""
        if not self.entries or self._local_room():
            return None
        if self.unity:
            return self._unplaced("--unity names an Editor of this machine")
        try:
            rewritten = plan(self.unity_args)
        except NotOffloadable as error:
            return self._unplaced(str(error))
        names = project_names(self.project_dir, self.git)
        version = support.project_unity_version(self.project_dir)
        deadline = self.monotonic() + (self.settings.settle_sec if not self.force else max(self.settings.settle_sec, 60.0))
        while True:
            targets, reasons = offload.survey(self.entries, self._plain, KIND, version, names)
            if not targets:
                return self._unplaced("no host can take this run: " + "; ".join(reasons) if reasons else "no host can take this run")
            for target in (t for t in targets if offload.free_slots(t.report) > 0 and t.entry.id not in self._given_up):
                attempt = self._attempt(target, rewritten)
                if attempt.code is not None:
                    return attempt.code
                if attempt.lost is not None:
                    return self._lost(target.entry, attempt.lost)
                if attempt.problem is not None:
                    return self._unplaced(attempt.problem, snapshot=True)
                if attempt.busy:
                    self.busy.append(target.entry.id)
                elif attempt.refused:
                    self.refusals.append(f"{target.entry.id}: {attempt.refused}")
                    if "in use" not in attempt.refused:
                        self._given_up.add(target.entry.id)  # waiting does not change it: not asked again for this run
                    if self.force:
                        return self._unplaced(self.refusals[-1])
            if self.monotonic() >= deadline or self._local_room():
                return self._unplaced("; ".join(self.refusals) or "no host has a free Editor slot")
            self.sleep(min(POLL_SEC, max(0.0, deadline - self.monotonic())))

    def _unplaced(self, reason: str, snapshot: bool = False) -> int | None:
        """The run did not go to another host. `--offload` ends it `BLOCKED`; otherwise it is run here and the reason is said."""
        if self.force:
            return self._blocked(reason if snapshot else f"--offload: {reason}")
        if self.busy and not self.refusals and not snapshot:
            self._say(f"offload: host {self.busy[-1]} was busy, running here")
        else:
            self._say(f"offload: not used: {reason}")
        return None

    def _lost(self, entry: HostEntry, reason: str) -> int | None:
        if self.settings.rerun_local:
            self._say(f"offload: host {entry.id} was lost while the run was in progress ({reason}); the run is run again here")
            return None
        return self._blocked(f"host {entry.id} was lost while the run was in progress, so its result is unknown ({reason}); "
                             f"check `valtrion lease list --host {entry.id}`")

    def _attempt(self, target: offload.Target, rewritten: Plan) -> _Attempt:
        entry = target.entry
        self._say(f"offload: local Editor slots are busy; trying host {entry.id}")
        try:
            prepared = offload.prepare(target, self.project_dir, self._plain, self.settings, self.git, KIND, self.snapshot)
        except codesync.SnapshotError as error:
            return _Attempt(problem=str(error))
        except (offload.OffloadError, ValueError, OSError) as error:
            return _Attempt(refused=str(error))
        try:
            out_dir = submit.push_files(self._plain(entry), entry, [])
            arguments, copies = rewritten.render(out_dir, entry.os)
            session = self._remote_for(entry, retry=False, lifeline=True)
        except (submit.SubmitError, ValueError, OSError) as error:
            return _Attempt(refused=str(error))
        argv = [*command_line(self.raw_argv), "--lease-wait", "0", "--stop-on-stdin-eof", "--offload-project", target.project,
                "--offload-checkout", prepared.checkout_id, "--offload-sha", prepared.sha, "--offload-from", self.local_host,
                "--offload-out", out_dir, "--", *arguments]
        result = session.call(argv, int(self.timeout_sec))
        return self._interpret(entry, prepared, result, copies)

    def _interpret(self, entry: HostEntry, prepared: offload.Prepared, result: Any, copies: list[tuple[str, Path]]) -> _Attempt:
        if result.timed_out or result.returncode in (SSH_FAILURE, None):
            if result.returncode == SSH_FAILURE and not result.stdout and _NOT_STARTED.search(result.stderr or ""):
                return _Attempt(refused=f"host {entry.id} is unreachable: {evidence(result)}")
            return _Attempt(lost="no answer before the deadline" if result.timed_out else (evidence(result) or "the connection ended"))
        status, reason = parse_status(result.stdout)
        if status is None:
            if is_tool_missing(result) or (result.returncode == 2 and not (result.stdout or "").strip()):
                what = "the valtrion command is missing there" if is_tool_missing(result) else (evidence(result) or f"exit {result.returncode}")
                return _Attempt(refused=f"host {entry.id} could not run the command: {what}")
            # The command ran and ended without a status: Unity may have started, so the run is not started a second time unasked.
            return _Attempt(lost=evidence(result) or f"exit {result.returncode} without a status line")
        if status == constants.BLOCKED and result.returncode == constants.EXIT_BLOCKED and reason:
            if offload.slot_busy(reason):
                return _Attempt(busy=True)
            if reason.startswith(REFUSED):
                return _Attempt(refused=reason[len(REFUSED):])
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        self._copy_back(entry, copies)
        self._say(f"offload: ran on host {entry.id} ({prepared.note})")
        return _Attempt(code=result.returncode)

    def _copy_back(self, entry: HostEntry, copies: list[tuple[str, Path]]) -> None:
        transport = self._plain(entry).transport
        for on_host, local in copies:
            try:
                local.parent.mkdir(parents=True, exist_ok=True)
                copied = transport.get(on_host, local, COPY_TIMEOUT_SEC)
            except OSError as error:
                self._say(f"offload: {local} was not copied back from host {entry.id}: {error}")
                continue
            if not copied.ok:
                self._say(f"offload: {local} was not copied back from host {entry.id}: {evidence(copied) or 'it is missing there'}")


def default_local_room(home: Path | None = None) -> bool:
    try:
        manager = lease.LeaseManager(home)
        report = manager.slot_report(lease.UNITY_EDITOR_RESOURCE, lease.unity_editor_capacity(home))
    except (ValueError, OSError):
        return True  # the run here says what is wrong with the host's configuration
    return offload.free_slots({"unityEditor": report}) > 0


# -- the receiving host ----------------------------------------------------------------------------------------------------------------


@dataclass
class Receiver:
    unity_args: list[str]
    project_dir: Path
    prepare: Callable[[], str | None]


def receive(unity_args: Sequence[str], project: str, checkout_id: str, sha: str, out_dir: str | None, home: Path | None = None,
            git: compare.GitRunner = compare.run_git, ensure: Callable[..., Any] = checkouts.ensure) -> Receiver | str:
    """What an offloaded run needs on this host, or the reason (it starts with `REFUSED`) why this host will not take it."""
    try:
        settings = config.load_offload_settings(home)
        host = config.load_host_settings(home)
    except config.ConfigError as error:
        return REFUSED + str(error)
    if not settings.accept or KIND not in settings.kinds:
        return REFUSED + f"this host does not accept offloaded work of kind {KIND} (host.json offload.accept and offload.kinds)"
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        return REFUSED + "--offload-sha must be the full 40-character commit"
    managed = host.project(project)
    if managed is None:
        return REFUSED + f"host.json declares no project {project!r}"
    chosen = managed.pick(checkout_id)
    if chosen is None:
        return REFUSED + f"project {project} has no checkout {checkout_id!r}"
    folder = managed.project_dir(chosen)
    arguments = [str(folder) if item == PROJECT_TOKEN else item for item in unity_args]
    if out_dir:
        base = _normal(out_dir)
        for name in _OUTPUT_OPTIONS:
            value = unityrun.option_value(arguments, name)
            if value and value != "-" and not _normal(value).startswith(base + "/"):
                return REFUSED + f"{name} must be inside {out_dir}"

    def slow(where: Path, args: Sequence[str]) -> tuple[int, str]:
        return git(where, args, timeout=1800) if git is compare.run_git else git(where, args)

    def prepare() -> str | None:
        keep = [Path(out_dir)] if out_dir else []
        done = ensure(chosen.path, sha, git, remote=managed.remote, keep=keep, slow_git=slow, unity_path=managed.unity_path)
        return None if done.ok else REFUSED + f"the commit {sha[:12]} cannot be checked out in {chosen.path}: {done.reason}"

    return Receiver(arguments, folder, prepare)


def _normal(path: str) -> str:
    import os

    return os.path.normcase(os.path.abspath(path)).replace("\\", "/").rstrip("/")


def blocked_result(reason: str, out: TextIO | None = None) -> int:
    """The answer of `unity run` that ends `BLOCKED` before it started: the reason, then `STATUS BLOCKED` (what a sender reads)."""
    stream = out or sys.stdout
    print(reason, file=stream)
    print(f"STATUS {constants.BLOCKED}", file=stream)
    stream.flush()
    return constants.EXIT_BLOCKED
