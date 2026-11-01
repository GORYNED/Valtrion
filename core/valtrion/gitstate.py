"""The commit and the working-tree state a result records for the project it validated.

`read_state` answers two questions about a project folder and never guesses:

- *Which commit?* The full `HEAD` of the repository that holds the folder (the folder may be a subfolder of the repository, or a
  folder of a linked worktree).
- *Is the tree dirty?* `dirty` means the project folder differs from that commit: a tracked file is modified, staged, deleted or
  conflicted, or there is an untracked file that is not ignored. Only the project folder counts, so edits elsewhere in the repository
  are not the project's dirt. The folders the run itself creates (artifacts, work root) are passed as `exclude`: untracked files below them never
count, tracked files below them still do.

When git cannot answer (not a repository, a lock, a timeout, git missing) the state is *unknown*: `known` is False, `dirty` is True
because a clean tree cannot be shown, `sha` is None when the commit could not be read, and `notes` says why in words. A caller records
the notes; a result is never marked dirty, or left without a commit, silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .compare import GitRunner

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
#: Changed paths named in a note; the rest are counted.
PATHS_SHOWN = 5


@dataclass
class GitState:
    sha: str | None  # the full commit of HEAD, None when it could not be read
    dirty: bool  # True also when the state is unknown: a clean tree is never claimed without evidence
    known: bool  # False when `git status` could not be read
    changed: list[str] = field(default_factory=list)  # paths of the changes (the new path of a rename)
    notes: list[str] = field(default_factory=list)  # what the result should say about the commit and the tree


def _inside(folder: Path, path: Path) -> set[str]:
    """Where `path` lies below `folder` (posix, relative), under every spelling that resolves there: a relative `path` is taken
    against `folder` (not the working directory), and a symlinked root is tried both resolved and as written."""
    candidate = path if path.is_absolute() else folder / path
    found: set[str] = set()
    for base, target in ((folder.resolve(), candidate.resolve()), (folder, candidate), (folder.resolve(), candidate)):
        try:
            relative = target.relative_to(base)
        except (OSError, ValueError):
            continue
        if relative.parts and ".." not in relative.parts:
            found.add(relative.as_posix())
    return found


def _entries(output: str) -> list[tuple[str, str]]:
    """(`XY`, path) of each entry of `git status --porcelain -z`; a rename or copy is followed by its source path."""
    entries: list[tuple[str, str]] = []
    tokens = iter(token for token in output.split("\0") if token)
    for token in tokens:
        if len(token) < 4:
            continue
        entries.append((token[:2], token[3:]))
        if token[0] in "RC" or token[1] in "RC":
            next(tokens, None)
    return entries


def _reason(output: str) -> str:
    text = " ".join(output.split())
    return text[:200] if text else "git gave no message"


def read_state(folder: Path, git: GitRunner, exclude: Sequence[Path] = ()) -> GitState:
    """The commit and the dirty flag of the project in `folder`; see the module text."""
    notes: list[str] = []
    code, head = git(folder, ["rev-parse", "HEAD"])
    head = head.strip()
    sha = head if code == 0 and _FULL_SHA.fullmatch(head) else None
    if sha is None:
        notes.append(f"the commit of the checkout could not be read ({_reason(head if code != 0 else 'HEAD is not a commit hash')})")
    # `.` limits the status to the project folder. Every untracked file is listed (not a collapsed folder) so an excluded root can be
    # told apart from other new files below the same parent.
    excluded = sorted({relative for path in exclude for relative in _inside(folder, Path(path))})
    prefix = ""
    code, output = 0, ""
    if excluded:
        code, output = git(folder, ["rev-parse", "--show-prefix"])  # where the project folder lies in the repository
        prefix = output.strip()
    if code == 0:
        code, output = git(folder, ["status", "--porcelain", "-z", "--untracked-files=all", "--ignore-submodules=untracked", "--", "."])
    if code != 0:
        notes.append(f"the working-tree state could not be read ({_reason(output)}); the result is marked dirty because a clean tree "
                     "cannot be shown, and it cannot become a baseline")
        return GitState(sha, True, False, [], notes)
    roots = [prefix + relative for relative in excluded]
    # Only an untracked file under a folder the run keeps is the run's own. A tracked file there is source: editing it is dirt, and an
    # excluded root that happens to be a source folder hides nothing that is tracked.
    changed = [path for xy, path in _entries(output)
               if not (xy == "??" and any(path == root or path.startswith(root + "/") for root in roots))]
    if changed:
        shown = "; ".join(changed[:PATHS_SHOWN])
        more = f", and {len(changed) - PATHS_SHOWN} more" if len(changed) > PATHS_SHOWN else ""
        notes.append(f"the project folder has {len(changed)} uncommitted change(s) ({shown}{more}); the result is marked dirty and "
                     "cannot become a baseline")
    # Without a readable commit there is nothing the tree could be compared with: the result is dirty in the sense that matters
    # (it cannot be tied to a commit), and the note above says so.
    return GitState(sha, bool(changed) or sha is None, True, changed, notes)
