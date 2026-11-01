"""Run data that a failed or interrupted run keeps as diagnostics.

User data and test isolation: a passing run deletes its data; a failed or interrupted run keeps it with a small
manifest (what failed, when, which holder), and removing it follows long-running-checks "Clean up after a run": the task that
started the run removes it after review, never another run. An adapter therefore moves the folder aside under a name no later run
clears (`<name>.retained-<utc>`) and writes `retained.json` into it, so the next run starts from an empty folder and the
diagnostics stay until their owner removes them.
"""

from __future__ import annotations

import getpass
import os
import socket
from pathlib import Path

from . import core

MANIFEST_NAME = "retained.json"
SCHEMA = "gdv.retained/1"
SUFFIX = ".retained-"


def holder() -> dict:
    """Who holds the retained data: the user, machine and process of the run that kept it."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is not a reason to lose the diagnostics
        user = "unknown"
    return {"user": user, "machine": socket.gethostname(), "processId": os.getpid()}


def has_content(folder: Path) -> bool:
    """Whether a folder holds anything worth keeping; an empty folder is just removed by the caller."""
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return True  # unreadable: keep rather than delete


def retain_folder(folder: Path, status: str, reason: str | None) -> Path:
    """Write the manifest into `folder` and move it aside to `<name>.retained-<utc>`; returns the new path.

    A manifest that is already there (an earlier attempt wrote it and the move failed) keeps saying what failed first; the new
    one is written next to it as `retained.<utc>.json`. Raises OSError when the folder cannot be moved: the manifest is written
    first, so the data is identified either way."""
    stamp = core.utc_now().replace("-", "").replace(":", "").replace(".", "")
    name = MANIFEST_NAME if not (folder / MANIFEST_NAME).exists() else f"retained.{stamp}.json"
    core.atomic_write_json(folder / name, {
        "schema": SCHEMA, "status": status, "reason": reason or f"the run ended {status}", "retainedUtc": core.utc_now(),
        "holder": holder(), "removal": "the task that started the run removes this folder after review"})
    target = folder.with_name(folder.name + SUFFIX + stamp)
    counter = 1
    while target.exists():  # two keeps in the same millisecond
        counter += 1
        target = folder.with_name(f"{folder.name}{SUFFIX}{stamp}-{counter}")
    os.rename(folder, target)
    return target


def is_retained(path: Path) -> bool:
    """Whether a folder name marks retained diagnostics; such a folder is never cleared by an adapter."""
    return SUFFIX in path.name
