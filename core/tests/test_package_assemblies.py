"""`byContent.assemblies` of a `jsonRules` key: a pin change that touches some assemblies of a git package selects only the test assemblies that
reference them, through the `.asmdef` and `.asmref` files of the project and of the package (fixtures: the `Game` package and the `App.*` project)."""

import json
import unittest

from valtrion import asmgraph, select

from .test_package_diff import BASE_FILES, LOCK, NAME, PackageCase
from .test_select import git, run, write

GAME_GUID = "1" * 32
EXTRAS_GUID = "3" * 32

RUNNERS = {"edit": "run --edit {assemblies}", "play": "run --play {assemblies}"}
WHOLE_EDIT = "editmode:A.Tests,B.Tests,C.Tests,U.Tests@edit"


def meta(guid):
    return f"fileFormatVersion: 2\nguid: {guid}\n"


def asm(name, references=(), test=False, editor=False, **extra):
    document = {"name": name, "references": list(references), "includePlatforms": ["Editor"] if editor else []}
    if test:
        document["defineConstraints"] = ["UNITY_INCLUDE_TESTS"]
    document.update(extra)
    return json.dumps(document)


def checks_map(**assemblies):
    member = {"kind": "narrowed", "runner": "edit", "playmodeRunner": "play"}
    member.update(assemblies)
    return {
        "schema": "gdv.checks/1", "platforms": {"required": ["ios"], "dev": ["editor"]}, "unclassified": "widest", "runners": dict(RUNNERS),
        "areas": [{"glob": "**/*", "kind": "light"}],
        "jsonRules": [{"glob": "Packages/packages-lock.json", "root": "dependencies", "keys": [
            {"match": NAME, "kind": "heavy", "byContent": {"managed": "whole", "platform": "heavy", "assemblies": member}}]}],
        "kinds": {
            "light": {"L0": ["l0"]},
            "whole": {"L0": ["l0"], "L2": [WHOLE_EDIT, "playmode:P.Tests@play"]},
            "narrowed": {"L0": ["l0"], "L3": [WHOLE_EDIT, "playmode:P.Tests@play"]},
            "heavy": {"L0": ["l0"], "L2": [WHOLE_EDIT], "L3": ["player:ios"]},
        },
    }


PROJECT = {
    "Assets/Core/App.Core.asmdef": asm("App.Core", ["Game"]),
    "Assets/Ui/App.Ui.asmdef": asm("App.Ui", ["Game.Extras"]),
    "Assets/Other/App.Other.asmdef": asm("App.Other"),
    "Assets/Tests/A/A.Tests.asmdef": asm("A.Tests", ["App.Core"], test=True, editor=True),
    "Assets/Tests/B/B.Tests.asmdef": asm("B.Tests", [f"GUID:{GAME_GUID}"], test=True, editor=True),
    "Assets/Tests/C/C.Tests.asmdef": asm("C.Tests", ["App.Other"], test=True, editor=True),
    "Assets/Tests/U/U.Tests.asmdef": asm("U.Tests", ["App.Ui"], test=True, editor=True),
    "Assets/Tests/P/P.Tests.asmdef": asm("P.Tests", ["Game"], test=True),
}


class AssemblyCase(PackageCase):
    others = False

    def setUp(self):
        super().setUp()
        base = self.package_commit({
            "Runtime/Game.asmdef.meta": meta(GAME_GUID),
            "Editor/Game.Editor.asmdef.meta": meta("2" * 32),
            "Tests/Editor/Game.Tests.asmdef.meta": meta("4" * 32),
            "Extras/E.cs": "public class E { }\n",
            "Extras/Game.Extras.asmdef": asm("Game.Extras", ["Game"]),
            "Extras/Game.Extras.asmdef.meta": meta(EXTRAS_GUID),
            "Shared/Part.cs": "public class Part { }\n",
            "Shared/Game.asmref": json.dumps({"reference": "Game"}),
        })
        self.old = base
        self.pin(base, branch_main=True)
        git(self.root, "checkout", "-q", "main")
        for index, (path, text) in enumerate(PROJECT.items()):
            write(self.root, path, text)
            write(self.root, path + ".meta", meta(f"{index + 10:032x}"))
        self.write_checks(checks_map())
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "project")
        git(self.root, "checkout", "-q", "-B", "work")

    def write_checks(self, data):
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")

    def pin(self, commit, branch_main=False, **options):
        super().pin(commit, branch_main=branch_main, **options)
        if self.others:  # another git package that depends on the one under test: its assemblies cannot be read
            lock = json.loads((self.root / LOCK).read_text(encoding="utf-8"))
            lock["dependencies"]["com.example.other"] = {"version": f"{self.url}#{'a' * 40}", "source": "git", "dependencies": {NAME: "1.0.0"}, "hash": "a" * 40}
            write(self.root, LOCK, json.dumps(lock))
            if branch_main:
                git(self.root, "add", "-A")
                git(self.root, "commit", "-q", "--amend", "-m", "pin")

    def narrowed(self, files, *arguments, **options):
        payload = self.verdict(files, *arguments, **options)
        return payload, self.record(payload)

    def kinds(self, payload):
        return [kind for kind in payload["kinds"] if kind != "light"]  # the map file itself is a changed path

    def suites(self, payload, level=None):
        return sorted(item["key"] for item in payload["editorSuites"] if level in (None, item["level"]))


class NarrowingTests(AssemblyCase):
    def test_a_change_selects_the_test_assemblies_that_reach_it_by_name_by_guid_and_through_other_assemblies(self):
        payload, record = self.narrowed({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, (), "--level", "L2")
        self.assertEqual("managed", record["verdict"])
        self.assertEqual("narrowed", record["assemblies"]["status"], record["assemblies"])
        self.assertEqual(["narrowed"], self.kinds(payload))
        # A.Tests through App.Core, B.Tests by GUID, U.Tests through App.Ui and Game.Extras (an assembly of the package), P.Tests in Play Mode.
        self.assertEqual(["editmode:A.Tests", "editmode:B.Tests", "editmode:U.Tests", "playmode:P.Tests"], self.suites(payload))
        self.assertNotIn("editmode:C.Tests", self.suites(payload))
        commands = {item["runnerName"]: item["command"] for item in payload["editorCommands"]}
        self.assertEqual({"edit": "run --edit A.Tests,B.Tests,U.Tests", "play": "run --play P.Tests"}, commands)

    def test_the_chain_of_references_is_in_the_verdict_and_in_explain(self):
        new = self.package_commit({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
        self.pin(new)
        self.commit()
        payload = self.select_json("--level", "L2", "--explain")
        tests = {item["assembly"]: item for item in self.record(payload)["assemblies"]["tests"]}
        self.assertEqual(["U.Tests", "App.Ui", "Game.Extras", "Game"], tests["U.Tests"]["via"])
        self.assertEqual(["B.Tests", "Game"], tests["B.Tests"]["via"])
        self.assertEqual(["Runtime/Foo.cs"], tests["A.Tests"]["files"])
        because = next(item for item in payload["explain"] if item["check"] == "editmode:U.Tests")["because"]
        entry = next(item for item in because if item["rule"] == "byContent.assemblies")
        self.assertEqual(["U.Tests", "App.Ui", "Game.Extras", "Game"], entry["via"])
        self.assertEqual("Game", entry["changedAssembly"])
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--level", "L2", "--explain")
        self.assertEqual(0, code)
        self.assertIn("U.Tests -> App.Ui -> Game.Extras -> Game", out)
        self.assertIn("test assemblies by reference (L2): editmode:A.Tests", out)

    def test_a_change_inside_one_assembly_of_the_package_leaves_out_the_tests_of_the_others(self):
        payload, record = self.narrowed({"Extras/E.cs": "public class E { int a; }\n"}, (), "--level", "L2")
        self.assertEqual(["editmode:U.Tests"], self.suites(payload))
        self.assertEqual([{"assembly": "Game.Extras", "files": ["Extras/E.cs"]}], record["assemblies"]["changed"])

    def test_an_editor_assembly_of_the_package_that_nothing_references_selects_nothing(self):
        payload, record = self.narrowed({"Editor/Tool.cs": "public class Tool { int a; }\n"}, (), "--level", "L2")
        self.assertEqual("narrowed", record["assemblies"]["status"])
        self.assertEqual([], self.suites(payload))
        self.assertEqual(["narrowed"], self.kinds(payload))

    def test_a_document_and_the_meta_file_of_a_folder_belong_to_no_assembly_and_change_nothing(self):
        payload, record = self.narrowed({"CHANGELOG.md": "more\n", "Documentation~/index.md": "more\n", "Shared.meta": "folderAsset: yes\n"}, (), "--level", "L2")
        self.assertEqual("narrowed", record["assemblies"]["status"], record["assemblies"])
        self.assertEqual([], self.suites(payload))

    def test_an_asmref_folder_counts_for_the_assembly_it_names(self):
        payload, record = self.narrowed({"Shared/Part.cs": "public class Part { int a; }\n"}, (), "--level", "L2")
        self.assertEqual([{"assembly": "Game", "files": ["Shared/Part.cs"]}], record["assemblies"]["changed"])
        self.assertEqual(["editmode:A.Tests", "editmode:B.Tests", "editmode:U.Tests", "playmode:P.Tests"], self.suites(payload))

    def test_an_asmref_by_guid_and_a_changed_asmref_count_too(self):
        payload, record = self.narrowed({"Shared/Game.asmref": json.dumps({"reference": f"GUID:{EXTRAS_GUID}"})}, (), "--level", "L2")
        self.assertEqual({"Game", "Game.Extras"}, {item["assembly"] for item in record["assemblies"]["changed"]})
        self.assertEqual(["editmode:A.Tests", "editmode:B.Tests", "editmode:U.Tests", "playmode:P.Tests"], self.suites(payload))

    def test_a_renamed_assembly_still_reaches_what_references_the_old_name(self):
        payload, record = self.narrowed({"Runtime/Game.asmdef": json.dumps({"name": "Game2", "references": [], "includePlatforms": [], "defineConstraints": []})}, (), "--level", "L2")
        self.assertEqual("narrowed", record["assemblies"]["status"], record["assemblies"])
        self.assertEqual({"Game", "Game2"}, {item["assembly"] for item in record["assemblies"]["changed"]})
        self.assertIn("editmode:A.Tests", self.suites(payload))

    def test_an_editor_assembly_of_the_package_whose_definition_changed_reaches_only_what_references_it(self):
        payload, _ = self.narrowed({"Editor/Game.Editor.asmdef": asm("Game.Editor", ["Game.Extras"], editor=True)}, (), "--level", "L2")
        self.assertEqual([], self.suites(payload))  # the editor assembly changed; nothing references it

    def test_an_assembly_that_takes_a_define_from_the_version_of_the_package_is_changed_with_every_pin(self):
        git(self.root, "checkout", "-q", "main")
        write(self.root, "Assets/Ver/App.Ver.asmdef", asm("App.Ver", versionDefines=[{"name": NAME, "expression": "1.0.0", "define": "HAS_LIB"}]))
        write(self.root, "Assets/Ver/App.Ver.asmdef.meta", meta("f" * 32))
        write(self.root, "Assets/Tests/V/V.Tests.asmdef", asm("V.Tests", ["App.Ver"], test=True, editor=True))
        write(self.root, "Assets/Tests/V/V.Tests.asmdef.meta", meta("e" * 32))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "versioned")
        git(self.root, "checkout", "-q", "-B", "work")
        payload, _ = self.narrowed({"CHANGELOG.md": "more\n"}, (), "--level", "L2")
        self.assertEqual(["editmode:V.Tests"], self.suites(payload))

    def test_a_working_tree_assembly_counts_only_with_include_worktree(self):
        new = self.package_commit({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
        self.pin(new)
        self.commit()
        write(self.root, "Assets/Tests/W/W.Tests.asmdef", asm("W.Tests", ["Game"], test=True, editor=True))
        write(self.root, "Assets/Tests/W/W.Tests.asmdef.meta", meta("d" * 32))
        self.assertNotIn("editmode:W.Tests", self.suites(self.select_json("--level", "L2")))
        self.assertIn("editmode:W.Tests", self.suites(self.select_json("--level", "L2", "--include-worktree")))


class LevelTests(AssemblyCase):
    def test_the_whole_set_stays_at_the_levels_the_map_keeps_it(self):
        files = {"Extras/E.cs": "public class E { int a; }\n"}
        payload, _ = self.narrowed(files, (), "--level", "L2")
        self.assertEqual(["editmode:U.Tests"], self.suites(payload))
        self.assertEqual(["editmode:A.Tests", "editmode:B.Tests", "editmode:C.Tests", "editmode:U.Tests", "playmode:P.Tests"],
                         self.suites(self.select_json("--level", "L3")))

    def test_below_the_level_of_the_member_nothing_is_added(self):
        payload, _ = self.narrowed({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, (), "--level", "L1")
        self.assertEqual([], self.suites(payload))

    def test_the_level_of_the_member_places_the_narrowed_suites(self):
        self.write_checks(checks_map(level="L3"))
        self.commit()
        payload, _ = self.narrowed({"Extras/E.cs": "public class E { int a; }\n"})
        self.assertEqual({"L3"}, {item["level"] for item in payload["editorSuites"] if item["key"] == "editmode:U.Tests"} - {"L2"} or {"L3"})
        self.assertEqual([], self.suites(self.select_json("--level", "L2")))
        self.assertEqual(["editmode:U.Tests"], [key for key in self.suites(self.select_json("--level", "L3")) if key == "editmode:U.Tests"])


class FallbackTests(AssemblyCase):
    """Whatever the graph cannot say keeps `managed` (the verdict was read); an unresolved diff and a `platform` verdict keep what they kept."""

    def unresolved(self, files, reason, **options):
        payload, record = self.narrowed(files, (), "--level", "L2", **options)
        self.assertEqual("managed", record["verdict"], record)
        self.assertEqual("unresolved", record["assemblies"]["status"], record["assemblies"])
        self.assertIn(reason, record["assemblies"]["reason"])
        self.assertEqual(["whole"], self.kinds(payload))
        self.assertEqual(["editmode:A.Tests", "editmode:B.Tests", "editmode:C.Tests", "editmode:U.Tests", "playmode:P.Tests"], self.suites(payload))
        return payload

    def test_a_changed_file_outside_any_assembly(self):
        self.unresolved({"Loose/Thing.cs": "public class Thing { }\n"}, "Loose/Thing.cs: belongs to no assembly")

    def test_a_change_of_a_test_assembly_of_the_package(self):
        self.unresolved({"Tests/Editor/FooTests.cs": "class T { }\n"}, "a test assembly of the package changed")

    def test_an_assembly_definition_of_the_project_that_cannot_be_read(self):
        git(self.root, "checkout", "-q", "main")
        write(self.root, "Assets/Broken/Broken.asmdef", "{ not json")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "broken")
        git(self.root, "checkout", "-q", "-B", "work")
        self.unresolved({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, "Broken.asmdef: cannot be read")

    def test_two_assemblies_with_the_same_name(self):
        git(self.root, "checkout", "-q", "main")
        write(self.root, "Assets/Dup/Game.asmdef", asm("Game"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "duplicate")
        git(self.root, "checkout", "-q", "-B", "work")
        self.unresolved({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, "the assembly 'Game' is defined in")

    def test_a_guid_that_resolves_to_nothing_while_an_assembly_has_no_meta_file(self):
        git(self.root, "checkout", "-q", "main")
        write(self.root, "Assets/NoMeta/App.NoMeta.asmdef", asm("App.NoMeta"))
        write(self.root, "Assets/Tests/N/N.Tests.asmdef", asm("N.Tests", ["GUID:" + "9" * 32], test=True, editor=True))
        write(self.root, "Assets/Tests/N/N.Tests.asmdef.meta", meta("c" * 32))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "no meta")
        git(self.root, "checkout", "-q", "-B", "work")
        self.unresolved({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, "has no readable .meta file")

    def test_a_guid_of_another_package_is_not_followed_when_every_meta_file_is_there(self):
        git(self.root, "checkout", "-q", "main")
        write(self.root, "Assets/Tests/N/N.Tests.asmdef", asm("N.Tests", ["GUID:" + "9" * 32, "UnityEngine.TestRunner"], test=True, editor=True))
        write(self.root, "Assets/Tests/N/N.Tests.asmdef.meta", meta("c" * 32))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "other package")
        git(self.root, "checkout", "-q", "-B", "work")
        payload, record = self.narrowed({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, (), "--level", "L2")
        self.assertEqual("narrowed", record["assemblies"]["status"])
        self.assertNotIn("editmode:N.Tests", self.suites(payload))

    def test_a_play_mode_test_assembly_without_a_play_mode_runner(self):
        data = checks_map()
        del data["jsonRules"][0]["keys"][0]["byContent"]["assemblies"]["playmodeRunner"]
        self.write_checks(data)
        self.commit()
        self.unresolved({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, "has no playmodeRunner")

    def test_another_package_that_depends_on_it_and_cannot_be_read(self):
        type(self).others = True
        self.addCleanup(setattr, type(self), "others", False)
        git(self.root, "checkout", "-q", "main")
        self.pin(self.old)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "other package on main")
        git(self.root, "checkout", "-q", "-B", "work")
        self.unresolved({"Runtime/Foo.cs": "public class Foo { int a; }\n"}, "com.example.other")

    def test_an_offline_run_and_a_platform_verdict_keep_what_they_always_kept(self):
        new = self.package_commit({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
        self.pin(new)
        self.commit()
        offline = self.select_json("--level", "L2", "--offline")
        self.assertEqual("unresolved", self.record(offline)["verdict"])
        self.assertNotIn("assemblies", self.record(offline))
        self.assertEqual(["heavy"], self.kinds(offline))
        git(self.package, "reset", "-q", "--hard", self.old)
        self.pin(self.old)
        payload = self.verdict({"Runtime/Foo.cs": "#if UNITY_IOS\nclass A { }\n#endif\n"}, (), "--level", "L2")
        self.assertEqual("platform", self.record(payload)["verdict"])
        self.assertNotIn("assemblies", self.record(payload))
        self.assertEqual(["heavy"], self.kinds(payload))


class ConfigTests(PackageCase):
    def errors_and_warnings(self, **assemblies):
        data = checks_map(**assemblies)
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        return select.validate_config(select.load_config(self.checks_path))

    def test_a_valid_member_has_no_message(self):
        errors, warnings = self.errors_and_warnings()
        self.assertEqual([], errors)
        self.assertEqual([], [message for message in warnings if "assemblies" in message])

    def test_a_kind_the_map_lacks_is_an_error(self):
        errors, _ = self.errors_and_warnings(kind="ghost")
        self.assertTrue(any("byContent.assemblies.kind" in message and "ghost" in message for message in errors), errors)

    def test_a_runner_without_an_assembly_filter_is_a_warning(self):
        data = checks_map()
        data["runners"]["edit"] = "run --all"
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, warnings = select.validate_config(select.load_config(self.checks_path))
        self.assertEqual([], errors)
        self.assertTrue(any("assemblies.runner" in message and "placeholder" in message for message in warnings), warnings)

    def test_a_mistyped_placeholder_is_an_error(self):
        data = checks_map()
        data["runners"]["edit"] = "run {assembly}"
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, _ = select.validate_config(select.load_config(self.checks_path))
        self.assertTrue(any("unknown placeholder" in message for message in errors), errors)

    def test_a_narrowed_kind_with_editor_suites_up_to_the_level_is_a_warning(self):
        _, warnings = self.errors_and_warnings(kind="whole")
        self.assertTrue(any("byContent.assemblies.kind" in message and "add nothing" in message for message in warnings), warnings)

    def test_the_schema_knows_the_member(self):
        from .test_select import CONTRACTS
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-by-content-assemblies.json"))
        self.assertEqual(0, code, out)
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-by-content-assemblies-without-runner.json"))
        self.assertEqual(1, code)


class GraphTests(unittest.TestCase):
    def test_the_shortest_chain_wins_and_ties_go_by_name(self):
        graph = asmgraph.Graph([asmgraph.parse_definition(f"{name}.asmdef", asm(name, refs).encode(), None)
                                for name, refs in (("T", ["B", "A"]), ("A", ["S"]), ("B", ["S"]), ("S", []))])
        self.assertEqual({"S": ["S"], "A": ["A", "S"], "B": ["B", "S"], "T": ["T", "A", "S"]}, graph.dependents(["S"]))

    def test_a_cycle_does_not_loop(self):
        graph = asmgraph.Graph([asmgraph.parse_definition(f"{name}.asmdef", asm(name, refs).encode(), None) for name, refs in (("A", ["B"]), ("B", ["A"]))])
        self.assertEqual({"A": ["A"], "B": ["B", "A"]}, graph.dependents(["A"]))

    def test_a_definition_that_is_not_an_object_or_has_no_name_is_an_error(self):
        for data in (b"[]", b"{}", b'{"name": 3}', b'{"name": "A", "references": [1]}', b"\xff"):
            with self.assertRaises(asmgraph.GraphError, msg=data):
                asmgraph.parse_definition("A.asmdef", data, None)

    def test_a_test_assembly_is_found_by_its_define_constraint_or_its_unity_reference(self):
        self.assertTrue(asmgraph.parse_definition("A.asmdef", asm("A", test=True).encode(), None).test)
        self.assertTrue(asmgraph.parse_definition("A.asmdef", json.dumps({"name": "A", "optionalUnityReferences": ["TestAssemblies"]}).encode(), None).test)
        self.assertFalse(asmgraph.parse_definition("A.asmdef", asm("A").encode(), None).test)

    def test_the_guid_is_read_from_a_meta_file(self):
        self.assertEqual("a" * 32, asmgraph.guid_of_meta(meta("A" * 32).encode()))
        self.assertIsNone(asmgraph.guid_of_meta(b"fileFormatVersion: 2\n"))

    def package(self, *definitions):
        return asmgraph.Graph([asmgraph.parse_definition(f"{name}.asmdef", asm(name, refs, test=test).encode(), None) for name, refs, test in definitions])

    def changes(self, graph, tree, changes):
        return asmgraph.changed_assemblies(changes, tree, graph, lambda path: b"", lambda path: b"")

    def test_a_test_assembly_of_the_package_that_references_what_changed_is_an_error(self):
        package = self.package(("Lib", [], False), ("Lib.Tests", ["Lib"], True))
        with self.assertRaises(asmgraph.GraphError):
            asmgraph.narrow([], package.assemblies, "com.example.lib", {"Lib": ["Runtime/a.cs"]})

    def test_precompiled_code_an_added_asmref_and_a_file_outside_any_assembly_are_errors(self):
        graph = asmgraph.Graph([asmgraph.parse_definition("Lib/Lib.asmdef", asm("Lib").encode(), None)])
        tree = ["Lib/Lib.asmdef", "Lib/Sub/x.asmref", "Lib/Sub/y.cs", "Lib/Plugin.dll", "loose.cs"]
        for change in (("M", "Lib/Plugin.dll"), ("A", "Lib/Sub/x.asmref"), ("M", "loose.cs")):
            with self.assertRaises(asmgraph.GraphError, msg=change):
                self.changes(graph, tree, [change])

    def test_a_file_below_an_assembly_belongs_to_it(self):
        graph = self.package(("Lib", [], False))
        self.assertEqual({"Lib": ["deep/er/a.cs"]}, self.changes(graph, ["Lib.asmdef", "deep/er/a.cs"], [("M", "deep/er/a.cs")]))


if __name__ == "__main__":
    unittest.main()
