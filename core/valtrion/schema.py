"""Standard-library validator for the subset of JSON Schema used by the valtrion contracts.

The validator rejects every keyword it does not implement, so a schema cannot become weaker without a failing test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import constants

def default_schemas_directory() -> Path:
    """The contract schemas: the copy packed into the wheel (`_schemas`) or, in a checkout, `contracts/schemas` at the repository root."""
    here = Path(__file__).resolve().parent
    packed = here / "_schemas"
    return packed if packed.is_dir() else here.parent.parent / "contracts" / "schemas"


SCHEMAS_DIRECTORY = default_schemas_directory()

MAX_INTEGER = 9_000_000_000_000_000

_ANNOTATIONS = {"$schema", "$id", "title", "description", "$comment"}
_SUPPORTED = _ANNOTATIONS | {
    "type", "enum", "const", "required", "properties", "additionalProperties", "items", "minItems", "maxItems",
    "minimum", "maximum", "minLength", "maxLength", "pattern", "$ref", "$defs", "anyOf",
}


class SchemaError(Exception):
    """The schema itself is unusable (unknown keyword, unresolved reference, missing file)."""


class UnreadableInput(Exception):
    """The input file exists or was named but cannot be read or decoded: a missing prerequisite, not a violation of a contract."""


def _type_matches(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        # Same range as the C# reader (about 2^53): larger values are not exact doubles.
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return abs(value) <= MAX_INTEGER
        return isinstance(value, float) and value.is_integer() and abs(value) <= MAX_INTEGER
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise SchemaError(f"unknown type {name!r}")


class Validator:
    def __init__(self, schemas_directory: Path = SCHEMAS_DIRECTORY) -> None:
        self._directory = Path(schemas_directory)
        self._cache: dict[str, dict] = {}

    def load(self, file_name: str) -> dict:
        if file_name not in self._cache:
            path = self._directory / file_name
            if not path.is_file():
                raise SchemaError(f"schema file not found: {file_name}")
            try:
                with path.open("r", encoding="utf-8") as stream:
                    self._cache[file_name] = json.load(stream)
            except (OSError, ValueError) as error:  # ValueError covers JSONDecodeError and UnicodeDecodeError
                raise SchemaError(f"schema file {file_name} cannot be loaded: {error}") from error
        return self._cache[file_name]

    def schema_file_for(self, schema_id: str) -> str:
        """`gdv.plan/1` is stored as `plan.schema.json`."""
        if schema_id not in constants.SCHEMA_IDS:
            raise SchemaError(f"unknown schema id {schema_id!r}")
        return schema_id.split(".", 1)[1].split("/", 1)[0] + ".schema.json"

    def validate(self, document: Any, schema_id: str) -> list[str]:
        """Return the list of violations of `document` against the named schema; empty means valid."""
        file_name = self.schema_file_for(schema_id)
        root = self.load(file_name)
        errors: list[str] = []
        self._check(document, root, file_name, root, "$", errors)
        return errors

    def _resolve(self, reference: str, file_name: str, root: dict) -> tuple[dict, str, dict]:
        target_file, _, pointer = reference.partition("#")
        if target_file:
            file_name = target_file
            root = self.load(target_file)
        node: Any = root
        if pointer:
            for part in pointer.lstrip("/").split("/"):
                if not isinstance(node, dict) or part not in node:
                    raise SchemaError(f"unresolved reference {reference!r} in {file_name}")
                node = node[part]
        return node, file_name, root

    def _check(self, value: Any, schema: dict, file_name: str, root: dict, path: str, errors: list[str]) -> None:
        unknown = set(schema) - _SUPPORTED
        if unknown:
            raise SchemaError(f"unsupported keyword(s) {sorted(unknown)} in {file_name}")
        if "$ref" in schema:
            target, target_file, target_root = self._resolve(schema["$ref"], file_name, root)
            self._check(value, target, target_file, target_root, path, errors)
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_type_matches(value, name) for name in names):
                errors.append(f"{path}: expected {'|'.join(names)}, got {_describe(value)}")
                return
        if "const" in schema and (value != schema["const"] or isinstance(value, bool) != isinstance(schema["const"], bool)):
            errors.append(f"{path}: expected {schema['const']!r}, got {value!r}")
        if "enum" in schema and not any(value == option and type(value) is type(option) for option in schema["enum"]):
            errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
        if "anyOf" in schema:
            branches = []
            for branch in schema["anyOf"]:
                branch_errors: list[str] = []
                self._check(value, branch, file_name, root, path, branch_errors)
                if not branch_errors:
                    break
                branches.append(branch_errors)
            else:
                errors.append(f"{path}: matches none of the allowed forms ({'; '.join(e[0] for e in branches)})")
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append(f"{path}: shorter than {schema['minLength']}")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                errors.append(f"{path}: longer than {schema['maxLength']}")
            if "pattern" in schema:
                # fullmatch: `$` alone would also accept a trailing newline, which the C# reader rejects.
                if re.fullmatch(schema["pattern"], value) is None:
                    errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: {value} is below {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{path}: {value} is above {schema['maximum']}")
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                errors.append(f"{path}: fewer than {schema['minItems']} items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                errors.append(f"{path}: more than {schema['maxItems']} items")
            if "items" in schema:
                for index, item in enumerate(value):
                    self._check(item, schema["items"], file_name, root, f"{path}[{index}]", errors)
        if isinstance(value, dict):
            for name in schema.get("required", []):
                if name not in value:
                    errors.append(f"{path}: missing required property {name!r}")
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            for name, item in value.items():
                if name in properties:
                    self._check(item, properties[name], file_name, root, f"{path}.{name}", errors)
                elif additional is False:
                    errors.append(f"{path}: unexpected property {name!r}")
                elif isinstance(additional, dict):
                    self._check(item, additional, file_name, root, f"{path}.{name}", errors)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed in JSON")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate property {key!r}")
        result[key] = value
    return result


def parse_json(text: str) -> Any:
    """Strict JSON like the C# reader: no NaN or Infinity, no duplicate property names, limited nesting and number range.

    A byte-order mark is a file-encoding matter: `validate_file` drops one when it reads the file, so text passed here has none."""
    document = json.loads(
        text, parse_constant=_reject_constant, parse_float=_finite_float, parse_int=_finite_int, object_pairs_hook=_reject_duplicates)
    if _depth(document) > MAX_DEPTH:
        raise ValueError(f"nested more than {MAX_DEPTH} levels")
    return document


#: Nesting limit of arrays and objects, shared with the C# reader (JsonParser.MaxDepth).
MAX_DEPTH = 64


def _depth(value: Any) -> int:
    """Nesting depth of arrays and objects, computed without recursion."""
    deepest = 0
    stack = [(value, 1)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, dict):
            deepest = max(deepest, level)
            stack.extend((child, level + 1) for child in node.values())
        elif isinstance(node, list):
            deepest = max(deepest, level)
            stack.extend((child, level + 1) for child in node)
    return deepest


def _finite_float(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError(f"{text} is out of range")
    return value


def _finite_int(text: str) -> int:
    if len(text) > 400:
        raise ValueError("number is out of range")
    value = int(text)
    try:
        float(value)
    except OverflowError:
        raise ValueError("number is out of range") from None
    return value


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def check_plan_semantics(plan: dict) -> list[str]:
    """Rules a schema cannot express: an `external.ui` step must not run inside a measured probe window of a performance plan."""
    errors: list[str] = []
    if plan.get("kind") != "performance":
        return errors
    measuring = False
    for index, step in enumerate(plan.get("steps", [])):
        step_type = step.get("type")
        if step_type == "probe.start":
            measuring = True
        elif step_type == "probe.stop":
            measuring = False
        elif step_type == "external.ui" and measuring:
            errors.append(f"$.steps[{index}]: external.ui is forbidden between probe.start and probe.stop in a performance plan")
    return errors


def check_result_semantics(result: dict) -> list[str]:
    """Rules a schema cannot express: PASS has no reason and no failed layer; any other status says why."""
    errors: list[str] = []
    status = result.get("status")
    reason = result.get("reason")
    if status == constants.PASS and reason is not None:
        errors.append("$.reason: a PASS result has no reason")
    if status != constants.PASS and not reason:
        errors.append(f"$.reason: a {status} result needs a reason")
    if status == constants.PASS:
        for name, layer in result.get("layers", {}).items():
            if layer != constants.PASS:
                errors.append(f"$.layers.{name}: a PASS result cannot have a {layer} layer")
    return errors


def check_hosts_semantics(document: dict) -> list[str]:
    """Rules a schema cannot express: unique ids, at most one local host, an ssh host names its target and a local host has none."""
    errors: list[str] = []
    seen: set[str] = set()
    local_count = 0
    for index, host in enumerate(document.get("hosts", [])):
        identifier = host.get("id")
        if identifier in seen:
            errors.append(f"$.hosts[{index}]: duplicate host id {identifier!r}")
        seen.add(identifier)
        transport = host.get("transport", {})
        if transport.get("kind") == "ssh" and not transport.get("sshTarget"):
            errors.append(f"$.hosts[{index}].transport: an ssh host needs sshTarget")
        if transport.get("kind") == "local":
            local_count += 1
            if "sshTarget" in transport:
                errors.append(f"$.hosts[{index}].transport: a local host has no sshTarget")
    if local_count > 1:
        errors.append("$.hosts: at most one local host")
    return errors


def validate_document(document: Any, validator: Validator | None = None) -> tuple[str | None, list[str]]:
    """Validate a parsed document by its own `schema` field; return the schema id and the violations."""
    validator = validator or Validator()
    schema_id = document.get("schema") if isinstance(document, dict) else None
    if not isinstance(schema_id, str):
        return None, ["$: missing string property 'schema'"]
    if schema_id not in constants.SCHEMA_IDS:
        return schema_id, [f"$: unknown schema {schema_id!r}"]
    errors = validator.validate(document, schema_id)
    if not errors and schema_id == "gdv.plan/1":
        errors = check_plan_semantics(document)
    if not errors and schema_id == "gdv.result/1":
        errors = check_result_semantics(document)
    if not errors and schema_id == "gdv.hosts/1":
        errors = check_hosts_semantics(document)
    return schema_id, errors


def validate_file(path: Path, validator: Validator | None = None) -> tuple[str | None, list[str]]:
    """Validate a `.json` document, or every line of a `.jsonl` file.

    Raises `UnreadableInput` when the file cannot be read or decoded; malformed content is returned as violations."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise UnreadableInput(f"cannot read {path}: {error}") from error
    try:
        if path.suffix.lower() == ".jsonl":
            schema_id: str | None = None
            errors: list[str] = []
            # Split on \n only: str.splitlines() would also cut at U+2028, U+0085 and other characters that are legal inside a JSON string.
            for number, line in enumerate(text.split("\n"), 1):
                if not line.strip(" \t\r"):  # JSON whitespace only; other blank-looking lines are errors like in the C# reader
                    continue
                line_schema, line_errors = validate_document(parse_json(line), validator)
                schema_id = schema_id or line_schema
                errors.extend(f"line {number}: {message}" for message in line_errors)
            return schema_id, errors
        return validate_document(parse_json(text), validator)
    except RecursionError:
        return None, [f"{path}: nested too deeply"]
    except ValueError as error:  # JSONDecodeError is a ValueError
        return None, [f"{path}: invalid JSON: {error}"]
