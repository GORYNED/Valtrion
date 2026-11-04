"""A host for the `external.ui` plan step: answers the app's request with a command the project names.

An `external.ui` step puts a request (`id`, `action`, optional `args`) into the heartbeat and waits for `inbox/reply-<id>.json`
(see docs/protocol.md). A `run` stage of a pipeline that has `externalUi` answers it: for each new request the host runs
`externalUi.command` once, bounded by `externalUi.timeoutSec`, and writes the reply. The command is project configuration, like a
profile's `signScript`: it runs as the user on the host that runs the pipeline (the machine the device is attached to), in the project
folder, and is trusted as the pipeline file is (docs/trust-model.md).

The command is an argv list. Its first element is the program and is never substituted. These placeholders are replaced inside the
other elements (never split, never passed through a shell): `{device}` (the platform's own id of the selected device, for example the
UDID), `{app}`, `{action}`, `{request}` and `{arg:NAME}` (a scalar of the request's `args`; a missing or non-scalar one makes the reply
`BLOCKED`). The same values are in the environment as `GDV_UI_DEVICE`, `GDV_UI_APP`, `GDV_UI_ACTION`, `GDV_UI_REQUEST` and
`GDV_UI_ARGS` (JSON). Neither a device id nor a team or signing identity has to be written into the pipeline: pass what the command
needs through the host's environment.

`action`, `args` and the request id come from the app's heartbeat, so they are untrusted data. The reply is `BLOCKED` (and nothing runs)
when a value has a NUL byte or is over its length cap, when a value that makes a whole element starts with `-` (it would be taken for
an option), or when a value has `/`, a backslash or `..` and goes into an element that itself has a path separator. A command takes such
values as data: `--flag={arg:x}`, and `--` before positionals.

The reply: exit code 0 is `PASS`, any other exit code `FAIL` and a command that hit its timeout `TIMEOUT`. The last output line may be a
JSON object `{"status": ..., "message": ...}`; its `message` is used as is, and its `status` can make a result more precise (`BLOCKED`,
`NEEDS_PERMISSION`, `TIMEOUT`; `STALL` is read as `TIMEOUT`), but never turns a non-zero exit into `PASS`.
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
#: Caps for what the app can put into the argv and the environment (a Windows environment block is 32 767 characters in all).
VALUE_LIMIT = 1024
ARGS_LIMIT = 8192
#: Only the end of the command's output is read: the answer is its last line.
OUTPUT_TAIL_BYTES = 64 * 1024
_HOST_VALUES = ("device", "app")
#: The statuses a command may name in its last line; `STALL` is a watchdog word and is read as `TIMEOUT`.
COMMAND_STATUSES = (constants.PASS, constants.FAIL, constants.BLOCKED, constants.NEEDS_PERMISSION, constants.TIMEOUT)
_PATH_SEPARATORS = ("/", "\\")


class UnsafeValue(ValueError):
    """A request value or a command element that must not reach a process."""


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


def _check_plain(name: str, text: str) -> None:
    if "\0" in text:
        raise UnsafeValue(f"the value of {name} contains a NUL byte")
    if len(text) > VALUE_LIMIT:
        raise UnsafeValue(f"the value of {name} is longer than {VALUE_LIMIT} characters")


def _check(name: str, text: str, whole: bool, path_like: bool) -> None:
    _check_plain(name, text)
    if name in _HOST_VALUES:
        return
    if whole and text.startswith("-"):
        raise UnsafeValue(f"the value of {name} starts with '-' and would be taken for an option")
    if path_like and (any(separator in text for separator in _PATH_SEPARATORS) or ".." in text):
        raise UnsafeValue(f"the value of {name} has a path separator or '..' and goes into a path")


def expand(command: Sequence[str], values: Mapping[str, str], args: Mapping[str, Any]) -> list[str]:
    """The argv with its placeholders replaced. Raises `KeyError(name)` for a missing or non-scalar `{arg:NAME}`, and `UnsafeValue` for
    a placeholder in the program or a value the app sent that must not be used (NUL, too long, an option, a path escape)."""
    if _PLACEHOLDER.search(command[0]):
        raise UnsafeValue("the program (the first element of externalUi.command) must not contain a placeholder")
    result = [command[0]]
    for part in command[1:]:
        whole = _PLACEHOLDER.fullmatch(part) is not None
        path_like = any(separator in part for separator in _PATH_SEPARATORS)

        def replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name.startswith("arg:"):
                text = _scalar(args.get(name[4:]))
                if text is None:
                    raise KeyError(name[4:])
            else:
                text = values[name]
            _check(name, text, whole, path_like)
            return text
        result.append(_PLACEHOLDER.sub(replace, part))
    return result


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
            if status == constants.STALL:
                status = constants.TIMEOUT
            return (text if isinstance(text, str) and text else fallback)[:_MESSAGE_LIMIT], status if status in COMMAND_STATUSES else None
        return lines[-1][:_MESSAGE_LIMIT] or fallback, None
    return fallback, None


def answer(spec: Mapping[str, Any], request: Mapping[str, Any], device: str, app: str, cwd: Path | None,
           environ: Mapping[str, str] | None = None, cancel_file: Path | None = None, remaining_sec: float | None = None) -> Reply:
    """Run the stage's `externalUi.command` for one heartbeat request and turn what happens into the reply. `cancel_file` stops the
    command when it appears; `remaining_sec` is what is left of the stage's budget and caps the command's timeout."""
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        return Reply(constants.BLOCKED, "externalUi.command must be a non-empty list of strings")
    action = str(request.get("action", ""))
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    request_id = str(request.get("id", ""))
    values = {"device": device, "app": app, "action": action, "request": request_id}
    try:
        for name, text in values.items():
            _check_plain(name, text)
        args_json = json.dumps(args, sort_keys=True)
        if len(args_json) > ARGS_LIMIT:
            raise UnsafeValue(f"the request's args are longer than {ARGS_LIMIT} characters")
        argv = expand(command, values, args)
    except KeyError as missing:
        return Reply(constants.BLOCKED, f"the request {action[:80]!r} has no scalar argument {missing.args[0]!r} that externalUi.command needs")
    except UnsafeValue as unsafe:
        return Reply(constants.BLOCKED, f"the command was not run: {unsafe}"[:_MESSAGE_LIMIT])
    timeout = float(spec.get("timeoutSec") or DEFAULT_TIMEOUT_SEC)
    if remaining_sec is not None:
        timeout = min(timeout, remaining_sec)
    if timeout <= 0:
        return Reply(constants.BLOCKED, "no time is left in the budget of the stage for the command")
    env = dict(os.environ if environ is None else environ)
    env.update({"GDV_UI_DEVICE": device, "GDV_UI_APP": app, "GDV_UI_ACTION": action, "GDV_UI_REQUEST": request_id,
                "GDV_UI_ARGS": args_json})
    try:
        result = supervise.run_bounded(argv, timeout, stall_sec=timeout, cwd=str(cwd) if cwd else None, env=env,
                                       cancel_file=cancel_file, tail_bytes=OUTPUT_TAIL_BYTES)
    except (ValueError, OSError) as error:
        return Reply(constants.BLOCKED, f"{Path(argv[0]).name} could not be started: {error}"[:_MESSAGE_LIMIT])
    name = Path(argv[0]).name
    if result.status in (constants.TIMEOUT, constants.STALL):
        return Reply(constants.TIMEOUT, f"{name} did not finish within {timeout:g} s")
    if result.status == constants.BLOCKED:
        return Reply(constants.BLOCKED, (result.reason or f"{name} could not be run")[:_MESSAGE_LIMIT])
    named: str | None
    if result.status != constants.PASS or result.returncode not in (0,):
        text, named = _message(result.output, result.reason or f"{name} exited with {result.returncode}")
        own = result.status if result.status != constants.PASS else constants.FAIL
        return Reply(named if named not in (None, constants.PASS) else own, text)
    text, named = _message(result.output, f"{action[:80]} done")
    return Reply(constants.PASS if named in (None, constants.PASS) else named, text)
