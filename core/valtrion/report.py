"""`valtrion report`: the `Local verification` block of a pull request, built from the selected checks and the collected results.

A selected check without a result is listed as not run, never as PASS. A result counts only for the candidate it was produced on and,
for a device plan, only with the evidence the plan requires.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import constants
from .schema import UnreadableInput, parse_json, validate_document
from .select import (
    DEVICE,
    LEVELS,
    PLAYER,
    required_evidence,
    Check,
    Selection,
    _add_common,
    _git,
    repository_root,
    resolve_selection,
    run_guarded,
)

NOT_RUN = "NOT_RUN"
NOT_VERIFIED = "NOT_VERIFIED"

#: Worst first. The exit code of `report` is the code of the worst row.
_SEVERITY = (
    constants.FAIL,
    constants.TIMEOUT,
    constants.STALL,
    constants.NEEDS_PERMISSION,
    constants.BLOCKED,
    NOT_VERIFIED,
    NOT_RUN,
    constants.PASS,
)
_COUNT_ORDER = (constants.PASS, constants.FAIL, constants.BLOCKED, constants.NEEDS_PERMISSION, constants.STALL, constants.TIMEOUT, NOT_VERIFIED, NOT_RUN)


@dataclass
class Row:
    key: str
    type: str
    platform: str | None
    status: str
    evidence: str | None = None
    host: str | None = None
    detail: str = ""
    run_id: str | None = None
    unverified: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key, "type": self.type, "platform": self.platform, "status": self.status, "evidence": self.evidence,
            "host": self.host, "detail": self.detail, "runId": self.run_id, "unverified": self.unverified,
        }


def load_results(directory: Path) -> tuple[list[dict], list[str]]:
    """Every valid `gdv.result/1` document below `directory` (sorted by path); files that are not results are ignored, broken results listed."""
    directory = Path(directory)
    if not directory.is_dir():
        raise UnreadableInput(f"results folder not found: {directory}")
    results: list[dict] = []
    problems: list[str] = []
    for path in sorted(directory.rglob("*.json")):
        try:
            document = parse_json(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
            problems.append(f"{path}: unreadable ({error})")
            continue
        if not isinstance(document, dict):
            continue
        schema = document.get("schema")
        if isinstance(schema, str) and schema.startswith("gdv.result/") and schema != "gdv.result/1":
            problems.append(f"{path}: unsupported result schema {schema}")
            continue
        if schema != "gdv.result/1":
            continue
        _, errors = validate_document(document)
        if errors:
            problems.append(f"{path}: invalid result: {errors[0]}")
            continue
        try:
            _utc(document["finishedUtc"])
        except ValueError:
            problems.append(f"{path}: invalid result: finishedUtc is not a real date")
            continue
        results.append(document)
    return results, problems


def _utc(text: str) -> datetime:
    """Parse the schema's UTC format, including a fraction of any length."""
    body = text[:-1]
    main, _, fraction = body.partition(".")
    value = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return value.replace(microsecond=int((fraction + "000000")[:6])) if fraction else value


def _latest(candidates: list[dict]) -> dict:
    """The newest result; among equal times the worst status, so a failure is never hidden by a tie."""
    return max(candidates, key=lambda result: (_utc(result["finishedUtc"]), -_SEVERITY.index(result["status"]), result["runId"]))


def _row(check: Check, platform: str | None, results: list[dict], sha: str | None) -> Row:
    matching = [r for r in results if r["plan"]["id"] == check.key and (platform is None or r["target"]["platform"] == platform)]
    current = [r for r in matching if sha is None or r["candidate"]["sha"] == sha]
    if not current:
        detail = "no result"
        if matching:
            detail = "only results for another revision: " + ", ".join(sorted({r["candidate"]["sha"][:8] for r in matching}))
        return Row(check.key, check.type, platform, NOT_RUN, detail=detail)
    if platform is None:
        # No platform in the plan: the newest result of every platform and host counts, and the worst of them is shown.
        groups: dict[tuple[str, str], list[dict]] = {}
        for result in current:
            groups.setdefault((result["target"]["platform"], result["host"]["id"]), []).append(result)
        rows = [_result_row(check, None, _latest(group)) for group in groups.values()]
        return min(rows, key=lambda row: (_SEVERITY.index(row.status), row.platform or "", row.host or ""))
    return _result_row(check, platform, _latest(current))


def _result_row(check: Check, platform: str | None, result: dict) -> Row:
    status = result["status"]
    detail = result["reason"] or ""
    required = required_evidence(check.plan) if check.plan is not None else ()
    evidence = result["target"]["evidence"]
    if status == constants.PASS and required and evidence not in required:
        status = NOT_VERIFIED
        detail = f"{evidence} evidence does not prove it; the plan requires {'/'.join(required)}"
    if result["candidate"]["dirty"]:
        # A run from a dirty tree proves nothing about the commit.
        if status == constants.PASS:
            status = NOT_VERIFIED
        detail = (detail + "; " if detail else "") + "produced from a dirty tree"
    sent_from = (result["host"].get("tools") or {}).get("offloadFrom")
    if sent_from:
        detail = (detail + "; " if detail else "") + f"ran on {result['host']['id']}, sent from {sent_from} (offloaded)"
    reused = result.get("reusedFrom")
    if isinstance(reused, dict) and reused.get("runId"):
        # A result answered from the result cache: the run that produced the evidence is named, not this job.
        detail = (detail + "; " if detail else "") + f"reused from run {reused['runId']}"
    row_platform = platform if platform is not None else result["target"]["platform"]
    return Row(check.key, check.type, row_platform, status, evidence, result["host"]["id"], detail, result["runId"], list(result["unverified"]))


def build_rows(selection: Selection, results: list[dict], sha: str | None) -> list[Row]:
    rows: list[Row] = []
    for check in selection.checks:
        platforms: list[str | None] = list(check.plan.platforms) if check.plan is not None and check.plan.platforms else [None]
        rows.extend(_row(check, platform, results, sha) for platform in platforms)
    return rows


def exit_code(rows: list[Row]) -> int:
    if not rows:
        return constants.EXIT_PASS
    worst = min(rows, key=lambda row: _SEVERITY.index(row.status)).status
    if worst in (NOT_RUN, NOT_VERIFIED):
        return constants.EXIT_BLOCKED
    return constants.exit_code_for_status(worst)


def not_verified_lines(rows: list[Row], selection: Selection | None = None) -> list[str]:
    lines = []
    if selection is not None and any(check.type != "l0" for check in selection.checks):
        # Unity-backed work was selected: a platform the project requires is verified only by a passing device or player row.
        proven = {
            row.platform
            for row in rows
            if row.status == constants.PASS and (row.type == DEVICE or (row.type == PLAYER and row.evidence in ("physical", "emulator")))
        }
        for platform in selection.config.data["platforms"]["required"]:
            if platform not in proven:
                lines.append(f"not verified on {platform}: no passing device or player check for the required platform")
    for row in rows:
        if row.status == constants.PASS:
            continue
        subject = f"on {row.platform}" if row.platform else "(no platform)"
        lines.append(f"not verified {subject}: {row.key}, {row.status}" + (f": {row.detail}" if row.detail else ""))
    return lines


def _flat(text: str) -> str:
    """One line: result text is data and must not add lines to the pull request block."""
    return " ".join(text.split())


def _cell(text: str | None) -> str:
    return _flat(text or "-").replace("|", "\\|")


def format_markdown(selection: Selection, rows: list[Row], sha: str | None, problems: list[str], heading: bool) -> str:
    counts = {status: sum(1 for row in rows if row.status == status) for status in _COUNT_ORDER}
    summary = ", ".join(f"{count} {status}" for status, count in counts.items() if count) or "nothing selected"
    lines = ["## Local verification", ""] if heading else []
    lines.append(
        f"Level {selection.level}, " + (f"base `{selection.base_ref}`" if selection.base_ref else "no diff")
        + (", named kinds " + ", ".join(f"`{name}`" for name in selection.named_kinds) if selection.named_kinds else "")
        + (f" (merge base `{selection.merge_base[:8]}`)" if selection.merge_base else "")
        + (f", candidate `{sha[:8]}`" if sha else "")
        + f", {len(selection.paths)} changed path(s). {len(rows)} check row(s): {summary}."
    )
    if selection.unclassified:
        lines.append("")
        lines.append(f"Unclassified paths ({selection.policy}): " + ", ".join(f"`{path}`" for path in selection.unclassified))
    if rows:
        lines += ["", "| Check | Platform | Status | Evidence | Host | Detail |", "|---|---|---|---|---|---|"]
        lines += [f"| `{_cell(row.key)}` | {_cell(row.platform)} | {row.status} | {_cell(row.evidence)} | {_cell(row.host)} | {_cell(row.detail)} |" for row in rows]
    missing = not_verified_lines(rows, selection)
    if missing:
        lines += ["", "Not verified:"] + [f"- {_flat(line)}" for line in missing]
    reported = [(row.key, item) for row in rows for item in row.unverified]
    if reported:
        lines += ["", "Reported by the runs as not covered:"] + [f"- {_flat(key)}: {_flat(item)}" for key, item in reported]
    if problems:
        lines += ["", "Ignored result files:"] + [f"- {_flat(problem)}" for problem in problems]
    if not selection.paths and not selection.named_kinds:
        lines += ["", "No changed paths: check that the base ref is the intended one."]
    return "\n".join(lines)


def _configure_report(parser: argparse.ArgumentParser) -> None:
    _add_common(parser)
    parser.add_argument("--results", type=Path, required=True, help="a folder searched recursively for gdv.result/1 files")
    parser.add_argument("--level", choices=LEVELS, default="L4", help="highest level the report covers (default: L4)")
    parser.add_argument("--pr", action="store_true", help="print the block with its `## Local verification` heading, ready for a pull request description")
    parser.add_argument("--json", action="store_true", help="print rows and counts as JSON")
    parser.add_argument("--sha", help="the candidate revision results must belong to (default: HEAD)")


def _report(arguments: argparse.Namespace) -> int:
    def action() -> int:
        selection = resolve_selection(arguments)
        if selection.failed:
            print("valtrion report: unclassified paths with policy fail; fix the area map first: " + ", ".join(selection.unclassified), file=sys.stderr)
            return constants.EXIT_FAIL
        root = repository_root(Path.cwd() if arguments.checks is None else Path(arguments.checks).resolve().parent)
        head = _git(root, "rev-parse", "HEAD").decode("utf-8", "replace").strip()
        sha = arguments.sha or head
        if sha != head:
            print(f"valtrion report: --sha {sha} is not HEAD ({head}); the selection is computed from HEAD", file=sys.stderr)
            return constants.EXIT_FAIL
        results, problems = load_results(arguments.results)
        rows = build_rows(selection, results, sha)
        if arguments.include_worktree and arguments.base_ref and _git(root, "status", "--porcelain").strip():
            # Results belong to a commit; the uncommitted edits they count as changed were never tested.
            for row in rows:
                if row.status == constants.PASS:
                    row.status = NOT_VERIFIED
                    row.detail = (row.detail + "; " if row.detail else "") + "worktree has uncommitted changes"
        if arguments.json:
            counts = {status: sum(1 for row in rows if row.status == status) for status in _COUNT_ORDER}
            print(json.dumps({"level": selection.level, "selectedKinds": selection.named_kinds, "kindsNamedExplicitly": bool(selection.named_kinds),
                              "candidate": sha, "counts": counts, "rows": [row.to_json() for row in rows],
                              "notVerified": not_verified_lines(rows, selection), "ignoredResults": problems}, indent=2))
        else:
            print(format_markdown(selection, rows, sha, problems, arguments.pr))
        code = exit_code(rows)
        if code == constants.EXIT_PASS and any(line.startswith("not verified on ") for line in not_verified_lines(rows, selection)):
            code = constants.EXIT_BLOCKED  # a required platform nothing proved is not a clean report for a machine consumer either
        # An unreadable or invalid result file may hold the newest outcome: the report cannot be trusted as clean.
        return max(code, constants.EXIT_BLOCKED) if problems and code == constants.EXIT_PASS else code

    return run_guarded("report", action)


from .cli import Command, register  # noqa: E402  (cli imports this module last; importing it here keeps both import orders working)

register(Command("report", "build the pull request Local verification block from selected checks and results", "check selection", _report, _configure_report))
