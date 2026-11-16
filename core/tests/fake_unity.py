"""A stand-in for the Unity Editor: the tests start it as `<python> fake_unity.py <Unity arguments>`.

It understands `-logFile` (like Unity) plus a few test switches: `--exit N`, `--sleep SEC`, `--grow` (keep writing to the log while
sleeping), `--text TEXT` (written to the log first), `--dump DIR` (writes argv, the leases it sees and its pid into DIR), `--print`, `--no-log` (never opens the log, like a Unity that fails first),
`--results pass|fail` (writes an NUnit `test-run` file to `-testResults`), `--append FILE` (changes a file while it runs).
(also writes the text to stdout)."""

import json
import os
import sys
import time
from pathlib import Path


def value(name, default=None):
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def main():
    log = value("-logFile")
    text = value("--text", "fake unity started")
    handle = open(log, "a", encoding="utf-8") if log and log != "-" and "--no-log" not in sys.argv else None
    if handle:
        handle.write(text + "\n")
        handle.flush()
    if "--print" in sys.argv:
        print(text, flush=True)
    dump = value("--dump")
    if dump:
        folder = Path(dump)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
        (folder / "pid").write_text(str(os.getpid()), encoding="utf-8")
        progress = os.environ.get("GDV_PROGRESS_FILE", "")
        (folder / "progress-env").write_text(progress, encoding="utf-8")
        (folder / "isolation-env").write_text(os.environ.get("GDV_ISOLATE_WINDOWS", "unset"), encoding="utf-8")
        if progress and "--progress-report" in sys.argv:  # what the Unity package does: report how far the tests have come
            Path(progress).parent.mkdir(parents=True, exist_ok=True)
            Path(progress).write_text(json.dumps({"schema": "gdv.testprogress/1", "state": "running", "total": 10, "done": 4, "passed": 4,
                                                  "failed": 0, "skipped": 0, "current": "A.B.C"}), encoding="utf-8")
            (folder / "progress-seen").write_text(Path(progress).read_text(encoding="utf-8"), encoding="utf-8")
        leases = Path(os.environ.get("FAKE_UNITY_LEASES", ""))
        names = sorted(p.name for p in leases.glob("*.lock")) if leases.is_dir() else []
        (folder / "leases.json").write_text(json.dumps(names), encoding="utf-8")
    append = value("--append")
    if append:  # change an input of the run while it runs
        with open(append, "a", encoding="utf-8") as changed:
            changed.write("// changed while the run ran\n")
    results, outcome = value("-testResults"), value("--results")
    if results and outcome:
        passed = outcome == "pass"
        Path(results).parent.mkdir(parents=True, exist_ok=True)
        Path(results).write_text(
            f'<?xml version="1.0" encoding="utf-8"?>\n<test-run id="2" testcasecount="3" result="{"Passed" if passed else "Failed(Child)"}" '
            f'total="3" passed="{3 if passed else 2}" failed="{0 if passed else 1}" skipped="0">\n</test-run>\n', encoding="utf-8")
    end = time.monotonic() + float(value("--sleep", "0"))
    while time.monotonic() < end:
        if handle and "--grow" in sys.argv:
            handle.write("tick\n")
            handle.flush()
        time.sleep(0.1)
    sys.exit(int(value("--exit", "0")))


if __name__ == "__main__":
    main()
