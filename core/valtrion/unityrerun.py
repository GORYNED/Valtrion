"""`valtrion unity run --rerun-failed RESULTS.xml`: run only the tests that failed in an earlier NUnit 3 result.

`failed_test_names` reads the result file; `test_filter` turns the names into a `-testFilter` value that Unity reads as literal names;
`apply` rewrites the command line of `unity run` (the option is replaced by `-testFilter <value>` after `--`), before the run is
placed, so a run on another host or in the user's session carries the finished filter and never needs the file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

from . import constants

OPTION = "--rerun-failed"
#: Status line and exit code of a rerun that has nothing to run. Not PASS: no test ran. Not one of `constants.STATUSES` (those are
#: results of work that was done), so a caller cannot take it for a pass or for a block.
STATUS_NOTHING_TO_RUN = "NOTHING_TO_RUN"
EXIT_NOTHING_TO_RUN = 4
MAX_RESULT_BYTES = 256 * 1024 * 1024
#: Characters that mean something in a .NET regular expression. Whitespace is escaped by `\s`-safe `\ `; `;` is handled apart.
_REGEX_SPECIAL = frozenset("\\*+?|{}[]()^$.#")


class ResultError(Exception):
    """The file cannot be used as the NUnit result of an earlier run."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _failed(element: ET.Element) -> bool:
    return (element.get("result") or "").startswith("Failed")


def failed_test_names(path: Path) -> list[str]:
    """Full names of the failed tests of an NUnit 3 result file, in file order, without repeats.

    - A `test-case` with `result="Failed"` counts (a failure in SetUp or TearDown marks the case failed).
    - A `test-suite` that failed while none of its test cases failed (a failure in OneTimeSetUp) counts with all test cases under it:
      none of them ran properly.
    - A failed suite with no test case under it cannot be named, so the file is refused (nothing is guessed, nothing runs).
    Raises ResultError for a file that is missing, too big, has a DOCTYPE, is not XML or is not an NUnit 3 `test-run`."""
    try:
        size = path.stat().st_size
        if size > MAX_RESULT_BYTES:
            raise ResultError(f"{path} is {size} bytes, more than the {MAX_RESULT_BYTES} that are read")
        data = path.read_bytes()
    except OSError as error:
        raise ResultError(f"cannot read {path}: {error}") from error
    if b"<!doctype" in data.lower() or b"<!entity" in data.lower():
        raise ResultError(f"{path} contains a DOCTYPE or entity declaration, which is not read")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ResultError(f"{path} is not valid XML: {error}") from error
    if _local(root.tag) != "test-run":
        raise ResultError(f"{path} is not an NUnit 3 result (the root element is <{_local(root.tag)}>, not <test-run>)")
    names: list[str] = []

    def add(name: str | None) -> None:
        if name and name not in names:
            names.append(name)

    def walk(element: ET.Element) -> None:
        for child in element:
            kind = _local(child.tag)
            if kind == "test-case":
                if _failed(child):
                    add(child.get("fullname"))
            elif kind == "test-suite":
                if _failed(child):
                    cases = [item for item in child.iter() if _local(item.tag) == "test-case"]
                    if not cases:
                        raise ResultError(f"the suite {child.get('fullname') or child.get('name')} failed and has no test cases, "
                                          f"so the tests to rerun cannot be named")
                    if not any(_failed(item) for item in cases):
                        for item in cases:
                            add(item.get("fullname"))
                walk(child)

    walk(root)
    return names


def escape_name(name: str) -> str:
    """A test name as a regular expression that matches exactly that name, anchored at both ends. `;` ends a name in `-testFilter`,
    so it is written as `\\x3B`."""
    out = []
    for char in name:
        if char in _REGEX_SPECIAL:
            out.append("\\" + char)
        elif char == ";":
            out.append("\\x3B")
        elif char == " ":
            out.append("\\x20")
        elif char in "\r\n\t":
            out.append({"\r": "\\r", "\n": "\\n", "\t": "\\t"}[char])
        else:
            out.append(char)
    return "^" + "".join(out) + "$"


def test_filter(names: Sequence[str]) -> str:
    """The value of `-testFilter`: the escaped names, separated by `;`."""
    return ";".join(escape_name(name) for name in names)


def _split(raw: Sequence[str]) -> tuple[list[str], list[str], str | None]:
    """(own options before `--` without the rerun option, the Unity arguments after `--`, the result file)."""
    before = list(raw[: raw.index("--")]) if "--" in raw else list(raw)
    after = list(raw[raw.index("--") + 1:]) if "--" in raw else []
    kept: list[str] = []
    result: str | None = None
    position = 0
    while position < len(before):
        item = before[position]
        if item == OPTION and position + 1 < len(before):
            result = before[position + 1]
            position += 2
            continue
        if item.startswith(OPTION + "="):
            result = item[len(OPTION) + 1:]
            position += 1
            continue
        kept.append(item)
        position += 1
    return kept, after, result


def apply(raw_argv: Sequence[str], say, result_line) -> tuple[list[str] | None, int | None]:
    """(new command line, None) to go on with, or (None, exit code) when the command ends here. `say(text)` prints a line,
    `result_line(status, reason)` prints the reason and the final `STATUS` line and returns the exit code of a known status."""
    kept, unity_args, result = _split(raw_argv)
    if result is None:
        return None, result_line(constants.BLOCKED, f"{OPTION} needs the path of the NUnit result file")
    for item in unity_args:
        if item.lower() == "-testfilter":
            return None, result_line(constants.BLOCKED, f"{OPTION} builds -testFilter itself; the Unity arguments already carry -testFilter, "
                                                        f"remove it (the two are not combined)")
    try:
        names = failed_test_names(Path(result).expanduser())
    except ResultError as error:
        return None, result_line(constants.BLOCKED, str(error))
    if not names:
        say(f"{OPTION}: no failed test in {result}; nothing to run, no test was run")
        say(f"STATUS {STATUS_NOTHING_TO_RUN}")
        return None, EXIT_NOTHING_TO_RUN
    say(f"{OPTION}: running {len(names)} failed test(s) from {result}")
    return [*kept, "--", *unity_args, "-testFilter", test_filter(names)], None
