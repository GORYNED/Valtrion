"""Which test classes a change to C# files can affect, found from the code alone (no Unity, no compiler).

A changed `.cs` file declares types. A file that names one of them can be affected by the change, and the types that file declares are
affected in turn, so the set of affected files grows until it stops (a closure over names). A test class declared in an affected file is a
fixture the change may affect; the other test classes of the test assembly cannot reach the change by a name, so they are left out.

This is an over-approximation by name: a word is a reference wherever it appears in code (comments are ignored, string contents are not,
since a type may be named in a string for reflection), and any type with that name counts, whatever its namespace. It cannot see what is
not named in C#: a scene, a prefab or an asset that holds a script by GUID, a type created from a string built at run time, or a change
inside a serialized field's value. Two guards cover the common cases: a changed type that is a Unity object (a `MonoBehaviour` and the
like, directly or through a project type) also selects every test class that loads assets, and anything that cannot be judged (a file that
cannot be parsed, a change that names no type, an unusable fixture name, no fixture at all) makes the caller keep the whole test assembly.

Pure functions over text; `select` reads the files and decides when to use this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


class Unjudgeable(Exception):
    """The code cannot be judged by name: the caller keeps the whole test assembly."""


#: Bases that make a type a Unity object, one that scenes, prefabs and assets can hold by GUID.
UNITY_OBJECT_BASES = frozenset({
    "MonoBehaviour", "ScriptableObject", "StateMachineBehaviour", "NetworkBehaviour", "Behaviour", "Component", "UIBehaviour", "Graphic",
    "MaskableGraphic", "Selectable", "Editor", "EditorWindow", "PropertyDrawer", "PlayableBehaviour", "Object", "Image", "Text", "Button",
    "TMP_Text", "TextMeshProUGUI", "TMP_InputField", "ScriptedImporter", "AssetPostprocessor"})
#: Words that show a test class loads something that is not named in C#.
ASSET_WORDS = frozenset({
    "Resources", "AssetDatabase", "LoadScene", "LoadSceneAsync", "SceneManager", "EditorSceneManager", "OpenScene", "Addressables",
    "PrefabUtility", "LoadAssetAtPath", "LoadAllAssetsAtPath", "FindAssets", "Instantiate", "LoadSceneMode"})
_KEYWORDS = frozenset({"is", "as", "in", "and", "or", "not", "with", "new", "where", "when", "of", "from", "var", "this", "base", "operator"})

_WORD = re.compile(r"[A-Za-z_]\w*")
_CHAR = re.compile(r"'(?:\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{1,4}|.)|[^'\\\n])'")
_TEST_ATTRIBUTE = re.compile(r"[\[,]\s*(?:NUnit\.Framework\.)?(Test|TestCase|TestCaseSource|UnityTest|Theory|TestFixture|TestFixtureSource)(?:Attribute)?\s*(?=[\](,])")
_METHOD_ATTRIBUTES = {"Test", "UnityTest", "Theory", "TestCase", "TestCaseSource"}
_TOKEN = re.compile(
    r"(?P<open>\{)|(?P<close>\})"
    r"|\bnamespace\s+(?P<ns>[A-Za-z_][\w.]*)\s*(?P<nsend>[{;])"
    r"|\b(?P<kw>class|struct|interface|enum|record)\b(?:\s+(?:class|struct)\b)?\s+(?P<name>[A-Za-z_]\w*)")
_EXTENSION = re.compile(r"([A-Za-z_]\w*)\s*(?:<[^<>()]*>)?\s*\(\s*(?:\[[^\]]*\]\s*)?this\s+")
_ASSEMBLY_ATTRIBUTE = re.compile(r"\[\s*assembly\s*:|\bglobal\s+using\b")


def scan(text: str) -> tuple[str, str]:
    """`(code, shape)`: the text with comments blanked, and the same with the contents of strings and characters blanked too. Both keep the
    length and the line breaks. Raises `Unjudgeable` for a string or comment that does not end."""
    code: list[str] = []
    shape: list[str] = []
    i, n = 0, len(text)
    start = 0  # text[start:i] is plain code not yet copied

    def flush(end: int) -> None:
        if end > start:
            chunk = text[start:end]
            code.append(chunk)
            shape.append(chunk)

    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            flush(i)
            if text[i + 1] == "/":
                j = text.find("\n", i)
                j = n if j < 0 else j
            else:
                j = text.find("*/", i + 2)
                if j < 0:
                    raise Unjudgeable("a comment does not end")
                j += 2
            blank = re.sub(r"[^\n]", " ", text[i:j])
            code.append(blank)
            shape.append(blank)
            i = start = j
        elif c == '"':
            flush(i)
            j = _string_end(text, i)
            code.append(text[i:j])
            shape.append('"' + re.sub(r"[^\n]", " ", text[i + 1:j - 1]) + '"')
            i = start = j
        elif c == "'":
            found = _CHAR.match(text, i)
            if found is None:
                i += 1
                continue
            flush(i)
            code.append(text[i:found.end()])
            shape.append("'" + " " * (found.end() - i - 2) + "'")
            i = start = found.end()
        else:
            i += 1
    flush(n)
    return "".join(code), "".join(shape)


def _string_end(text: str, i: int) -> int:
    """The index after the string literal that opens at `text[i]` (a `"`). An interpolated string (`$"..."`) holds code between braces, and
    that code may hold strings of its own."""
    n = len(text)
    k = i - 1
    while k >= 0 and text[k] in "$@":
        k -= 1
    prefix = text[k + 1:i]
    verbatim = "@" in prefix
    interpolated = "$" in prefix
    if not verbatim and text.startswith('"""', i):
        quotes = 3
        while text[i + quotes:i + quotes + 1] == '"':
            quotes += 1
        close = text.find('"' * quotes, i + quotes)
        if close < 0:
            raise Unjudgeable("a raw string does not end")
        return close + quotes
    j = i + 1
    while j < n:
        ch = text[j]
        if interpolated and ch == "{":
            if text[j + 1:j + 2] == "{":
                j += 2
            else:
                j = _expression_end(text, j + 1)
            continue
        if interpolated and ch == "}" and text[j + 1:j + 2] == "}":
            j += 2
            continue
        if verbatim:
            if ch == '"':
                if text[j + 1:j + 2] == '"':
                    j += 2
                    continue
                return j + 1
        elif ch == "\\":
            j += 2
            continue
        elif ch == '"':
            return j + 1
        elif ch == "\n":
            raise Unjudgeable("a string does not end on its line")
        j += 1
    raise Unjudgeable("a string does not end")


def _expression_end(text: str, j: int) -> int:
    """The index after the `}` that closes the code of an interpolation which starts at `text[j]` (just after its `{`)."""
    n = len(text)
    depth = 1
    while j < n:
        ch = text[j]
        if ch == '"':
            j = _string_end(text, j)
            continue
        if ch == "'":
            found = _CHAR.match(text, j)
            j = found.end() if found else j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise Unjudgeable("an interpolation does not end")


@dataclass(frozen=True)
class TypeDecl:
    name: str
    #: `Namespace.Name`, or `Name` in the global namespace; None for a nested or generic type, which a test filter cannot name.
    full_name: str | None
    bases: tuple[str, ...]
    abstract: bool
    tests: int
    #: Whether the type declares test methods or carries a fixture attribute.
    has_tests: bool
    #: `Namespace.Name` also for a nested or generic type (where a test's real name differs): enough to tell which namespace it is under.
    approx: str = ""


@dataclass(frozen=True)
class Source:
    types: tuple[TypeDecl, ...]
    words: frozenset[str]
    extensions: frozenset[str]
    #: A declaration the names cannot judge: an assembly attribute or a global using.
    global_effect: bool


def _bases(shape: str, begin: int, end: int) -> tuple[str, ...]:
    head = shape[begin:end]
    head = re.split(r"\bwhere\b", head, maxsplit=1)[0]
    depth = 0
    cleaned = []
    for ch in head:  # primary constructor arguments and generic arguments hold no base names
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth = max(0, depth - 1)
        elif depth == 0:
            cleaned.append(ch)
    text = "".join(cleaned)
    if ":" not in text:
        return ()
    names = []
    for part in text.split(":", 1)[1].split(","):
        found = _WORD.findall(part)
        if found:
            names.append(found[-1])
    return tuple(names)


def parse(text: str) -> Source:
    """The types a C# file declares and the words it uses. Raises `Unjudgeable` when the braces do not balance or a string does not end."""
    code, shape = scan(text)
    frames: list[tuple[str, int | None]] = []  # ("ns", None) | ("type", index into found) | ("block", None)
    found: list[dict] = []
    namespace_stack: list[str] = []
    file_namespace = ""
    pending: dict[int, int] = {}  # position of the `{` that opens a type's body -> index into found
    for match in _TOKEN.finditer(shape):
        position = match.start()
        if match.group("open"):
            index = pending.pop(position, None)
            if index is not None:
                frames.append(("type", index))
                found[index]["start"] = position
            else:
                frames.append(("block", None))
        elif match.group("close"):
            if not frames:
                raise Unjudgeable("a closing brace without an opening one")
            kind, index = frames.pop()
            if kind == "ns":
                namespace_stack.pop()
            elif kind == "type":
                found[index]["end"] = position  # type: ignore[index]
        elif match.group("ns"):
            if match.group("nsend") == "{":
                namespace_stack.append(match.group("ns"))
                frames.append(("ns", None))
            else:
                file_namespace = match.group("ns")
        else:
            name = match.group("name")
            tail = shape[match.end():]
            lead = len(tail) - len(tail.lstrip())
            rest = tail[lead:]
            generic = rest.startswith("<")
            if generic:
                depth = 0
                for offset, ch in enumerate(rest):
                    depth += (ch == "<") - (ch == ">")
                    if depth == 0:
                        rest = rest[offset + 1:].lstrip()
                        break
            if name in _KEYWORDS or not (rest[:1] in {"{", ":", "(", ";"} or rest.startswith("where")):
                continue
            stop = re.search(r"[{;]", shape[match.end():])
            if stop is None:
                raise Unjudgeable(f"the declaration of {name} has no body")
            end = match.end() + stop.start()
            nested = any(kind == "type" for kind, _ in frames)
            before = shape[max(0, position - 60):position]
            namespace = ".".join(namespace_stack) or file_namespace
            found.append({
                "name": name, "pos": position, "generic": generic, "nested": nested, "namespace": namespace, "start": None, "end": None,
                "bases": _bases(shape, match.end(), end), "abstract": bool(re.search(r"\babstract\b[^;{}]*$", before)), "open": end,
                "attributes": 0, "tests": 0, "fixture": False})
            if shape[end] == "{":
                pending[end] = len(found) - 1
    if frames:
        raise Unjudgeable("an opening brace without a closing one")

    # Attributes belong to the innermost type whose body holds them, or to the type declared right after them (`[TestFixture] class X`).
    marks = [(m.start(), m.group(1)) for m in _TEST_ATTRIBUTE.finditer(shape)]
    groups: list[tuple[int, list[str]]] = []
    for position, name in marks:
        if groups and not re.search(r"[;{}]", shape[groups[-1][0]:position]) and not re.search(r"\)\s*[A-Za-z_<]", shape[groups[-1][0]:position]):
            groups[-1][1].append(name)
        else:
            groups.append((position, [name]))
    for position, names in groups:
        owner = None
        for index, item in enumerate(found):
            if item["start"] is not None and item["end"] is not None and item["start"] < position < item["end"]:
                if owner is None or item["start"] > found[owner]["start"]:
                    owner = index
        declared = next((index for index, item in enumerate(found) if item["pos"] > position
                         and not re.search(r"[;{}]", shape[position:item["pos"]])), None)
        if declared is not None and set(names) <= {"TestFixture", "TestFixtureSource"}:
            found[declared]["fixture"] = True
        elif owner is not None:
            if set(names) & {"TestFixture", "TestFixtureSource"}:
                found[owner]["fixture"] = True
            if set(names) & _METHOD_ATTRIBUTES:
                cases = sum(1 for name in names if name == "TestCase")
                found[owner]["tests"] += max(cases, 1)
    types = tuple(TypeDecl(
        item["name"], None if item["nested"] or item["generic"] else (f"{item['namespace']}.{item['name']}" if item["namespace"] else item["name"]),
        item["bases"], item["abstract"], item["tests"], bool(item["tests"]) or item["fixture"],
        f"{item['namespace']}.{item['name']}" if item["namespace"] else item["name"]) for item in found)
    return Source(types, frozenset(_WORD.findall(code)), frozenset(_EXTENSION.findall(shape)), bool(_ASSEMBLY_ATTRIBUTE.search(shape)))


def _chain(name: str, bases_of: Mapping[str, set[str]]) -> set[str]:
    """Every base name that `name` reaches through the types of the project, itself not included."""
    seen: set[str] = set()
    queue = [name]
    while queue:
        for base in bases_of.get(queue.pop(), ()):
            if base not in seen:
                seen.add(base)
                queue.append(base)
    return seen


@dataclass(frozen=True)
class TestClass:
    path: str
    item: TypeDecl
    #: Whether the change can reach the class by a name (see the module text).
    selected: bool


class Analysis:
    """The closure of one change over the files of a scope: which files it can reach, and from them which test classes."""

    def __init__(self, files: set[str], unity_object: bool, sources: Mapping[str, Source], bases_of: Mapping[str, set[str]]) -> None:
        self.files = files
        self.unity_object = unity_object
        self.sources = sources
        self._bases_of = bases_of
        self._by_name: dict[str, list[TypeDecl]] = {}
        for source in sources.values():
            for item in source.types:
                self._by_name.setdefault(item.name, []).append(item)

    def is_test(self, item: TypeDecl) -> bool:
        if item.abstract:
            return False
        if item.has_tests:
            return True
        return any(other.has_tests for base in _chain(item.name, self._bases_of) for other in self._by_name.get(base, ()))

    def is_unity_object(self, item: TypeDecl) -> bool:
        """Whether the type is a `MonoBehaviour`, a `ScriptableObject` or the like, directly or through a type of the scope."""
        return bool(_chain(item.name, self._bases_of) & UNITY_OBJECT_BASES)

    def test_classes(self, paths: Iterable[str]) -> list[TestClass]:
        """The test classes declared in `paths`, each marked with whether the change can reach it."""
        found = []
        for path in sorted(paths):
            source = self.sources.get(path)
            if source is None:
                continue
            for item in source.types:
                if self.is_test(item):
                    found.append(TestClass(path, item, path in self.files or (self.unity_object and bool(source.words & ASSET_WORDS))))
        return found


def analyse(changed: Mapping[str, Sequence[Source | None]], sources: Mapping[str, Source]) -> Analysis:
    """Close the change over `sources` (every file of the scope, changed ones included). `changed` maps each changed path to its old and new
    `Source` (None for a file that did not exist or is gone). Raises `Unjudgeable` for a change that names nothing."""
    names: set[str] = set()
    files: set[str] = set(changed)
    for path, versions in changed.items():
        declared = False
        for source in versions:
            if source is None:
                continue
            if source.global_effect:
                raise Unjudgeable(f"{path}: an assembly attribute or a global using changes every file of the assembly")
            for item in source.types:
                names.add(item.name)
                declared = True
            if source.extensions:
                names |= source.extensions
                declared = True
        if not declared:
            raise Unjudgeable(f"{path}: declares no type, so no name says who uses it")
    index: dict[str, set[str]] = {}
    for path, source in sources.items():
        for word in source.words:
            index.setdefault(word, set()).add(path)
    bases_of: dict[str, set[str]] = {}
    for source in [*sources.values(), *(old for versions in changed.values() for old in versions if old is not None)]:
        for item in source.types:  # the old version of a changed file too: a deleted type is not in `sources`
            bases_of.setdefault(item.name, set()).update(item.bases)
    seen = set(names)
    frontier = list(names)
    while frontier:
        for path in sorted(index.get(frontier.pop(), ())):
            if path in files:
                continue
            files.add(path)
            source = sources[path]
            for word in [item.name for item in source.types] + sorted(source.extensions):
                if word not in seen:
                    seen.add(word)
                    frontier.append(word)
    unity_object = any(_chain(item.name, bases_of) & UNITY_OBJECT_BASES
                       for versions in changed.values() for source in versions if source is not None for item in source.types)
    return Analysis(files, unity_object, sources, bases_of)


def affected_fixtures(
        changed: Mapping[str, Sequence[Source | None]],
        sources: Mapping[str, Source],
        test_files: Mapping[str, Iterable[str]],
) -> dict[str, list[str] | None]:
    """For each test assembly (`test_files`: name -> its `.cs` paths), the full names of the test classes the change can affect, sorted, or
    None when the whole assembly has to run (a class that cannot be named, or no class at all). See `analyse` for the inputs."""
    analysis = analyse(changed, sources)
    result: dict[str, list[str] | None] = {}
    for assembly, paths in test_files.items():
        chosen = [found.item for found in analysis.test_classes(paths) if found.selected]
        usable = all(item.full_name is not None for item in chosen)
        result[assembly] = sorted({item.full_name for item in chosen}) if usable and chosen else None  # type: ignore[type-var]
    return result


def count_tests(sources: Mapping[str, Source], fixtures: Iterable[str] | None, paths: Iterable[str]) -> int:
    """The number of test methods (a `TestCase` counts once per case) declared by the fixtures named, or by every test class of `paths` when
    `fixtures` is None. Inherited tests are not counted. Used to show how much a selection runs, not to decide it."""
    wanted = None if fixtures is None else set(fixtures)
    total = 0
    for path in paths:
        source = sources.get(path)
        if source is None:
            continue
        for item in source.types:
            if wanted is None or item.full_name in wanted:
                total += item.tests
    return total
