"""Command line entry and subcommand registry of valtrion.

A command is a `Command` registered by name. Commands owned by later work are registered as stubs that exit with code 2; the
owning change replaces the handler with `register(Command(...))` (the registry keeps the last registration for a name).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import __version__, constants
from .schema import SchemaError, UnreadableInput, Validator, validate_file

#: A handler receives the parsed arguments and returns the process exit code.
Handler = Callable[[argparse.Namespace], int]


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    owner: str
    handler: Handler | None = None
    configure: Callable[[argparse.ArgumentParser], None] | None = None


COMMANDS: dict[str, Command] = {}


def register(command: Command) -> Command:
    """Add or replace a command; the last registration of a name wins."""
    COMMANDS[command.name] = command
    return command


def _not_implemented(command: Command) -> Handler:
    def run(_: argparse.Namespace) -> int:
        print(f"valtrion {command.name}: not implemented (owner: {command.owner})", file=sys.stderr)
        return constants.EXIT_BLOCKED

    return run


def _configure_validate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, help="a valtrion document (.json) or an events log (.jsonl)")


def _validate(arguments: argparse.Namespace) -> int:
    try:
        schema_id, errors = validate_file(arguments.file, Validator())
    except UnreadableInput as error:
        # A file that is missing, unreadable or not UTF-8 is a missing prerequisite, not a failed document.
        print(f"valtrion validate: BLOCKED: {error}", file=sys.stderr)
        return constants.EXIT_BLOCKED
    except SchemaError as error:
        # A broken schema installation is a missing prerequisite, not a failed document.
        print(f"valtrion validate: BLOCKED: {error}", file=sys.stderr)
        return constants.EXIT_BLOCKED
    if errors:
        for message in errors:
            print(f"{arguments.file}: {message}", file=sys.stderr)
        return constants.EXIT_FAIL
    print(f"{arguments.file}: valid {schema_id}")
    return constants.EXIT_PASS


def _configure_exit_code(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("status", choices=constants.STATUSES)


def _exit_code(arguments: argparse.Namespace) -> int:
    print(constants.exit_code_for_status(arguments.status))
    return constants.EXIT_PASS


def _schemas(_: argparse.Namespace) -> int:
    for schema_id in constants.SCHEMA_IDS:
        print(schema_id)
    return constants.EXIT_PASS


def _version(_: argparse.Namespace) -> int:
    print(f"valtrion {__version__} (contract {constants.CLI_CONTRACT_VERSION})")
    return constants.EXIT_PASS


_STUB_OWNERS = {
    "build": "platform adapters",
    "install": "platform adapters",
    "launch": "platform adapters",
    "push": "platform adapters",
    "pull": "platform adapters",
    "screenshot": "platform adapters",
    "logs": "platform adapters",
}


def _register_builtin() -> None:
    register(Command("validate", "validate a valtrion document against its schema", "contracts", _validate, _configure_validate))
    register(Command("schemas", "list the contract schema ids", "contracts", _schemas))
    register(Command("exit-code", "print the exit code of a status", "contracts", _exit_code, _configure_exit_code))
    register(Command("version", "print the core version and the CLI contract version", "contracts", _version))
    for name, owner in _STUB_OWNERS.items():
        stub = Command(name, f"reserved for {owner}", owner)
        register(Command(name, stub.help, owner, _not_implemented(stub), _accept_rest))


def _accept_rest(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="ignored until the command is implemented")


_register_builtin()

# Implemented commands register themselves on import and replace their stubs; they import this module, so these imports stay last.
from . import report as _report  # noqa: E402,F401
from . import select as _select  # noqa: E402,F401

# The host-core commands replace the reserved stubs of the same names (the last registration wins).
from . import commands as _commands  # noqa: E402

_commands.register_all()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valtrion", description="Valtrion device validation host CLI")
    parser.add_argument("--version", action="version", version=f"valtrion {__version__} (contract {constants.CLI_CONTRACT_VERSION})")
    sub = parser.add_subparsers(dest="command", metavar="command")
    for name in sorted(COMMANDS):
        command = COMMANDS[name]
        child = sub.add_parser(name, help=command.help)
        if command.configure is not None:
            command.configure(child)
        child.set_defaults(handler=command.handler)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Output may name paths outside the console code page; escape instead of failing a valid run with a traceback.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    arguments_list = list(sys.argv[1:] if argv is None else argv)
    # A reserved command takes any arguments (options included) so the owning change can define them; until then it only reports.
    if arguments_list and arguments_list[0] in COMMANDS and COMMANDS[arguments_list[0]].configure is _accept_rest:
        return COMMANDS[arguments_list[0]].handler(argparse.Namespace(arguments=arguments_list[1:]))
    parser = build_parser()
    arguments = parser.parse_args(arguments_list)
    arguments.raw_argv = arguments_list  # commands that run on another host forward their own command line
    if getattr(arguments, "handler", None) is None:
        parser.print_help(sys.stderr)
        return constants.EXIT_BLOCKED
    try:
        return arguments.handler(arguments)
    except SystemExit as stop:  # a command that has already reported why it cannot go on
        return stop.code if isinstance(stop.code, int) else constants.EXIT_BLOCKED
