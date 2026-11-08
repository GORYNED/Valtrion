"""Built-in presets of `checks.json` (`extends`): path rules and classes of change that every project of a platform would otherwise write by hand.

A preset knows *classes* (kinds such as `native` or `package-managed`) and which level of checks a class needs, never a project's own tests.
Where a class needs a check, the preset names a *slot* (`bind:editor`, `bind:player`, `bind:device`) and the project's `bindings` say which
concrete checks fill it. The preset is merged into the project's map when it is loaded, so selection, `explain` and `check-config` see one
ordinary map; the preset is versioned, so a Core upgrade never changes a selection: a new preset version is opted into with a new `extends`.
"""

from __future__ import annotations

import copy
from typing import Any

#: The slots a preset's kinds name as `bind:NAME` in a level list.
UNITY_SLOTS = ("editor", "player", "device")

_DOC_EXTENSIONS = ("md", "txt", "png", "jpg", "jpeg", "gif", "svg", "pdf")
_SCRIPT_EXTENSIONS = ("py", "sh", "ps1", "cmd", "bat", "md", "txt", "json", "yml", "yaml", "toml", "ini", "cfg")

_HEAVY = {"L0": ["l0"], "L2": ["bind:editor"], "L3": ["bind:player"], "L4": ["bind:device"]}
_EDITOR = {"L0": ["l0"], "L2": ["bind:editor"]}

_NATIVE_EXTENSIONS = ("so", "a", "aar", "jar", "dylib", "bundle", "xcframework", "framework", "mm", "m", "h", "c", "cc", "cpp", "hpp", "swift", "java",
                      "kt", "gradle", "plist", "entitlements", "xcprivacy", "aidl", "proguard", "dll")
_SHADER_EXTENSIONS = ("shader", "shadergraph", "shadersubgraph", "shadervariants", "hlsl", "cginc", "compute", "metal", "glsl", "glslinc", "vfx", "uitkshader")
#: `ProjectSettings` files without a platform, shader or inclusion section: cross-platform runtime or Editor behavior (Editor suites cover it).
_LIGHT_SETTINGS = ("TagManager", "InputManager", "TimeManager", "AudioManager", "DynamicsManager", "Physics2DSettings", "PhysicsCoreProjectSettings2D",
                   "NavMeshAreas", "ClusterInputManager", "NetworkManager", "EditorSettings", "PresetManager", "ProjectAuditorSettings",
                   "VersionControlSettings")
_LIGHT_SETTING_GLOBS = [f"**/ProjectSettings/{name}.asset" for name in _LIGHT_SETTINGS]


def _dependency_rule(glob: str) -> dict[str, Any]:
    return {"glob": glob, "root": "dependencies", "keys": [
        {"match": "com.unity.ide.*", "kind": "package-managed"},
        {"match": "com.unity.test-framework*", "kind": "package-managed"},
        {"match": "com.goryned.valtrion.*", "kind": "package-managed"},
        # Anything else may carry native or platform content: fail closed to the heavy kind, on purpose.
        {"match": "*", "kind": "package-platform", "allowHeavyFallback": True},
    ]}


def _unity_1() -> dict[str, Any]:
    # `docs/` and `tools/` hold text, scripts and pictures, never assets: a Unity project nested under them (a showcase, a sample) must not
    # fall to L0 because of its folder, so the rules name file types, and an asset or prefab there stays for the project's policy.
    areas: list[dict[str, Any]] = [
        *({"glob": f"docs/**/*.{extension}", "kind": "docs"} for extension in _DOC_EXTENSIONS),
        {"glob": "**/*.md", "kind": "docs"},
        {"glob": "**/LICENSE*", "kind": "docs"},
        *({"glob": f"tools/**/*.{extension}", "kind": "tooling"} for extension in _SCRIPT_EXTENSIONS),
        {"glob": ".github/**", "kind": "tooling"},
        {"glob": ".gitignore", "kind": "tooling"},
        {"glob": ".gitattributes", "kind": "tooling"},
        {"glob": "**/*.meta", "kind": "asset-meta"},
        {"glob": "**/*.cs", "kind": "managed-code"},
        {"glob": "**/Editor/**", "kind": "editor-code"},
        {"glob": "**/Tests/**", "kind": "tests"},
        {"glob": "**/*.asmdef", "kind": "assembly-config"},
        {"glob": "**/*.asmref", "kind": "assembly-config"},
        {"glob": "**/link.xml", "kind": "linking"},
        {"glob": "**/Android/**", "kind": "native"},
        {"glob": "**/iOS/**", "kind": "native"},
        {"glob": "**/Plugins/**", "kind": "native"},
    ]
    areas += [{"glob": f"**/*.{extension}", "kind": "native"} for extension in _NATIVE_EXTENSIONS]
    areas += [{"glob": f"**/*.{extension}.meta", "kind": "native"} for extension in _NATIVE_EXTENSIONS]
    areas += [{"glob": f"**/*.{extension}", "kind": "shader"} for extension in _SHADER_EXTENSIONS]
    areas += [{"glob": f"**/*.{extension}.meta", "kind": "shader"} for extension in _SHADER_EXTENSIONS]
    areas += [
        {"glob": "**/ProjectSettings/**", "kind": "platform-settings", "exclude": list(_LIGHT_SETTING_GLOBS)},
        {"glob": "**/Assets/Settings/**", "kind": "platform-settings"},
        {"glob": "**/AddressableAssetsData/**", "kind": "platform-settings"},
    ]
    areas += [{"glob": glob, "kind": "project-settings"} for glob in _LIGHT_SETTING_GLOBS]
    return {
        "areas": areas,
        "jsonRules": [_dependency_rule("**/Packages/manifest.json"), _dependency_rule("**/Packages/packages-lock.json")],
        "contentRules": [{"glob": "**/*.cs", "kind": "comment-only", "lineComment": ["//"], "blockComment": {"start": "/*", "end": "*/"}}],
        "kinds": {
            "docs": {"L0": ["l0"]},
            "tooling": {"L0": ["l0"]},
            "comment-only": {"L0": ["l0"]},
            "asset-meta": copy.deepcopy(_EDITOR),
            "managed-code": copy.deepcopy(_EDITOR),
            "editor-code": copy.deepcopy(_EDITOR),
            "tests": copy.deepcopy(_EDITOR),
            "assembly-config": copy.deepcopy(_EDITOR),
            "project-settings": copy.deepcopy(_EDITOR),
            "package-managed": copy.deepcopy(_EDITOR),
            "linking": {"L0": ["l0"], "L2": ["bind:editor"], "L3": ["bind:player"]},
            "native": copy.deepcopy(_HEAVY),
            "shader": copy.deepcopy(_HEAVY),
            "platform-settings": copy.deepcopy(_HEAVY),
            "package-platform": copy.deepcopy(_HEAVY),
        },
    }


#: id -> builder (a fresh copy each time, so a caller can never change a preset).
PRESETS = {"valtrion:unity/1": _unity_1}


class PresetError(ValueError):
    """The map's `extends` or `bindings` cannot be applied; the message says what to change."""


def preset_ids() -> list[str]:
    return sorted(PRESETS)


def preset_kinds(preset_id: str) -> set[str]:
    return set(PRESETS[preset_id]()["kinds"])


def expand(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The effective map for a project map with `extends`, and a description of what came from the preset; (data, None) without `extends`.

    Order: the project's areas, jsonRules and contentRules first, then the preset's, so for a rule where the first match wins the project's
    rule wins; kinds of the project replace a preset kind of the same name. Every `bind:NAME` of a preset kind becomes the project's list
    `bindings[NAME]` (an empty list is a valid, explicit answer; a missing binding is an error, so nothing is dropped silently)."""
    preset_id = data.get("extends")
    if preset_id is None:
        return data, None
    if preset_id not in PRESETS:
        raise PresetError(f"extends: unknown preset {preset_id!r} (built-in presets: {', '.join(preset_ids())})")
    preset = PRESETS[preset_id]()
    bindings = data.get("bindings", {})
    slots = UNITY_SLOTS
    unknown = sorted(set(bindings) - set(slots))
    if unknown:
        raise PresetError(f"bindings: {', '.join(unknown)} is not a slot of {preset_id} (slots: {', '.join(slots)})")
    missing = [slot for slot in slots if slot not in bindings]
    if missing:
        raise PresetError(f"bindings: {preset_id} needs a list of checks for {', '.join(missing)} (an empty list says there is none)")
    kinds: dict[str, dict[str, list[str]]] = {}
    for name, levels in preset["kinds"].items():
        kinds[name] = {level: [item for entry in entries for item in (bindings[entry[5:]] if entry.startswith("bind:") else [entry])]
                       for level, entries in levels.items()}
    project_kinds = copy.deepcopy(data.get("kinds", {}))
    # A project kind of the same name replaces the preset's; record the levels it leaves out, so `check-config` can say that paths get lighter.
    replaced = {name: [level for level in kinds[name] if level not in project_kinds[name]] for name in project_kinds if name in kinds}
    kinds.update(project_kinds)
    effective = copy.deepcopy(data)
    project_counts = {"areas": len(data.get("areas", [])), "jsonRules": len(data.get("jsonRules", [])),
                      "contentRules": len(data.get("contentRules", []))}
    for name in project_counts:
        effective[name] = copy.deepcopy(data.get(name, [])) + preset[name]
    effective["kinds"] = kinds
    info = {"id": preset_id, "projectCounts": project_counts, "kinds": sorted(preset["kinds"]), "replaced": replaced}
    return effective, info
