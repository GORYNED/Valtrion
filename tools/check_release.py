"""Check source versions, license notices, public examples and tracked Unity output.

Run from any directory with Python 3.10 or newer. These repository checks do not
certify Git history, legal ownership, Unity execution or publication readiness.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

from valtrion import __version__  # noqa: E402
from valtrion.project import ProjectError, load_compat_document, package_matches, version_in_range  # noqa: E402
from valtrion.schema import SchemaError, UnreadableInput, Validator, validate_file  # noqa: E402
from valtrion.select import ConfigError, load_config, validate_config  # noqa: E402


def project_field(text: str, name: str) -> str:
    """Read one quoted scalar from this project's simple [project] metadata."""
    section = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    match = re.search(rf'(?m)^{re.escape(name)}\s*=\s*"([^"\n]+)"\s*$', section.group(1)) if section else None
    if match is None:
        raise ValueError(f"core/pyproject.toml: missing quoted [project].{name}")
    return match.group(1)


def check_sources(root: Path, tracked_paths: list[str]) -> list[str]:
    """Return actionable problems in the checked-out public source inputs."""
    problems: list[str] = []
    metadata = (root / "core" / "pyproject.toml").read_text(encoding="utf-8")
    if project_field(metadata, "version") != __version__:
        problems.append("Core __version__ differs from core/pyproject.toml")
    if project_field(metadata, "license") != "MIT":
        problems.append("core/pyproject.toml must declare the repository's MIT license")

    package_root = root / "unity" / "com.goryned.valtrion.unity"
    package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    if package.get("license") != "MIT":
        problems.append("Unity package.json must declare the MIT license")
    license_text = (root / "LICENSE").read_bytes()
    if license_text != (package_root / "LICENSE.md").read_bytes():
        problems.append("Unity LICENSE.md differs from the root LICENSE")

    package_name, ranges = load_compat_document(root / "compat.json")
    if package.get("name") != package_name:
        problems.append("Unity package name differs from compat.json")
    if not any(package_matches(package["version"], entry["package"])
               and version_in_range(__version__, entry["core"]) for entry in ranges):
        problems.append(f"Unity {package.get('version')} and Core {__version__} are not paired in compat.json")

    validator = Validator(root / "contracts" / "schemas")
    for name, expected_schema in (("smoke.plan.json", "gdv.plan/1"), ("quick-start-checks.json", "gdv.checks/1")):
        schema, errors = validate_file(root / "examples" / name, validator)
        problems.extend(f"examples/{name}: {error}" for error in errors)
        if schema != expected_schema:
            problems.append(f"examples/{name}: expected {expected_schema}, found {schema}")
    config = load_config(root / "examples" / "quick-start-checks.json", validator)
    errors, _ = validate_config(config, validator)
    problems.extend(f"examples/quick-start-checks.json: {error}" for error in errors)

    generated = {"library", "temp", "logs", "obj", "usersettings"}
    for name in tracked_paths:
        parts = name.replace("\\", "/").split("/")
        if parts[0] == "unity" and any(part.lower() in generated for part in parts[1:-1]):
            problems.append(f"generated Unity output is tracked: {name}")
    return problems


def main() -> int:
    try:
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                                 capture_output=True, timeout=30).stdout.decode("utf-8").split("\0")
        problems = check_sources(ROOT, [name for name in tracked if name])
    except (OSError, ValueError, KeyError, ProjectError, SchemaError, UnreadableInput, ConfigError,
            subprocess.SubprocessError) as error:
        print(f"FAIL release source checks: {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"Release source checks: {len(problems)} problem(s); publication readiness requires the separate audit.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
