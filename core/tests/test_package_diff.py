"""`byContent` of a `jsonRules` key: a git dependency is judged by what changed inside it, using a local repository as the package's repository."""

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from valtrion import packagediff, select

from .test_presets import PRESET, PresetCase
from .test_select import CONTRACTS, git, run, write

NAME = "com.example.lib"
LOCK = "Packages/packages-lock.json"

BASE_FILES = {
    "package.json": json.dumps({"name": NAME, "version": "1.0.0", "dependencies": {"com.unity.modules.ui": "1.0.0"}}),
    "Runtime/Foo.cs": "public class Foo { }\n",
    "Runtime/Game.asmdef": json.dumps({"name": "Game", "references": [], "includePlatforms": [], "defineConstraints": []}),
    "Editor/Tool.cs": "public class Tool { }\n",
    "Editor/Game.Editor.asmdef": json.dumps({"name": "Game.Editor", "includePlatforms": ["Editor"]}),
    "Tests/Editor/Game.Tests.asmdef": json.dumps({"name": "Game.Tests", "includePlatforms": ["Editor"], "defineConstraints": ["UNITY_INCLUDE_TESTS"]}),
    "Documentation~/index.md": "docs\n",
    "CHANGELOG.md": "changes\n",
}


class PackageCase(PresetCase):
    """A project repository whose lock file pins a git package, and a second repository that is that package's repository."""

    FOLDER_NAME = "Pkg"

    def setUp(self):
        super().setUp()
        package_folder = tempfile.TemporaryDirectory()
        self.addCleanup(package_folder.cleanup)
        self.package = Path(package_folder.name).resolve()
        git(self.package, "init", "-q", "-b", "main")
        git(self.package, "config", "uploadpack.allowAnySHA1InWant", "true")
        git(self.package, "config", "uploadpack.allowFilter", "true")
        self.url = self.package.as_posix()
        self.package_commit(BASE_FILES)
        self.old = git(self.package, "rev-parse", "HEAD")
        self.pin(self.old, branch_main=True)

    @property
    def location(self):
        return f"{self.url}?path={quote(self.FOLDER_NAME, safe='+')}"

    def package_commit(self, files, delete=(), outside=None):
        for relative, text in files.items():
            write(self.package, f"{self.FOLDER_NAME}/{relative}", text)
        for relative in delete:
            (self.package / self.FOLDER_NAME / relative).unlink()
        for relative, text in (outside or {}).items():
            write(self.package, relative, text)
        git(self.package, "add", "-A")
        git(self.package, "commit", "-q", "-m", "package change")
        return git(self.package, "rev-parse", "HEAD")

    def lock_entry(self, commit, **changes):
        entry = {"version": f"{self.location}#{commit}", "depth": 0, "source": "git", "dependencies": {}, "hash": commit}
        entry.update(changes)
        return entry

    def pin(self, commit, branch_main=False, name=NAME, folder="", **changes):
        manifest = {"dependencies": {name: f"{self.location}#{commit}"}}
        lock = {"dependencies": {name: self.lock_entry(commit, **changes)}}
        if branch_main:
            git(self.root, "checkout", "-q", "main")
        prefix = f"{folder}/" if folder else ""
        write(self.root, f"{prefix}Packages/manifest.json", json.dumps(manifest))
        write(self.root, f"{prefix}{LOCK}", json.dumps(lock))
        if branch_main:
            git(self.root, "add", "-A")
            git(self.root, "commit", "-q", "-m", "pin")
            git(self.root, "checkout", "-q", "-B", "work")

    def verdict(self, files, delete=(), *arguments, name=NAME, outside=None):
        new = self.package_commit(files, delete, outside)
        self.pin(new, name=name)
        self.commit()
        return self.select_json(*arguments)

    def record(self, payload, name=NAME, lock=LOCK):
        return payload["packageDiffs"][lock][name]


class JudgementTests(PackageCase):
    def test_a_change_of_managed_code_editor_code_tests_and_documents_is_managed(self):
        payload = self.verdict({"Runtime/Foo.cs": "public class Foo { int a; }\n", "Editor/Tool.cs": "public class Tool { int b; }\n",
                                "Documentation~/index.md": "more docs\n", "CHANGELOG.md": "more changes\n", "Tests/Editor/FooTests.cs": "class T { }\n"})
        self.assertEqual("managed", self.record(payload)["verdict"], self.record(payload))
        self.assertEqual([], self.record(payload)["reasons"])
        self.assertEqual(["package-managed"], payload["kinds"])
        self.assertEqual([], self.heavy(payload))
        self.assertEqual(5, self.record(payload)["files"])

    def platform_for(self, files, reason, **options):
        payload = self.verdict(files, **options)
        record = self.record(payload)
        self.assertEqual("platform", record["verdict"], record)
        self.assertEqual(["package-platform"], payload["kinds"])
        self.assertEqual(["device", "player"], self.heavy(payload))
        self.assertIn(reason, " | ".join(record["reasons"]))
        return record

    def test_a_native_file_makes_it_platform(self):
        self.platform_for({"Plugins/Android/lib.aar": "x"}, "native, shader or platform content")

    def test_a_shader_makes_it_platform(self):
        self.platform_for({"Runtime/Effect.shader": "x"}, "native, shader or platform content")

    def test_a_link_file_and_a_platform_folder_make_it_platform(self):
        self.platform_for({"Runtime/link.xml": "<linker/>"}, "native, shader or platform content")

    def test_an_unknown_file_makes_it_platform_because_the_map_does_not_know_it(self):
        self.platform_for({"Runtime/data.bin": "x"}, "no kind of the map matches it")

    def test_a_platform_define_in_runtime_code_makes_it_platform(self):
        self.platform_for({"Runtime/Foo.cs": "#if UNITY_IOS\nclass A { }\n#endif\n"}, "a file with a platform define changed")

    def test_the_legacy_iphone_define_counts(self):
        self.platform_for({"Runtime/Foo.cs": "#if UNITY_IPHONE\nclass A { }\n#endif\n"}, "a file with a platform define changed")

    def test_a_change_inside_an_existing_platform_block_makes_it_platform(self):
        base = self.package_commit({"Runtime/Plat.cs": "#if UNITY_ANDROID\nclass A { int a; }\n#endif\n"})
        self.pin(base, branch_main=True)
        self.platform_for({"Runtime/Plat.cs": "#if UNITY_ANDROID\nclass A { int a; int b; }\n#endif\n"}, "a file with a platform define changed")

    def test_reflection_and_native_interop_in_runtime_code_make_it_platform(self):
        for text in ("using System.Reflection;\nclass Foo { }\n", "class Foo { AndroidJavaClass c; }\n", "class Foo { [DllImport(\"x\")] static extern void F(); }\n"):
            record = self.platform_for({"Runtime/Foo.cs": text}, "reflection or native interop changed")
            self.assertEqual(1, record["files"])
            git(self.package, "reset", "-q", "--hard", self.old)
            self.pin(self.old)

    def test_a_platform_define_in_editor_code_stays_managed(self):
        payload = self.verdict({"Editor/Tool.cs": "#if UNITY_IOS\nclass A { }\n#endif\n"})
        self.assertEqual("managed", self.record(payload)["verdict"], self.record(payload))

    def test_a_folder_called_editor_is_not_trusted_without_an_editor_assembly(self):
        self.platform_for({"Runtime/Editor/Hack.cs": "using System.Reflection;\nclass Hack { }\n"}, "reflection or native interop changed")

    def test_a_file_with_no_assembly_definition_above_it_is_runtime_code(self):
        self.platform_for({"Loose/Editor/Hack.cs": "#if UNITY_IOS\nclass Hack { }\n#endif\n"}, "a file with a platform define changed")

    def test_an_assembly_definition_is_judged_by_its_platform_members(self):
        self.platform_for({"Runtime/Game.asmdef": json.dumps({"name": "Game", "references": [], "includePlatforms": ["iOS"], "defineConstraints": []})},
                          "includePlatforms")

    def test_an_assembly_definition_whose_references_changed_stays_managed(self):
        payload = self.verdict({"Runtime/Game.asmdef": json.dumps({"name": "Game", "references": ["Other"], "includePlatforms": [], "defineConstraints": []})})
        self.assertEqual("managed", self.record(payload)["verdict"], self.record(payload))

    def test_an_added_and_an_unreadable_assembly_definition_are_platform(self):
        self.platform_for({"Runtime/Extra.asmdef": json.dumps({"name": "Extra"})}, "assembly definition added")

    def test_an_assembly_definition_that_is_not_an_object_is_platform(self):
        self.platform_for({"Runtime/Game.asmdef": "[]"}, "(unreadable)")

    def test_package_json_is_judged_by_everything_but_the_harmless_members(self):
        base = json.loads(BASE_FILES["package.json"])
        harmless = self.verdict({"package.json": json.dumps({**base, "version": "1.1.0", "displayName": "New name"})})
        self.assertEqual("managed", self.record(harmless)["verdict"], self.record(harmless))

    def test_a_change_of_the_dependencies_of_the_package_makes_it_platform(self):
        base = json.loads(BASE_FILES["package.json"])
        self.platform_for({"package.json": json.dumps({**base, "dependencies": {"com.unity.modules.ui": "1.0.0", "com.unity.modules.androidjni": "1.0.0"}})},
                          "package.json: dependencies changed")

    def test_a_new_commit_without_package_json_at_the_folder_is_unresolved(self):
        new = self.package_commit({}, delete=("package.json",))
        self.pin(new)
        self.commit()
        payload = self.select_json()
        self.assertEqual("unresolved", self.record(payload)["verdict"])
        self.assertIn("no package.json", self.record(payload)["reason"])

    def test_a_plugin_importer_in_a_meta_file_makes_it_platform(self):
        base = self.package_commit({"Runtime/Foo.cs.meta": "fileFormatVersion: 2\nMonoImporter:\n"})
        self.pin(base, branch_main=True)
        self.platform_for({"Runtime/Foo.cs.meta": "fileFormatVersion: 2\nPluginImporter:\n  isPreloaded: 0\n"}, "plugin importer changed")

    def test_a_removed_native_file_counts_too(self):
        added = self.package_commit({"Plugins/lib.so": "x"})
        self.pin(added, branch_main=True)
        payload = self.verdict({}, delete=("Plugins/lib.so",))
        self.assertEqual("platform", self.record(payload)["verdict"])

    def test_a_moved_file_counts_as_removed_and_added(self):
        (self.package / self.FOLDER_NAME / "Plugins").mkdir()
        git(self.package, "mv", f"{self.FOLDER_NAME}/Runtime/Foo.cs", f"{self.FOLDER_NAME}/Plugins/Foo.cs")
        record = self.platform_for({}, "native, shader or platform content")
        self.assertEqual(2, record["files"], "a removal and an addition, never one rename")

    def test_the_reasons_are_limited_and_the_files_are_counted(self):
        payload = self.verdict({f"Plugins/Android/lib{number}.aar": "x" for number in range(9)})
        self.assertEqual(9, self.record(payload)["files"])
        self.assertEqual(5, len(self.record(payload)["reasons"]))

    def test_a_change_of_more_than_the_limit_is_platform_without_reading_the_files(self):
        record = self.platform_for({f"Runtime/Many/File{number}.cs": "class C { }\n" for number in range(packagediff.MAX_FILES + 1)}, "more than 500")
        self.assertEqual(packagediff.MAX_FILES + 1, record["files"])

    def test_a_commit_that_changed_nothing_in_the_package_folder_is_managed_with_no_files(self):
        payload = self.verdict({}, outside={"Other/readme.txt": "elsewhere\n"})
        self.assertEqual(("managed", 0), (self.record(payload)["verdict"], self.record(payload)["files"]))


class FailClosedTests(PackageCase):
    def test_offline_keeps_the_key_kind(self):
        new = self.package_commit({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
        self.pin(new)
        self.commit()
        payload = self.select_json("--offline")
        self.assertEqual({"verdict": "unresolved", "reason": "offline"}, self.record(payload))
        self.assertEqual(["package-platform"], payload["kinds"], "the `*` fallback of the preset is heavy")

    def test_a_repository_that_cannot_be_reached_keeps_the_key_kind_and_says_why(self):
        self.pin("a" * 40)
        self.commit()
        payload = self.select_json()
        self.assertEqual("unresolved", self.record(payload)["verdict"])
        self.assertIn("git fetch", self.record(payload)["reason"])
        self.assertEqual(["package-platform"], payload["kinds"])

    def test_a_dependency_that_is_not_a_git_source_keeps_the_key_kind(self):
        self.pin("b" * 40, source="registry")
        self.commit()
        payload = self.select_json()
        self.assertEqual("unresolved", self.record(payload)["verdict"])
        self.assertIn("not a git dependency", self.record(payload)["reason"])

    def test_a_dependency_that_is_added_keeps_the_key_kind(self):
        self.pin(self.old, name="com.example.other")
        self.commit()
        record = self.record(self.select_json(), "com.example.other")
        self.assertEqual("unresolved", record["verdict"])
        self.assertIn("added or removed", record["reason"])

    def test_the_same_commit_before_and_after_proves_nothing(self):
        manifest = {"dependencies": {NAME: f"{self.location}#renamed-ref"}}
        write(self.root, "Packages/manifest.json", json.dumps(manifest))
        self.commit()
        payload = self.select_json()
        self.assertEqual("unresolved", self.record(payload)["verdict"])
        self.assertIn("same commit", self.record(payload)["reason"])
        self.assertEqual(["package-platform"], payload["kinds"], "a stale lock file never gives the light kind")

    def test_a_valtrion_package_that_cannot_be_read_keeps_its_own_light_kind(self):
        name = "com.goryned.valtrion.unity"
        self.pin(self.old, branch_main=True, name=name)
        self.pin("c" * 40, name=name)
        self.commit()
        payload = self.select_json()
        self.assertEqual("unresolved", self.record(payload, name)["verdict"])
        self.assertEqual(["package-managed"], payload["kinds"])

    def test_a_repository_or_folder_change_is_unresolved(self):
        new = self.package_commit({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
        self.pin(new, version=f"{self.url}?path=Other#{new}")
        self.commit()
        self.assertIn("repository or the folder", self.record(self.select_json())["reason"])

    def test_a_folder_that_does_not_exist_is_not_nothing_changed(self):
        new = self.package_commit({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
        self.pin(self.old, branch_main=True, version=f"{self.url}?path=Missing#{self.old}")
        self.pin(new, version=f"{self.url}?path=Missing#{new}")
        self.commit()
        record = self.record(self.select_json())
        self.assertEqual("unresolved", record["verdict"], record)
        self.assertIn("no package.json at the package folder 'Missing'", record["reason"])

    def test_a_hostile_url_is_never_run(self):
        marker = self.package / "marker.txt"
        for url in ("-x", "--upload-pack=touch", f'ext::sh -c "touch {marker.as_posix()}"'):
            self.pin("d" * 40, version=f"{url}?path=Pkg#{'d' * 40}")
            self.commit()
            record = self.record(self.select_json())
            self.assertEqual("unresolved", record["verdict"], url)
        self.assertFalse(marker.exists())

    def test_two_lock_files_with_the_same_dependency_are_judged_apart(self):
        docs = self.package_commit({"Documentation~/index.md": "more docs\n"})
        native = self.package_commit({"Plugins/Android/lib.aar": "x"})
        git(self.root, "checkout", "-q", "main")
        self.pin(self.old, folder="Sub")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "second project")
        git(self.root, "checkout", "-q", "-B", "work")
        self.pin(docs)
        self.pin(native, folder="Sub")
        self.commit()
        payload = self.select_json()
        self.assertEqual("managed", self.record(payload)["verdict"])
        self.assertEqual("platform", self.record(payload, lock=f"Sub/{LOCK}")["verdict"])


class FolderNameTests(unittest.TestCase):
    """A package folder with a space or a plus, as it is written in the `path=` of a dependency."""

    def check(self, folder):
        class Case(PackageCase):
            FOLDER_NAME = folder

            def runTest(self):
                payload = self.verdict({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
                self.assertEqual("managed", self.record(payload)["verdict"], self.record(payload))
                self.assertEqual(1, self.record(payload)["files"])

        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(Case())
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_a_folder_with_a_space(self):
        self.check("My Pkg")

    def test_a_folder_with_a_plus(self):
        self.check("My+Pkg")


class OutputTests(PackageCase):
    def test_the_text_has_a_package_diff_block_and_explain_names_the_verdict(self):
        new = self.package_commit({"Plugins/Android/lib.aar": "x"})
        self.pin(new)
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--explain")
        self.assertEqual(0, code)
        self.assertIn("PACKAGE DIFF (1)", out)
        self.assertIn(f"{LOCK}: {NAME}: platform ({self.old[:8]}..{new[:8]}, 1 file(s))", out)
        payload = self.select_json("--explain")
        because = next(item for item in payload["explain"] if item["check"] == "player:android")["because"]
        self.assertTrue(any(item.get("byContent") == "platform" and item["from"] == PRESET for item in because), because)

    def test_a_project_key_with_by_content_uses_its_own_kinds(self):
        rule = {"glob": "Packages/packages-lock.json", "root": "dependencies", "keys": [
            {"match": "com.example.*", "kind": "package-platform", "byContent": {"managed": "docs", "platform": "native"}}]}
        self.write_map(jsonRules=[rule])
        new = self.package_commit({"Runtime/Foo.cs": "public class Foo { int a; }\n"})
        self.pin(new)
        self.commit()
        payload = self.select_json()
        self.assertEqual("managed", self.record(payload)["verdict"])
        self.assertIn("docs", payload["pathKinds"][LOCK])

    def test_the_built_in_floor_holds_whatever_the_map_says(self):
        light = {"schema": "gdv.checks/1", "platforms": {"required": ["ios"]}, "unclassified": "widest",
                 "areas": [{"glob": "**/*", "kind": "light"}],
                 "jsonRules": [{"glob": "Packages/packages-lock.json", "root": "dependencies", "keys": [
                     {"match": "*", "kind": "heavy", "byContent": {"managed": "light", "platform": "heavy"}}]}],
                 "kinds": {"light": {"L0": ["l0"]}, "heavy": {"L0": ["l0"], "L3": ["player:ios"]}}}
        self.checks_path.write_text(json.dumps(light), encoding="utf-8")
        new = self.package_commit({"Plugins/lib.so": "x", "Runtime/Effect.shader": "x"})
        self.pin(new)
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json")
        record = json.loads(out)["packageDiffs"][LOCK][NAME]
        self.assertEqual("platform", record["verdict"], record)


class ConfigTests(PackageCase):
    def test_a_by_content_kind_that_the_map_lacks_is_an_error(self):
        rule = {"glob": "Packages/packages-lock.json", "root": "dependencies", "keys": [
            {"match": "*", "kind": "docs", "byContent": {"managed": "docs", "platform": "ghost"}}]}
        self.write_map(jsonRules=[rule])
        errors, _ = select.validate_config(self.load())
        self.assertTrue(any("byContent.platform" in message and "ghost" in message for message in errors), errors)

    def test_the_schema_knows_by_content(self):
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-by-content.json"))
        self.assertEqual(0, code, out)
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-by-content-without-platform.json"))
        self.assertEqual(1, code)

    def test_the_offline_flag_is_a_common_argument_so_report_has_it_too(self):
        parser = argparse.ArgumentParser()
        select._add_common(parser)
        self.assertTrue(parser.parse_args(["--offline"]).offline)
        self.assertFalse(parser.parse_args([]).offline)


class SourceTests(unittest.TestCase):
    def test_a_lock_entry_gives_the_repository_the_folder_and_the_commit(self):
        entry = {"version": "https://example.com/team/lib.git?path=/Packages/lib#main", "source": "git", "hash": "d" * 40}
        self.assertEqual(packagediff.Source("https://example.com/team/lib.git", "Packages/lib", "d" * 40), packagediff.entry_source(entry))
        self.assertEqual("", packagediff.entry_source({**entry, "version": "https://example.com/team/lib.git#main"}).subpath)

    def test_the_path_is_read_as_unity_reads_it(self):
        entry = {"version": "https://example.com/lib.git?path=/My+Pkg%20Two#main", "source": "git", "hash": "d" * 40}
        self.assertEqual("My+Pkg Two", packagediff.entry_source(entry).subpath)

    def test_anything_else_is_not_a_source(self):
        good = {"version": "https://example.com/lib.git#main", "source": "git", "hash": "e" * 40}
        for entry in (None, "1.0.0", {**good, "source": "registry"}, {**good, "hash": "short"}, {**good, "hash": None}, {**good, "version": 3},
                      {"source": "git", "hash": "e" * 40}, {**good, "version": "-x#main"}, {**good, "version": "https://x/y.git?path=../up#m"},
                      {**good, "version": "https://x/y\n.git#m"}):
            self.assertIsNone(packagediff.entry_source(entry), entry)


if __name__ == "__main__":
    unittest.main()
