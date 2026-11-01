"""The project folder a job builds in, at the commit the job asked for.

A job names its commit when it is submitted (`--sha`). A queue can hold several jobs on different commits of one project, so a folder
cannot be at every job's commit while they wait. Two kinds of folder exist:

- A **managed checkout** is declared in `host.json` (`projects.<name>.checkouts`). Valtrion owns it: it creates a missing one (a
  clone of the project's `remote`), and when a job's `build` stage starts, under the checkout's `unity-project:` lease, `ensure`
  resets it to the job's commit (detached, `checkout --force`, `clean -fd`, `lfs pull` when the repository uses LFS) and removes
  stray build folders (`Builds/`). Ignored files such as `Library/` stay: they are the import cache. Clients never create or switch
  it by hand.
- Any other folder (`--project-dir`) is never switched. `verify_head` refuses to build when its `HEAD` is not the job's commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import gitstate
from .compare import GitRunner

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
LFS_FILTER = "filter=lfs"
#: Folders of a managed checkout where a build tool leaves its output (`Builds/iOS`, `Builds/iOS-cancelled-1`), below the checkout root
#: and below the Unity project. Only untracked and ignored files in them are removed.
STRAY_BUILD_FOLDERS = ("Builds",)


@dataclass
class CheckoutResult:
    ok: bool
    reason: str | None  # why it is not ok
    sha: str | None  # the commit the folder is at now (None when it could not be read)
    previous: str | None  # the commit before the switch
    switched: bool = False
    fetched: bool = False
    created: bool = False
    folder: str = ""
    removed: tuple[str, ...] = ()  # stray build folders removed

    def describe(self, checkout_id: str | None = None) -> dict:
        """The record kept in `result.json` (`checkout`) and in the events."""
        record: dict = {"folder": self.folder, "sha": self.sha, "switched": self.switched, "fetched": self.fetched}
        if checkout_id:
            record["id"] = checkout_id
        if self.previous:
            record["previousSha"] = self.previous
        if self.created:
            record["created"] = True
        return record


def _head(folder: Path, git: GitRunner) -> str | None:
    code, output = git(folder, ["rev-parse", "HEAD"])
    return output.strip() if code == 0 and _FULL_SHA.fullmatch(output.strip()) else None


def _has_commit(folder: Path, git: GitRunner, sha: str) -> bool:
    code, _ = git(folder, ["cat-file", "-e", f"{sha}^{{commit}}"])
    return code == 0


def verify_head(folder: Path, sha: str, git: GitRunner) -> str | None:
    """None when `folder` is at `sha`; otherwise why it is not (both commits are named)."""
    head = _head(folder, git)
    if head == sha:
        return None
    return (f"the project folder {folder} is at {head[:12] if head else 'an unreadable commit'}, not the job's commit {sha[:12]}; "
            "an unmanaged folder is never switched (declare the project in host.json projects to let valtrion check out commits)")


def escape_pattern(text: str) -> str:
    """`text` as a gitignore pattern that matches only itself."""
    escaped = re.sub(r"([\\\[\]*?!])", r"\\\1", text)
    return "\\" + escaped if escaped.startswith(("#", " ")) else escaped


def _uses_lfs(folder: Path, git: GitRunner) -> bool:
    """Whether a `filter=lfs` attribute applies in the repository: in any tracked `.gitattributes` (the Unity project may be a
    subfolder with its own) or in the repository's `info/attributes`."""
    code, top = git(folder, ["rev-parse", "--show-toplevel"])
    places: list[Path] = []
    if code == 0:
        code, listed = git(folder, ["ls-files", "-z", "--", ":(glob).gitattributes", ":(glob)**/.gitattributes"])
        if code == 0:
            places += [Path(top.strip()) / name for name in listed.split(chr(0)) if name]
    code, info = git(folder, ["rev-parse", "--path-format=absolute", "--git-path", "info/attributes"])
    if code == 0:
        places.append(Path(info.strip()))
    for place in places:
        try:
            if LFS_FILTER in place.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def preflight(folder: Path, sha: str, remote: str | None, git: GitRunner, slow_git: GitRunner | None = None) -> str | None:
    """At submit, before the job waits for any lease: why the commit cannot be built in the managed checkout `folder`, or None. A
    checkout that exists gets the commit fetched when it is missing (objects only: the working tree is not touched, so no lease is
    needed); one that does not exist yet needs a `remote` that answers."""
    slow = slow_git or git
    if (folder / ".git").exists():
        if _has_commit(folder, git, sha):
            return None
        code, output = slow(folder, ["fetch", "--quiet", "origin", sha])
        if code != 0 or not _has_commit(folder, git, sha):
            code, output = slow(folder, ["fetch", "--quiet", "origin"])
            if code != 0:
                return f"the remote of {folder} cannot be reached to fetch {sha[:12]}: {' '.join(output.split())[:200]}"
            if not _has_commit(folder, git, sha):
                return f"the commit {sha[:12]} is not on the remote of {folder}"
        return None
    if not remote:
        return f"{folder} does not exist and the project declares no remote to create it from"
    code, output = git(folder.parent if folder.parent.is_dir() else Path.cwd(), ["ls-remote", "--exit-code", "--heads", remote])
    if code != 0:
        return f"the remote {remote} cannot be reached to create {folder}: {' '.join(output.split())[:200]}"
    return None


def ensure(folder: Path, sha: str, git: GitRunner, *, remote: str | None = None, keep: Sequence[Path] = (),
           slow_git: GitRunner | None = None, cancelled=lambda: False, unity_path: str = ".") -> CheckoutResult:
    """Make the managed checkout `folder` be at `sha`, clean (see the module text). `keep` are folders the run itself keeps inside it
    (artifacts, work root): `clean` leaves them. The caller holds the checkout's `unity-project:` lease. `slow_git` runs the calls
    that can take long (clone, fetch, checkout, clean, lfs); `cancelled` is looked at between them."""
    slow = slow_git or git
    here = str(folder)
    if not _FULL_SHA.fullmatch(sha):
        return CheckoutResult(False, f"{sha!r} is not a full commit hash", None, None, folder=here)
    created = False
    if not (folder / ".git").exists():
        if folder.exists() and any(folder.iterdir()):
            return CheckoutResult(False, f"{folder} exists, is not empty and is not a git checkout", None, None, folder=here)
        if not remote:
            return CheckoutResult(False, f"{folder} does not exist and the project declares no remote to create it from", None, None,
                                  folder=here)
        try:
            folder.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return CheckoutResult(False, f"cannot create {folder.parent}: {error}", None, None, folder=here)
        code, output = slow(folder.parent, ["clone", "--quiet", "--no-checkout", "--", remote, str(folder)])
        if code != 0:
            return CheckoutResult(False, f"cannot clone {remote} into {folder}: {' '.join(output.split())[:200]}", None, None, folder=here)
        created = True
    elif remote:
        code, origin = git(folder, ["remote", "get-url", "origin"])
        if code == 0 and origin.strip() != remote:
            return CheckoutResult(False, f"{folder} has origin {origin.strip()}, not the {remote} that host.json declares for it; valtrion "
                                  "resets managed checkouts, so it will not touch a repository that may be someone's own", None, None,
                                  folder=here)
    if cancelled():
        return CheckoutResult(False, "cancelled", None, None, folder=here, created=created)
    previous = _head(folder, git)
    fetched = False
    if not _has_commit(folder, git, sha):
        code, output = slow(folder, ["fetch", "--quiet", "origin", sha])
        if code != 0 or not _has_commit(folder, git, sha):
            code, output = slow(folder, ["fetch", "--quiet", "origin"])  # a server that does not serve a commit by hash
            if code != 0 or not _has_commit(folder, git, sha):
                text = " ".join(output.split())[:200] if code != 0 else "it is not on the remote"
                return CheckoutResult(False, f"the commit {sha[:12]} is not in the project repository and could not be fetched ({text})",
                                      previous, previous, folder=here, created=created)
        fetched = True
    if cancelled():
        return CheckoutResult(False, "cancelled", previous, previous, fetched=fetched, folder=here, created=created)
    code, output = slow(folder, ["checkout", "--quiet", "--force", "--detach", sha])
    if code != 0:
        return CheckoutResult(False, f"git checkout {sha[:12]} failed: {' '.join(output.split())[:200]}", previous, _head(folder, git),
                              fetched=fetched, folder=here, created=created)
    clean = ["clean", "-ffd"]  # also nested repositories left by a tool
    code, output = git(folder, ["rev-parse", "--show-prefix"])
    prefix = output.strip() if code == 0 else ""
    for path in keep:
        for relative in gitstate._inside(folder, Path(path)):  # noqa: SLF001 - the same spelling rules as the dirty check
            clean += ["-e", "/" + escape_pattern(prefix + relative)]
    code, output = slow(folder, clean)
    if code != 0:
        return CheckoutResult(False, f"git clean failed after the checkout: {' '.join(output.split())[:200]}", previous, _head(folder, git),
                              switched=True, fetched=fetched, folder=here, created=created)
    stray = [name for name in STRAY_BUILD_FOLDERS]
    if unity_path not in ("", "."):
        stray += [f"{unity_path.strip('/')}/{name}" for name in STRAY_BUILD_FOLDERS]
    # Untracked and ignored files only (`-x`): a tracked file under Builds/ is source. The folders the run keeps stay.
    code, output = slow(folder, ["clean", "-ffdx", *clean[2:], "--", *stray])
    removed = [line[len("Removing "):].strip() for line in output.splitlines() if line.startswith("Removing ")] if code == 0 else []
    if _uses_lfs(folder, git):
        if cancelled():
            return CheckoutResult(False, "cancelled", previous, _head(folder, git), switched=True, fetched=fetched, folder=here,
                                  created=created)
        code, output = slow(folder, ["lfs", "pull"])
        if code != 0:
            return CheckoutResult(False, f"git lfs pull failed after the checkout: {' '.join(output.split())[:200]}", previous,
                                  _head(folder, git), switched=True, fetched=fetched, folder=here, created=created)
    now = _head(folder, git)
    if now != sha:
        return CheckoutResult(False, f"after the checkout the folder is at {now[:12] if now else 'an unreadable commit'}, not {sha[:12]}",
                              previous, now, switched=True, fetched=fetched, folder=here, created=created)
    return CheckoutResult(True, None, now, previous, switched=previous != now, fetched=fetched, created=created, folder=here,
                          removed=tuple(removed))
