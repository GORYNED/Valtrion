"""Install a fresh offline CLI and verify the example's pass, fail and timeout paths."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new directory for installation, state and raw evidence")
    arguments = parser.parse_args()
    directory = arguments.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[2]
    temporary = directory / "tmp"
    temporary.mkdir()
    environment = dict(os.environ, PIP_NO_INDEX="1", TEMP=str(temporary), TMP=str(temporary),
                       VALTRION_HOME=str(directory / "state"), GDV_WORK_ROOT=str(directory / "work"))
    environment.pop("GDV_UNITY_PROJECT", None)
    environment.pop("PYTHONPATH", None)

    def run(command: list[str], label: str, timeout: int = 60) -> subprocess.CompletedProcess:
        result = subprocess.run(command, cwd=source, env=environment, capture_output=True, text=True, timeout=timeout)
        (directory / f"{label}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        return result

    installed = run([sys.executable, str(source / "tools/install.py"), "--venv", "--home", str(directory)], "install", 120)
    if installed.returncode:
        raise RuntimeError(f"installation failed; see {directory / 'install.log'}")
    command = installed.stdout.strip().splitlines()[-1]
    scripts = Path(command).parent
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    if run([str(python), "-m", "pip", "check"], "pip-check").returncode:
        raise RuntimeError("pip check failed")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    cases = (("pass", "catalog.json", [], 30, "PASS", 0),
             ("fail", "invalid-catalog.json", [], 30, "FAIL", 1),
             ("timeout", "catalog.json", ["--delay-sec", "20"], 2, "TIMEOUT", 3))
    summary = []
    for label, catalog, extra, timeout, expected_status, expected_code in cases:
        result = run([command, "job", "start", "--project", "python-check", "--sha", sha,
                      "--label", label, "--cwd", str(source), "--timeout-sec", str(timeout),
                      "--stall-sec", "10", "--watch", "--", str(python),
                      "examples/python-check/check_catalog.py", f"examples/python-check/{catalog}", *extra], label)
        if result.returncode != expected_code:
            raise RuntimeError(f"{label}: expected exit {expected_code}, got {result.returncode}")
        info, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())
        folder = Path(info["dir"])
        job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
        if job["status"] != expected_status or job["state"] != "finished":
            raise RuntimeError(f"{label}: unexpected job state")
        console = (folder / "console.log").read_text(encoding="utf-8")
        expected_text = {"pass": "inventory value 2150 cents", "fail": "duplicate sku: notebook",
                         "timeout": "Checking catalog.json"}[label]
        if expected_text not in console:
            raise RuntimeError(f"{label}: missing check output")
        if (folder / "result.json").exists():
            raise RuntimeError(f"{label}: command job unexpectedly produced an app result")
        for name in ("job.json", "events.jsonl"):
            if run([command, "validate", str(folder / name)], f"{label}-{name}").returncode:
                raise RuntimeError(f"{label}: invalid {name}")
        if label == "timeout":
            if "inventory value" in console:
                raise RuntimeError("timeout: check unexpectedly completed")
            events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            if not any(event["type"] == "timeout.detected" for event in events):
                raise RuntimeError("timeout: no timeout event")
        summary.append({"case": label, "status": job["status"], "exitCode": result.returncode, "jobId": info["jobId"]})
    (directory / "summary.json").write_text(json.dumps({"sourceSha": sha, "cases": summary}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
