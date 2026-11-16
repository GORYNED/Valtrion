"""Passing Editor test results of `valtrion unity run`, kept by a content key and given back while nothing they depend on has changed.

`plan` turns one Unity command into a key: a SHA-256 over the Editor (version, executable path, size and modification time), the OS,
the Valtrion Core version, the command (with `-projectPath`, `-logFile` and `-testResults` left out, paths inside the project written as
`<project>/...` and an argument that names an existing file replaced by the hash of its content), the selected test assemblies and,
by content, every file of those assemblies and of the assemblies they reach through `references` (source files, `.asmdef`/`.asmref`
and `.meta`), every precompiled file (`.dll`, `.so`, `.rsp`, ... and its `.meta`), `ProjectSettings/`, `Packages/manifest.json`,
`Packages/packages-lock.json` and the `package.json` of every local package. The files are found in `Assets`, in the embedded packages
of `Packages/` and in the `file:` packages of the manifest; a file belongs to the nearest folder above it that holds an `.asmdef` or an
`.asmref`. Whatever cannot be determined gives no key, and the run is not cached (fail closed).

The store holds one entry per key, `<valtrion home>/unity-result-cache/<key>.json`, shared by every working copy on the host. Only a
run whose result file is a `test-run` with result `Passed` and `failed="0"` is stored; the trust rules on reading are those of
`resultcache` (a regular file of this user, no link or junction, a size limit, the stored text must match its recorded hash).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from xml.etree import ElementTree

from . import __version__, core, resultcache
from .asmgraph import GUID_PREFIX, Graph, GraphError, guid_of_meta, parse_definition
from .providers.unity import support

FORMAT = "valtrion.unityresultcache/1"
FOLDER = "unity-result-cache"
#: An entry (and a result file) larger than this is neither written nor read.
MAX_ENTRY_BYTES = 32 << 20
#: The lease that says a key is being computed: `unity-result:<key>`.
MARKER_PREFIX = "unity-result:"
PROJECT_LABEL = "<project>"
#: Test platforms whose result depends only on the Editor and the project (a player platform builds and runs a player).
PLATFORMS = {"editmode": "EditMode", "playmode": "PlayMode"}
#: Options whose value says where output goes, not what is tested.
_OUTPUT_OPTIONS = ("-projectpath", "-logfile", "-testresults")
#: Files that reach assemblies other than the one whose folder they sit in (precompiled references, compiler options).
PRECOMPILED_SUFFIXES = (".dll", ".so", ".a", ".bundle", ".dylib", ".jar", ".aar", ".rsp")


class NoKey(Exception):
    """The command cannot be cached; the message says why."""


@dataclass(frozen=True)
class Plan:
    """The key of one command, or the reason why it has none (`key` None)."""
    key: str | None
    components: dict[str, Any]
    result_path: Path | None
    reason: str | None
    args: tuple[str, ...] = ()
    project: Path | None = None
    editor: str | None = None

    @property
    def marker(self) -> str | None:
        return MARKER_PREFIX + self.key if self.key else None


@dataclass(frozen=True)
class Hit:
    key: str
    stored_utc: str
    text: str
    path: Path


def cache_root(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / FOLDER


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def option_value(args: Sequence[str], name: str) -> str | None:
    found = None
    for position, item in enumerate(args[:-1]):
        if item.lower() == name.lower():
            found = args[position + 1]
    return found


def _absolute(text: str) -> Path:
    return Path(text).expanduser().absolute()


def assembly_names(args: Sequence[str]) -> list[str]:
    value = option_value(args, "-assemblyNames")
    return sorted({name.strip() for name in re.split(r"[;,]", value or "") if name.strip()})


def plan(args: Sequence[str], project: Path, editor: str) -> Plan:
    """The key of the Unity command `args` run on `project` with the Editor executable `editor`."""
    args = tuple(args)
    given = option_value(args, "-testResults")
    result_path = _absolute(given) if given else None
    try:
        components = _components(args, project, editor)
    except NoKey as error:
        return Plan(None, {}, result_path, str(error), args, project, editor)
    except GraphError as error:
        return Plan(None, {}, result_path, f"the assembly graph cannot be used: {error}", args, project, editor)
    except (OSError, ValueError) as error:
        return Plan(None, {}, result_path, f"an input cannot be read: {error}", args, project, editor)
    return Plan(core.sha256_bytes(_canonical(components)), components, result_path, None, args, project, editor)


def no_key(reason: str, args: Sequence[str] = ()) -> Plan:
    given = option_value(args, "-testResults")
    return Plan(None, {}, _absolute(given) if given else None, reason, tuple(args))


def _components(args: Sequence[str], project: Path, editor: str) -> dict[str, Any]:
    lowered = [item.lower() for item in args]
    if "-runtests" not in lowered:
        raise NoKey("the command runs no tests (-runTests)")
    if "-editortestsresultfile" in lowered:
        raise NoKey("-editorTestsResultFile is not cached; name the result file with -testResults")
    if option_value(args, "-testResults") is None:
        raise NoKey("the command names no -testResults file")
    platform = option_value(args, "-testPlatform") or "EditMode"  # Unity's default
    if platform.lower() not in PLATFORMS:
        raise NoKey(f"the test platform {platform} is not cached (only EditMode and PlayMode)")
    selected = assembly_names(args)
    if not selected:
        raise NoKey("the command selects no assemblies (-assemblyNames); a run of every assembly is not cached")
    project = Path(project)
    if not project.is_dir():
        raise NoKey(f"the Unity project folder {project} does not exist")
    info = os.stat(editor)
    editor_part = {"version": support.project_unity_version(project), "path": str(editor), "size": info.st_size,
                   "mtimeNs": info.st_mtime_ns}
    scan = _Scan(project)
    graph = Graph(scan.assemblies)
    for name in selected:
        if name not in graph.by_name:
            raise NoKey(f"the assembly {name} is not defined in Assets, an embedded package or a local (file:) package")
    closure = _closure(graph, selected)
    files: dict[str, str] = {}
    for item in scan.files:
        owner = scan.owner(item.folder, graph)
        if item.precompiled or (owner is not None and owner in closure):
            files[item.label] = core.sha256_file(item.path)
    for label, path in scan.extra:
        files[label] = core.sha256_file(path) if path.exists() else "missing"
    return {"format": FORMAT, "coreVersion": __version__, "os": core.local_os(), "editor": editor_part,
            "testPlatform": PLATFORMS[platform.lower()], "assemblies": selected, "closure": sorted(closure),
            "command": _command(args, project), "files": files}


def _closure(graph: Graph, selected: Sequence[str]) -> set[str]:
    """The selected assemblies and every assembly they reach through `references` (names outside the graph are kept as they are:
    a folder of the project can still extend them with an `.asmref`)."""
    seen: set[str] = set()
    stack = list(selected)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        assembly = graph.by_name.get(name)
        if assembly is not None:
            stack.extend(_target(graph, reference) for reference in assembly.references)
    return seen


def _target(graph: Graph, reference: str) -> str:
    name = graph.target(reference)
    return name.lower() if name[:len(GUID_PREFIX)].upper() == GUID_PREFIX else name


def _ignored(name: str) -> bool:
    """What Unity does not import: hidden names, names ending in `~`, `cvs` and `.tmp` files."""
    return name.startswith(".") or name.endswith("~") or name.lower() == "cvs" or name.lower().endswith(".tmp")


def _precompiled(relative: str) -> bool:
    parts = relative.split("/")
    if parts[-1].lower().endswith(".meta"):
        parts[-1] = parts[-1][:-5]
    return any(part.lower().endswith(PRECOMPILED_SUFFIXES) for part in parts)


def _fail(error: OSError) -> None:
    raise error


def _inside(path: Path, project_real: str) -> str | None:
    """`<project>/relative` for a path inside the project, else None."""
    real = os.path.realpath(path)
    try:
        relative = os.path.relpath(real, project_real)
    except ValueError:  # another drive
        return None
    if relative == ".":
        return PROJECT_LABEL
    if relative.startswith("..") or os.path.isabs(relative):
        return None
    return f"{PROJECT_LABEL}/{Path(relative).as_posix()}"


@dataclass(frozen=True)
class _File:
    label: str
    path: Path
    #: (root number, folder relative to the root).
    folder: tuple[int, str]
    precompiled: bool


@dataclass
class _Scan:
    """The files and assembly definitions of the project's `Assets`, its embedded packages and its `file:` packages."""
    project: Path
    files: list[_File] = field(default_factory=list)
    assemblies: list = field(default_factory=list)
    #: Files that are always in the key: (label, path); a missing one counts as `missing`.
    extra: list[tuple[str, Path]] = field(default_factory=list)
    _definitions: dict[tuple[int, str], str] = field(default_factory=dict)
    _references: dict[tuple[int, str], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        project_real = os.path.realpath(self.project)
        roots: list[tuple[str, Path]] = []
        if (self.project / "Assets").is_dir():
            roots.append((f"{PROJECT_LABEL}/Assets", self.project / "Assets"))
        packages = self.project / "Packages"
        manifest = packages / "manifest.json"
        self.extra += [(f"{PROJECT_LABEL}/Packages/manifest.json", manifest),
                       (f"{PROJECT_LABEL}/Packages/packages-lock.json", packages / "packages-lock.json")]
        if packages.is_dir():
            for child in sorted(packages.iterdir(), key=lambda item: item.name):
                if child.is_dir() and not _ignored(child.name) and (child / "package.json").is_file():
                    roots.append((f"{PROJECT_LABEL}/Packages/{child.name}", child))
        for name, location in self._local_packages(manifest):
            label = _inside(location, project_real) or f"<package:{name}>"
            if location.is_dir():
                roots.append((label, location))
            elif location.is_file():
                self.extra.append((label, location))  # a tarball: its assemblies are not read, its content is in the key
            else:
                raise NoKey(f"the local package {name} at {location} does not exist")
        settings = self.project / "ProjectSettings"
        if not settings.is_dir():
            raise NoKey("the project has no ProjectSettings folder")
        for dirpath, dirnames, filenames in os.walk(settings, followlinks=True, onerror=_fail):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                self.extra.append((f"{PROJECT_LABEL}/{Path(os.path.relpath(path, self.project)).as_posix()}", path))
        visited: set[str] = set()
        for number, (label, root) in enumerate(roots):
            if (root / "package.json").is_file():
                self.extra.append((f"{label}/package.json", root / "package.json"))
            for dirpath, dirnames, filenames in os.walk(root, followlinks=True, onerror=_fail):
                real = os.path.normcase(os.path.realpath(dirpath))
                if real in visited:  # a link back up, or a folder already reached through another root
                    dirnames[:] = []
                    continue
                visited.add(real)
                dirnames[:] = sorted(name for name in dirnames if not _ignored(name))
                relative_dir = Path(os.path.relpath(dirpath, root)).as_posix()
                relative_dir = "" if relative_dir == "." else relative_dir
                folder = (number, relative_dir)
                for name in sorted(item for item in filenames if not _ignored(item)):
                    path = Path(dirpath) / name
                    relative = f"{relative_dir}/{name}" if relative_dir else name
                    self.files.append(_File(f"{label}/{relative}", path, folder, _precompiled(relative)))
                    if name.endswith(".asmdef"):
                        if folder in self._definitions:
                            raise GraphError(f"{label}/{relative_dir}: holds more than one assembly definition")
                        meta = path.with_name(name + ".meta")
                        guid = guid_of_meta(meta.read_bytes()) if meta.is_file() else None
                        assembly = parse_definition(f"{label}/{relative}", path.read_bytes(), guid)
                        self.assemblies.append(assembly)
                        self._definitions[folder] = assembly.name
                    elif name.endswith(".asmref"):
                        self._references[folder] = self._asmref(f"{label}/{relative}", path.read_bytes())

    @staticmethod
    def _local_packages(manifest: Path) -> list[tuple[str, Path]]:
        if not manifest.exists():
            return []
        try:
            document = json.loads(manifest.read_bytes().decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise NoKey(f"Packages/manifest.json cannot be read: {error}") from error
        dependencies = document.get("dependencies") if isinstance(document, dict) else None
        if not isinstance(dependencies, dict):
            return []
        found = []
        for name, value in sorted(dependencies.items()):
            if isinstance(value, str) and value.startswith("file:"):
                location = Path(value[len("file:"):]).expanduser()
                found.append((name, location if location.is_absolute() else manifest.parent / location))
        return found

    @staticmethod
    def _asmref(path: str, data: bytes) -> str:
        try:
            document = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise GraphError(f"{path}: cannot be read: {error}") from error
        reference = document.get("reference") if isinstance(document, dict) else None
        if not isinstance(reference, str) or not reference:
            raise GraphError(f"{path}: names no assembly")
        return reference

    def owner(self, folder: tuple[int, str], graph: Graph) -> str | None:
        """The assembly the files of `folder` belong to, or None (the predefined assemblies, or no code at all)."""
        number, relative = folder
        while True:
            key = (number, relative)
            if key in self._definitions:
                return self._definitions[key]
            if key in self._references:
                return _target(graph, self._references[key])
            if not relative:
                return None
            relative = relative.rpartition("/")[0]


def _command(args: Sequence[str], project: Path) -> list[str]:
    project_real = os.path.realpath(project)
    result: list[str] = []
    skip = False
    for item in args:
        if skip:
            skip = False
            continue
        if item.lower() in _OUTPUT_OPTIONS:
            skip = True
            continue
        if item.lower() == "-assemblynames":
            result.append(item)
            skip = True
            result.append(";".join(assembly_names(args)))
            continue
        result.append(_argument(item, project_real))
    return result


def _argument(item: str, project_real: str) -> str:
    if not item or item.startswith("-"):
        return item
    try:
        path = _absolute(item)
        is_file, is_dir = path.is_file(), path.is_dir()
    except (OSError, ValueError):
        return item
    if not (is_file or is_dir):
        return item
    inside = _inside(path, project_real)
    if is_file:
        return f"{inside or 'file'}#sha256:{core.sha256_file(path)}"
    return inside or item


# -- the result file ---------------------------------------------------------------------------------------------------------------


def failure(text: str) -> str | None:
    """Why `text` is not the result file of a passing test run, or None."""
    lowered = text[:4096].lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        return "the result file declares a document type"
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        return f"the result file is not readable XML ({error})"
    if root.tag != "test-run":
        return f"the result file is a <{root.tag}>, not a <test-run>"
    if root.get("result") != "Passed":
        return f"the test run is {root.get('result')!r}, not 'Passed'"
    if root.get("failed") != "0":
        return f"the test run has failed={root.get('failed')!r}"
    return None


def read_result(path: Path) -> tuple[str | None, str | None]:
    """(text, None) of a result file, or (None, why it cannot be used)."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ENTRY_BYTES + 1)
    except OSError as error:
        return None, f"the result file cannot be read ({error})"
    if len(data) > MAX_ENTRY_BYTES:
        return None, f"the result file is larger than {MAX_ENTRY_BYTES} bytes"
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "the result file is not UTF-8"


# -- the store ---------------------------------------------------------------------------------------------------------------------


def store(root: Path, entry_plan: Plan, text: str, clock: Callable[[], str] = core.utc_now) -> tuple[Path | None, str]:
    """Keep the passing result `text` under the key of `entry_plan`. Returns (the entry, "stored") or (None, why not)."""
    if not entry_plan.key:
        return None, entry_plan.reason or "no key"
    why = failure(text)
    if why is not None:
        return None, why
    entry = {"format": FORMAT, "key": entry_plan.key, "components": entry_plan.components, "storedUtc": clock(),
             "resultSha256": core.sha256_bytes(text.encode("utf-8")), "result": text}
    data = core.to_json(entry).encode("utf-8")
    if len(data) > MAX_ENTRY_BYTES:
        return None, f"the entry would be larger than {MAX_ENTRY_BYTES} bytes"
    resultcache._ensure_root(root)
    problem = resultcache._untrusted(root, folder=True)
    if problem is not None:
        return None, f"the cache folder is not used: {problem}"
    path = root / f"{entry_plan.key}.json"
    resultcache._write_private(path, data)
    try:
        resultcache.gc(root, protect=(entry_plan.key,))
    except (OSError, ValueError):
        pass
    return path, "stored"


def lookup(root: Path, entry_plan: Plan, touch: bool = True) -> tuple[Hit | None, str]:
    """The trusted passing result for the key of `entry_plan`, or (None, why it is a miss)."""
    key = entry_plan.key
    if not key:
        return None, entry_plan.reason or "no key"
    problem = resultcache._untrusted(root, folder=True)
    if problem == "no entry":
        return None, "no cached result"
    if problem is not None:
        return None, f"the cache folder is ignored: {problem}"
    path = root / f"{key}.json"
    problem = resultcache._untrusted(path, limit=MAX_ENTRY_BYTES)
    if problem == "no entry":
        return None, "no cached result"
    if problem is not None:
        return None, f"the cached entry is ignored: {problem}"
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ENTRY_BYTES + 1)
        entry = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        return None, f"the cached entry is ignored: unreadable ({type(error).__name__})"
    if len(data) > MAX_ENTRY_BYTES:
        return None, "the cached entry is ignored: it is too large"
    if not isinstance(entry, dict) or entry.get("format") != FORMAT:
        return None, "the cached entry is ignored: not a Unity result cache entry"
    if entry.get("key") != key or entry.get("components") != entry_plan.components:
        return None, "the cached entry is ignored: it was stored for another key"
    text, stored = entry.get("result"), entry.get("storedUtc")
    if not isinstance(text, str) or entry.get("resultSha256") != core.sha256_bytes(text.encode("utf-8")):
        return None, "the cached entry is ignored: the stored result does not match its recorded hash"
    why = failure(text)
    if why is not None:
        return None, f"the cached entry is ignored: {why}"
    if not isinstance(stored, str) or not stored:
        return None, "the cached entry is ignored: it does not say when it was stored"
    if touch:
        try:
            os.utime(path)  # a use keeps the entry from `gc`
        except OSError:
            pass
    return Hit(key, stored, text, path), "hit"
