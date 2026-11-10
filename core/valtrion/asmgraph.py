"""The assembly graph of a Unity project and of a changed git package: which test assemblies a change inside the package reaches.

`select` uses it for `byContent.assemblies` of a `jsonRules` key. A package change is mapped to the package assemblies it touches (the nearest
`.asmdef` above each changed file; the assembly an `.asmref` in a folder names for the files below it), and the project's test assemblies are the
ones that reach such an assembly through `references` (by name or by `GUID:`), directly or through other assemblies of the project or the
package. Whatever the graph cannot say is a `GraphError` and the caller keeps the broader kind (fail closed): a file that belongs to no
assembly, an assembly that cannot be read, a name or a GUID that two assemblies share, a change of a test assembly of the package itself.
The module has no dependency outside the standard library; the callers pass the readers of the files.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

GUID_PREFIX = "GUID:"
_META_GUID = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)
_FOLDER_META = re.compile(r"^folderAsset:\s*yes\s*$", re.MULTILINE)
#: Files that belong to no assembly and change no behavior: documents of a package.
_DOCUMENT_SUFFIXES = (".md", ".markdown", ".txt")
#: Files that reach assemblies other than the one whose folder they sit in (precompiled references, `csc.rsp`).
_PRECOMPILED_SUFFIXES = (".dll", ".so", ".a", ".bundle", ".dylib", ".jar", ".aar", ".rsp")
_DOCUMENT_STEMS = frozenset({"license", "licence", "changelog", "readme", "notice", "authors"})


class GraphError(Exception):
    """The graph cannot be used for this change; the message says why."""


@dataclass(frozen=True)
class Assembly:
    name: str
    #: The `.asmdef` file, as the caller knows it (only used in messages and to find the assembly of a file).
    path: str
    guid: str | None
    references: tuple[str, ...]
    #: Names of the packages whose version sets a define of this assembly (`versionDefines`).
    version_defines: tuple[str, ...]
    test: bool
    editor_only: bool

    @property
    def mode(self) -> str:
        """`editmode` for an assembly that exists only in the Editor, else `playmode`."""
        return "editmode" if self.editor_only else "playmode"


def _strings(document: Mapping[str, Any], key: str, path: str) -> list[str]:
    value = document.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GraphError(f"{path}: {key} is not a list of strings")
    return value


def guid_of_meta(data: bytes) -> str | None:
    match = _META_GUID.search(data.decode("utf-8", "replace"))
    return match.group(1).lower() if match else None


def is_folder_meta(data: bytes) -> bool:
    return bool(_FOLDER_META.search(data.decode("utf-8", "replace")))


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
    versions = document.get("versionDefines") or []
    if not isinstance(versions, list):
        raise GraphError(f"{path}: versionDefines is not a list")
    return Assembly(
        name=name, path=path, guid=guid, references=tuple(_strings(document, "references", path)),
        version_defines=tuple(sorted({item["name"] for item in versions if isinstance(item, dict) and isinstance(item.get("name"), str)})),
        test="UNITY_INCLUDE_TESTS" in _strings(document, "defineConstraints", path) or "TestAssemblies" in _strings(document, "optionalUnityReferences", path),
        editor_only=document.get("includePlatforms") == ["Editor"])


class Graph:
    """Assemblies by name and by GUID, and the edges of their `references`."""

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

    def dependents(self, seeds: Iterable[str]) -> dict[str, list[str]]:
        """Every assembly of the graph that is a seed or reaches one through `references`, with the shortest chain `[assembly, ..., seed]`
        (each one references the next). Ties are broken by name, so the result does not depend on the order of the files."""
        seed_names = sorted(set(seeds))
        reverse: dict[str, list[str]] = {}
        for assembly in self.assemblies:
            for reference in assembly.references:
                reverse.setdefault(self.target(reference), []).append(assembly.name)
        # A seed may be a name no assembly has any more (an assembly that was renamed): what still references it is reached all the same.
        next_step: dict[str, str | None] = {name: None for name in seed_names}
        queue = deque(seed_names)
        while queue:
            current = queue.popleft()
            for referrer in sorted(reverse.get(current, ())):
                if referrer not in next_step:
                    next_step[referrer] = current
                    queue.append(referrer)
        chains: dict[str, list[str]] = {}
        for name in next_step:
            if name not in self.by_name:
                continue
            chain = [name]
            while next_step[chain[-1]] is not None:
                chain.append(next_step[chain[-1]])  # type: ignore[arg-type]
            chains[name] = chain
        return chains


def _is_document(relative: str) -> bool:
    segments = relative.split("/")
    if any(segment.endswith("~") for segment in segments):
        return True  # a folder whose name ends in `~` (`Documentation~`, `Samples~`) is not imported by Unity
    name = segments[-1].lower()
    if name.endswith(".meta"):
        name = name[:-5]
    return name.endswith(_DOCUMENT_SUFFIXES) or name.split(".")[0] in _DOCUMENT_STEMS


def package_assemblies(tree: Sequence[str], read: Callable[[str], bytes]) -> list[Assembly]:
    """The assembly definitions of a package at one commit: `tree` lists its files (relative to the package folder), `read` returns one."""
    files = set(tree)
    found = []
    for path in sorted(item for item in files if item.endswith(".asmdef")):
        meta = path + ".meta"
        found.append(parse_definition(path, read(path), guid_of_meta(read(meta)) if meta in files else None))
    return found


def changed_assemblies(
        changes: Sequence[tuple[str, str]], tree: Sequence[str], package: Graph, read_new: Callable[[str], bytes],
        read_old: Callable[[str], bytes]) -> dict[str, list[str]]:
    """Assembly name -> the changed files of the package that belong to it. `changes` are (status, path) of the files that differ, `tree` the
    files of the new commit, `package` the graph of its assemblies. A renamed assembly is listed under both names.

    Raises `GraphError` for a file that belongs to no assembly (a document and the meta file of a folder do not), for an `.asmref` that names an
    assembly outside the package, for a GUID that changed and for precompiled code."""
    files = set(tree)
    definitions: dict[str, str] = {}
    references: dict[str, str] = {}
    for path in tree:
        folder, _, name = path.rpartition("/")
        if name.endswith(".asmdef"):
            definitions.setdefault(folder, path)
        elif name.endswith(".asmref"):
            references.setdefault(folder, path)
    by_path = {assembly.path: assembly for assembly in package.assemblies}
    changed: dict[str, list[str]] = {}

    def add(name: str, relative: str) -> None:
        listed = changed.setdefault(name, [])
        if relative not in listed:
            listed.append(relative)

    def referenced(path: str, data: bytes) -> str:
        reference = _json_document(path, data).get("reference")
        if not isinstance(reference, str) or not reference:
            raise GraphError(f"{path}: names no assembly")
        name = package.target(reference)
        if name not in package.by_name:
            raise GraphError(f"{path}: names {reference!r}, which is not an assembly of this package")
        return name

    for status, relative in changes:
        name = relative.rsplit("/", 1)[-1]
        if relative == "package.json":
            continue  # the judgement has accepted it: only members that say nothing about what ships
        if name.endswith(".asmdef"):
            current = by_path.get(relative)
            if current is None:
                raise GraphError(f"{relative}: the assembly definition is gone")
            add(current.name, relative)
            if status == "M":
                add(parse_definition(relative, read_old(relative), None).name, relative)
        elif name.endswith(".asmdef.meta"):
            current = by_path.get(relative[:-5])
            if current is None:
                raise GraphError(f"{relative}: the assembly definition is gone")
            if status != "M" or guid_of_meta(read_old(relative)) != guid_of_meta(read_new(relative)):
                raise GraphError(f"{relative}: the GUID of the assembly changed")
            add(current.name, relative)
        elif name.endswith((".asmref", ".asmref.meta")):
            if status != "M" and name.endswith(".asmref"):
                # Files below the folder move to or from the enclosing assembly, which would have to be changed too.
                raise GraphError(f"{relative}: an assembly reference was {'added' if status == 'A' else 'removed'}")
            path = relative[:-5] if name.endswith(".meta") else relative
            if path != relative:  # the meta file of an `.asmref`: the folder keeps the assembly the `.asmref` names
                sides = [read_new] if path in files else [read_old]
            else:
                sides = [read_new] if status == "A" else [read_old] if status == "D" else [read_new, read_old]
            for read in sides:
                add(referenced(path, read(path)), relative)
        elif name.lower().endswith(_PRECOMPILED_SUFFIXES):
            raise GraphError(f"{relative}: precompiled code or compiler options may be referenced by assemblies the graph does not link to it")
        else:
            if name.endswith(".meta") and is_folder_meta(read_old(relative) if status == "D" else read_new(relative)):
                continue
            folder = relative.rpartition("/")[0]
            owner = None
            while True:
                if folder in definitions:
                    owner = by_path[definitions[folder]].name
                    break
                if folder in references:
                    owner = referenced(references[folder], read_new(references[folder]))
                    break
                if not folder:
                    break
                folder = folder.rpartition("/")[0]
            if owner is None:
                if _is_document(relative):
                    continue
                raise GraphError(f"{relative}: belongs to no assembly")
            add(owner, relative)
    return changed


def narrow(
        project: Sequence[Assembly], packages: Mapping[str, Sequence[Assembly]], package_name: str, changed: Mapping[str, Sequence[str]],
        testables: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The test assemblies that reach a changed assembly of the package: `{assembly, mode, via, files}` sorted by name, `via` being the chain
    of references from the test assembly to the changed assembly. `project` are the assemblies of the project and `packages` those of every
    package the graph needs (the changed one, named `package_name`, and the ones that depend on it), by package name. A test assembly of the
    project is selectable; one of a package only when the package is in `testables` (otherwise the project never runs it). An assembly that
    sets a define from the version of the package (`versionDefines`) counts as changed too: the version moves with every pin.

    Raises `GraphError` when the sets of assemblies clash."""
    members = [assembly for items in packages.values() for assembly in items]
    graph = Graph([*project, *members])
    seeds: dict[str, list[str]] = {name: list(files) for name, files in changed.items()}
    for assembly in graph.assemblies:
        if package_name in assembly.version_defines:
            seeds.setdefault(assembly.name, []).append(f"(versionDefines of {package_name})")
    reached = graph.dependents(seeds)
    listed = set(testables)
    selectable = {assembly.name for assembly in project} | {assembly.name for name, items in packages.items() if name in listed for assembly in items}
    return [{"assembly": name, "mode": graph.by_name[name].mode, "via": chain, "files": seeds[chain[-1]]}
            for name, chain in sorted(reached.items()) if name in selectable and graph.by_name[name].test]
