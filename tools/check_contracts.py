"""Checks contracts/: every schema and fixture is well-formed, ids match file names, and each fixture is accepted or rejected as its folder says.

Standard library only. The fixture verdicts use the Valtrion validator when `core/valtrion` is present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "contracts" / "schemas"
FIXTURES = ROOT / "contracts" / "fixtures"


def load(path: Path) -> list[object]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return [json.loads(text)]


def main() -> int:
    problems: list[str] = []
    ids: dict[str, Path] = {}
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            problems.append(f"{path.name}: not JSON: {error}")
            continue
        schema_id = document.get("$id", "")
        if not schema_id.startswith("gdv.") or not schema_id.split(".", 1)[1].startswith(path.name.split(".", 1)[0]):
            problems.append(f"{path.name}: $id {schema_id!r} does not match the file name")
        if schema_id in ids:
            problems.append(f"{path.name}: $id {schema_id!r} already used by {ids[schema_id].name}")
        ids[schema_id] = path
    for folder in ("valid", "invalid"):
        files = sorted((FIXTURES / folder).glob("*"))
        if not files:
            problems.append(f"contracts/fixtures/{folder} is empty")
        for path in files:
            try:
                load(path)
            except ValueError as error:
                if folder == "valid":
                    problems.append(f"fixtures/{folder}/{path.name}: not JSON: {error}")

    sys.path.insert(0, str(ROOT / "core"))
    try:
        from valtrion.schema import UnreadableInput, Validator, validate_file  # type: ignore[import-not-found]
    except ImportError:
        print("core/valtrion not present: structural checks only")
    else:
        validator = Validator()
        for folder in ("valid", "invalid"):
            for path in sorted((FIXTURES / folder).glob("*.json*")):
                try:
                    _, errors = validate_file(path, validator)
                except UnreadableInput as error:
                    errors = [str(error)]
                if folder == "valid" and errors:
                    problems.append(f"fixtures/valid/{path.name} is rejected: {errors[:2]}")
                if folder == "invalid" and not errors:
                    problems.append(f"fixtures/invalid/{path.name} is accepted")
    for problem in problems:
        print("FAIL", problem)
    print(f"{len(ids)} schemas checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
