"""Running a plan or pipeline on an SSH host without assuming anything about the host's disk.

The caller reads the plan (or the pipeline file and the plans and pre-launch files it names) on its own machine and sends them to a
private `inbox-<id>` folder that the host creates under its own work root (`valtrion workroot inbox`). The command line forwarded to the
host then names the files in that folder, so no path of the caller has to exist on the host.

The Unity project is resolved on the host. An explicit remote project (`--remote-project`, `GDV_REMOTE_PROJECT`) is the working
directory, as before. Otherwise, when the host has a `projectsRoot`, the project is `<projectsRoot>/<name>`, where `<name>` is the
final folder of the Unity project the caller resolved (`--project-dir`, `GDV_UNITY_PROJECT` or `valtrion.json`), else the last segment of
`candidate.repo`; the folder must exist on the host. A host without `projectsRoot` gets the command line as it is.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import core
from .config import HostEntry
from .remote import Remote
from .transport import evidence


class SubmitError(Exception):
    """The run cannot be sent to the host; the message says why and is reported as BLOCKED."""


#: Exit code of the Python one-liner for "no such folder"; any other non-zero exit means the check itself failed.
_NO_SUCH_FOLDER = 3
HOME_MARKER = "valtrion-home="
_IS_DIRECTORY = f"import os, sys; sys.exit(0 if os.path.isdir(sys.argv[1]) else {_NO_SUCH_FOLDER})"
_SEPARATORS = re.compile(r"[\\/:]")
#: Options of `run` and `pipeline` that take no value (every other `--name` is followed by its value).
_FLAGS = frozenset({"--build", "--foreground", "--watch", "--local"})


def last_segment(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1]


def project_names(project_dir: Path | None, repo: str) -> list[tuple[str, str]]:
    """(folder name, where it came from) to look for under projectsRoot, in order: the final folder of the local Unity project, then
    the last segment of `candidate.repo`. Empty and repeated names are left out."""
    repo_name = _SEPARATORS.split(repo.strip().rstrip("/\\"))[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[: -len(".git")]
    names: list[tuple[str, str]] = []
    for name, source in (((project_dir.name, "the Unity project folder name") if project_dir is not None else ("", "")),
                         (repo_name, "candidate.repo")):
        if name and name not in (".", "..") and name not in [n for n, _ in names]:
            names.append((name, source))
    return names


def join_path(root: str, name: str, os_name: str) -> str:
    """`name` below `root` in the notation of the host's operating system (the host's path never goes through this machine's Path)."""
    separator = "\\" if os_name == "windows" else "/"
    return root.rstrip("/\\") + separator + name


def is_directory(prep: Remote, entry: HostEntry, path: str) -> bool | None:
    """True or False when the host answered; None when the host cannot be asked (a Windows host without a recorded Python)."""
    if entry.os != "windows":
        command, missing = ["test", "-d", path], 1
    elif entry.python:
        command, missing = [entry.python, "-c", _IS_DIRECTORY, path], _NO_SUCH_FOLDER
    else:
        return None
    answer = prep.transport.run(command, 30, None)
    if answer.returncode == 0 and not answer.timed_out:
        return True
    if answer.returncode == missing and not answer.timed_out:
        return False
    # Not an answer about the folder: ssh failed, the command was refused or is missing, the Python path is wrong.
    raise SubmitError(f"host {entry.id} could not tell whether {path} exists "
                      f"({'no answer before the deadline' if answer.timed_out else f'exit {answer.returncode}'}): {evidence(answer) or 'no message'}")


def expand_home(prep: Remote, entry: HostEntry, root: str) -> str:
    """A projects root that starts with `~` on a POSIX host, with the host's home folder in its place (a quoted `~` is not expanded)."""
    if entry.os == "windows" or not (root == "~" or root.startswith("~/")):
        return root
    answer = prep.transport.run(["sh", "-c", f'printf "{HOME_MARKER}%s\\n" "$HOME"'], 30, None)
    # A login script may print a banner around the answer: only the last marked line counts.
    marked = [line[len(HOME_MARKER):].strip() for line in answer.stdout.splitlines() if line.startswith(HOME_MARKER)]
    home = marked[-1] if marked else ""
    if not answer.ok or not home.startswith("/"):
        raise SubmitError(f"host {entry.id} did not tell its home folder, needed for projectsRoot {root!r}: {evidence(answer) or 'no message'}")
    return home.rstrip("/") + root[1:]


def find_project(prep: Remote, entry: HostEntry, names: Sequence[tuple[str, str]]) -> str:
    """The first of `names` that is a folder under the host's projectsRoot, as a path on the host. `SubmitError` names the host, the
    root and every name and path that was looked for."""
    root = entry.projects_root or ""
    if not names:
        raise SubmitError(f"host {entry.id} has projectsRoot {root!r}, but no project name could be taken from the project or candidate.repo; "
                          "give --remote-project")
    base = expand_home(prep, entry, root)
    tried = []
    for name, source in names:
        path = join_path(base, name, entry.os)
        if is_directory(prep, entry, path) is not False:
            return path
        tried.append(f"{name!r} (from {source}): {path}")
    raise SubmitError(f"host {entry.id}: no project folder under projectsRoot {root!r}; looked for " + "; ".join(tried)
                      + "; none exists on the host; give --remote-project to name another folder")


def push_files(prep: Remote, entry: HostEntry, files: Sequence[tuple[Path, str]]) -> str:
    """Create an inbox on the host and send `files` (local path, name inside the inbox) into it; returns the inbox path on the host."""
    answer, document = prep.call_json(["workroot", "inbox"], 60)
    inbox = document.get("inbox") if document else None
    if not isinstance(inbox, str) or not inbox:
        raise SubmitError(f"host {entry.id} did not create a folder for the plan: {(answer.stderr or answer.stdout).strip()[:300] or 'no answer'}")
    for local, name in files:
        sent = prep.transport.put(local, join_path(inbox, name, entry.os), 300)
        if not sent.ok:
            raise SubmitError(f"could not send {local} to host {entry.id}: {evidence(sent) or 'no message'}")
    return inbox


# -- what to send -----------------------------------------------------------------------------------------------------------


class Inputs:
    """The files of one run and the names they get in the inbox (one flat folder; the same file is sent once)."""

    def __init__(self) -> None:
        self.files: list[tuple[Path, str]] = []
        self._names: dict[Path, str] = {}

    def add(self, local: Path, called: str | None = None) -> str:
        if not local.is_file():
            raise SubmitError(f"{local} does not exist on this machine, so it cannot be sent to the host")
        key = local.resolve()
        if key not in self._names:
            self._names[key] = f"{len(self._names) + 1}-{core.safe_segment(called or local.name, 'file')}"
            self.files.append((local, self._names[key]))
        return self._names[key]


def _locate(base: Path, name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else base / path


def pipeline_inputs(pipeline_file: Path, document: Mapping[str, Any], inputs: Inputs, staging: Path) -> str:
    """Add a pipeline to `inputs` and return its inbox name. The plans and pre-launch files its stages name are added too (they
    resolve against the pipeline file's folder, as they do on the host); the copy that is sent names them by their inbox names."""
    base = pipeline_file.resolve().parent
    rewritten = json.loads(json.dumps(document))
    for stage in rewritten["stages"]:
        if stage.get("plan"):
            stage["plan"] = inputs.add(_locate(base, stage["plan"]))
        entries = stage.get("prelaunchFiles") or []
        if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("local"), str) and e["local"] for e in entries):
            raise SubmitError(f"prelaunchFiles of the {stage.get('stage')} stage must be a list of {{local, remote}} entries")
        for entry in entries:
            entry["local"] = inputs.add(_locate(base, entry["local"]))
    staged = staging / pipeline_file.name
    core.atomic_write_json(staged, rewritten)
    return inputs.add(staged, pipeline_file.name)


def prelaunch_names(raw: Sequence[str], plan_file: Path, inputs: Inputs) -> dict[str, str]:
    """Inbox names of the `--prelaunch-file LOCAL=REMOTE` files of a `run` (LOCAL is absolute or relative to the plan file)."""
    base = plan_file.resolve().parent
    names: dict[str, str] = {}
    for position, item in enumerate(raw):
        value = item[len("--prelaunch-file="):] if item.startswith("--prelaunch-file=") else raw[position + 1] if item == "--prelaunch-file" and position + 1 < len(raw) else None
        local = value.partition("=")[0] if value else ""
        if local:
            names[local] = inputs.add(_locate(base, local))
    return names


def drop_option(raw: Sequence[str], name: str) -> list[str]:
    """`raw` without `--name VALUE` and `--name=VALUE`."""
    result, skip = [], False
    for item in raw:
        if skip:
            skip = False
        elif item == name:
            skip = True
        elif not item.startswith(name + "="):
            result.append(item)
    return result


def replace_positional(raw: Sequence[str], original: Path, replacement: str) -> list[str]:
    """`raw` with the plan (or pipeline) word replaced: the first word after the command word that is not an option, not the value of
    an option that takes one, and names `original`."""
    result = list(raw)
    for position, item in enumerate(result):
        if position == 0 or item.startswith("-"):
            continue
        previous = result[position - 1]
        if position > 1 and previous.startswith("--") and "=" not in previous and previous not in _FLAGS:
            continue  # the value of an option such as --protect
        if Path(item) == original:
            result[position] = replacement
            return result
    raise SubmitError(f"{original} was not found in the command line to forward")


def replace_prelaunch(raw: Sequence[str], names: Mapping[str, str]) -> list[str]:
    """`--prelaunch-file LOCAL=REMOTE` with LOCAL replaced by the inbox name of that file."""
    result: list[str] = []
    for position, item in enumerate(raw):
        if item.startswith("--prelaunch-file="):
            local, _, remote = item[len("--prelaunch-file="):].partition("=")
            item = f"--prelaunch-file={names.get(local, local)}={remote}"
        elif position and raw[position - 1] == "--prelaunch-file":
            local, _, remote = item.partition("=")
            item = f"{names.get(local, local)}={remote}"
        result.append(item)
    return result

