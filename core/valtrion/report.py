"""`valtrion report`: what the runs in a results folder say, as a table: passed, failed, skipped, the files they left and how to re-run
the ones that did not pass.

The folder holds `gdv.result/1` documents (written by `valtrion run`, `pipeline` and `build`, or by `valtrion result write`). For each
name (`plan.id`), platform and host the newest result counts. Valtrion does not know what should have run: a name without a result
does not appear.
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

#: Worst first. The exit code of `report` is the code of the worst row.
_SEVERITY = (
    constants.FAIL,
    constants.TIMEOUT,
    constants.STALL,
    constants.NEEDS_PERMISSION,
    constants.BLOCKED,
    constants.PASS,
)
_COUNT_ORDER = (constants.PASS, constants.FAIL, constants.BLOCKED, constants.NEEDS_PERMISSION, constants.STALL, constants.TIMEOUT)
#: Metric names of a result that are shown as counts of tests (`valtrion result write --count passed=12`).
_TEST_COUNTS = ("passed", "failed", "skipped")


@dataclass
class Row:
    name: str
    platform: str
    status: str
    evidence: str
    host: str
    sha: str
    dirty: bool
    detail: str = ""
    run_id: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    artifacts: list[dict[str, str]] = field(default_factory=list)
    rerun: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "platform": self.platform, "status": self.status, "evidence": self.evidence, "host": self.host,
            "sha": self.sha, "dirty": self.dirty, "detail": self.detail, "runId": self.run_id, "counts": self.counts,
            "artifacts": self.artifacts, "rerun": self.rerun, "unverified": self.unverified,
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


def _counts(result: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for metric in result["metrics"]:
        if metric["name"] in _TEST_COUNTS and metric.get("unit") == "count" and metric["samples"]:
            counts[metric["name"]] = int(metric["samples"][0])
    return counts


def _rerun_hints(result: dict) -> list[str]:
    """How to run again what did not pass: failed tests from their NUnit XML, failed stages of a pipeline."""
    if result["status"] == constants.PASS:
        return []
    hints = [f"valtrion unity run --rerun-failed {item['path']} -- <the same Unity arguments>" for item in result["artifacts"] if item["kind"] == "xml"]
    run = result.get("pipelineRun")
    if isinstance(run, dict):
        hints.append(f"valtrion pipeline FILE --only-failed --resume-from {result['runId']}   (pipeline {run['pipelineId']})")
    return hints


def _row(result: dict) -> Row:
    detail = result["reason"] or ""
    if result["candidate"]["dirty"]:
        detail = (detail + "; " if detail else "") + "produced from a dirty tree"
    sent_from = (result["host"].get("tools") or {}).get("offloadFrom")
    if sent_from:
        detail = (detail + "; " if detail else "") + f"ran on {result['host']['id']}, sent from {sent_from} (offloaded)"
    reused = result.get("reusedFrom")
    if isinstance(reused, dict) and reused.get("runId"):
        # A result answered from the result cache: the run that produced the evidence is named, not this job.
        detail = (detail + "; " if detail else "") + f"reused from run {reused['runId']}"
    return Row(result["plan"]["id"], result["target"]["platform"], result["status"], result["target"]["evidence"], result["host"]["id"],
               result["candidate"]["sha"], bool(result["candidate"]["dirty"]), detail, result["runId"], _counts(result),
               [{"kind": item["kind"], "path": item["path"]} for item in result["artifacts"]], _rerun_hints(result), list(result["unverified"]))


def build_rows(results: list[dict], sha: str | None = None) -> list[Row]:
    """One row per name, platform and host: the newest result (of the commit `sha` when given), sorted by name."""
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for result in results:
        if sha is None or result["candidate"]["sha"] == sha:
            groups.setdefault((result["plan"]["id"], result["target"]["platform"], result["host"]["id"]), []).append(result)
    return [_row(_latest(group)) for _, group in sorted(groups.items())]


def exit_code(rows: list[Row]) -> int:
    if not rows:
        return constants.EXIT_BLOCKED  # nothing to report on
    return constants.exit_code_for_status(min(rows, key=lambda row: _SEVERITY.index(row.status)).status)


def _flat(text: str) -> str:
    """One line: result text is data and must not add lines to the block."""
    return " ".join(text.split())


def _cell(text: str | None) -> str:
    return _flat(text or "-").replace("|", "\\|")


def _count_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{counts[name]} {name}" for name in _TEST_COUNTS if name in counts)


def format_markdown(rows: list[Row], problems: list[str], heading: bool) -> str:
    tally = {status: sum(1 for row in rows if row.status == status) for status in _COUNT_ORDER}
    summary = ", ".join(f"{count} {status}" for status, count in tally.items() if count) or "no results"
    shas = sorted({row.sha[:8] for row in rows})
    lines = ["## Run results", ""] if heading else []
    lines.append(f"{len(rows)} result(s): {summary}." + (f" Candidate {', '.join(f'`{item}`' for item in shas)}." if shas else ""))
    if rows:
        lines += ["", "| Name | Platform | Status | Tests | Host | Detail |", "|---|---|---|---|---|---|"]
        lines += [f"| `{_cell(row.name)}` | {_cell(row.platform)} | {row.status} | {_cell(_count_text(row.counts))} | {_cell(row.host)} | {_cell(row.detail)} |"
                  for row in rows]
    files = [(row, item) for row in rows if row.status != constants.PASS for item in row.artifacts]
    if files:
        lines += ["", "Files of runs that did not pass:"] + [f"- `{_flat(row.name)}` ({_flat(row.platform)}) {_flat(item['kind'])}: {_flat(item['path'])}" for row, item in files]
    again = [(row, hint) for row in rows for hint in row.rerun]
    if again:
        lines += ["", "Run again what did not pass:"] + [f"- `{_flat(row.name)}`: {_flat(hint)}" for row, hint in again]
    reported = [(row.name, item) for row in rows for item in row.unverified]
    if reported:
        lines += ["", "Reported by the runs as not covered:"] + [f"- {_flat(name)}: {_flat(item)}" for name, item in reported]
    if problems:
        lines += ["", "Ignored result files:"] + [f"- {_flat(problem)}" for problem in problems]
    return "\n".join(lines)


def _configure_report(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--results", type=Path, required=True, help="a folder searched recursively for gdv.result/1 files")
    parser.add_argument("--sha", help="show only results of this commit (default: the newest result of every name, platform and host)")
    parser.add_argument("--pr", action="store_true", help="print the block with a heading, ready for a pull request description")
    parser.add_argument("--json", action="store_true", help="print rows and counts as JSON")


def _report(arguments: argparse.Namespace) -> int:
    try:
        results, problems = load_results(arguments.results)
    except UnreadableInput as error:
        print(f"valtrion report: BLOCKED: {error}", file=sys.stderr)
        return constants.EXIT_BLOCKED
    rows = build_rows(results, arguments.sha)
    if arguments.json:
        counts = {status: sum(1 for row in rows if row.status == status) for status in _COUNT_ORDER}
        print(json.dumps({"candidate": arguments.sha, "counts": counts, "rows": [row.to_json() for row in rows], "ignoredResults": problems}, indent=2))
    else:
        print(format_markdown(rows, problems, arguments.pr))
    code = exit_code(rows)
    # An unreadable or invalid result file may hold the newest outcome: the report cannot be trusted as clean.
    return max(code, constants.EXIT_BLOCKED) if problems and code == constants.EXIT_PASS else code


from .cli import Command, register  # noqa: E402  (cli imports this module last; importing it here keeps both import orders working)

register(Command("report", "show the results of runs in a folder: passed, failed, files, how to run the failed ones again", "results", _report, _configure_report))
