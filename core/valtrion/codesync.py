"""Deliver the code of a project working copy to another host without pushing it.

An Editor test run can be offloaded to a host that has no copy of the sender's uncommitted work. `snapshot` reads the working copy
of the repository that holds the project and answers which commit the host must check out: the repository's `HEAD` when nothing is
uncommitted, otherwise a snapshot commit (a child of `HEAD` holding the tracked changes and the untracked files that are not
ignored). When there is uncommitted work, or commits that no remote-tracking ref has, a git bundle carries the missing objects; the
host fetches from its own remote when there is no bundle.

The sender's index, working tree, branches and stash are never touched: the snapshot is built in a temporary index file, and the one
temporary ref that `git bundle` needs is always removed. The same content gives the same commit hash (fixed author, committer and
date). A file that the repository stores in LFS cannot be snapshotted (its content is not in the bundle), nor can a changed
submodule; `snapshot` refuses both and names the first paths.

`import_bundle` runs on the receiving host inside its managed checkout: it fetches the snapshot into a ref under
`refs/valtrion/offload/` and leaves the working tree alone (`checkout.ensure` switches to the commit later). It keeps the newest few
of those refs and deletes the older ones.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import compare, nowindow
from .compare import GitRunner

REF_PREFIX = "refs/valtrion/offload/"
MESSAGE = "valtrion offload snapshot"
IDENTITY = {"GIT_AUTHOR_NAME": "valtrion", "GIT_AUTHOR_EMAIL": "valtrion@localhost",
            "GIT_COMMITTER_NAME": "valtrion", "GIT_COMMITTER_EMAIL": "valtrion@localhost"}
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")
_CHUNK = 100  # paths per `check-attr` call
_SHOWN = 5


class SnapshotError(Exception):
    """The working copy cannot be delivered; the message is the reason."""


@dataclass(frozen=True)
class Snapshot:
    base_sha: str  # HEAD of the repository holding the project
    sha: str  # the commit the host must check out: base_sha when nothing is uncommitted, else a snapshot commit
    dirty: bool  # uncommitted changes (tracked, or untracked and not ignored) anywhere in the repository
    unpushed: int  # commits reachable from HEAD but from no remote-tracking ref
    bundle: Path | None  # a git bundle in `staging` when dirty or unpushed > 0, else None (the host fetches `sha` from the remote)
    changed: int  # number of changed paths in the snapshot


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _run(folder: Path, arguments: Sequence[str], env: dict[str, str] | None = None, timeout: float = 600) -> tuple[int, str]:
    """(exit code, stdout or error text) of `git <arguments>` with extra environment variables; nothing is inherited that points git
    at another repository, and nothing is trimmed (a `-z` list ends with its terminator)."""
    environment = compare.git_environment()
    environment.update(env or {})
    try:
        done = subprocess.run(["git", *arguments], cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, env=environment, stdin=subprocess.DEVNULL, **nowindow.OPTIONS)
    except subprocess.TimeoutExpired:
        return 124, f"git {arguments[0] if arguments else ''} timed out after {timeout:g} s"
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    if done.returncode != 0:
        return done.returncode, (done.stderr.strip() or done.stdout.strip())
    return done.returncode, done.stdout


def _text(output: str) -> str:
    return " ".join(output.split())[:200]


def _list(output: str) -> list[str]:
    return [item for item in output.split("\0") if item]


def _names(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:_SHOWN])
    return shown + (f" and {len(paths) - _SHOWN} more" if len(paths) > _SHOWN else "")


def _uncommitted(top: Path, git: GitRunner) -> list[tuple[str, str]]:
    """(status code, path) of every uncommitted path; a renamed or copied path is listed with its origin too."""
    code, output = git(top, ["status", "--porcelain=v1", "-z", "-uall", "--ignore-submodules=none"])
    if code != 0:
        raise SnapshotError(f"git status failed: {_text(output)}")
    entries = output.split("\0")
    found: list[tuple[str, str]] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        found.append((status, path))
        if ("R" in status or "C" in status) and index < len(entries):
            found.append((status, entries[index]))
            index += 1
    return found


def _submodules(top: Path, git: GitRunner) -> set[str]:
    code, output = git(top, ["ls-files", "--stage", "-z"])
    if code != 0:
        raise SnapshotError(f"git ls-files failed: {_text(output)}")
    return {item.split("\t", 1)[1] for item in _list(output) if item.startswith("160000 ") and "\t" in item}


def _lfs_paths(top: Path, paths: Sequence[str], git: GitRunner) -> list[str]:
    found: list[str] = []
    for start in range(0, len(paths), _CHUNK):
        chunk = list(paths[start:start + _CHUNK])
        code, output = git(top, ["check-attr", "-z", "filter", "--", *chunk])
        if code != 0:
            raise SnapshotError(f"git check-attr failed: {_text(output)}")
        parts = output.split("\0")
        for index in range(0, len(parts) - 2, 3):
            if parts[index + 2] == "lfs":
                found.append(parts[index])
    return found


def snapshot(project_dir: Path, git: GitRunner, staging: Path, max_bytes: int = DEFAULT_MAX_BYTES,
             new_id: Callable[[], str] = _new_id) -> Snapshot:
    """The commit a host must check out to get the working copy of the repository that holds `project_dir` (see the module text)."""
    project_dir = Path(project_dir)
    code, output = git(project_dir, ["rev-parse", "--show-toplevel"])
    if code != 0 or not output.strip():
        raise SnapshotError(f"{project_dir} is not inside a git repository: {_text(output)}")
    top = Path(output.strip())
    code, output = git(top, ["rev-parse", "--verify", "HEAD^{commit}"])
    if code != 0 or not _SHA.fullmatch(output.strip()):
        raise SnapshotError(f"the HEAD of {top} cannot be read (a repository without a commit cannot be delivered): {_text(output)}")
    base = output.strip()
    code, output = git(top, ["rev-parse", "--is-shallow-repository"])
    if code == 0 and output.strip() == "true":
        raise SnapshotError(f"{top} is a shallow clone; its history cannot be bundled (run git fetch --unshallow)")

    uncommitted = _uncommitted(top, git)
    live = sorted({path for status, path in uncommitted if "D" not in status})
    if live:
        modules = _submodules(top, git)
        nested = [path for path in live if path in modules or path.endswith("/")
                  or any(path.startswith(module + "/") for module in modules)]
        if nested:
            raise SnapshotError(f"a submodule or a nested repository has changes and cannot be snapshotted: {_names(nested)}")
        lfs = _lfs_paths(top, live, git)
        if lfs:
            raise SnapshotError(f"{len(lfs)} uncommitted path(s) are stored in LFS and cannot be sent (commit and push them): "
                                f"{_names(lfs)}")

    code, output = git(top, ["rev-parse", "HEAD^{tree}"])
    head_tree = output.strip()
    sha = base
    dirty = False
    changed = 0
    if uncommitted:
        try:
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SnapshotError(f"cannot create {staging}: {error}") from error
        index_file = staging / f"index-{new_id()}"
        env = {"GIT_INDEX_FILE": str(index_file)}
        try:
            for step in (["read-tree", "HEAD"], ["add", "-A"]):
                code, output = _run(top, step, env)
                if code != 0:
                    raise SnapshotError(f"git {step[0]} failed while building the snapshot: {_text(output)}")
            code, output = _run(top, ["write-tree"], env)
            tree = output.strip()
            if code != 0 or not _SHA.fullmatch(tree):
                raise SnapshotError(f"git write-tree failed while building the snapshot: {_text(output)}")
            if tree != head_tree:
                code, output = git(top, ["log", "-1", "--format=%cI", "HEAD"])
                moment = output.strip() if code == 0 else ""
                stamp = {"GIT_AUTHOR_DATE": moment, "GIT_COMMITTER_DATE": moment} if moment else {}
                code, output = _run(top, ["commit-tree", tree, "-p", base, "-m", MESSAGE], {**IDENTITY, **stamp})
                if code != 0 or not _SHA.fullmatch(output.strip()):
                    raise SnapshotError(f"git commit-tree failed: {_text(output)}")
                sha = output.strip()
                dirty = True
                code, output = git(top, ["diff-tree", "-r", "--name-only", "-z", "--no-renames", head_tree, tree])
                changed = len(_list(output)) if code == 0 else len(live)
        finally:
            for leftover in (index_file, Path(str(index_file) + ".lock")):
                try:
                    leftover.unlink()
                except OSError:
                    pass

    code, output = git(top, ["rev-list", "--count", "HEAD", "--not", "--remotes"])
    try:
        unpushed = int(output.strip()) if code == 0 else 0
    except ValueError:
        unpushed = 0
    if code != 0:
        raise SnapshotError(f"git rev-list failed: {_text(output)}")

    bundle: Path | None = None
    if dirty or unpushed > 0:
        bundle = _make_bundle(top, git, staging, sha, max_bytes)
    return Snapshot(base, sha, dirty, unpushed, bundle, changed)


def _make_bundle(top: Path, git: GitRunner, staging: Path, sha: str, max_bytes: int) -> Path:
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SnapshotError(f"cannot create {staging}: {error}") from error
    ref = f"{REF_PREFIX}{sha[:8]}"
    target = staging / f"{sha[:8]}.bundle"
    code, output = git(top, ["update-ref", ref, sha])
    if code != 0:
        raise SnapshotError(f"cannot create the temporary ref {ref}: {_text(output)}")
    try:
        code, output = git(top, ["bundle", "create", str(target), ref, "--not", "--remotes"])
    finally:
        git(top, ["update-ref", "-d", ref])
    if code != 0:
        raise SnapshotError(f"git bundle create failed: {_text(output)}")
    try:
        size = target.stat().st_size
    except OSError as error:
        raise SnapshotError(f"the bundle {target} cannot be read: {error}") from error
    if size > max_bytes:
        try:
            target.unlink()
        except OSError:
            pass
        raise SnapshotError(f"too large to send: {size // (1024 * 1024)} MiB, limit {max_bytes // (1024 * 1024)} MiB "
                            "(push the commits and let the host fetch them)")
    return target


def import_bundle(folder: Path, bundle: Path, sha: str, git: GitRunner, slow_git: GitRunner | None = None, remote: str | None = None,
                  keep: int = 5) -> str | None:
    """On the receiving host: make `sha` available in the managed checkout `folder` from `bundle`, without touching its working tree.
    None on success, otherwise a one-line reason."""
    slow = slow_git or git
    if not _SHA.fullmatch(sha):
        return f"{sha!r} is not a full commit hash"
    if not (folder / ".git").exists():
        if not remote:
            return f"{folder} does not exist and the project declares no remote to create it from"
        try:
            folder.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return f"cannot create {folder.parent}: {error}"
        code, output = slow(folder.parent, ["clone", "--quiet", "--no-checkout", "--", remote, str(folder)])
        if code != 0:
            return f"cannot clone {remote} into {folder}: {_text(output)}"
    slow(folder, ["fetch", "--quiet", "origin"])  # best effort: offline, the prerequisites may be here already
    code, output = git(folder, ["bundle", "verify", str(bundle)])
    if code != 0:
        missing = sorted(set(_SHA.findall(output)))
        if missing:
            return (f"the bundle needs commits this host does not have ({', '.join(item[:12] for item in missing[:_SHOWN])}); the "
                    "sender has commits on no remote this host can reach: push them, or give this host access to that remote")
        return f"the bundle cannot be used: {_text(output)}"
    ref = f"{REF_PREFIX}{sha[:8]}"
    code, output = slow(folder, ["fetch", "--quiet", str(bundle), f"+{ref}:{ref}"])
    if code != 0:
        return f"cannot fetch {ref} from the bundle: {_text(output)}"
    code, output = git(folder, ["cat-file", "-e", f"{sha}^{{commit}}"])
    if code != 0:
        return f"the bundle does not hold the commit {sha[:12]}"
    _prune(folder, git, ref, keep)
    return None


def _prune(folder: Path, git: GitRunner, current: str, keep: int) -> None:
    """Delete all but the newest `keep` offload refs (by commit date); the ref just imported always stays."""
    code, output = git(folder, ["for-each-ref", "--sort=-committerdate", "--format=%(refname)", REF_PREFIX])
    if code != 0:
        return
    others = [name for name in output.split("\n") if name.strip() and name.strip() != current]
    for name in others[max(keep - 1, 0):]:
        git(folder, ["update-ref", "-d", name.strip()])
