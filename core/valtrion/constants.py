"""Shared constants of the valtrion contracts: statuses, evidence kinds, exit codes and schema ids."""

from __future__ import annotations

import os

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NEEDS_PERMISSION = "NEEDS_PERMISSION"
STALL = "STALL"
TIMEOUT = "TIMEOUT"

STATUSES = (PASS, FAIL, BLOCKED, NEEDS_PERMISSION, STALL, TIMEOUT)

EVIDENCE_PHYSICAL = "physical"
EVIDENCE_EMULATOR = "emulator"
EVIDENCE_EDITOR = "editor"
EVIDENCE_HOST = "host"

EVIDENCE_KINDS = (EVIDENCE_PHYSICAL, EVIDENCE_EMULATOR, EVIDENCE_EDITOR, EVIDENCE_HOST)

# Comparison verdicts of `valtrion compare`.
VERDICTS = ("PASS", "REGRESSION", "IMPROVED", "NOISY", "NOT_COMPARABLE")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2
EXIT_STALL = 3

_EXIT_CODE_BY_STATUS = {
    PASS: EXIT_PASS,
    FAIL: EXIT_FAIL,
    BLOCKED: EXIT_BLOCKED,
    NEEDS_PERMISSION: EXIT_BLOCKED,
    STALL: EXIT_STALL,
    TIMEOUT: EXIT_STALL,
}

# A comparison verdict maps to the same codes: only a regression is a failure.
_EXIT_CODE_BY_VERDICT = {
    "PASS": EXIT_PASS,
    "IMPROVED": EXIT_PASS,
    "REGRESSION": EXIT_FAIL,
    "NOISY": EXIT_BLOCKED,
    "NOT_COMPARABLE": EXIT_BLOCKED,
}

SCHEMA_IDS = (
    "gdv.plan/1",
    "gdv.pipeline/1",
    "gdv.result/1",
    "gdv.heartbeat/1",
    "gdv.job/1",
    "gdv.event/1",
    "gdv.hosts/1",
    "gdv.registry/1",
    "gdv.lease/1",
    "gdv.checks/1",
    "gdv.baseline/1",
)

# Version of the valtrion CLI contracts. The CLI and the Runtime ship in one package, so the package version and the pinned git SHA
# identify the pair; this number changes only with a breaking change of the command surface.
CLI_CONTRACT_VERSION = 1


def exit_code_for_status(status: str) -> int:
    """Map a result status to the CLI exit code: 0 pass, 1 fail, 2 blocked or needs permission, 3 stall or timeout."""
    try:
        return _EXIT_CODE_BY_STATUS[status]
    except KeyError:
        raise ValueError(f"unknown status: {status!r}") from None


def exit_code_for_verdict(verdict: str) -> int:
    """Map a comparison verdict to the CLI exit code; an unusable comparison is blocked, not a pass."""
    try:
        return _EXIT_CODE_BY_VERDICT[verdict]
    except KeyError:
        raise ValueError(f"unknown verdict: {verdict!r}") from None

#: How long an Android launch may wait for a slow cold start (`am start -W` gives up by itself after about 15 s): the default and the
#: hard upper limit of `launchTimeoutSec` (run stage `args`, `valtrion run --launch-timeout`, `host.json`).
LAUNCH_TIMEOUT_DEFAULT_SEC = 60
LAUNCH_TIMEOUT_MAX_SEC = 300

#: `creationflags` for a child process that must not open a console window. The detached runner has no console, so every console child
#: (git, adb, tasklist, taskkill, python) would otherwise get a window of its own. 0 elsewhere (no effect).
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
