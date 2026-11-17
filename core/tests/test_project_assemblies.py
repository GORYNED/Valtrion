"""The `assemblies` member of an area: a change of the project's own C# code selects only the test assemblies that reference the assembly it
belongs to, through the `.asmdef` and `.asmref` files of the project (fixtures: the `Game.*` project)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from valtrion import asmgraph, select

from .test_select import CONTRACTS, git, run, write

CORE_GUID = "1" * 32
RUNNERS = {"edit": "run --edit {assemblies}", "play": "run --play {assemblies}"}
WHOLE_EDIT = "editmode:Game.Core.Tests,Game.Other.Tests,Game.Ui.Tests@edit"
WHOLE = ["editmode:Game.Core.Tests", "editmode:Game.Other.Tests", "editmode:Game.Ui.Tests", "playmode:Game.Play.Tests"]
MEMBER = {"kind": "narrowed", "runner": "edit", "playmodeRunner": "play"}


def meta(guid):
    return f"fileFormatVersion: 2\nguid: {guid}\n"


def asm(name, references=(), test=False, editor=False, auto=False, **extra):
    """An assembly definition; not referenced by the predefined assemblies unless `auto` (the fixture has code outside every definition)."""
    document = {"name": name, "references": list(references), "includePlatforms": ["Editor"] if editor else [], "autoReferenced": auto}
    if test:
        document["defineConstraints"] = ["UNITY_INCLUDE_TESTS"]
    document.update(extra)
    return json.dumps(document)


def checks_map(member=None, areas=None):
    area = {"glob": "Assets/**", "kind": "code"}
    if member is not False:
        area["assemblies"] = dict(MEMBER if member is None else member)
    return {
        "schema": "gdv.checks/1", "platforms": {"required": ["ios"], "dev": ["editor"]}, "unclassified": "widest", "runners": dict(RUNNERS),
        "areas": [*(areas or []), area, {"glob": "validation/**", "kind": "light"}, {"glob": "Packages/**", "kind": "light"}],
        "kinds": {
            "light": {"structure": ["structure"]},
            "code": {"structure": ["structure"], "affected": [WHOLE_EDIT, "playmode:Game.Play.Tests@play"]},
            "narrowed": {"structure": ["structure"], "full": [WHOLE_EDIT, "playmode:Game.Play.Tests@play"]},
            "extra": {"structure": ["structure:extra"]},
        },
    }


def preset_map():
    data = checks_map()
    data.update(extends="valtrion:unity/1", bindings={"editor": [WHOLE_EDIT], "player": [], "device": []})
    return data


PROJECT = {
    "Assets/Core/Game.Core.asmdef": asm("Game.Core"),
    "Assets/Core/Core.cs": "public class Core { }\n",
    "Assets/Core/Core.cs.meta": meta("c" * 32),
    "Assets/Shared/Game.Core.asmref": json.dumps({"reference": "Game.Core"}),
    "Assets/Shared/Game.Core.asmref.meta": meta("d" * 32),
    "Assets/Shared/Part.cs": "public class Part { }\n",
    "Assets/Ui/Game.Ui.asmdef": asm("Game.Ui", ["Game.Core"]),
    "Assets/Ui/Ui.cs": "public class Ui { }\n",
    "Assets/Other/Game.Other.asmdef": asm("Game.Other"),
    "Assets/Other/Other.cs": "public class Other { }\n",
    "Assets/Tests/Core/Game.Core.Tests.asmdef": asm("Game.Core.Tests", [f"GUID:{CORE_GUID}"], test=True, editor=True),
    "Assets/Tests/Ui/Game.Ui.Tests.asmdef": asm("Game.Ui.Tests", ["Game.Ui"], test=True, editor=True),
    "Assets/Tests/Ui/UiTests.cs": "public class UiTests { }\n",
    "Assets/Tests/Other/Game.Other.Tests.asmdef": asm("Game.Other.Tests", ["Game.Other"], test=True, editor=True),
    "Assets/Tests/Play/Game.Play.Tests.asmdef": asm("Game.Play.Tests", ["Game.Ui"], test=True),
    "Assets/Loose/Loose.cs": "public class Loose { }\n",
    "Assets/Art/Image.png": "png\n",
    "Packages/manifest.json": json.dumps({"dependencies": {}}),
    "ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 6000.0.0f1\n",
}
LIB = {
    "Packages/com.example.lib/package.json": json.dumps({"name": "com.example.lib", "version": "1.0.0"}),
    "Packages/com.example.lib/Runtime/Example.Lib.asmdef": asm("Example.Lib"),
    "Packages/com.example.lib/Runtime/Lib.cs": "public class Lib { }\n",
    "Packages/com.example.lib/Tests/Example.Lib.Tests.asmdef": asm("Example.Lib.Tests", ["Example.Lib"], test=True, editor=True),
    "Game/Packages/manifest.json": json.dumps({"dependencies": {"com.example.lib": "file:../../Packages/com.example.lib"}}),
    "Game/ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 6000.0.0f1\n",
    "Game/Assets/Tests/Game.Lib.Tests.asmdef": asm("Game.Lib.Tests", ["Example.Lib"], test=True, editor=True),
}
CODE_MAP = {"glob": "**/*.cs", "kind": "code", "assemblies": dict(MEMBER)}


class ProjectCase(unittest.TestCase):
    data = None

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        for index, (path, text) in enumerate(PROJECT.items()):
            write(self.root, path, text)
            if path.endswith(".asmdef"):
                write(self.root, path + ".meta", meta(CORE_GUID if path.endswith("Game.Core.asmdef") else f"{index + 10:032x}"))
        self.checks_path = write(self.root, "validation/device/checks.json", json.dumps(self.data or checks_map()))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "checkout", "-q", "-b", "work")

    def commit(self, files=None, remove=()):
        for path, text in (files or {}).items():
            write(self.root, path, text)
        for path in remove:
            os.remove(self.root / path)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")

    def base(self, files, remove=()):
        """Change the base commit (`main`) and start `work` again from it."""
        git(self.root, "checkout", "-q", "main")
        self.commit(files, remove)
        git(self.root, "checkout", "-q", "-B", "work")

    def write_checks(self, data):
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")

    def select_json(self, *arguments):
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", "--level", "affected", *arguments)
        self.assertEqual(0, code, err)
        return json.loads(out)

    def changed(self, files, *arguments, remove=()):
        self.commit(files, remove)
        return self.select_json(*arguments)

    def kinds(self, payload):
        return [kind for kind in payload["kinds"] if kind != "light"]

    def suites(self, payload):
        return sorted(item["key"] for item in payload["editorSuites"])

    def record(self, payload, path):
        records = payload["projectAssemblies"][path]
        self.assertEqual(1, len(records), records)
        return records[0]


class NarrowingTests(ProjectCase):
    def test_a_change_selects_the_test_assemblies_that_reach_its_assembly_directly_by_guid_and_transitively(self):
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
        record = self.record(payload, "Assets/Core/Core.cs")
        self.assertEqual("narrowed", record["status"], record)
        self.assertEqual([{"assembly": "Game.Core", "files": ["Assets/Core/Core.cs"]}], record["changed"])
        self.assertEqual(["narrowed"], self.kinds(payload))
        self.assertEqual(["editmode:Game.Core.Tests", "editmode:Game.Ui.Tests", "playmode:Game.Play.Tests"], self.suites(payload))
        commands = {item["runnerName"]: item["command"] for item in payload["editorCommands"]}
        self.assertEqual({"edit": "run --edit Game.Core.Tests,Game.Ui.Tests", "play": "run --play Game.Play.Tests"}, commands)

    def test_edit_mode_and_play_mode_test_assemblies_get_their_own_runner(self):
        payload = self.changed({"Assets/Ui/Ui.cs": "public class Ui { int a; }\n"})
        tests = {item["assembly"]: item for item in self.record(payload, "Assets/Ui/Ui.cs")["tests"]}
        self.assertEqual({"Game.Ui.Tests": ("editmode", "edit"), "Game.Play.Tests": ("playmode", "play")},
                         {name: (item["mode"], item["runner"]) for name, item in tests.items()})
        self.assertEqual(["Game.Play.Tests", "Game.Ui"], tests["Game.Play.Tests"]["via"])

    def test_a_file_in_an_asmref_folder_counts_for_the_assembly_it_names(self):
        payload = self.changed({"Assets/Shared/Part.cs": "public class Part { int a; }\n"})
        self.assertEqual([{"assembly": "Game.Core", "files": ["Assets/Shared/Part.cs"]}], self.record(payload, "Assets/Shared/Part.cs")["changed"])

    def test_a_changed_test_assembly_reaches_itself(self):
        payload = self.changed({"Assets/Tests/Ui/UiTests.cs": "public class UiTests { int a; }\n"})
        self.assertEqual(["editmode:Game.Ui.Tests"], self.suites(payload))

    def test_an_added_file_with_its_meta_file_is_narrowed(self):
        payload = self.changed({"Assets/Other/New.cs": "public class New { }\n", "Assets/Other/New.cs.meta": meta("a" * 32)})
        self.assertEqual(["editmode:Game.Other.Tests"], self.suites(payload))
        self.assertEqual({"Assets/Other/New.cs", "Assets/Other/New.cs.meta"},
                         {path for path, records in payload["projectAssemblies"].items() if records[0]["status"] == "narrowed"})

    def test_the_meta_file_of_a_folder_changes_no_assembly(self):
        folder = "fileFormatVersion: 2\nguid: " + "e" * 32 + "\nfolderAsset: yes\n"
        self.base({"Assets/Core.meta": folder})
        payload = self.changed({"Assets/Core.meta": folder + "labels: []\n"})
        record = self.record(payload, "Assets/Core.meta")
        self.assertEqual(("narrowed", [], []), (record["status"], record["changed"], record["tests"]))

    def test_two_areas_with_the_member_are_judged_apart(self):
        self.write_checks(checks_map(areas=[{"glob": "Assets/Ui/**", "kind": "extra", "assemblies": {"kind": "narrowed", "runner": "edit"}}]))
        payload = self.changed({"Assets/Ui/Ui.cs": "public class Ui { int a; }\n"})
        records = payload["projectAssemblies"]["Assets/Ui/Ui.cs"]
        self.assertEqual([(0, "unresolved"), (1, "narrowed")], [(item["area"], item["status"]) for item in records])
        self.assertIn("has no playmodeRunner", records[0]["reason"])
        self.assertEqual(["extra", "narrowed"], self.kinds(payload))

    def test_the_meta_file_of_an_asmref_counts_for_the_assembly_it_names(self):
        payload = self.changed({"Assets/Shared/Game.Core.asmref.meta": meta("d" * 32) + "labels: []\n"})
        self.assertEqual([{"assembly": "Game.Core", "files": ["Assets/Shared/Game.Core.asmref.meta"]}],
                         self.record(payload, "Assets/Shared/Game.Core.asmref.meta")["changed"])

    def test_the_whole_set_stays_at_the_level_the_narrowed_kind_keeps_it(self):
        self.commit({"Assets/Other/Other.cs": "public class Other { int a; }\n"})
        self.assertEqual(["editmode:Game.Other.Tests"], self.suites(self.select_json()))
        self.assertEqual(WHOLE, self.suites(self.select_json("--level", "full")))
        self.assertEqual([], self.suites(self.select_json("--level", "fast")))

    def test_another_matching_area_still_adds_its_kind(self):
        self.write_checks(checks_map(areas=[{"glob": "Assets/Core/**", "kind": "extra"}]))
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.assertEqual(["extra", "narrowed"], self.kinds(payload))
        self.assertIn("structure:extra", [item["key"] for item in payload["structure"]])

    def test_a_definition_in_a_folder_unity_does_not_import_is_not_in_the_graph(self):
        self.base({"Assets/Samples~/Game.Core.asmdef": asm("Game.Core"), "Assets/.hidden/Game.Core.asmdef": asm("Game.Core")})
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.assertEqual("narrowed", self.record(payload, "Assets/Core/Core.cs")["status"])

    def test_a_working_tree_change_counts_with_include_worktree(self):
        write(self.root, "Assets/Other/Other.cs", "public class Other { int a; }\n")
        payload = self.select_json("--include-worktree")
        self.assertEqual(["editmode:Game.Other.Tests"], self.suites(payload))


class DefinitionTests(ProjectCase):
    def test_a_change_of_references_only_is_narrowed(self):
        payload = self.changed({"Assets/Other/Game.Other.asmdef": asm("Game.Other", ["Game.Core"])})
        record = self.record(payload, "Assets/Other/Game.Other.asmdef")
        self.assertEqual("narrowed", record["status"], record)
        self.assertEqual(["editmode:Game.Other.Tests"], self.suites(payload))

    def test_a_change_of_the_platforms_or_defines_keeps_the_area_kind(self):
        for document in (asm("Game.Other", includePlatforms=["Android"]), asm("Game.Other", defineConstraints=["SOME_DEFINE"])):
            with self.subTest(document=document):
                git(self.root, "reset", "-q", "--hard", "main")
                payload = self.changed({"Assets/Other/Game.Other.asmdef": document})
                record = self.record(payload, "Assets/Other/Game.Other.asmdef")
                self.assertEqual("unresolved", record["status"])
                self.assertIn("only references and rootNamespace", record["reason"])
                self.assertEqual(["code"], self.kinds(payload))
                self.assertEqual(WHOLE, self.suites(payload))

    def test_a_renamed_assembly_keeps_the_area_kind(self):
        payload = self.changed({"Assets/Other/Game.Other.asmdef": asm("Game.Other2")})
        record = self.record(payload, "Assets/Other/Game.Other.asmdef")
        self.assertEqual("unresolved", record["status"])
        self.assertIn("name changed", record["reason"])
        self.assertEqual(WHOLE, self.suites(payload))

    def test_a_changed_asmref_counts_for_the_old_and_the_new_assembly(self):
        payload = self.changed({"Assets/Shared/Game.Core.asmref": json.dumps({"reference": "Game.Other"})})
        record = self.record(payload, "Assets/Shared/Game.Core.asmref")
        self.assertEqual({"Game.Core", "Game.Other"}, {item["assembly"] for item in record["changed"]})

    def test_a_changed_guid_keeps_the_area_kind(self):
        payload = self.changed({"Assets/Other/Game.Other.asmdef.meta": meta("b" * 32)})
        self.assertIn("GUID", self.record(payload, "Assets/Other/Game.Other.asmdef.meta")["reason"])


class FallbackTests(ProjectCase):
    def unresolved(self, payload, path, reason):
        record = self.record(payload, path)
        self.assertEqual("unresolved", record["status"], record)
        self.assertIn(reason, record["reason"])
        self.assertIn("code", self.kinds(payload))
        self.assertEqual(WHOLE, self.suites(payload))

    def test_a_file_that_belongs_to_no_assembly_definition(self):
        payload = self.changed({"Assets/Loose/Loose.cs": "public class Loose { int a; }\n"})
        self.unresolved(payload, "Assets/Loose/Loose.cs", "belongs to no assembly definition")

    def test_a_file_that_is_not_code(self):
        payload = self.changed({"Assets/Art/Image.png": "other\n"})
        self.unresolved(payload, "Assets/Art/Image.png", "not C# code")

    def test_a_play_mode_test_assembly_without_a_play_mode_runner(self):
        self.write_checks(checks_map({"kind": "narrowed", "runner": "edit"}))
        payload = self.changed({"Assets/Ui/Ui.cs": "public class Ui { int a; }\n"})
        self.unresolved(payload, "Assets/Ui/Ui.cs", "has no playmodeRunner")

    def test_an_edit_mode_only_change_needs_no_play_mode_runner(self):
        self.write_checks(checks_map({"kind": "narrowed", "runner": "edit"}))
        payload = self.changed({"Assets/Other/Other.cs": "public class Other { int a; }\n"})
        self.assertEqual(["editmode:Game.Other.Tests"], self.suites(payload))

    def test_an_added_assembly_definition_fails_every_path_of_the_project(self):
        payload = self.changed({"Assets/New/Game.New.asmdef": asm("Game.New"), "Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.unresolved(payload, "Assets/Core/Core.cs", "Assets/New/Game.New.asmdef was added")

    def test_a_removed_assembly_definition_fails_every_path_of_the_project(self):
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"},
                               remove=["Assets/Other/Game.Other.asmdef", "Assets/Other/Game.Other.asmdef.meta"])
        self.unresolved(payload, "Assets/Core/Core.cs", "Assets/Other/Game.Other.asmdef was removed")

    def test_an_assembly_definition_that_cannot_be_read(self):
        payload = self.changed({"Assets/Ui/Game.Ui.asmdef": "{ not json", "Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.unresolved(payload, "Assets/Core/Core.cs", "cannot be read")

    def test_two_assemblies_with_the_same_name(self):
        payload = self.changed({"Assets/Ui/Game.Ui.asmdef": asm("Game.Core"), "Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.unresolved(payload, "Assets/Core/Core.cs", "the assembly 'Game.Core' is defined in")

    def test_a_folder_that_was_added_or_whose_guid_changed(self):
        payload = self.changed({"Assets/New.meta": "fileFormatVersion: 2\nguid: " + "e" * 32 + "\nfolderAsset: yes\n"})
        self.unresolved(payload, "Assets/New.meta", "a folder was added or removed")
        self.base({"Assets/Core.meta": "fileFormatVersion: 2\nguid: " + "e" * 32 + "\nfolderAsset: yes\n"})
        payload = self.changed({"Assets/Core.meta": "fileFormatVersion: 2\nguid: " + "f" * 32 + "\nfolderAsset: yes\n"})
        self.unresolved(payload, "Assets/Core.meta", "its GUID changed")

    def test_a_folder_that_is_not_a_unity_project_in_the_working_tree(self):
        self.base(LIB, remove=["Packages/manifest.json", "ProjectSettings/ProjectVersion.txt"])
        self.write_checks(checks_map(areas=[CODE_MAP]))
        self.commit()
        write(self.root, "Packages/com.example.lib/Runtime/Lib.cs", "public class Lib { int a; }\n")
        payload = self.select_json("--include-worktree")
        self.unresolved(payload, "Packages/com.example.lib/Runtime/Lib.cs", "has no Packages/manifest.json")

    def test_a_package_that_another_project_takes_through_file(self):
        self.base(LIB)
        self.write_checks(checks_map(areas=[CODE_MAP]))
        payload = self.changed({"Packages/com.example.lib/Runtime/Lib.cs": "public class Lib { int a; }\n"})
        self.unresolved(payload, "Packages/com.example.lib/Runtime/Lib.cs", "Game/Packages/manifest.json takes com.example.lib")
        write(self.root, "Packages/com.example.lib/Runtime/Lib.cs", "public class Lib { int b; }\n")
        self.unresolved(self.select_json("--include-worktree"), "Packages/com.example.lib/Runtime/Lib.cs", "takes com.example.lib")

    def test_a_removed_folder_and_a_folder_without_a_guid(self):
        folder = "fileFormatVersion: 2\nguid: " + "e" * 32 + "\nfolderAsset: yes\n"
        self.base({"Assets/Core.meta": folder})
        self.unresolved(self.changed({}, remove=["Assets/Core.meta"]), "Assets/Core.meta", "a folder was added or removed")
        self.base({"Assets/Core.meta": "fileFormatVersion: 2\nfolderAsset: yes\n"})
        payload = self.changed({"Assets/Core.meta": "fileFormatVersion: 2\nfolderAsset: yes\nlabels: []\n"})
        self.unresolved(payload, "Assets/Core.meta", "its GUID changed")

    def test_a_project_without_its_markers_at_head(self):
        self.base({}, remove=["ProjectSettings/ProjectVersion.txt"])
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.unresolved(payload, "Assets/Core/Core.cs", "has no ProjectSettings/ProjectVersion.txt")

    def test_another_manifest_that_cannot_be_read(self):
        for text in ('{"dependencies": {}, "dependencies": {}}', "{ not json", '{"dependencies": []}'):
            with self.subTest(text=text):
                git(self.root, "reset", "-q", "--hard", "main")
                self.base({"Other/Packages/manifest.json": text})
                payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
                self.unresolved(payload, "Assets/Core/Core.cs", "Other/Packages/manifest.json cannot be read")
                write(self.root, "Assets/Core/Core.cs", "public class Core { int b; }\n")
                self.unresolved(self.select_json("--include-worktree"), "Assets/Core/Core.cs", "Other/Packages/manifest.json cannot be read")
                git(self.root, "reset", "-q", "--hard")
                git(self.root, "checkout", "-q", "main")
                self.commit({}, remove=["Other/Packages/manifest.json"])
                git(self.root, "checkout", "-q", "-B", "work")

    def test_another_manifest_that_is_broken_only_in_the_working_tree(self):
        self.base({"Other/Packages/manifest.json": json.dumps({"dependencies": {}})})
        write(self.root, "Other/Packages/manifest.json", "{ not json")
        write(self.root, "Assets/Core/Core.cs", "public class Core { int a; }\n")
        self.unresolved(self.select_json("--include-worktree"), "Assets/Core/Core.cs", "cannot be read")

    def test_an_absolute_or_differently_cased_file_dependency(self):
        for value in ("file:C:/libs/x", "file:/libs/x", "file:../../packages/COM.EXAMPLE.LIB"):
            with self.subTest(value=value):
                git(self.root, "reset", "-q", "--hard", "main")
                self.base({"Game/Packages/manifest.json": json.dumps({"dependencies": {"x": value}})})
                payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
                self.unresolved(payload, "Assets/Core/Core.cs", "Game/Packages/manifest.json takes x")
                git(self.root, "checkout", "-q", "main")
                self.commit({}, remove=["Game/Packages/manifest.json"])
                git(self.root, "checkout", "-q", "-B", "work")

    def test_a_deleted_script(self):
        payload = self.changed({}, remove=["Assets/Other/Other.cs"])
        self.unresolved(payload, "Assets/Other/Other.cs", "a script was deleted or moved")

    def test_a_script_whose_guid_changed(self):
        payload = self.changed({"Assets/Core/Core.cs.meta": meta("f" * 32)})
        self.unresolved(payload, "Assets/Core/Core.cs.meta", "the GUID of a script changed")

    def test_an_auto_referenced_assembly_while_the_project_has_code_outside_every_definition(self):
        self.base({"Assets/Other/Game.Other.asmdef": asm("Game.Other", auto=True), "Assets/Editor/Tests/EditorTests.cs": "public class EditorTests { }\n"})
        payload = self.changed({"Assets/Other/Other.cs": "public class Other { int a; }\n"})
        self.unresolved(payload, "Assets/Other/Other.cs", "Game.Other is referenced by Unity's predefined assemblies")
        git(self.root, "reset", "-q", "--hard", "main")  # a change that reaches no auto referenced assembly still narrows
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.assertEqual("narrowed", self.record(payload, "Assets/Core/Core.cs")["status"])

    def test_an_auto_referenced_assembly_narrows_when_every_script_has_a_definition(self):
        self.base({"Assets/Other/Game.Other.asmdef": asm("Game.Other", auto=True)}, remove=["Assets/Loose/Loose.cs"])
        payload = self.changed({"Assets/Other/Other.cs": "public class Other { int a; }\n"})
        self.assertEqual(["editmode:Game.Other.Tests"], self.suites(payload))

    def test_too_many_assembly_definitions(self):
        original = select.MAX_PROJECT_ASSEMBLIES
        select.MAX_PROJECT_ASSEMBLIES = 3
        self.addCleanup(setattr, select, "MAX_PROJECT_ASSEMBLIES", original)
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.unresolved(payload, "Assets/Core/Core.cs", "more than 3")


class WithoutMemberTests(ProjectCase):
    data = checks_map(False)

    def test_a_map_without_the_member_selects_as_before(self):
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n"})
        self.assertEqual({}, payload["projectAssemblies"])
        self.assertEqual(["code"], self.kinds(payload))
        self.assertEqual(WHOLE, self.suites(payload))
        self.assertEqual({}, select.project_assembly_paths(self.root, select.load_config(self.checks_path), ["Assets/Core/Core.cs"], "main", False))


class ExplainTests(ProjectCase):
    def test_explain_names_the_chain_of_references_in_json_and_text(self):
        payload = self.changed({"Assets/Core/Core.cs": "public class Core { int a; }\n",
                                "Packages/manifest.json": json.dumps({"dependencies": {}, "scopedRegistries": []})}, "--explain")
        because = next(item for item in payload["explain"] if item["check"] == "editmode:Game.Ui.Tests")["because"]
        entry = next(item for item in because if item["rule"] == "areas[0].assemblies")
        self.assertEqual(["Game.Ui.Tests", "Game.Ui", "Game.Core"], entry["via"])
        self.assertEqual("Game.Core", entry["changedAssembly"])
        self.assertEqual(["Assets/Core/Core.cs"], entry["paths"])
        structure = next(item for item in payload["explain"] if item["check"] == "structure")["because"]
        self.assertIn({"rule": "areas[0]", "assemblies": "narrowed", "kind": "narrowed"},
                      [{key: item.get(key) for key in ("rule", "assemblies", "kind")} for item in structure])
        self.assertNotIn("assemblies", next(item for item in structure if item["rule"] == "areas[2]"))  # an area without the member
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--level", "affected", "--explain")
        self.assertEqual(0, code)
        self.assertIn("PROJECT ASSEMBLIES (1)", out)
        self.assertIn("areas[0].assemblies glob Assets/**: Game.Ui.Tests -> Game.Ui -> Game.Core", out)
        self.assertIn("test assemblies by reference (affected): editmode:Game.Core.Tests, playmode:Game.Play.Tests, editmode:Game.Ui.Tests", out)

    def test_an_unresolved_path_says_why_in_the_text(self):
        self.commit({"Assets/Loose/Loose.cs": "public class Loose { int a; }\n"})
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--level", "affected")
        self.assertEqual(0, code)
        self.assertIn("Assets/Loose/Loose.cs: areas[0] not narrowed, keeps the area's kind", out)


class ConfigTests(unittest.TestCase):
    def messages(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "checks.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return select.validate_config(select.load_config(path))

    def test_a_valid_member_has_no_message_and_its_kind_counts_as_used(self):
        errors, warnings = self.messages(checks_map())
        self.assertEqual([], errors)
        self.assertEqual([], [message for message in warnings if "assemblies" in message or "'narrowed'" in message])

    def test_a_kind_the_map_lacks_is_an_error(self):
        errors, _ = self.messages(checks_map({"kind": "ghost", "runner": "edit"}))
        self.assertTrue(any("areas[0].assemblies.kind" in message and "ghost" in message for message in errors), errors)

    def test_a_narrowed_kind_with_editor_suites_up_to_the_level_is_a_warning(self):
        _, warnings = self.messages(checks_map({"kind": "code", "runner": "edit"}))
        self.assertTrue(any("areas[0].assemblies.kind" in message and "add nothing" in message for message in warnings), warnings)

    def test_a_map_with_extends_is_told_which_preset_kinds_a_narrowed_path_drops(self):
        errors, warnings = self.messages(preset_map())
        self.assertEqual([], errors)
        found = [message for message in warnings if "assemblies" in message]
        self.assertEqual(1, len(found), warnings)
        self.assertIn("drops the kinds assembly-config, asset-meta, editor-code, managed-code, tests", found[0])
        self.assertNotIn("overridesPreset", found[0])
        data = preset_map()
        data["kinds"]["managed-code"] = {"structure": ["structure"]}
        _, warnings = self.messages(data)
        self.assertTrue(any("drops the kinds assembly-config, asset-meta, editor-code, tests of" in message for message in warnings), warnings)

    def test_a_check_of_the_area_kind_that_the_member_kind_lacks_is_a_warning(self):
        data = checks_map()
        data["kinds"]["code"]["full"] = ["player:ios"]
        _, warnings = self.messages(data)
        self.assertTrue(any("lacks full player:ios" in message for message in warnings), warnings)

    def test_editor_suites_of_the_area_kind_below_the_member_level_are_a_warning(self):
        _, warnings = self.messages(checks_map({"kind": "narrowed", "runner": "edit", "playmodeRunner": "play", "level": "full"}))
        self.assertTrue(any("areas[0].assemblies" in message and "below the member's level full" in message for message in warnings), warnings)

    def test_the_schema_knows_the_member(self):
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-area-assemblies.json"))
        self.assertEqual(0, code, out)
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-area-assemblies-without-runner.json"))
        self.assertEqual(1, code)


class PresetNarrowingTests(ProjectCase):
    data = preset_map()

    def test_a_narrowed_path_drops_the_preset_kinds_the_narrowing_replaces(self):
        payload = self.changed({"Assets/Other/Other.cs": "public class Other { int a; }\n"})
        self.assertEqual(["narrowed"], self.kinds(payload))
        self.assertEqual(["editmode:Game.Other.Tests"], self.suites(payload))

    def test_a_preset_kind_the_project_replaced_stays(self):
        data = preset_map()
        data["kinds"]["managed-code"] = {"structure": ["structure"]}
        self.write_checks(data)
        payload = self.changed({"Assets/Other/Other.cs": "public class Other { int a; }\n"})
        self.assertEqual(["managed-code", "narrowed"], self.kinds(payload))

    def test_a_narrowed_path_keeps_the_heavy_preset_kinds(self):
        self.base({"Assets/Plugins/Android/Foo/Game.Foo.asmdef": asm("Game.Foo"), "Assets/Plugins/Android/Foo/Foo.cs": "public class Foo { }\n"})
        payload = self.changed({"Assets/Plugins/Android/Foo/Foo.cs": "public class Foo { int a; }\n"})
        self.assertEqual("narrowed", self.record(payload, "Assets/Plugins/Android/Foo/Foo.cs")["status"])
        self.assertEqual(["narrowed", "native"], self.kinds(payload))

    def test_an_unresolved_path_keeps_every_preset_kind(self):
        payload = self.changed({"Assets/Loose/Loose.cs": "public class Loose { int a; }\n"})
        self.assertEqual(["code", "managed-code"], self.kinds(payload))


class GraphTests(unittest.TestCase):
    def test_the_project_of_a_path(self):
        self.assertEqual("", select.unity_project_of("Assets/A/a.cs"))
        self.assertEqual("game", select.unity_project_of("game/Assets/a.cs"))
        self.assertEqual("apps/game", select.unity_project_of("apps/game/Packages/com.example.local/Runtime/a.cs"))
        self.assertIsNone(select.unity_project_of("tools/a.cs"))
        self.assertIsNone(select.unity_project_of("Assets"))

    def test_a_definition_change_of_references_and_root_namespace_only_is_judged(self):
        old = asm("A", ["B"]).encode()
        self.assertEqual(["A"], asmgraph.changed_definition("A.asmdef", asm("A", ["C"], rootNamespace="Game").encode(), old))
        for new in (asm("A2", ["B"]), asm("A", ["B"], editor=True),asm("A", ["B"], allowUnsafeCode=True), asm("A", ["B"], versionDefines=[{"name": "x"}])):
            with self.assertRaises(asmgraph.GraphError, msg=new):
                asmgraph.changed_definition("A.asmdef", new.encode(), old)

    def test_the_owner_of_a_file(self):
        graph = asmgraph.Graph([asmgraph.parse_definition("Assets/A/A.asmdef", asm("A").encode(), None)])
        tree = ["Assets/A/A.asmdef", "Assets/A/Deep/a.cs", "Assets/R/A.asmref", "Assets/R/r.cs", "Assets/X/B.asmref", "Assets/X/x.cs", "Assets/l.cs"]
        files = {"Assets/R/A.asmref": json.dumps({"reference": "A"}).encode(), "Assets/X/B.asmref": json.dumps({"reference": "B"}).encode()}
        folders = asmgraph.ProjectFolders(tree, graph, files.get)
        self.assertEqual("A", folders.owner("Assets/A/Deep/a.cs"))
        self.assertEqual("A", folders.owner("Assets/R/r.cs"))
        for path in ("Assets/l.cs", "Assets/X/x.cs"):
            with self.assertRaises(asmgraph.GraphError, msg=path):
                folders.owner(path)
        self.assertEqual("Assets/l.cs", folders.loose_code("Assets"))
        self.assertIsNone(asmgraph.ProjectFolders(tree[:-1], graph, files.get).loose_code("Assets"))

    def test_two_assembly_files_in_one_folder_are_an_error(self):
        graph = asmgraph.Graph([asmgraph.parse_definition("Assets/A/A.asmdef", asm("A").encode(), None)])
        for other in ("Assets/A/B.asmdef", "Assets/A/C.asmref"):
            with self.assertRaises(asmgraph.GraphError, msg=other):
                asmgraph.ProjectFolders(["Assets/A/A.asmdef", other], graph, lambda path: None)

    def test_a_reference_to_the_test_runner_or_nunit_makes_a_test_assembly(self):
        for document in (asm("T", ["UnityEngine.TestRunner"]), asm("T", ["UnityEditor.TestRunner"]), asm("T", precompiledReferences=["nunit.framework.dll"]),
                         asm("T", ["GUID:27619889B8BA8C24980F49EE34DBB44A"]), asm("T", ["guid:0acc523941302664db1f4e527237feb3"])):
            self.assertTrue(asmgraph.parse_definition("T.asmdef", document.encode(), None).test, document)
        self.assertFalse(asmgraph.parse_definition("T.asmdef", asm("T", ["UnityEngine.UI"]).encode(), None).test)

    def test_an_assembly_is_auto_referenced_unless_it_says_otherwise(self):
        self.assertTrue(asmgraph.parse_definition("A.asmdef", json.dumps({"name": "A"}).encode(), None).auto_referenced)
        self.assertFalse(asmgraph.parse_definition("A.asmdef", asm("A").encode(), None).auto_referenced)


if __name__ == "__main__":
    unittest.main()
