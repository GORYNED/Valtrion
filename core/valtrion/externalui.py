"""A host for the `external.ui` plan step: answers the app's request with a command the project names.

An `external.ui` step puts a request (`id`, `action`, optional `args`) into the heartbeat and waits for `inbox/reply-<id>.json`
(see docs/protocol.md). A `run` stage of a pipeline that has `externalUi` answers it: for each new request the host runs
`externalUi.command` once, bounded by `externalUi.timeoutSec`, and writes the reply. The command is project configuration, like a
profile's `signScript`: it runs as the user on the host that runs the pipeline (the machine the device is attached to), in the project
folder, and is trusted as the pipeline file is (docs/trust-model.md).

The command is an argv list. These placeholders are replaced inside one element (never split, never passed through a shell):
`{device}` (the platform's own id of the selected device, for example the UDID), `{app}`, `{action}`, `{request}` and `{arg:NAME}` (a
scalar of the request's `args`; a missing or non-scalar one makes the reply `BLOCKED`). The same values are in the environment as
`GDV_UI_DEVICE`, `GDV_UI_APP`, `GDV_UI_ACTION`, `GDV_UI_REQUEST` and `GDV_UI_ARGS` (JSON). Neither a device id nor a team or signing
identity has to be written into the pipeline: pass what the command needs through the host's environment.

The reply: exit code 0 is `PASS`, any other exit code `FAIL` and a command that hit its timeout `TIMEOUT`. The last output line may be a
JSON object `{"status": ..., "message": ...}`; its `message` is used as is, and its `status` can make a failure more precise
(`BLOCKED`, `NEEDS_PERMISSION`, `TIMEOUT`), but never turns a non-zero exit into `PASS`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import constants, supervise

DEFAULT_TIMEOUT_SEC = 120.0
#: Reply files are named after the request id; it comes from the app, so only the contract's identifier characters are accepted.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PLACEHOLDER = re.compile(r"\{(device|app|action|request|arg:[A-Za-z0-9_.-]+)\}")
_MESSAGE_LIMIT = 500


@dataclass(frozen=True)
class Reply:
    status: str
    message: str


def safe_request_id(value: object) -> str | None:
    """The request id when it is safe as a part of a file name, else None."""
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def expand(command: Sequence[str], values: Mapping[str, str], args: Mapping[str, Any]) -> list[str]:
    """The argv with its placeholders replaced. Raises `KeyError(name)` for a missing or non-scalar `{arg:NAME}`."""
    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name.startswith("arg:"):
            text = _scalar(args.get(name[4:]))
            if text is None:
                raise KeyError(name[4:])
            return text
        return values[name]
    return [_PLACEHOLDER.sub(replace, part) for part in command]


def _message(output: str, fallback: str) -> tuple[str, str | None]:
    """The reply message and the status the command named in its last line (None when it named none)."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if lines:
        try:
            document = json.loads(lines[-1])
        except ValueError:
            document = None
        if isinstance(document, dict) and ("status" in document or "message" in document):
            status = document.get("status")
            text = document.get("message")
            return (text if isinstance(text, str) and text else fallback)[:_MESSAGE_LIMIT], status if status in constants.STATUSES else None
        return lines[-1][:_MESSAGE_LIMIT] or fallback, None
    return fallback, None


def answer(spec: Mapping[str, Any], request: Mapping[str, Any], device: str, app: str, cwd: Path | None,
           environ: Mapping[str, str] | None = None) -> Reply:
    """Run the stage's `externalUi.command` for one heartbeat request and turn what happens into the reply."""
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        return Reply(constants.BLOCKED, "externalUi.command must be a non-empty list of strings")
    action = str(request.get("action", ""))
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    request_id = str(request.get("id", ""))
    values = {"device": device, "app": app, "action": action, "request": request_id}
    try:
        argv = expand(command, values, args)
    except KeyError as missing:
        return Reply(constants.BLOCKED, f"the request {action!r} has no scalar argument {missing.args[0]!r} that externalUi.command needs")
    timeout = float(spec.get("timeoutSec") or DEFAULT_TIMEOUT_SEC)
    env = dict(os.environ if environ is None else environ)
    env.update({"GDV_UI_DEVICE": device, "GDV_UI_APP": app, "GDV_UI_ACTION": action, "GDV_UI_REQUEST": request_id,
                "GDV_UI_ARGS": json.dumps(args, sort_keys=True)})
    result = supervise.run_bounded(argv, timeout, stall_sec=timeout, cwd=str(cwd) if cwd else None, env=env)
    if result.status == constants.TIMEOUT:
        return Reply(constants.TIMEOUT, f"{Path(argv[0]).name} did not finish within {timeout:g} s")
    named: str | None
    if result.status != constants.PASS or result.returncode not in (0,):
        text, named = _message(result.output, result.reason or f"{Path(argv[0]).name} exited with {result.returncode}")
        own = result.status if result.status != constants.PASS else constants.FAIL
        return Reply(named if named not in (None, constants.PASS) else own, text)
    text, named = _message(result.output, f"{action} done")
    return Reply(constants.PASS if named in (None, constants.PASS) else named, text)
