"""Classify a change of a git dependency by what changed inside it (`byContent` of a `jsonRules` key).

A dependency file (a manifest, a lock file) says which package moved, not what moved in it. For a git dependency the lock file pins the
commit, so Core can ask the package's repository for the files that changed between the old and the new commit and judge them: a change
that touches only managed code, Editor code, tests or documentation is `managed`; any native, platform, shader, linking or unknown file, a
platform define, a native import or reflection call in runtime code, a platform or define change of an assembly definition, or a change of
the package's own dependencies is `platform`. It classifies a change; it proves no behavior. Whatever cannot be read (not a git dependency,
no network, a commit that cannot be fetched, a time-out, a change too large to read) is unresolved and the caller keeps the key's own kinds
(fail closed). Native, shader and linking files and the folders `Plugins`, `Android`, `iOS`, `WebGL` and `StreamingAssets` are `platform`
whatever the project's map says: that floor is built in.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote

from . import nowindow, presets

FETCH_TIMEOUT_SEC = 120
GIT_TIMEOUT_SEC = 60
#: More changed files than this are not read one by one: the change is `platform`.
MAX_FILES = 500

#: `package.json` members whose change says nothing about what ships.
HARMLESS_PACKAGE_KEYS = frozenset({"version", "displayName", "description", "documentationUrl", "changelogUrl", "licensesUrl", "keywords", "author", "samples"})
#: `asmdef` members that decide on which platforms the assembly exists and under which defines.
ASMDEF_PLATFORM_KEYS = ("includePlatforms", "excludePlatforms", "defineConstraints", "versionDefines", "noEngineReferences", "allowUnsafeCode",
                        "overrideReferences", "precompiledReferences")
#: A preprocessor line that tests a platform define. A file that has one, in the old or the new text, is platform code in all of its body.
PLATFORM_DEFINE = re.compile(
    r"^\s*#\s*(?:if|elif)\b.*\bUNITY_(?:ANDROID|IOS|IPHONE|TVOS|VISIONOS|STANDALONE|WEBGL|WSA|SWITCH|PS4|PS5|XBOXONE|GAMECORE|STADIA|TIZEN|LUMIN|LINUX|EMBEDDED_LINUX|QNX)\w*\b")
#: Code that behaves differently under IL2CPP stripping or that calls native code.
RISKY_API = re.compile(r"\bDllImport\b|\bextern\b|System\.Reflection|\bActivator\.CreateInstance\b|\bMakeGenericType\b|\bMakeGenericMethod\b|\bType\.GetType\b"
                       r"|\bAndroidJavaClass\b|\bAndroidJavaObject\b|\bMarshal\.|\bNativeLibrary\b|\bBurstCompile\b")
#: Built in, whatever the map says: a file of these kinds or in these folders is platform content.
FLOOR_EXTENSIONS = frozenset(presets.NATIVE_EXTENSIONS) | frozenset(presets.SHADER_EXTENSIONS)
FLOOR_FOLDERS = frozenset({"plugins", "android", "ios", "webgl", "streamingassets"})
FLOOR_NAMES = frozenset({"link.xml"})
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_REASONS = 5
#: Transports git may use for the fetch: the allow-list of `GIT_ALLOW_PROTOCOL` (no `ext::` and the like).
_ALLOWED_PROTOCOLS = "file:https:ssh:git"
#: Variables that would send git to another repository than the throw-away one.
_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
                         "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")


class Unresolved(Exception):
    """The content of the change could not be read; the message says why."""


@dataclass(frozen=True)
class Source:
    url: str
    subpath: str
    commit: str


def _usable_url(url: str) -> bool:
    return bool(url) and not url.startswith("-") and len(url) < 2048 and all(ord(char) >= 32 and char != "\x7f" for char in url)


def entry_source(entry: Any) -> Source | None:
    """The git repository, folder and commit a lock-file entry pins, or None when the entry is not a git dependency with a commit and a usable URL."""
    if not isinstance(entry, Mapping) or entry.get("source") != "git":
        return None
    version, commit = entry.get("version"), entry.get("hash")
    if not isinstance(version, str) or not isinstance(commit, str) or not _SHA.match(commit):
        return None
    url, _, query = version.split("#", 1)[0].partition("?")
    if not _usable_url(url):
        return None
    subpath = ""
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name == "path":
            subpath = unquote(value).strip("/")  # as Unity reads it: percent-escapes, but a `+` stays a plus
    if ".." in subpath.split("/"):
        return None
    return Source(url, subpath, commit)


def _environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in _REPOSITORY_VARIABLES}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1", GIT_ALLOW_PROTOCOL=_ALLOWED_PROTOCOLS)
    return environment


def _run(arguments: Sequence[str], cwd: str, timeout: int) -> bytes:
    try:
        completed = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, timeout=timeout, check=False, env=_environment(),
                                   stdin=subprocess.DEVNULL, **nowindow.OPTIONS)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise Unresolved(f"git {arguments[0]}: {error}") from error
    if completed.returncode != 0:
        raise Unresolved(f"git {arguments[0]} failed ({completed.returncode}): {completed.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return completed.stdout


class PackageRepo:
    """The two commits of one package repository in a throw-away bare repository (nothing is kept between runs)."""

    def __init__(self, source: Source, old: str, new: str):
        self.source, self.old, self.new = source, old, new
        self.prefix = source.subpath + "/" if source.subpath else ""
        self._folder = tempfile.TemporaryDirectory(prefix="valtrion-package-", ignore_cleanup_errors=True)
        try:
            _run(["init", "--bare", "-q", "."], self._folder.name, GIT_TIMEOUT_SEC)
            base = ["fetch", "-q", "--depth=1", "--no-recurse-submodules"]
            try:  # without file contents first: only the names and the few blobs of the changed files are needed
                _run([*base, "--filter=blob:none", "--", source.url, old, new], self._folder.name, FETCH_TIMEOUT_SEC)
            except Unresolved:
                _run([*base, "--", source.url, old, new], self._folder.name, FETCH_TIMEOUT_SEC)
            for commit in (old, new):  # a folder that is not there would give an empty diff, which must not read as "nothing changed"
                try:
                    _run(["cat-file", "-e", f"{commit}:{self.prefix}package.json"], self._folder.name, GIT_TIMEOUT_SEC)
                except Unresolved as error:
                    raise Unresolved(f"no package.json at the package folder {source.subpath or '(the root)'!r} in {commit[:8]}") from error
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            self._folder.cleanup()
        except OSError:
            pass

    def changed(self) -> list[tuple[str, str]]:
        """(status, path relative to the package folder) of every file that differs between the commits."""
        arguments = ["diff", "--name-status", "-z", "--no-renames", self.old, self.new]
        if self.prefix:
            arguments += ["--", self.source.subpath]
        fields = [item.decode("utf-8", "surrogateescape") for item in _run(arguments, self._folder.name, GIT_TIMEOUT_SEC).split(b"\0") if item]
        result = []
        for status, path in zip(fields[0::2], fields[1::2]):
            if self.prefix and not path.startswith(self.prefix):
                raise Unresolved("a changed path is outside the package folder")
            result.append((status[:1], path[len(self.prefix):]))
        return result

    def read(self, commit: str, relative: str) -> bytes:
        return _run(["show", f"{commit}:{self.prefix}{relative}"], self._folder.name, GIT_TIMEOUT_SEC)

    def files(self) -> list[str]:
        """Every file of the package folder in the new commit, relative to the package folder, sorted."""
        arguments = ["ls-tree", "-r", "--name-only", "-z", self.new]
        if self.prefix:
            arguments += ["--", self.source.subpath]
        paths = (item.decode("utf-8", "surrogateescape") for item in _run(arguments, self._folder.name, GIT_TIMEOUT_SEC).split(b"\0") if item)
        return sorted(path[len(self.prefix):] if path.startswith(self.prefix) else path for path in paths)

    def assemblies(self) -> dict[str, str]:
        """Folder (relative to the package folder, '' for the root) -> its assembly definition, for the new commit."""
        found: dict[str, str] = {}
        for relative in self.files():
            if relative.endswith(".asmdef"):
                found.setdefault(relative.rpartition("/")[0], relative)
        return found


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except RecursionError as error:
        raise ValueError("nested too deeply") from error
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _floor(relative: str) -> bool:
    segments = relative.lower().split("/")
    name = segments[-1]
    if name.endswith(".meta"):
        name = name[:-5]
    return (name in FLOOR_NAMES or name.rpartition(".")[2] in FLOOR_EXTENSIONS or bool(FLOOR_FOLDERS.intersection(segments[:-1])))


def _compiled_for(relative: str, assemblies: Mapping[str, str], definitions: dict[str, dict | None]) -> str:
    """`editor` or `tests` when the nearest assembly definition above the file keeps the code out of a player, else `runtime`. A file with no
    assembly definition above it is runtime: a folder called `Editor` or `Tests` proves nothing by its name."""
    folder = relative.rpartition("/")[0]
    while True:
        if folder in assemblies:
            definition = definitions.get(assemblies[folder])
            if definition is None:
                return "runtime"
            if definition.get("includePlatforms") == ["Editor"]:
                return "editor"
            if "UNITY_INCLUDE_TESTS" in (definition.get("defineConstraints") or []):
                return "tests"
            return "runtime"
        if not folder:
            return "runtime"
        folder = folder.rpartition("/")[0]


def judge(repo: PackageRepo, files: Sequence[tuple[str, str]], path_reason: Callable[[str], str | None]) -> list[str]:
    """The reasons a change needs more than the Editor suites; an empty list means managed-only.

    `path_reason(relative)` is the project map's verdict on a package-relative path: None when every kind of the path is light, else why not
    (a kind with player or device checks, or no kind at all)."""
    if len(files) > MAX_FILES:
        return [f"{len(files)} files changed: more than {MAX_FILES} are not read one by one"]
    reasons: list[str] = []

    def add(text: str) -> None:
        if len(reasons) < _MAX_REASONS:
            reasons.append(text)

    assemblies = repo.assemblies() if any(relative.endswith((".cs", ".meta")) for _, relative in files) else {}
    definitions: dict[str, dict | None] = {}
    for relative in assemblies.values():
        try:
            definitions[relative] = _json_object(repo.read(repo.new, relative))
        except (Unresolved, ValueError):
            definitions[relative] = None

    def texts(status: str, relative: str) -> tuple[list[str], list[str]]:
        old = repo.read(repo.old, relative).decode("utf-8", "replace").splitlines() if status != "A" else []
        new = repo.read(repo.new, relative).decode("utf-8", "replace").splitlines() if status != "D" else []
        return old, new

    for status, relative in files:
        name = relative.rsplit("/", 1)[-1]
        if relative == "package.json":
            if status in ("A", "D"):
                add(f"package.json: {'added' if status == 'A' else 'removed'}")
                continue
            try:
                old, new = (_json_object(repo.read(commit, relative)) for commit in (repo.old, repo.new))
            except (Unresolved, ValueError):
                add("package.json: cannot be compared")
                continue
            changed = sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key) and key not in HARMLESS_PACKAGE_KEYS)
            if changed:
                add(f"package.json: {', '.join(changed)} changed")
            continue
        if _floor(relative):
            add(f"{relative}: native, shader or platform content")
            continue
        reason = path_reason(relative)
        if reason:
            add(f"{relative}: {reason}")
        if name.endswith(".asmdef"):
            if status != "M":
                add(f"{relative}: assembly definition {'added' if status == 'A' else 'removed'}")
                continue
            try:
                old, new = (_json_object(repo.read(commit, relative)) for commit in (repo.old, repo.new))
                changed = [key for key in ASMDEF_PLATFORM_KEYS if old.get(key) != new.get(key)]
            except (Unresolved, ValueError):
                changed = ["(unreadable)"]
            if changed:
                add(f"{relative}: {', '.join(changed)} changed")
        elif name.endswith((".cs", ".meta")) and _compiled_for(relative, assemblies, definitions) == "runtime":
            try:
                old_lines, new_lines = texts(status, relative)
            except Unresolved:
                add(f"{relative}: cannot be read")
                continue
            if name.endswith(".meta"):
                if old_lines != new_lines and any("PluginImporter" in line for line in old_lines + new_lines):
                    add(f"{relative}: plugin importer changed")
            elif any(PLATFORM_DEFINE.match(line) for line in old_lines + new_lines):
                add(f"{relative}: a file with a platform define changed")
            else:
                old_set, new_set = set(old_lines), set(new_lines)
                if any(RISKY_API.search(line) for line in [line for line in new_lines if line not in old_set] + [line for line in old_lines if line not in new_set]):
                    add(f"{relative}: reflection or native interop changed")
    return reasons
