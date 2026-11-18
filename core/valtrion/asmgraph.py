"""The assembly graph of a Unity project: assembly definitions by name and by GUID, and the edges of their `references`.

The Unity result cache (`unitycache`) uses it to find the folders of the assemblies a test run names and of the assemblies they reference.
Whatever the graph cannot say is a `GraphError`, and the caller then does not cache the run: an assembly that cannot be read, a name or a GUID
that two assemblies share, a reference by GUID that cannot be resolved. The module has no dependency outside the standard library.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

GUID_PREFIX = "GUID:"
_META_GUID = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)


class GraphError(Exception):
    """The graph cannot be used; the message says why."""


@dataclass(frozen=True)
class Assembly:
    name: str
    #: The `.asmdef` file, as the caller knows it (used in messages).
    path: str
    guid: str | None
    references: tuple[str, ...]


def _strings(document: Mapping[str, Any], key: str, path: str) -> list[str]:
    value = document.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GraphError(f"{path}: {key} is not a list of strings")
    return value


def guid_of_meta(data: bytes) -> str | None:
    match = _META_GUID.search(data.decode("utf-8", "replace"))
    return match.group(1).lower() if match else None


def _json_document(path: str, data: bytes) -> dict[str, Any]:
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise GraphError(f"{path}: cannot be read: {error}") from error
    if not isinstance(document, dict):
        raise GraphError(f"{path}: not an object")
    return document


def parse_definition(path: str, data: bytes, guid: str | None) -> Assembly:
    """An assembly definition. Raises `GraphError` when it is not a readable object with a name."""
    document = _json_document(path, data)
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise GraphError(f"{path}: has no name")
    return Assembly(name=name, path=path, guid=guid, references=tuple(_strings(document, "references", path)))


class Graph:
    """Assemblies by name and by GUID."""

    def __init__(self, assemblies: Iterable[Assembly]) -> None:
        self.assemblies = list(assemblies)
        self.by_name: dict[str, Assembly] = {}
        self.by_guid: dict[str, Assembly] = {}
        for assembly in self.assemblies:
            other = self.by_name.setdefault(assembly.name, assembly)
            if other is not assembly:
                raise GraphError(f"the assembly {assembly.name!r} is defined in {other.path} and in {assembly.path}")
            if assembly.guid is not None:
                other = self.by_guid.setdefault(assembly.guid, assembly)
                if other is not assembly:
                    raise GraphError(f"the GUID of {assembly.path} is also the GUID of {other.path}")
        self._missing_guid = [assembly.path for assembly in self.assemblies if assembly.guid is None]

    def target(self, reference: str) -> str:
        """The assembly name a `references` entry names. A `GUID:` that no assembly of the graph has belongs to an assembly outside it (another
        package, the engine), which the graph does not follow, and is returned as it is; with an assembly whose `.meta` could not be read it
        might be that assembly, so the graph cannot be used."""
        if reference[:len(GUID_PREFIX)].upper() == GUID_PREFIX:
            guid = reference[len(GUID_PREFIX):].lower()
            found = self.by_guid.get(guid)
            if found is not None:
                return found.name
            if self._missing_guid:
                raise GraphError(f"the reference {reference} cannot be resolved: {self._missing_guid[0]} has no readable .meta file with a GUID")
            return reference
        return reference
