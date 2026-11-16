"""Passing Editor test results of `valtrion unity run`, kept by a content key and given back while nothing they depend on has changed.

`plan` turns one Unity command into a key: a SHA-256 over the Editor (version, executable path, size and modification time), the OS,
the Valtrion Core version, the command (with `-projectPath`, `-logFile` and `-testResults` left out, paths inside the project written as
`<project>/...` and an argument that names an existing file replaced by the hash of its content; it must name `-buildTarget`, which
`unity run` adds from host.json `unityBuildTarget` when the command has none), the selected test assemblies (every assembly of the
project when the command names no `-assemblyNames`, for example a run that selects by `-testFilter` only) and, by content, every file of those assemblies and of the assemblies they reach through `references`, the
code of the whole project (every `.cs`, `.asmdef` and `.asmref` and its `.meta`: everything compiles with the run), every precompiled
file (`.dll`, `.so`, `.rsp`, ... and its `.meta`), `ProjectSettings/`, `Packages/manifest.json`,
`Packages/packages-lock.json` and the `package.json` of every local package. The files are found in `Assets`, in the embedded packages
of `Packages/` and in the `file:` packages of the manifest; a file belongs to the nearest folder above it that holds an `.asmdef` or an
`.asmref`. Whatever cannot be determined gives no key, and the run is not cached (fail closed).

The store holds one entry per key, `<valtrion home>/unity-result-cache/<key>.json`, shared by every working copy on the host. Only a
run whose result file is a `test-run` with result `Passed` or `Skipped` (ignored tests), `failed="0"`, no inconclusive test and a
passed test is stored; the trust rules on reading are those of
`resultcache` (a regular file of this user, no link or junction, a size limit, the stored text must match its recorded hash).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from xml.parsers import expat

from . import __version__, core, nowindow, resultcache
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
#: Inputs of the compilation of every assembly of the project, kept in the key wherever they are.
COMPILED_SUFFIXES = (".cs", ".asmdef", ".asmref")
#: The store keeps at most this many entries and this many bytes (the least recently used go first).
MAX_ENTRIES = 200
MAX_STORE_BYTES = 512 << 20


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
    except Exception as error:  # noqa: BLE001 - whatever else goes wrong means no key (fail closed), never a run without a status
        return Plan(None, {}, result_path, f"the key cannot be computed ({type(error).__name__}: {error})", args, project, editor)
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
    results = option_value(args, "-testResults")
    if results is None:
        raise NoKey("the command names no -testResults file")
    if not Path(results).expanduser().is_absolute():
        raise NoKey("a relative -testResults path is not cached; give an absolute path")
    platform = option_value(args, "-testPlatform") or "EditMode"  # Unity's default
    if platform.lower() not in PLATFORMS:
        raise NoKey(f"the test platform {platform} is not cached (only EditMode and PlayMode)")
    if not option_value(args, "-buildTarget"):
        # Without it the active platform of the project's Library decides the defines (UNITY_ANDROID, ...) and Application.platform.
        raise NoKey("the command names no -buildTarget, so the active build platform is unknown; name -buildTarget to cache")
    selected = assembly_names(args)
    project = Path(project)
    if not project.is_dir():
        raise NoKey(f"the Unity project folder {project} does not exist")
    info = os.stat(editor)  # the executable itself (inside the .app on macOS), not the bundle folder
    editor_part = {"version": support.project_unity_version(project), "path": str(editor), "size": info.st_size,
                   "mtimeNs": info.st_mtime_ns, "fileVersion": editor_version(editor)}
    scan = _Scan(project)
    graph = Graph(scan.assemblies)
    for name in selected:
        if name not in graph.by_name:
            raise NoKey(f"the assembly {name} is not defined in Assets, an embedded package or a local (file:) package")
    # Without -assemblyNames Unity runs every test assembly it finds (a -testFilter in the command, which is part of the key, may pick
    # some tests of them), the predefined ones (Assembly-CSharp-Editor and the like) too: every scanned file of the project is in the key,
    # scenes and assets outside any assembly folder included. Registry and git packages are keyed by the manifest and lock files.
    closure = _closure(graph, selected) if selected else set(graph.by_name)
    files: dict[str, str] = {}
    for item in scan.files:
        owner = scan.owner(item.folder, graph)
        # The compiler inputs of the whole project count too: a compile error anywhere fails the run, and initialization code
        # ([InitializeOnLoad], [RuntimeInitializeOnLoadMethod]) of any assembly runs during the tests.
        if not selected or item.precompiled or item.compiled or (owner is not None and owner in closure):
            files[item.label] = core.sha256_file(item.path)
    settings_text: dict[str, str] = {}
    for label, path in scan.extra:
        files[label] = core.sha256_file(path) if path.exists() else "missing"
        if "/ProjectSettings/" in label and path.is_file() and path.stat().st_size <= _TEXT_LIMIT:
            # The same file with `\n` line endings: tells "Unity re-saved a CRLF checkout with LF" from a real change (`changed_inputs`).
            settings_text[label] = core.sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    return {"format": FORMAT, "coreVersion": __version__, "os": core.local_os(), "editor": editor_part,
            "testPlatform": PLATFORMS[platform.lower()], "assemblies": selected, "closure": sorted(closure),
            "command": _command(args, project), "files": files, "settingsText": settings_text}


LIBRARIES = "libraries"
FIRST_RUN_HINT = ("this was the first run Valtrion saw with this working copy's Library (new or rebuilt), when Unity often re-saves "
                  "assets and settings; the next identical run usually stores")


def library_identity(project: Path) -> str | None:
    """Which `Library` folder the project has: its file id and creation time (a rebuilt Library is a new folder); None without one."""
    try:
        info = os.stat(Path(project) / "Library")
    except OSError:
        return None
    born = getattr(info, "st_birthtime", None) or (info.st_ctime if os.name == "nt" else None)
    return f"{info.st_dev}:{info.st_ino}:{born}"


def _library_record(root: Path, project: Path) -> Path:
    name = core.sha256_bytes(os.path.normcase(os.path.realpath(project)).encode("utf-8", "surrogateescape"))
    return Path(root) / LIBRARIES / f"{name}.txt"


def first_with_library(root: Path, project: Path) -> bool:
    """True when no run of this project was seen with its current Library: none recorded, or the Library is another folder now."""
    current = library_identity(project)
    try:
        return _library_record(root, project).read_text(encoding="utf-8").strip() != (current or "")
    except OSError:
        return True


def remember_library(root: Path, project: Path) -> None:
    """Record the project's current Library after a run (best effort: a failure only makes the next run look like a first one)."""
    current = library_identity(project)
    if current is None:
        return
    record = _library_record(root, project)
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(current + "\n", encoding="utf-8")
    except OSError:
        pass


_TEXT_LIMIT = 4 * 1024 * 1024
_EOL_HINT = ("Unity re-saved them with LF line endings: give them LF in the checkout, for example `ProjectSettings/**/*.asset text eol=lf` "
             "in the project's .gitattributes")


def changed_inputs(before: Plan, after: Plan) -> str:
    """Why `after` (the key computed again once Unity ended) differs from `before`: the inputs that changed, and a hint when the only change of
    project settings files is their line endings."""
    if after.key is None:
        return after.reason or "the inputs cannot be read after the run"
    old, new = before.components.get("files", {}), after.components.get("files", {})
    changed = sorted(label for label in set(old) | set(new) if old.get(label) != new.get(label))
    if not changed:
        return "the inputs changed while Unity ran"
    shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
    old_text, new_text = before.components.get("settingsText", {}), after.components.get("settingsText", {})
    eol_only = [label for label in changed if label in old_text and old_text.get(label) == new_text.get(label)]
    if eol_only and len(eol_only) == len(changed):
        return f"the inputs changed while Unity ran: {shown}; only their line endings changed, {_EOL_HINT}"
    return f"the inputs changed while Unity ran: {shown}" + (f" ({', '.join(eol_only[:5])}: line endings only; {_EOL_HINT})" if eol_only else "")


def editor_version(executable: str | Path) -> str | None:
    """The version the Editor executable states: `CFBundleVersion` of the `.app` it lies in (macOS) or the file version resource
    (Windows). None when the executable carries no version (no `.app`, no resource, Linux): the path, size and modification time still
    identify it. A version source that exists but cannot be read raises `NoKey` (fail closed)."""
    path = Path(executable)
    for parent in path.parents:
        if parent.suffix == ".app":
            try:
                with open(parent / "Contents" / "Info.plist", "rb") as handle:
                    info = plistlib.load(handle)
                value = info.get("CFBundleVersion") or info.get("CFBundleShortVersionString")
            except Exception as error:  # noqa: BLE001 - a truncated or odd plist raises expat, attribute or value errors
                raise NoKey(f"the version of the Editor cannot be read from {parent.name}/Contents/Info.plist "
                            f"({type(error).__name__})") from error
            if not isinstance(value, str) or not value:
                raise NoKey(f"{parent.name}/Contents/Info.plist states no CFBundleVersion")
            return value
    if os.name == "nt":
        try:
            return _windows_file_version(path)
        except Exception as error:  # noqa: BLE001 - ctypes.ArgumentError and the like
            raise NoKey(f"the file version of the Editor cannot be read ({type(error).__name__})") from error
    return None


def _windows_file_version(path: Path) -> str | None:  # pragma: no cover - needs Windows
    """The file version resource of `path`, or None when it has none; raises when it has one that cannot be read."""
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise OSError("GetFileVersionInfoW failed")
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 16:
        return None
    signature, _structure, high, low = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 4)).contents
    if signature != 0xFEEF04BD:
        raise ValueError("the version resource has no fixed file information")
    return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"


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


def _compiled(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(".meta"):
        lowered = lowered[:-5]
    return lowered.endswith(COMPILED_SUFFIXES)


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
class Ignored:
    """The files and folders of a project that git ignores (`git ls-files --others --ignored --exclude-standard --directory`), as
    normalized absolute paths; empty when the project is not in a git working copy or git cannot say."""
    files: frozenset[str] = frozenset()
    folders: tuple[str, ...] = ()

    def covers(self, path: Path) -> bool:
        text = os.path.normcase(os.path.abspath(path))
        return text in self.files or any(text.startswith(folder) for folder in self.folders)


_GIT_TIMEOUT_SEC = 60


def git_ignored(project: Path) -> Ignored:
    """What git ignores under the project's `Assets` and `Packages`. Any failure means nothing is left out (the key keeps every file)."""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    try:
        completed = subprocess.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", "--",
                                    "Assets", "Packages"], cwd=project, capture_output=True, timeout=_GIT_TIMEOUT_SEC, check=False,
                                   env=environment, **nowindow.OPTIONS)
    except (OSError, subprocess.SubprocessError):
        return Ignored()
    if completed.returncode != 0:
        return Ignored()
    files, folders = set(), []
    for item in completed.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not item:
            continue
        absolute = os.path.normcase(os.path.abspath(Path(project) / item))
        if item.endswith("/"):
            folders.append(absolute.rstrip("\\/") + os.sep)
        else:
            files.add(absolute)
    return Ignored(frozenset(files), tuple(folders))


@dataclass(frozen=True)
class _File:
    label: str
    path: Path
    #: (root number, folder relative to the root).
    folder: tuple[int, str]
    precompiled: bool
    #: A source file or an assembly definition (or its `.meta`): an input of the compilation of the whole project.
    compiled: bool = False


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
        ignored = git_ignored(self.project)
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
        # Not UserSettings/: it belongs to one working copy (usually not in git) and the Editor rewrites it as it runs.
        visited_settings: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(settings, followlinks=True, onerror=_fail):
            real = os.path.normcase(os.path.realpath(dirpath))
            if real in visited_settings:
                dirnames[:] = []
                continue
            visited_settings.add(real)
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
                    precompiled, compiled = _precompiled(relative), _compiled(name)
                    # A file git ignores is build output (a generated link.xml, say) that may come and go while Unity runs; code and
                    # precompiled files stay in the key even when ignored, as they compile with the run.
                    if not (precompiled or compiled) and ignored.covers(path):
                        continue
                    self.files.append(_File(f"{label}/{relative}", path, folder, precompiled, compiled))
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
        if not (manifest.parent / "packages-lock.json").exists():
            moving = sorted(name for name, value in dependencies.items() if isinstance(value, str) and ("://" in value or value.startswith("git@")))
            if moving:
                # Without a lock file a git or URL dependency is whatever its branch holds today: its content is in no file of the key.
                raise NoKey(f"Packages/packages-lock.json does not exist and {moving[0]} is a git or URL dependency, so its version is unknown")
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
    for position, item in enumerate(args):
        if skip:
            skip = False
            continue
        if item.lower() in _OUTPUT_OPTIONS:
            following = args[position + 1] if position + 1 < len(args) else None
            # Its value, unless it has none (`-logFile` as the last argument, or followed by the next option): that one stays.
            skip = following is not None and (following == "-" or not following.startswith("-"))
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
    """Why `text` is not the result file of a passing test run, or None. A document type or an entity declaration anywhere is
    refused before it is expanded (expat calls the handler when it reads the declaration)."""
    root: list[tuple[str, dict]] = []
    parser = expat.ParserCreate()

    def refuse(*_: object) -> None:
        raise _Declared()

    def element(name: str, attributes: dict) -> None:
        if not root:
            root.append((name, attributes))

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = element
    try:
        parser.Parse(text, True)
    except _Declared:
        return "the result file declares a document type or an entity"
    except expat.ExpatError as error:
        return f"the result file is not readable XML ({error})"
    if not root:
        return "the result file has no element"
    tag, attributes = root[0]
    if tag != "test-run":
        return f"the result file is a <{tag}>, not a <test-run>"
    # NUnit reports a run whose only non-passing tests are ignored as `Skipped:Ignored`: that is a pass when nothing failed and some test passed.
    result = attributes.get("result") or ""
    if result.split(":", 1)[0] not in ("Passed", "Skipped"):
        return f"the test run is {result!r}, not 'Passed'"
    if attributes.get("failed") != "0":
        return f"the test run has failed={attributes.get('failed')!r}"
    if attributes.get("inconclusive", "0") != "0":
        return f"the test run has inconclusive={attributes.get('inconclusive')!r}"
    passed = attributes.get("passed", "")
    if not passed.isdigit() or int(passed) < 1:
        return f"the test run has passed={passed!r}: no test passed"
    return None


class _Declared(Exception):
    pass


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
        resultcache.gc(root, max_entries=MAX_ENTRIES, protect=(entry_plan.key,))
        _limit_bytes(root, MAX_STORE_BYTES, entry_plan.key)
    except (OSError, ValueError):
        pass
    return path, "stored"


def _limit_bytes(root: Path, limit: int, protect: str) -> list[str]:
    """Remove the least recently used entries until the store holds at most `limit` bytes. Returns the removed file names."""
    entries = []
    for path in root.glob("*.json"):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        entries.append((info.st_mtime, info.st_size, path))
    total = sum(size for _, size, _ in entries)
    removed = []
    for _, size, path in sorted(entries, key=lambda item: item[0]):
        if total <= limit:
            break
        if path.stem == protect:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path.name)
    return removed


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
