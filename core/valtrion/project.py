"""Project discovery (`valtrion.json`) and the compatibility check between the core and the Unity package of a project.

`valtrion.json` at a project root names the Unity project: `{"unityProject": "<path relative to that file>"}`, and optionally the check map:
`"checks": "<path relative to that file>"`. The CLI finds it by walking
up from the working directory. `--project-dir` and the `GDV_UNITY_PROJECT` environment variable override it, in that order.
`compat.json` maps Unity package versions to the range of core versions that supports them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from . import __version__

PROJECT_FILE = "valtrion.json"
PROJECT_ENV = "GDV_UNITY_PROJECT"
UNITY_PACKAGE = "com.goryned.valtrion.unity"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_COMPARATOR = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+){0,2})$")


class ProjectError(Exception):
    """valtrion.json is unreadable or invalid, or the named Unity project does not exist."""


# -- valtrion.json ----------------------------------------------------------------------------------------------------------


def find_project_file(start: Path) -> Path | None:
    """The nearest `valtrion.json` in `start` or a parent folder."""
    start = start.resolve()
    for folder in (start, *start.parents):
        candidate = folder / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def load_project_file(path: Path) -> Path:
    """The Unity project folder a `valtrion.json` names, checked to exist."""
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ProjectError(f"cannot read {path}: {error}") from error
    unity = document.get("unityProject") if isinstance(document, dict) else None
    if not isinstance(unity, str) or not unity:
        raise ProjectError(f'{path} must be an object with "unityProject": "<path relative to this file>"')
    project = (path.parent / unity).resolve()
    if not project.is_dir():
        raise ProjectError(f"{path}: unityProject {unity!r} is not a folder ({project})")
    return project


def checks_file_setting(start: Path, root: Path | None = None) -> Path | None:
    """The `checks` path of the nearest valtrion.json (relative to that file), or None when there is no file or no such key.
    A file outside `root` (the repository) is ignored, so a stray file in a parent folder cannot redirect the check map."""
    found = find_project_file(start)
    if found is None or (root is not None and root.resolve() not in (found.parent, *found.parent.parents)):
        return None
    try:
        document = json.loads(found.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ProjectError(f"cannot read {found}: {error}") from error
    checks = document.get("checks") if isinstance(document, dict) else None
    if checks is None:
        return None
    if not isinstance(checks, str) or not checks:
        raise ProjectError(f'{found}: "checks" must be a path relative to this file')
    return (found.parent / checks).resolve()


def resolve_unity_project(explicit: Path | None = None, environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path | None:
    """`--project-dir`, else `GDV_UNITY_PROJECT`, else the project named by the nearest valtrion.json; None when none is set."""
    if explicit is not None:
        return explicit.resolve()
    environment = os.environ if environ is None else environ
    if environment.get(PROJECT_ENV):
        return Path(environment[PROJECT_ENV]).expanduser().resolve()
    found = find_project_file(cwd or Path.cwd())
    return load_project_file(found) if found else None


# -- compat.json ------------------------------------------------------------------------------------------------------------


def compat_file() -> Path | None:
    """The compatibility table: the copy packed into the wheel or, in a checkout, `compat.json` at the repository root."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "_compat.json", here.parent.parent / "compat.json"):
        if candidate.is_file():
            return candidate
    return None


def parse_version(text: str) -> tuple[int, int, int]:
    parts = [int(part) for part in re.findall(r"\d+", text.split("-", 1)[0].split("+", 1)[0])[:3]]
    return tuple(parts + [0] * (3 - len(parts)))  # type: ignore[return-value]


def version_in_range(version: str, spec: str) -> bool:
    """`spec` is comma-separated comparators such as `>=0.7.0,<0.8`."""
    number = parse_version(version)
    for part in spec.split(","):
        match = _COMPARATOR.match(part.strip())
        if match is None:
            raise ProjectError(f"unsupported core range {spec!r}")
        operator, bound = match.group(1), parse_version(match.group(2))
        if not {">=": number >= bound, "<=": number <= bound, "==": number == bound, "!=": number != bound,
                ">": number > bound, "<": number < bound}[operator]:
            return False
    return True


def package_matches(version: str, pattern: str) -> bool:
    """`0.7.x` matches 0.7.0 and 0.7.12; a segment without `x` must be equal."""
    wanted = pattern.split(".")
    have = [str(part) for part in parse_version(version)]
    return all(w == "x" or w == h for w, h in zip(wanted, have)) and len(wanted) <= 3


def load_compat(path: Path | None = None) -> list[dict]:
    return load_compat_document(path)[1]


def load_compat_document(path: Path | None = None) -> tuple[str, list[dict]]:
    """(Unity package id, ranges) of compat.json; the id is stored there, so it lives in one place."""
    path = path or compat_file()
    if path is None:
        raise ProjectError("compat.json is missing from this installation")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectError(f"cannot read {path}: {error}") from error
    ranges = document.get("ranges") if isinstance(document, dict) else None
    if not isinstance(ranges, list) or not all(isinstance(r, dict) and "package" in r and "core" in r for r in ranges):
        raise ProjectError(f"{path} must hold a list of ranges with package and core")
    package = document.get("package")
    if not isinstance(package, str) or not package:
        raise ProjectError(f"{path} must name the Unity package in \"package\"")
    return package, ranges


# -- the package in a Unity project -----------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def package_version(project: Path, package: str = UNITY_PACKAGE) -> tuple[str, str]:
    """(state, text): `absent` with the reason, `found` with the version, or `unknown` with the reason."""
    lock_path = project / "Packages" / "packages-lock.json"
    if not lock_path.is_file():
        return "absent", f"{lock_path} does not exist"
    try:
        lock = _read_json(lock_path)
    except (OSError, ValueError) as error:
        return "unknown", f"{lock_path} cannot be read: {error}"
    dependencies = lock.get("dependencies") if isinstance(lock, dict) else None
    entry = dependencies.get(package) if isinstance(dependencies, dict) else None
    if entry is None:
        return "absent", f"{package} is not in {lock_path}"
    if not isinstance(entry, dict):
        return "unknown", f"the lock entry of {package} is not an object"
    version = str(entry.get("version", ""))
    if _SEMVER.match(version):
        return "found", version
    if version.startswith("file:"):
        manifest = (project / "Packages" / version[len("file:"):] / "package.json").resolve()
    elif entry.get("source") == "embedded":
        manifest = project / "Packages" / package / "package.json"
    else:  # a git package: Unity unpacks it to Library/PackageCache/<name>@<hash prefix>
        cache = project / "Library" / "PackageCache"
        revision = str(entry.get("hash", ""))
        folders = sorted(path for path in cache.glob(f"{package}@*") if path.is_dir()) if cache.is_dir() else []
        named = [path for path in folders if revision.startswith(path.name.split("@", 1)[1])]
        chosen = named or folders
        if len(chosen) != 1:
            return "unknown", f"the version of {UNITY_PACKAGE} cannot be read from the package cache (open the project in Unity once)"
        manifest = chosen[0] / "package.json"
    try:
        found = str(_read_json(manifest).get("version", ""))
    except (OSError, ValueError, AttributeError):
        return "unknown", f"cannot read the version from {manifest}"
    return ("found", found) if _SEMVER.match(found) else ("unknown", f"{manifest} has no usable version")


def check_project(project: Path, core_version: str = __version__, compat_path: Path | None = None) -> dict:
    """`{"status": "ok"|"skipped"|"BLOCKED", "reason": ..., "action": ...}`; skipped when the project does not use the Unity package."""
    try:
        package, ranges = load_compat_document(compat_path)
    except ProjectError as error:
        return {"status": "BLOCKED", "reason": str(error), "action": "reinstall valtrion"}
    state, text = package_version(project, package)
    if state == "absent":
        return {"status": "skipped", "reason": text}
    if state == "unknown":
        return {"status": "BLOCKED", "reason": text, "action": "open the project in Unity so the package is resolved, then retry"}
    matching = [r for r in ranges if package_matches(text, str(r["package"]))]
    if not matching:
        return {"status": "BLOCKED", "package": text, "core": core_version,
                "reason": f"{package} {text} is not in compat.json",
                "action": f"update {package} to a listed version ({', '.join(str(r['package']) for r in ranges)}) or install a core that lists {text}"}
    try:
        supported = any(version_in_range(core_version, str(r["core"])) for r in matching)
    except ProjectError as error:
        return {"status": "BLOCKED", "reason": str(error), "action": "reinstall valtrion"}
    if supported:
        return {"status": "ok", "package": text, "core": core_version, "reason": f"{package} {text} supports core {core_version}"}
    wanted = " or ".join(str(r["core"]) for r in matching)
    return {"status": "BLOCKED", "package": text, "core": core_version,
            "reason": f"{package} {text} needs core {wanted}, but this is core {core_version}",
            "action": f"install a core in that range (`valtrion hosts sync` after installing it here) or move the package to a version that supports {core_version}"}
