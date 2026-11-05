"""Declared job outputs: paths a job removes when it ends (`job start --cleanup PATH`, a pipeline run stage's `cleanup`).

A job that works in a folder that stays (a project copy given with `--cwd`, `--project-dir` or `--remote-project`) leaves its build
products there; neither `gc` nor `workroot sweep` owns them. The job declares them when it starts, as paths relative to its working
directory, and the runner removes them when the job ends under these guards:

- **refused**: the path is absolute or names a parent (`..`), names the working directory itself or a `.git` entry, resolves outside
  the working directory (a symlink or junction that escapes), is or contains a working copy, or holds files git tracks. A refusal is
  an explicit error at declaration (the job does not start) and, if it only appears later, an entry under `refused`;
- **kept**: the path is absent (nothing to remove), something uses it (a lease on the folder, another active job working in it or
  declaring it), the run is retained or protected, cleanup is off, or the removal failed;
- **removed**: everything else.

The result records `{base, removed, kept, refused}` (job.json and, for a pipeline, result.json), so a run shows what is left.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Sequence

from . import artifacts, compare

#: Runs `git <arguments>` in a folder; (exit code, stdout or error text). `compare.run_git` by default; replaced in tests.
GitRunner = Callable[[Path, Sequence[str]], "tuple[int, str]"]
#: Why something else uses a path right now (a lease holder, another job), or None.
InUse = Callable[[Path], "str | None"]

ABSENT = "absent: nothing to remove"


class OutputError(ValueError):
    """A declared output path cannot be accepted; the message names the path and the reason."""


@dataclass
class CleanupRecord:
    base: str
    removed: list[str] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)

    def to_document(self) -> dict:
        return {"base": self.base, "removed": list(self.removed), "kept": [dict(item) for item in self.kept],
                "refused": [dict(item) for item in self.refused]}


def normalize(text: object) -> str:
    """The declared path in a portable form (`/`-separated, no `.` parts); raises OutputError for a path that is not a plain relative
    path below the working directory. Both Windows and POSIX rules apply, so a declaration means the same on every host."""
    if not isinstance(text, str) or not text.strip():
        raise OutputError(f"cleanup path {text!r} must be a non-empty relative path")
    if "\0" in text:
        raise OutputError(f"cleanup path {text!r} contains a NUL character")
    windows, posix = PureWindowsPath(text), PurePosixPath(text.replace("\\", "/"))
    if windows.drive or windows.root or posix.is_absolute() or text.startswith(("~", "\\", "/")):
        raise OutputError(f"cleanup path {text!r} must be relative to the job's working directory, not absolute")
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise OutputError(f"cleanup path {text!r} must not name a parent folder (`..`)")
    if not parts:
        raise OutputError(f"cleanup path {text!r} names the working directory itself")
    if any(part.casefold() == ".git" for part in parts):
        raise OutputError(f"cleanup path {text!r} names a .git entry")
    for part in parts:
        # Windows resolves these to another name (a stream, the name without its trailing dots and spaces, a device), so the
        # declared text would not be the path removed; they are refused on every host so a declaration means the same everywhere.
        if ":" in part:
            raise OutputError(f"cleanup path {text!r} has a ':' in {part!r} (a drive or an NTFS stream)")
        if part.endswith((".", " ")):
            raise OutputError(f"cleanup path {text!r} has a part ending in a dot or a space ({part!r})")
        if part.split(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
            raise OutputError(f"cleanup path {text!r} names the Windows device {part!r}")
    return "/".join(parts)


_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10)),
                             "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³", "CONIN$", "CONOUT$"})


def _inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return path != base


def name_key(text: str) -> str:
    """How a file system that ignores letter case and Unicode normalization (APFS, NTFS) compares a name: NFC, case-folded. Used for
    every comparison of a declared path with the index, so `Café` in NFD and in NFC, or `out` and `Out`, are one path. A refusal too
    many on a file system that tells them apart is harmless."""
    return unicodedata.normalize("NFC", text).casefold()


def index_match(index_paths: Iterable[str], relative: str) -> str | None:
    """Why the repository-relative `relative` must not be removed given the repository's index (`git ls-files -z` entries), or None:
    it is a tracked entry, a folder with tracked entries below it, or lies inside a tracked entry (a submodule's gitlink, a file).
    Pure: compares `name_key` forms, never the pathspec matching of git (which compares bytes)."""
    key = name_key(relative.strip("/"))
    hits: list[str] = []
    for entry in index_paths:
        if not entry:
            continue
        other = name_key(entry.rstrip("/"))
        if other == key or other.startswith(key + "/"):
            hits.append(entry)
        elif key.startswith(other + "/"):
            return f"it lies inside the tracked entry {entry} (a submodule or a tracked file)"
    if hits:
        return f"it is tracked by git ({len(hits)} tracked file{'s' if len(hits) != 1 else ''}, e.g. {hits[0]})"
    return None


def _has_git_entry(folder: Path) -> bool:
    entry = folder / ".git"
    return entry.exists() or is_link(entry)


def _tracked(base: Path, target: Path, git: GitRunner) -> str | None:
    """Why git forbids removing `target` (inside the resolved `base`), or None. Default deny:

    - a `.git` entry in any folder from `target` up to, not including, `base` means the path lies in (or is) another working copy
      (a submodule, a clone, a nested repository): refused, whatever that repository tracks;
    - otherwise the repository whose root is `base` or the nearest folder above it with a `.git` entry is asked for its whole index,
      and any failure of git (a broken `.git` file, no git, a refused safe.directory) refuses the path;
    - with no `.git` entry at `base` or above there is no repository and nothing is tracked."""
    for folder in (target, *target.parents):
        if folder == base or not _inside(folder, base):
            break
        if _has_git_entry(folder):
            return "it is a working copy" if folder == target else f"it lies inside another working copy ({folder})"
    root = next((folder for folder in (base, *base.parents) if _has_git_entry(folder)), None)
    if root is None:
        return None
    code, output = git(root, ["ls-files", "-z", "--cached"])
    if code != 0:
        return f"git could not list the tracked files of {root} ({output.strip()[:200] or f'exit {code}'})"
    return index_match(output.split("\0"), target.relative_to(root).as_posix())


def is_link(path: Path | str) -> bool:
    """A symlink, or on Windows any reparse point (a directory junction included, which `Path.is_symlink` does not report before
    Python 3.12). Removing one removes the link, never what it points to."""
    try:
        status = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(status.st_mode):
        return True
    return bool(getattr(status, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _working_copy_inside(target: Path) -> str | None:
    """A `.git` entry at or below `target` (another task's working copy, a submodule), walked without following links or junctions."""
    if is_link(target) or not target.is_dir():
        return None
    pending = [target]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.name.casefold() == ".git":
                return "it is a working copy" if Path(folder) == target else f"it contains a working copy ({folder})"
            if not is_link(entry.path) and entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
    return None


def _remove(path: Path) -> None:
    """Remove a file, a folder tree or a link (a junction too: only the link goes); raises OSError when something is left."""
    if is_link(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)  # a directory junction or symlink on an older Windows Python
    else:
        artifacts._remove(path)  # noqa: SLF001 - nested links and junctions are unlinked by rmtree, never followed
    if path.exists() or is_link(path):
        raise OSError(f"{path} is still there")


def locate(base: Path, relative: str) -> tuple[Path | None, str]:
    """(the path on disk that removing `relative` would remove, or None, and why it is refused, or ""). The declared text must name
    the path on disk itself: no folder on the way may be a link or junction, and the resolved name must be the declared one (letter
    case aside), so a short 8.3 name or any other alias the file system resolves cannot reach another path. A declared path that is
    itself a link is located as the link (removing it unlinks it)."""
    resolved_base = base.resolve()
    parts = relative.split("/")
    for depth in range(1, len(parts)):
        prefix = "/".join(parts[:depth])
        if is_link(base / prefix):
            return None, f"the folder {prefix} on its way is a link or junction (only the declared path itself may be one)"
    candidate = base / relative
    leaf_link = is_link(candidate)
    target = (candidate.parent.resolve() / candidate.name) if leaf_link else candidate.resolve()
    if not _inside(target, resolved_base):
        return None, f"it resolves outside the working directory ({candidate.resolve()})"
    if leaf_link and not _inside(candidate.resolve(), resolved_base):
        return None, f"it is a link that points outside the working directory ({candidate.resolve()})"
    actual = target.relative_to(resolved_base).as_posix()
    if name_key(actual) != name_key(relative):
        return None, f"it names a different path on disk ({actual})"
    return target, ""


def refusal(base: Path, relative: str, git: GitRunner = compare.run_git, walk: bool = True) -> str | None:
    """Why the normalized `relative` must never be removed from `base`, or None. Resolves links (symlinks, junctions) and file
    system aliases of the path as it exists now; a path that does not exist yet is judged by the part that does. The git and
    working-copy checks run on the path as found on disk."""
    target, reason = locate(base, relative)
    if target is None:
        return reason
    reason = _tracked(base.resolve(), target, git)
    if reason is None and walk and target.exists() and not is_link(target):
        reason = _working_copy_inside(target)
    return reason


def stage_paths(stages: Sequence[object]) -> list[str]:
    """The `cleanup` paths of a pipeline's run stages, in order; raises OutputError for `cleanup` on another kind of stage."""
    paths: list[str] = []
    for index, stage in enumerate(stages, 1):
        if not isinstance(stage, dict):
            continue
        items = stage.get("cleanup") or []
        if items and stage.get("stage") != "run":
            raise OutputError(f"stage {index} ({stage.get('stage')}): `cleanup` is accepted on run stages only")
        paths.extend(items)
    return paths


def declare(base: Path | None, paths: Iterable[object], git: GitRunner = compare.run_git) -> list[str]:
    """Check paths when the job is declared (job start, pipeline submit) and return them normalized; raises OutputError with the first
    problem. Without `base` (no working directory) any path is refused. When `base` does not exist yet (a managed checkout that the
    build stage creates) only the form is checked here; the rest is checked again when the job ends."""
    normalized: list[str] = []
    items = list(paths)
    if not items:
        return normalized
    if base is None:
        raise OutputError("cleanup paths need the job's working directory they are relative to")
    for item in items:
        relative = normalize(item)
        if relative in normalized:
            continue
        if base.is_dir():
            reason = refusal(base, relative, git, walk=False)  # the outputs do not exist yet, or are about to be rewritten
            if reason:
                raise OutputError(f"cleanup path {item!r} is refused: {reason}")
        normalized.append(relative)
    return normalized


def remove_declared(base: Path, paths: Sequence[str], in_use: InUse = lambda path: None, keep_reason: str | None = None,
                    protected: Iterable[Path] = (), git: GitRunner = compare.run_git) -> CleanupRecord:
    """Remove the declared paths at the end of a job. Every path ends in exactly one of `removed`, `kept` or `refused`."""
    record = CleanupRecord(str(base))
    protected_paths = list(protected)
    for item in paths:
        try:
            relative = normalize(item)
        except OutputError as error:
            record.refused.append({"path": str(item), "reason": str(error)})
            continue
        candidate = base / relative
        if not base.is_dir():
            record.kept.append({"path": relative, "reason": f"{ABSENT} (the working directory {base} does not exist)"})
            continue
        exists = candidate.exists() or is_link(candidate)
        reason = refusal(base, relative, git) if exists else None
        if reason:
            record.refused.append({"path": relative, "reason": reason})
            continue
        if not exists:
            record.kept.append({"path": relative, "reason": ABSENT})
            continue
        if keep_reason:
            record.kept.append({"path": relative, "reason": keep_reason})
            continue
        overlap = artifacts._overlap(candidate.resolve(), protected_paths)  # noqa: SLF001 - same package
        busy = overlap or in_use(candidate)
        if busy:
            record.kept.append({"path": relative, "reason": f"in use: {busy}" if not overlap else busy})
            continue
        # Checked once more just before the removal, and the path removed is the one found on disk, never the declared text.
        target, located = locate(base, relative)
        reason = located or (refusal(base, relative, git) if target is not None else None)
        if target is None or reason:
            record.refused.append({"path": relative, "reason": reason or located})
            continue
        resolved_base = base.resolve()
        swapped = next((folder for folder in target.parents if _inside(folder, resolved_base) and is_link(folder)), None)
        if swapped is not None:  # a folder on the way became a link since the check: narrow, but never followed
            record.refused.append({"path": relative, "reason": f"the folder {swapped} on its way became a link or junction"})
            continue
        try:
            _remove(target)
        except OSError as error:
            record.kept.append({"path": relative, "reason": f"could not be removed (a process may still use it): {error}"})
            continue
        record.removed.append(relative)
    return record
