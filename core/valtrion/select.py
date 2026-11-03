"""Check selection: the project's `checks.json` applied to the changed paths of a candidate, and the `check-config` L0 helper.

`valtrion select` implements a change matrix (validation levels): changed paths are mapped to kinds with the area map,
the checks of those kinds are united for the level and every lower level, and an unclassified path fails closed. The module has no
dependency outside the standard library and the schema validator of this package.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import constants, project
from .schema import SchemaError, UnreadableInput, Validator, parse_json, validate_file

LEVELS = ("L0", "L1", "L2", "L3", "L4")
DEFAULT_CHECKS_PATH = Path("validation") / "device" / "checks.json"


def default_checks_path(root: Path, start: Path) -> Path:
    """`checks` of the nearest valtrion.json inside the repository, else `validation/device/checks.json` under the repository root."""
    try:
        configured = project.checks_file_setting(start, root)
    except project.ProjectError as error:
        raise UnreadableInput(str(error)) from error
    return configured or root / DEFAULT_CHECKS_PATH

L0, EDITMODE, PLAYMODE, PLAYER, DEVICE = "l0", "editmode", "playmode", "player", "device"
#: Order of the check types in every listing.
TYPES = (L0, EDITMODE, PLAYMODE, PLAYER, DEVICE)
_EDITOR_TYPES = (EDITMODE, PLAYMODE)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_TIMEOUT_SEC = 60


class ConfigError(Exception):
    """`checks.json` or a plan it references is invalid; `errors` lists every violation found."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class GitError(Exception):
    """A git query failed or git is missing: a missing prerequisite, reported as BLOCKED."""


@dataclass(frozen=True)
class Spec:
    """One entry of a kind's level list: `l0`, `editmode:A,B@runner`, `playmode:A`, `player:target` or `device:plans/name`."""

    type: str
    target: str
    runner: str | None
    raw: str


@dataclass(frozen=True)
class DevicePlan:
    path: Path
    plan_id: str
    platforms: tuple[str, ...]
    capabilities: tuple[str, ...]
    evidence: tuple[str, ...]
    profile: str | None


@dataclass
class Check:
    """One selected check. `key` is unique; a result file names its check by putting the key in `plan.id`."""

    key: str
    type: str
    target: str
    level: str
    runner: str | None = None
    kinds: set[str] = field(default_factory=set)
    plan: DevicePlan | None = None
    #: The key of `runners` the runner came from, or None for a literal command.
    runner_name: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    data: dict

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def kinds(self) -> dict[str, dict[str, list[str]]]:
        return self.data["kinds"]

    @property
    def areas(self) -> list[dict]:
        return self.data["areas"]

    @property
    def unclassified_policy(self) -> str:
        return self.data["unclassified"]

    @property
    def runners(self) -> dict[str, str]:
        """The optional top-level `runners`: name -> command template (empty when the map has none)."""
        return self.data.get("runners", {})

    def resolve_runner(self, text: str | None) -> tuple[str | None, str | None]:
        """(command template, runner name) for the text after `@` of an entry. The text is a name when it is exactly a key of `runners`;
        otherwise it is the literal command, as it always was. (A literal command that equals a runner name cannot be written; give the
        runner another name or the command another spelling.)"""
        if text is None:
            return None, None
        if text in self.runners:
            return self.runners[text], text
        return text, None


# --- specs -----------------------------------------------------------------------------------------------------------------------


def parse_spec(raw: str) -> Spec:
    """Parse one check entry. Raises `ValueError` naming the rule that is broken."""
    text = raw.strip()
    head, at, runner = text.partition("@")
    runner_text = runner.strip() if at else None
    if at and not runner_text:
        raise ValueError(f"{raw!r}: empty runner after '@'")
    kind, colon, target = head.partition(":")
    kind = kind.strip().lower()
    target = target.strip()
    if kind not in TYPES:
        raise ValueError(f"{raw!r}: unknown check type {kind!r} (expected one of {', '.join(TYPES)})")
    if kind == L0:
        if runner_text is not None:
            raise ValueError(f"{raw!r}: an l0 check has no runner")
        return Spec(L0, target, None, raw)
    if not colon or not target:
        raise ValueError(f"{raw!r}: {kind} needs a target after ':'")
    if kind in _EDITOR_TYPES:
        names = [name.strip() for name in target.split(",")]
        for name in names:
            if not _NAME.match(name):
                raise ValueError(f"{raw!r}: invalid assembly name {name!r}")
        return Spec(kind, ",".join(names), runner_text, raw)
    if runner_text is not None:
        raise ValueError(f"{raw!r}: only editmode and playmode entries name a runner")
    if kind == PLAYER and not _NAME.match(target):
        raise ValueError(f"{raw!r}: invalid player target {target!r}")
    if kind == DEVICE and (target.startswith("/") or ".." in target.replace("\\", "/").split("/")):
        raise ValueError(f"{raw!r}: a device plan path is relative to the checks folder and stays inside it")
    return Spec(kind, target, None, raw)


def plan_file(config: Config, target: str) -> Path:
    name = target if target.lower().endswith(".json") else f"{target}.plan.json"
    return config.directory / name


def load_plan(config: Config, target: str, validator: Validator | None = None) -> DevicePlan:
    path = plan_file(config, target)
    try:
        _, errors = validate_file(path, validator)
        text = path.read_text(encoding="utf-8-sig")
    except UnreadableInput as error:
        raise ConfigError([f"plan {target}: {error}"]) from error
    except OSError as error:
        raise ConfigError([f"plan {target}: cannot read {path}: {error}"]) from error
    if errors:
        raise ConfigError([f"plan {path}: {message}" for message in errors])
    document = parse_json(text)
    if document.get("schema") != "gdv.plan/1":
        raise ConfigError([f"plan {path}: expected schema gdv.plan/1"])
    requires = document.get("requires", {})
    return DevicePlan(
        path=path,
        plan_id=document["id"],
        platforms=tuple(requires.get("platforms", ())),
        capabilities=tuple(requires.get("capabilities", ())),
        evidence=tuple(requires.get("evidence", ())),
        profile=requires.get("profile"),
    )


# --- configuration ---------------------------------------------------------------------------------------------------------------


def load_config(path: Path, validator: Validator | None = None) -> Config:
    """Read and schema-validate a `checks.json`. Raises `UnreadableInput`, `SchemaError` or `ConfigError`."""
    path = Path(path)
    schema_id, errors = validate_file(path, validator)
    if errors:
        raise ConfigError([f"{path}: {message}" for message in errors])
    if schema_id != "gdv.checks/1":
        raise ConfigError([f"{path}: expected schema gdv.checks/1, found {schema_id}"])
    return Config(path, parse_json(path.read_text(encoding="utf-8-sig")))


def validate_config(config: Config, validator: Validator | None = None) -> tuple[list[str], list[str]]:
    """Check what the schema cannot: area kinds exist, every entry parses, every device plan loads and plan ids are unique."""
    errors: list[str] = []
    warnings: list[str] = []
    kinds = config.kinds
    used: set[str] = set()
    for index, area in enumerate(config.areas):
        glob = area["glob"]
        if area["kind"] not in kinds:
            errors.append(f"areas[{index}]: kind {area['kind']!r} has no entry in kinds")
        used.add(area["kind"])
        if glob.startswith("/") or ".." in glob.replace("\\", "/").split("/") or "\\" in glob:
            errors.append(f"areas[{index}]: glob {glob!r} must be repository-relative with '/' separators and no '..'")
        for excluded in area.get("exclude", ()):
            if not excluded or excluded.startswith("/") or ".." in excluded.replace("\\", "/").split("/") or "\\" in excluded:
                errors.append(f"areas[{index}]: exclude glob {excluded!r} must be repository-relative with '/' separators and no '..'")
    for name, template in config.runners.items():
        if not _NAME.match(name):
            errors.append(f"runners: invalid runner name {name!r} (letters, digits, '.', '_' and '-', starting with a letter or digit)")
        errors.extend(f"runners.{name}: {message}" for message in _placeholder_problems(template))
    for kind in sorted(set(kinds) - used):
        warnings.append(f"kind {kind!r} is not used by any area (selected only by the widest set)")
    plan_ids: dict[str, str] = {}
    for kind in sorted(kinds):
        for level in LEVELS:
            for raw in kinds[kind].get(level, ()):
                try:
                    spec = parse_spec(raw)
                except ValueError as error:
                    errors.append(f"kinds.{kind}.{level}: {error}")
                    continue
                if spec.runner is not None and spec.type in _EDITOR_TYPES:
                    template, name = config.resolve_runner(spec.runner)
                    if name is None:  # a literal command is checked here; a named one was checked once under `runners`
                        errors.extend(f"kinds.{kind}.{level}: {message}" for message in _placeholder_problems(template or ""))
                        if config.runners and _NAME.match(spec.runner):
                            warnings.append(f"kinds.{kind}.{level}: {spec.runner!r} is not a key of runners ({', '.join(sorted(config.runners))}); "
                                            "it is used as a literal command")
                if spec.type != DEVICE:
                    continue
                try:
                    plan = load_plan(config, spec.target, validator)
                except ConfigError as error:
                    errors.extend(f"kinds.{kind}.{level}: {message}" for message in error.errors)
                    continue
                if plan.plan_id.split(":", 1)[0].lower() in TYPES:
                    errors.append(f"kinds.{kind}.{level}: plan id {plan.plan_id!r} collides with the check keys of type {plan.plan_id.split(':', 1)[0]!r}")
                previous = plan_ids.setdefault(plan.plan_id, spec.target)
                if previous != spec.target:
                    errors.append(f"kinds.{kind}.{level}: plan id {plan.plan_id!r} is used by {previous!r} and {spec.target!r}")
    return errors, warnings


#: Placeholders of a runner template: the united, sorted assembly list joined with `,` or with `;` (Unity's separator for
#: `-assemblyNames` and `-testFilter`; written with the `;` variant it cannot end a command in a shell by being in the checks file).
ASSEMBLIES_COMMA = "{assemblies}"
ASSEMBLIES_SEMICOLON = "{assemblies;}"
_PLACEHOLDER = re.compile(r"(?<!\$)\{(?:assemblies[^{}\s]*|assembly)\}")  # look-alikes of ours only; a shell's ${assemblyDir}, ${HOME} or {a,b} is the command's own


def _placeholder_problems(template: str) -> list[str]:
    """A `{assembl...}` in a runner template must be one of the two assembly placeholders; a typo would otherwise be run as text."""
    known = (ASSEMBLIES_COMMA, ASSEMBLIES_SEMICOLON)
    return [f"unknown placeholder {found!r} in the runner {template!r} (use {ASSEMBLIES_COMMA} or {ASSEMBLIES_SEMICOLON})"
            for found in _PLACEHOLDER.findall(template) if found not in known]


def expand_runner(template: str, assemblies: Sequence[str]) -> str:
    return template.replace(ASSEMBLIES_SEMICOLON, ";".join(assemblies)).replace(ASSEMBLIES_COMMA, ",".join(assemblies))


# --- paths -----------------------------------------------------------------------------------------------------------------------


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """`*` and `?` stay inside a path segment, `**/` matches any number of folders (also none) and a final `**` the rest."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        char = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def classify(config: Config, paths: Sequence[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Map each path to every kind whose area matches it; paths without a match are unclassified.

    A path matches an area when it matches `glob` and none of the area's optional `exclude` globs. An excluded path can still match
    another area, so it keeps that area's kind."""
    compiled = [(glob_to_regex(area["glob"]), [glob_to_regex(item) for item in area.get("exclude", ())], area["kind"]) for area in config.areas]
    classified: dict[str, list[str]] = {}
    unclassified: list[str] = []
    for path in paths:
        kinds = sorted({kind for pattern, excluded, kind in compiled if pattern.match(path) and not any(skip.match(path) for skip in excluded)})
        if kinds:
            classified[path] = kinds
        else:
            unclassified.append(path)
    return classified, unclassified


def _git(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, timeout=_GIT_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise GitError(f"git {' '.join(arguments)}: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(arguments)} failed ({completed.returncode}): {detail}")
    return completed.stdout


def _split_z(data: bytes) -> list[str]:
    return [item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item]


def repository_root(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip())


def changed_paths(root: Path, base_ref: str, include_worktree: bool) -> tuple[list[str], str]:
    """Paths of the full candidate diff from the merge base with `base_ref`, sorted and without duplicates.

    Renames are reported as a delete and an add (`--no-renames`) so the old location is classified too. With `include_worktree`
    the staged and unstaged changes and the untracked files that are not ignored are part of the diff."""
    merge_base = _git(root, "merge-base", base_ref, "HEAD").decode("utf-8", "replace").strip()
    if include_worktree:
        # Against the merge base without a second revision: committed, staged and unstaged changes of tracked files together.
        paths = set(_split_z(_git(root, "diff", "--name-only", "--no-renames", "-z", merge_base)))
        paths.update(_split_z(_git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    else:
        paths = set(_split_z(_git(root, "diff", "--name-only", "--no-renames", "-z", merge_base, "HEAD")))
    return sorted(paths), merge_base


# --- selection -------------------------------------------------------------------------------------------------------------------


@dataclass
class Selection:
    level: str
    base_ref: str | None
    merge_base: str | None
    include_worktree: bool
    paths: list[str]
    classified: dict[str, list[str]]
    unclassified: list[str]
    policy: str
    widest: bool
    kinds: list[str]
    checks: list[Check]
    config: Config
    failed: bool = False
    #: Kinds the caller named directly (`--kind`), without a diff; sorted. They are part of `kinds`.
    named_kinds: list[str] = field(default_factory=list)

    def of_type(self, *types: str) -> list[Check]:
        return [check for check in self.checks if check.type in types]

    def device_checks(self) -> list[Check]:
        return self.of_type(DEVICE)

    def required_platforms(self) -> list[str]:
        return sorted({platform for check in self.device_checks() for platform in check.plan.platforms})  # type: ignore[union-attr]

    def editor_commands(self) -> list[dict[str, Any]]:
        """One command per runner: the suites that share a runner run in one Unity process."""
        groups: dict[str | None, list[Check]] = {}
        for check in self.of_type(*_EDITOR_TYPES):
            groups.setdefault(check.runner, []).append(check)
        commands = []
        for runner in sorted(groups, key=lambda value: (value is None, value or "")):
            suites = sorted(groups[runner], key=lambda item: item.key)
            assemblies = sorted({suite.target for suite in suites})
            command = None if runner is None else expand_runner(runner, assemblies)
            runner_name = next((suite.runner_name for suite in suites if suite.runner_name), None)
            commands.append({"runner": runner, "runnerName": runner_name, "command": command, "assemblies": assemblies,
                             "suites": [suite.key for suite in suites]})
        return commands

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "level": self.level,
            "baseRef": self.base_ref,
            "mergeBase": self.merge_base,
            "includeWorktree": self.include_worktree,
            "changedPaths": self.paths,
            "pathKinds": self.classified,
            "unclassifiedPaths": self.unclassified,
            "unclassifiedPolicy": self.policy,
            "widest": self.widest,
            "failed": self.failed,
            "kinds": self.kinds,
            "selectedKinds": self.named_kinds,
            "kindsNamedExplicitly": bool(self.named_kinds),
            "l0": [_check_json(check) for check in self.of_type(L0)],
            "editorSuites": [_check_json(check) for check in self.of_type(*_EDITOR_TYPES)],
            "editorCommands": self.editor_commands(),
            "playerTests": [_check_json(check) for check in self.of_type(PLAYER)],
            "devicePlans": [_check_json(check) for check in self.device_checks()],
            "requiredPlatforms": self.required_platforms(),
            "projectPlatforms": self.config.data["platforms"],
            "unityEditorSlots": self.config.data.get("unityEditorSlots"),
            "afterFinalReview": [check.key for check in self.device_checks()],
        }


def required_evidence(plan: DevicePlan) -> tuple[str, ...]:
    """Evidence a device plan needs; a plan that declares none needs a physical device (fail closed)."""
    return plan.evidence or ("physical",)


def _check_json(check: Check) -> dict[str, Any]:
    item: dict[str, Any] = {"key": check.key, "type": check.type, "target": check.target, "level": check.level, "kinds": sorted(check.kinds)}
    if check.type in _EDITOR_TYPES:
        item["runner"] = check.runner
        item["runnerName"] = check.runner_name
    if check.plan is not None:
        item.update(
            planId=check.plan.plan_id,
            plan=check.plan.path.as_posix(),
            platforms=list(check.plan.platforms),
            capabilities=list(check.plan.capabilities),
            evidence=list(check.plan.evidence),
            profile=check.plan.profile,
            requiresPhysicalDevice="physical" in required_evidence(check.plan),
        )
    return item


def _expand(spec: Spec, config: Config, level: str, kind: str, found: dict[str, Check], validator: Validator | None) -> None:
    if spec.type in _EDITOR_TYPES:
        pairs = [(f"{spec.type}:{name}", name) for name in spec.target.split(",")]
    elif spec.type == DEVICE:
        plan = load_plan(config, spec.target, validator)
        pairs = [(plan.plan_id, spec.target)]
    elif spec.type == L0:
        pairs = [(f"l0:{spec.target}" if spec.target else "l0", spec.target)]
    else:
        pairs = [(f"{spec.type}:{spec.target}", spec.target)]
    runner, runner_name = config.resolve_runner(spec.runner)
    for key, target in pairs:
        check = found.get(key)
        if check is None:
            check = found[key] = Check(key, spec.type, target, level, runner, runner_name=runner_name)
            if spec.type == DEVICE:
                check.plan = load_plan(config, spec.target, validator)
        elif spec.type == DEVICE and check.plan is not None and check.plan.path != load_plan(config, spec.target, validator).path:
            raise ConfigError([f"kinds.{kind}.{level}: plan id {key!r} is declared by two plan files"])
        elif check.type != spec.type:
            raise ConfigError([f"kinds.{kind}.{level}: check key {key!r} is used by a {check.type} and a {spec.type} entry"])
        else:
            if check.runner is not None and runner is not None and check.runner != runner:
                raise ConfigError([f"kinds.{kind}.{level}: {key!r} names two different runners"])
            if check.runner is None:
                check.runner, check.runner_name = runner, runner_name
            if LEVELS.index(level) < LEVELS.index(check.level):
                check.level = level
        check.kinds.add(kind)


def select(
    config: Config,
    paths: Sequence[str],
    level: str = "L4",
    *,
    base_ref: str | None = None,
    merge_base: str | None = None,
    include_worktree: bool = False,
    validator: Validator | None = None,
    named_kinds: Sequence[str] = (),
) -> Selection:
    """Union of the checks of the matched kinds for `level` and every lower level; unclassified paths widen the set.

    `named_kinds` selects those kinds directly, with or without paths (a union with the kinds of the paths); a name that is not a
    kind of the map is an error that lists the kinds. With the policy `fail` an unclassified path selects nothing and sets `failed`."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    named = sorted(set(named_kinds))
    unknown = [name for name in named if name not in config.kinds]
    if unknown:
        raise ConfigError([f"unknown kind(s): {', '.join(unknown)} (kinds in the map: {', '.join(sorted(config.kinds)) or 'none'})"])
    classified, unclassified = classify(config, paths)
    kinds = {kind for matched in classified.values() for kind in matched} | set(named)
    policy = config.unclassified_policy
    widest = bool(unclassified) and policy == "widest"
    failed = bool(unclassified) and policy == "fail"
    if widest:
        kinds.update(config.kinds)
    found: dict[str, Check] = {}
    missing = sorted(kind for kind in kinds if kind not in config.kinds)
    if missing:
        raise ConfigError([f"areas name kind(s) without an entry in kinds: {', '.join(missing)}"])
    if not failed:
        for kind in sorted(kinds):
            levels = config.kinds.get(kind, {})
            for current in LEVELS[: LEVELS.index(level) + 1]:
                for raw in levels.get(current, ()):
                    _expand(parse_spec(raw), config, current, kind, found, validator)
    checks = sorted(found.values(), key=lambda check: (TYPES.index(check.type), check.key))
    return Selection(
        level, base_ref, merge_base, include_worktree, list(paths), classified, unclassified, policy, widest, sorted(kinds), checks, config, failed,
        named,
    )


# --- CLI -------------------------------------------------------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-ref", help="the base of the candidate diff, compared from the merge base (for example origin/main); required unless --kind is given")
    parser.add_argument("--kind", action="append", default=[], metavar="NAME",
                        help="select the checks of this kind of the map directly, without a diff, at --level (repeatable, or a comma list); "
                             "with --base-ref it is added to the kinds of the diff")
    parser.add_argument("--checks", type=Path, help=f"the project check map (default: `checks` of valtrion.json, else {DEFAULT_CHECKS_PATH.as_posix()} under the repository root)")
    parser.add_argument("--include-worktree", action="store_true", help="also count staged, unstaged and untracked files as changed")


def _configure_select(parser: argparse.ArgumentParser) -> None:
    _add_common(parser)
    parser.add_argument("--level", choices=LEVELS, default="L4", help="highest level to select, cumulative (default: L4, everything the change needs)")
    parser.add_argument("--json", action="store_true", help="print the selection as JSON")


def resolve_selection(arguments: argparse.Namespace) -> Selection:
    """Shared by `select` and `report`: locate the checks file, compute the diff and select. May raise the errors `run_guarded` handles."""
    checks_path = arguments.checks
    start = Path.cwd() if checks_path is None else Path(checks_path).resolve().parent
    root = repository_root(start)
    if checks_path is None:
        checks_path = default_checks_path(root, start)
    config = load_config(Path(checks_path))
    named = [name.strip() for item in arguments.kind for name in item.split(",") if name.strip()]
    if arguments.base_ref:
        paths, merge_base = changed_paths(root, arguments.base_ref, arguments.include_worktree)
    elif named:
        paths, merge_base = [], None  # no diff: the named kinds alone
    else:
        print("error: --base-ref is required unless --kind is given", file=sys.stderr)
        raise SystemExit(2)
    return select(config, paths, arguments.level, base_ref=arguments.base_ref, merge_base=merge_base,
                  include_worktree=bool(arguments.base_ref) and arguments.include_worktree, named_kinds=named)


def run_guarded(name: str, action) -> int:
    """Map the errors of reading the inputs to exit codes: a broken contract fails (1), a missing prerequisite is BLOCKED (2)."""
    try:
        return action()
    except (UnreadableInput, SchemaError, GitError) as error:
        print(f"valtrion {name}: BLOCKED: {error}", file=sys.stderr)
        return constants.EXIT_BLOCKED
    except ConfigError as error:
        for message in error.errors:
            print(f"valtrion {name}: {message}", file=sys.stderr)
        return constants.EXIT_FAIL
    except ValueError as error:  # a malformed check entry
        print(f"valtrion {name}: {error}", file=sys.stderr)
        return constants.EXIT_FAIL


def format_selection(selection: Selection) -> str:
    lines = [
        f"valtrion select: level {selection.level}, base {selection.base_ref or '-'}"
        + (f" (merge base {selection.merge_base[:8]})" if selection.merge_base else "")
        + f", {len(selection.paths)} changed path(s)" + (" incl. worktree" if selection.include_worktree else "")
    ]
    if selection.named_kinds:
        lines.append("named kinds: " + ", ".join(selection.named_kinds))
    if selection.unclassified:
        action = "no checks selected, fix the area map" if selection.failed else "widest set selected"
        lines.append(f"UNCLASSIFIED ({len(selection.unclassified)}), policy {selection.policy}: {action}")
        lines.extend(f"  {path}" for path in selection.unclassified)
    if not selection.paths and not selection.named_kinds:
        lines.append("no changed paths: nothing to select")
    lines.append("kinds: " + (", ".join(selection.kinds) or "-"))
    l0 = selection.of_type(L0)
    if l0:
        lines.append("L0:")
        lines.extend(f"  {check.key}" for check in l0)
    commands = selection.editor_commands()
    if commands:
        lines.append("Editor suites:")
        for entry in commands:
            lines.append(f"  {entry['command'] or '(no runner named in checks.json; use the project runner)'}")
            lines.extend(f"    {key} [{_level_of(selection, key)}]" for key in entry["suites"])
    player = selection.of_type(PLAYER)
    if player:
        lines.append("Player tests:")
        lines.extend(f"  {check.key} [{check.level}]" for check in player)
    device = selection.device_checks()
    if device:
        lines.append("Device plans (run once on the final head after the clean independent review):")
        for check in device:
            plan = check.plan
            extras = [f"platforms {','.join(plan.platforms) or '-'}"]  # type: ignore[union-attr]
            if plan.capabilities:  # type: ignore[union-attr]
                extras.append(f"capabilities {','.join(plan.capabilities)}")  # type: ignore[union-attr]
            if plan.evidence:  # type: ignore[union-attr]
                extras.append(f"evidence {','.join(plan.evidence)}")  # type: ignore[union-attr]
            lines.append(f"  {check.key} [{check.level}] " + "; ".join(extras))
    lines.append("Required platforms: " + (", ".join(selection.required_platforms()) or "-"))
    if not selection.checks and not selection.failed:
        lines.append("Nothing selected.")
    return "\n".join(lines)


def _level_of(selection: Selection, key: str) -> str:
    return next(check.level for check in selection.checks if check.key == key)


def _select(arguments: argparse.Namespace) -> int:
    def action() -> int:
        selection = resolve_selection(arguments)
        if arguments.json:
            print(json.dumps(selection.to_json(), indent=2, sort_keys=False))
        else:
            print(format_selection(selection))
        return constants.EXIT_FAIL if selection.failed else constants.EXIT_PASS

    return run_guarded("select", action)


def _configure_check_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--checks", type=Path, help=f"the project check map (default: `checks` of valtrion.json, else {DEFAULT_CHECKS_PATH.as_posix()} under the repository root)")
    parser.add_argument("--base-ref", help="also report the unclassified paths of the diff from the merge base with this ref")
    parser.add_argument("--include-worktree", action="store_true", help="with --base-ref, count staged, unstaged and untracked files too")
    parser.add_argument("--warn-unclassified", action="store_true", help="report unclassified paths as warnings (exit 0) instead of errors (exit 1)")


def _check_config(arguments: argparse.Namespace) -> int:
    def action() -> int:
        checks_path = arguments.checks
        root: Path | None = None
        if checks_path is None or arguments.base_ref:
            root = repository_root(Path.cwd() if checks_path is None else Path(checks_path).resolve().parent)
        if checks_path is None:
            checks_path = default_checks_path(root, Path.cwd())  # type: ignore[arg-type]
        config = load_config(Path(checks_path))
        errors, warnings = validate_config(config)
        unclassified: list[str] = []
        if arguments.base_ref:
            paths, _ = changed_paths(root, arguments.base_ref, arguments.include_worktree)  # type: ignore[arg-type]
            _, unclassified = classify(config, paths)
            if unclassified:
                message = f"{len(unclassified)} changed path(s) match no area: " + ", ".join(unclassified)
                (warnings if arguments.warn_unclassified else errors).append(message)
        for message in warnings:
            print(f"warning: {message}", file=sys.stderr)
        for message in errors:
            print(f"error: {message}", file=sys.stderr)
        if errors:
            return constants.EXIT_FAIL
        print(f"{checks_path}: valid gdv.checks/1 ({len(config.kinds)} kinds, {len(config.areas)} areas)")
        return constants.EXIT_PASS

    return run_guarded("check-config", action)


from .cli import Command, register  # noqa: E402  (cli imports this module last; importing it here keeps both import orders working)

register(Command("select", "select the checks a candidate diff needs", "check selection", _select, _configure_select))
register(Command("check-config", "validate checks.json, its plans and the unclassified paths of a diff", "check selection", _check_config, _configure_check_config))
