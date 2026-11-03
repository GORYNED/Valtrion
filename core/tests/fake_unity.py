"""A stand-in for the Unity Editor: the tests start it as `<python> fake_unity.py <Unity arguments>`.

It understands `-logFile` (like Unity) plus a few test switches: `--exit N`, `--sleep SEC`, `--grow` (keep writing to the log while
sleeping), `--text TEXT` (written to the log first), `--dump DIR` (writes argv, the leases it sees and its pid into DIR), `--print`, `--no-log` (never opens the log, like a Unity that fails first).
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
        leases = Path(os.environ.get("FAKE_UNITY_LEASES", ""))
        names = sorted(p.name for p in leases.glob("*.lock")) if leases.is_dir() else []
        (folder / "leases.json").write_text(json.dumps(names), encoding="utf-8")
    end = time.monotonic() + float(value("--sleep", "0"))
    while time.monotonic() < end:
        if handle and "--grow" in sys.argv:
            handle.write("tick\n")
            handle.flush()
        time.sleep(0.1)
    sys.exit(int(value("--exit", "0")))


if __name__ == "__main__":
    main()
