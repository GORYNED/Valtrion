"""`valtrion result write`: a `gdv.result/1` for a run that Valtrion did not make itself.

A project often runs tests or a script with its own runner and decides PASS or FAIL itself (skip policy, XML inspection). This command
records that decision so `valtrion report` can show it: the caller names what it ran, gives the status and Valtrion never interprets it.
The document is the ordinary `gdv.result/1`, with `plan.id` equal to the name and, because no plan file exists, `plan.sha256` equal to
the SHA-256 of the name's UTF-8 text. The commit and the dirty flag are read from git, never taken from the caller.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from . import compare, constants, core
from .gitstate import read_state
from .schema import UnreadableInput, Validator, validate_document

#: Platforms the schema knows (common.schema.json `platform`).
PLATFORMS = ("editor", "windows", "macos", "ios", "android")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
#: No device is named in these results, so a claim of physical or emulator evidence cannot be true.
EVIDENCE = (constants.EVIDENCE_EDITOR, constants.EVIDENCE_HOST)


class WriteRefused(Exception):
    """The inputs cannot make a valid, honest result; the message says why."""


def repository_root(start: Path) -> Path:
    code, output = compare.run_git(start, ["rev-parse", "--show-toplevel"])
    if code != 0:
        raise UnreadableInput(f"{start} is not inside a git repository: {output}")
    return Path(output.strip())


def run_id(key: str, sha: str, host_id: str, platform: str, evidence: str, dirty: bool) -> str:
    """Deterministic: the same check, commit, host, platform, evidence and tree state always give the same id (and file), so writing
    a result again replaces it, while a result from a dirty tree never replaces a clean one."""
    tag = core.sha256_bytes("\0".join((key, host_id, platform, evidence, "dirty" if dirty else "clean")).encode("utf-8"))[:10]
    return core.safe_identifier(f"{core.safe_identifier(key, limit=60)}-{sha[:12]}-{core.safe_identifier(host_id, limit=40)}-{tag}", limit=200)


def _pairs(items: Sequence[str], what: str) -> list[tuple[str, str]]:
    pairs = []
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name or not value:
            raise WriteRefused(f"{what} {item!r} must be NAME=VALUE")
        pairs.append((name, value))
    return pairs


def build_document(
        key: str, status: str, reason: str | None, sha: str, dirty: bool, repo: str, profile: str, unity: str, platform: str, evidence: str,
        host_id: str, started: str, finished: str, artifacts: Sequence[dict[str, str]] = (), counts: Sequence[tuple[str, int]] = (),
        unverified: Sequence[str] = ()) -> dict[str, Any]:
    metrics = [{"name": name, "unit": "count", "samples": [count], "stats": compare.compute_stats([count])} for name, count in counts]
    return {
        "schema": "gdv.result/1", "runId": run_id(key, sha, host_id, platform, evidence, dirty), "status": status,
        "reason": None if status == constants.PASS else reason,
        "candidate": {"repo": repo, "sha": sha, "profile": profile, "unity": unity, "dirty": dirty},
        "target": {"platform": platform, "deviceClass": "none", "osMajor": 0, "evidence": evidence},
        "host": {"id": host_id, "transport": "local", "selectedBy": "local"},
        "plan": {"id": key, "sha256": core.sha256_bytes(key.encode("utf-8"))},
        "startedUtc": started, "finishedUtc": finished, "layers": {}, "steps": [], "metrics": metrics,
        "artifacts": list(artifacts), "unverified": list(unverified),
    }


def write_result(arguments: argparse.Namespace) -> Path:
    key = arguments.name
    if not _NAME.fullmatch(key) or len(key) > 200:
        raise WriteRefused(f"name {key!r} is not a valid identifier (letters, digits and ._:-)")
    if arguments.status != constants.PASS and not (arguments.reason or "").strip():
        raise WriteRefused(f"a {arguments.status} result needs --reason")
    if arguments.status == constants.PASS and arguments.reason:
        raise WriteRefused("a PASS result has no reason")
    platform = arguments.platform
    evidence = arguments.evidence or (constants.EVIDENCE_EDITOR if platform == "editor" else constants.EVIDENCE_HOST)

    folder = Path(arguments.repo_dir).resolve() if arguments.repo_dir else Path.cwd()
    root = repository_root(folder)
    out = Path(arguments.out).resolve()
    state = read_state(root, compare.run_git, [out])
    if state.sha is None:
        raise WriteRefused("; ".join(state.notes) or "the commit could not be read")
    if arguments.sha and arguments.sha != state.sha:
        raise WriteRefused(f"--sha {arguments.sha} is not HEAD ({state.sha}); a result describes the checked-out commit")
    if arguments.sha and not _HEX40.fullmatch(arguments.sha):
        raise WriteRefused("--sha must be the full 40-character commit")
    dirty = state.dirty or arguments.dirty

    now = core.utc_now()
    started, finished = arguments.started_utc or arguments.finished_utc or now, arguments.finished_utc or now
    try:
        if core.parse_utc(started) > core.parse_utc(finished):
            raise WriteRefused("--started-utc is after --finished-utc")
    except ValueError as error:
        raise WriteRefused(str(error)) from error

    artifacts = []
    for kind, text in _pairs(arguments.artifact, "--artifact"):
        path = Path(text)
        if not path.is_file():
            raise WriteRefused(f"--artifact {kind}={text}: not a file")
        artifacts.append({"kind": kind, "path": text, "sha256": core.sha256_file(path)})
    counts = []
    for name, text in _pairs(arguments.count, "--count"):
        if not _NAME.fullmatch(name) or not text.isdigit():
            raise WriteRefused(f"--count {name}={text}: a count is NAME=NON-NEGATIVE-INTEGER")
        counts.append((name, int(text)))

    document = build_document(
        key, arguments.status, arguments.reason.strip() if arguments.reason else None, state.sha, dirty, arguments.repo or root.name,
        arguments.profile, arguments.unity, platform, evidence, arguments.host_id or core.local_host_id(), started, finished,
        artifacts, counts, [*arguments.unverified, *(state.notes if state.dirty else (["marked dirty by --dirty"] if dirty else []))])
    _, errors = validate_document(document, Validator())
    if errors:
        raise WriteRefused("the result violates gdv.result/1: " + "; ".join(errors[:5]))
    destination = out / (core.safe_segment(document["runId"], limit=200) + ".result.json")
    core.atomic_write_json(destination, document)
    return destination


def _configure(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    write = sub.add_parser("write", help="write a gdv.result/1 for a run Valtrion did not make itself")
    write.add_argument("name", help="what was run, as the agent names it (letters, digits and ._:-), for example `editmode.Game.Tests`")
    write.add_argument("--status", required=True, choices=constants.STATUSES, help="decided by the caller; Valtrion does not interpret skip policies")
    write.add_argument("--reason", help="required for every status but PASS, forbidden for PASS")
    write.add_argument("--out", type=Path, required=True, help="the folder to write into; `valtrion report --results` reads it")
    write.add_argument("--evidence", choices=EVIDENCE, help="default: editor on platform editor, host otherwise; physical and emulator are only for results of a device run")
    write.add_argument("--platform", choices=PLATFORMS, default="editor", help="where it ran (default: editor)")
    write.add_argument("--repo-dir", type=Path, help="a folder of the repository whose HEAD was checked (default: the current directory)")
    write.add_argument("--sha", help="the full commit; must equal HEAD (default: HEAD)")
    write.add_argument("--dirty", action="store_true", help="mark the result dirty even if git shows a clean tree (never the other way round)")
    write.add_argument("--repo", help="candidate.repo (default: the name of the repository folder)")
    write.add_argument("--profile", default="none", help="candidate.profile (default: none)")
    write.add_argument("--unity", default="n/a", help="candidate.unity, the Editor version if known (default: n/a)")
    write.add_argument("--host-id", help="host.id (default: this machine's host id)")
    write.add_argument("--started-utc", help="UTC time, 2026-01-02T03:04:05Z (default: now)")
    write.add_argument("--finished-utc", help="UTC time (default: now)")
    write.add_argument("--artifact", action="append", default=[], metavar="KIND=FILE", help="a file to list with its SHA-256 (repeatable)")
    write.add_argument("--count", action="append", default=[], metavar="NAME=N", help="a number to record, such as passed=120 (repeatable)")
    write.add_argument("--unverified", action="append", default=[], metavar="TEXT", help="something the run did not cover (repeatable)")


def _result(arguments: argparse.Namespace) -> int:
    try:
        destination = write_result(arguments)
    except WriteRefused as error:
        print(f"valtrion result write: {error}", file=sys.stderr)
        return constants.EXIT_FAIL
    except UnreadableInput as error:
        print(f"valtrion result write: BLOCKED: {error}", file=sys.stderr)
        return constants.EXIT_BLOCKED
    print(destination)
    return constants.EXIT_PASS


from .cli import Command, register  # noqa: E402

register(Command("result", "write a gdv.result/1 for a run the project made itself", "results", _result, _configure))
