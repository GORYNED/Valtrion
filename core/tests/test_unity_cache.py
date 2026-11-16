"""The result cache of `valtrion unity run` (unitycache.py): the key and what changes it, the store and its trust rules, a hit that
starts no Unity, the marker of a key in progress, `--no-cache` and `--cache-status`. The Editor is tests/fake_unity.py."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, commands, config, constants, core, lease, unitycache, unityrun  # noqa: F401  (cli first: it registers the commands)

from .test_host import HostTestCase, run as run_cli
from .test_unity_run import FAKE, OTHER, FakeProbe

LIB_GUID = "0123456789abcdef0123456789abcdef"
GAME_GUID = "fedcba9876543210fedcba9876543210"
PASSING = ('<?xml version="1.0" encoding="utf-8"?>\n<test-run id="2" testcasecount="3" result="Passed" total="3" passed="3" failed="0" '
           'skipped="0">\n</test-run>\n')


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def asmdef(path, name, references=(), test=False, guid=None):
    document = {"name": name, "references": list(references)}
    if test:
        document["defineConstraints"] = ["UNITY_INCLUDE_TESTS"]
    write(path, json.dumps(document))
    if guid:
        write(str(path) + ".meta", f"fileFormatVersion: 2\nguid: {guid}\n")


def make_project(root, local_package):
    """A project with a test assembly that references `Game` (Assets), `Embedded` (an embedded package) and `Lib` (a `file:`
    package, by GUID), an unrelated assembly `Other`, a scene outside every assembly and a native plugin."""
    project = Path(root)
    write(project / "ProjectSettings" / "ProjectVersion.txt", "m_EditorVersion: 6000.6.7f1\n")
    write(project / "ProjectSettings" / "ProjectSettings.asset", "productName: Game\n")
    relative = os.path.relpath(local_package, project / "Packages")
    write(project / "Packages" / "manifest.json", json.dumps({"dependencies": {
        "com.unity.test-framework": "1.4.5", "com.example.local": "file:" + Path(relative).as_posix()}}))
    write(project / "Packages" / "packages-lock.json", json.dumps({"dependencies": {"com.unity.test-framework": {"version": "1.4.5"}}}))
    embedded = project / "Packages" / "com.example.embedded"
    write(embedded / "package.json", json.dumps({"name": "com.example.embedded", "version": "1.0.0"}))
    asmdef(embedded / "Runtime" / "Embedded.asmdef", "Embedded")
    write(embedded / "Runtime" / "Embedded.cs", "class Embedded {}\n")
    write(embedded / "Documentation~" / "index.md", "docs\n")
    asmdef(project / "Assets" / "Game" / "Game.asmdef", "Game", ["Embedded"], guid=GAME_GUID)
    write(project / "Assets" / "Game" / "Player.cs", "class Player {}\n")
    write(project / "Assets" / "Game" / "Player.cs.meta", "fileFormatVersion: 2\nguid: 11111111111111111111111111111111\n")
    asmdef(project / "Assets" / "Tests" / "Game.Tests.asmdef", "Game.Tests", ["Game", "GUID:" + LIB_GUID], test=True)
    write(project / "Assets" / "Tests" / "PlayerTests.cs", "class PlayerTests {}\n")
    asmdef(project / "Assets" / "Other" / "Other.asmdef", "Other")
    write(project / "Assets" / "Other" / "Other.cs", "class Other {}\n")
    write(project / "Assets" / "Scenes" / "Main.unity", "%YAML 1.1\n")
    write(project / "Assets" / "Plugins" / "native.dll", "MZ")
    return project


def make_local_package(root):
    package = Path(root)
    write(package / "package.json", json.dumps({"name": "com.example.local", "version": "0.1.0"}))
    asmdef(package / "Lib" / "Lib.asmdef", "Lib", guid=LIB_GUID)
    write(package / "Lib" / "Lib.cs", "class Lib {}\n")
    return package


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.package = make_local_package(self.base / "LocalPkg")
        self.project = make_project(self.base / "A" / "Proj", self.package)
        self.editor = write(self.base / "editor" / "Unity", "an editor\n")
        self.results = self.base / "out" / "results.xml"
        self.manager = lease.LeaseManager(self.home)
        host = mock.patch.object(core, "local_host_id", return_value="this-machine")
        host.start()
        self.addCleanup(host.stop)
        leases = mock.patch.dict(os.environ, {"FAKE_UNITY_LEASES": str(self.manager.directory)})
        leases.start()
        self.addCleanup(leases.stop)

    def args(self, *extra, project=None, results=None, assemblies="Game.Tests", platform="EditMode", target="Win64"):
        args = [FAKE, "-batchmode", "-projectPath", str(project or self.project), "-runTests", "-testPlatform", platform,
                "-testResults", str(results or self.results), "-logFile", str(self.base / "Editor.log")]
        if target:
            args += ["-buildTarget", target]
        if assemblies:
            args += ["-assemblyNames", assemblies]
        return args + list(extra)

    def plan(self, *extra, project=None, editor=None, **options):
        return unitycache.plan(self.args(*extra, project=project, **options), project or self.project, str(editor or self.editor))

    def key(self, *extra, **options):
        found = self.plan(*extra, **options)
        self.assertIsNotNone(found.key, found.reason)
        return found.key


class KeyTests(CacheTestCase):
    def test_the_same_inputs_give_the_same_key_and_the_inputs_are_listed(self):
        first = self.plan()
        self.assertEqual(first.key, self.plan().key)
        self.assertEqual(64, len(first.key))
        files = first.components["files"]
        self.assertIn("<project>/Assets/Game/Player.cs", files)
        self.assertIn("<project>/Assets/Tests/PlayerTests.cs", files)
        self.assertIn("<project>/Packages/com.example.embedded/Runtime/Embedded.cs", files)
        self.assertIn("<package:com.example.local>/Lib/Lib.cs", files)
        self.assertIn("<project>/Assets/Plugins/native.dll", files)
        self.assertIn("<project>/ProjectSettings/ProjectSettings.asset", files)
        self.assertIn("<project>/Assets/Other/Other.cs", files)  # code of every assembly: it compiles with the run
        self.assertIn("<project>/Assets/Other/Other.asmdef", files)
        self.assertNotIn("<project>/Assets/Other/icon.png", files)
        self.assertNotIn("<project>/Assets/Scenes/Main.unity", files)
        self.assertNotIn("<project>/Packages/com.example.embedded/Documentation~/index.md", files)
        self.assertEqual(["Embedded", "Game", "Game.Tests", "Lib"], first.components["closure"])
        self.assertEqual(self.results, first.result_path)

    def test_output_paths_and_the_working_copy_do_not_change_the_key(self):
        before = self.key()
        self.assertEqual(before, self.key(results=self.base / "elsewhere" / "r.xml"))
        copy = self.base / "B" / "Proj"
        shutil.copytree(self.project, copy)
        self.assertEqual(before, self.key(project=copy))

    def test_every_input_class_changes_the_key(self):
        before = self.key()
        changes = {
            "test source": self.project / "Assets" / "Tests" / "PlayerTests.cs",
            "dependency source": self.project / "Assets" / "Game" / "Player.cs",
            "dependency meta": self.project / "Assets" / "Game" / "Player.cs.meta",
            "dependency definition": self.project / "Assets" / "Game" / "Game.asmdef",
            "embedded package": self.project / "Packages" / "com.example.embedded" / "Runtime" / "Embedded.cs",
            "embedded package.json": self.project / "Packages" / "com.example.embedded" / "package.json",
            "local package by GUID": self.package / "Lib" / "Lib.cs",
            "local package.json": self.package / "package.json",
            "settings": self.project / "ProjectSettings" / "ProjectSettings.asset",
            "manifest": self.project / "Packages" / "manifest.json",
            "lock": self.project / "Packages" / "packages-lock.json",
            "precompiled": self.project / "Assets" / "Plugins" / "native.dll",
            "code of an unrelated assembly": self.project / "Assets" / "Other" / "Other.cs",
            "definition of an unrelated assembly": self.project / "Assets" / "Other" / "Other.asmdef",
        }
        for name, path in changes.items():
            with self.subTest(name):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assertNotEqual(before, self.key(), name)
                path.write_bytes(original)
                self.assertEqual(before, self.key(), name)

    def test_the_command_editor_os_and_core_version_change_the_key(self):
        before = self.key()
        self.assertNotEqual(before, self.key("-testFilter", "Game.Tests.PlayerTests"))
        self.assertNotEqual(before, self.key(platform="PlayMode"))
        self.assertNotEqual(before, self.key(assemblies="Game.Tests,Other"))
        self.assertNotEqual(before, self.key(target="Android"))
        other_editor = write(self.base / "editor2" / "Unity", "another editor\n")
        self.assertNotEqual(before, self.key(editor=other_editor))
        os.utime(self.editor, ns=(1, 1))
        self.assertNotEqual(before, self.key())
        current = self.key()
        with mock.patch.object(core, "local_os", return_value="plan9"):
            self.assertNotEqual(current, self.key())
        with mock.patch.object(unitycache, "__version__", "0.0.0"):
            self.assertNotEqual(current, self.key())

    def test_user_settings_are_not_in_the_key(self):
        before = self.key()
        write(self.project / "UserSettings" / "Layouts" / "default.dwlt", "layout")
        write(self.project / "UserSettings" / "EditorUserSettings.asset", "settings")
        self.assertEqual(before, self.key())

    def test_an_output_option_without_a_value_keeps_the_next_argument(self):
        command = unitycache._command(["-logFile", "-runTests", "-testResults"], self.project)
        self.assertEqual(["-runTests"], command)
        self.assertEqual(["-quit"], unitycache._command(["-logFile", "-", "-quit"], self.project))

    def test_the_editor_version_is_read_from_the_app_bundle(self):
        import plistlib
        app = self.base / "Unity.app"
        executable = write(app / "Contents" / "MacOS" / "Unity", "binary")
        with open(write(app / "Contents" / "Info.plist", ""), "wb") as handle:
            plistlib.dump({"CFBundleVersion": "6000.6.7f1"}, handle)
        self.assertEqual("6000.6.7f1", unitycache.editor_version(executable))
        self.assertEqual("6000.6.7f1", self.plan(editor=executable).components["editor"]["fileVersion"])
        for broken in ("broken", '<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleVersion</key>',
                       '<?xml version="1.0"?><plist version="1.0"><dict><key>Built</key><date>xx</date></dict></plist>',
                       '<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>'):
            with self.subTest(broken):
                (app / "Contents" / "Info.plist").write_text(broken, encoding="utf-8")
                with self.assertRaises(unitycache.NoKey):
                    unitycache.editor_version(executable)
                found = self.plan(editor=executable)
                self.assertIsNone(found.key)
                self.assertIn("Info.plist", found.reason)
        self.assertIsNone(unitycache.editor_version(self.editor))  # an executable without a version: identified by its file

    def test_anything_unexpected_while_the_key_is_computed_means_no_key(self):
        with mock.patch.object(unitycache, "editor_version", side_effect=AttributeError("odd")):
            found = self.plan()
        self.assertIsNone(found.key)
        self.assertIn("AttributeError", found.reason)

    @unittest.skipUnless(os.name == "nt", "the file version resource is a Windows feature")
    def test_the_editor_version_is_read_from_the_file_version_resource(self):
        self.assertRegex(unitycache.editor_version(sys.executable) or "", r"^\d+\.\d+\.\d+\.\d+$")
        self.assertIsNone(unitycache.editor_version(self.editor))

    def test_a_file_argument_is_hashed_by_content(self):
        settings = write(self.base / "run.json", "{}")
        before = self.key(str(settings))
        settings.write_text('{"a": 1}', encoding="utf-8")
        self.assertNotEqual(before, self.key(str(settings)))
        inside = self.plan(str(self.project / "Assets" / "Scenes" / "Main.unity"))
        self.assertTrue(any(item.startswith("<project>/Assets/Scenes/Main.unity#sha256:") for item in inside.components["command"]))

    def test_assets_outside_the_closure_do_not_change_the_key_but_any_code_does(self):
        before = self.key()
        write(self.project / "Assets" / "Other" / "icon.png", "changed")
        write(self.project / "Assets" / "Scenes" / "Main.unity", "%YAML 1.1\nchanged\n")
        write(self.project / "Assets" / "Scenes" / "notes.txt", "changed")
        self.assertEqual(before, self.key())
        write(self.project / "Assets" / "Loose.cs", "class Loose {}\n")  # Assembly-CSharp: compiled with every run
        self.assertNotEqual(before, self.key())

    def test_an_asmref_folder_belongs_to_the_assembly_it_names(self):
        write(self.project / "Assets" / "Extra" / "Extra.asmref", json.dumps({"reference": "GUID:" + GAME_GUID}))
        source = write(self.project / "Assets" / "Extra" / "More.cs", "class More {}\n")
        before = self.key()
        self.assertIn("<project>/Assets/Extra/More.cs", self.plan().components["files"])
        source.write_text("class More { int changed; }\n", encoding="utf-8")
        self.assertNotEqual(before, self.key())

    def test_a_nested_assembly_takes_its_folder_out_of_the_enclosing_one(self):
        asmdef(self.project / "Assets" / "Game" / "Inner" / "Inner.asmdef", "Inner")
        asset = write(self.project / "Assets" / "Game" / "Inner" / "data.bytes", "data")
        write(self.project / "Assets" / "Game" / "data.bytes", "data")
        before = self.plan()
        self.assertIn("<project>/Assets/Game/data.bytes", before.components["files"])
        self.assertNotIn("<project>/Assets/Game/Inner/data.bytes", before.components["files"])
        asset.write_text("changed", encoding="utf-8")
        self.assertEqual(before.key, self.key())

    def test_a_linked_folder_is_followed_once(self):
        try:
            os.symlink(self.project / "Assets", self.project / "Assets" / "Game" / "Loop", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this machine cannot create a symbolic link")
        self.assertIsNotNone(self.plan().key)

    def test_commands_that_are_not_cached_have_no_key_and_say_why(self):
        cases = {
            "-runTests": unitycache.plan([FAKE, "-projectPath", str(self.project), "-testResults", str(self.results),
                                          "-assemblyNames", "Game.Tests"], self.project, str(self.editor)),
            "-testResults": unitycache.plan([FAKE, "-projectPath", str(self.project), "-runTests", "-assemblyNames", "Game.Tests"],
                                            self.project, str(self.editor)),
            "-editorTestsResultFile": self.plan("-editorTestsResultFile", str(self.base / "x.xml")),
            "test platform": self.plan(platform="StandaloneWindows64"),
            "-buildTarget": self.plan(target=None),
            "relative -testResults": self.plan(results="results.xml"),
            "not defined": self.plan(assemblies="Missing.Tests"),
        }
        for words, found in cases.items():
            with self.subTest(words):
                self.assertIsNone(found.key)
                self.assertIn(words, found.reason)

    def test_a_graph_that_cannot_be_used_or_an_unreadable_input_gives_no_key(self):
        asmdef(self.project / "Assets" / "Twin" / "Game.asmdef", "Game")
        self.assertIn("assembly graph", self.plan().reason)
        (self.project / "Assets" / "Twin" / "Game.asmdef").write_text("{broken", encoding="utf-8")
        self.assertIn("assembly graph", self.plan().reason)
        (self.project / "Assets" / "Twin" / "Game.asmdef").write_text('{"name": "Twin"}', encoding="utf-8")
        self.assertIsNotNone(self.plan().key)
        with mock.patch.object(core, "sha256_file", side_effect=PermissionError("denied")):
            self.assertIn("cannot be read", self.plan().reason)

    def test_a_missing_local_package_gives_no_key(self):
        write(self.project / "Packages" / "manifest.json", json.dumps({"dependencies": {"com.example.gone": "file:../../Gone"}}))
        self.assertIn("does not exist", self.plan().reason)

    def test_the_result_file_check(self):
        self.assertIsNone(unitycache.failure(PASSING))
        self.assertIn("Failed", unitycache.failure(PASSING.replace('result="Passed"', 'result="Failed"')))
        self.assertIn("failed=", unitycache.failure(PASSING.replace('failed="0"', 'failed="1"')))
        # A real Editor reports a run with one ignored test as `Skipped:Ignored`: a pass while nothing failed and some test passed.
        ignored = PASSING.replace('result="Passed"', 'result="Skipped:Ignored"').replace('passed="3"', 'passed="2"')
        self.assertIsNone(unitycache.failure(ignored))
        self.assertIn("no test passed", unitycache.failure(ignored.replace('passed="2"', 'passed="0"')))
        self.assertIn("inconclusive", unitycache.failure(PASSING.replace('failed="0"', 'failed="0" inconclusive="1"')))
        self.assertIn("Inconclusive", unitycache.failure(PASSING.replace('result="Passed"', 'result="Inconclusive"')))
        self.assertIn("test-run", unitycache.failure("<test-suite/>"))
        self.assertIn("XML", unitycache.failure("<test-run"))
        self.assertIn("document type", unitycache.failure('<!DOCTYPE x [<!ENTITY a "b">]><test-run result="Passed" failed="0"/>'))
        padded = "<?xml version='1.0'?>\n" + "<!--" + "x" * 10000 + "-->\n" + '<!DOCTYPE x [<!ENTITY a "b">]><test-run result="Passed" failed="0"/>'
        self.assertIn("document type", unitycache.failure(padded))


class StoreTests(CacheTestCase):
    def root(self):
        return unitycache.cache_root(self.home)

    def test_a_stored_pass_is_found_and_only_for_its_key(self):
        found = self.plan()
        path, why = unitycache.store(self.root(), found, PASSING)
        self.assertEqual("stored", why)
        hit, why = unitycache.lookup(self.root(), found)
        self.assertEqual((found.key, PASSING), (hit.key, hit.text))
        self.assertEqual((None, "no cached result"), unitycache.lookup(self.root(), self.plan("-testFilter", "X")))

    def test_a_failing_result_is_not_stored(self):
        path, why = unitycache.store(self.root(), self.plan(), PASSING.replace('failed="0"', 'failed="2"'))
        self.assertIsNone(path)
        self.assertEqual([], list(self.root().glob("*.json")) if self.root().exists() else [])

    def test_a_corrupt_foreign_or_tampered_entry_is_ignored(self):
        found = self.plan()
        path, _ = unitycache.store(self.root(), found, PASSING)
        good = path.read_text(encoding="utf-8")
        path.write_text("{broken", encoding="utf-8")
        self.assertIn("unreadable", unitycache.lookup(self.root(), found)[1])
        entry = json.loads(good)
        entry["components"] = {**entry["components"], "os": "plan9"}
        path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertIn("another key", unitycache.lookup(self.root(), found)[1])
        entry = json.loads(good)
        entry["result"] = entry["result"].replace("3", "4")
        path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertIn("hash", unitycache.lookup(self.root(), found)[1])
        entry = json.loads(good)
        entry["format"] = "valtrion.resultcache/1"
        path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertIn("not a Unity result cache entry", unitycache.lookup(self.root(), found)[1])

    def test_a_linked_entry_is_ignored(self):
        found = self.plan()
        path, _ = unitycache.store(self.root(), found, PASSING)
        target = self.base / "planted.json"
        shutil.copyfile(path, target)
        path.unlink()
        try:
            os.symlink(target, path)
        except (OSError, NotImplementedError):
            self.skipTest("this machine cannot create a symbolic link")
        self.assertIn("link", unitycache.lookup(self.root(), found)[1])

    def test_the_store_is_bounded_in_bytes(self):
        first = self.plan()
        unitycache.store(self.root(), first, PASSING)
        os.utime(self.root() / f"{first.key}.json", (1, 1))  # the least recently used
        second = self.plan("-testFilter", "X")
        with mock.patch.object(unitycache, "MAX_STORE_BYTES", 1):
            unitycache.store(self.root(), second, PASSING)
        self.assertEqual([f"{second.key}.json"], [path.name for path in self.root().glob("*.json")])

    def test_the_store_is_bounded_in_entries(self):
        first = self.plan()
        unitycache.store(self.root(), first, PASSING)
        os.utime(self.root() / f"{first.key}.json", (1, 1))
        second = self.plan("-testFilter", "X")
        with mock.patch.object(unitycache, "MAX_ENTRIES", 1):
            unitycache.store(self.root(), second, PASSING)
        self.assertEqual([f"{second.key}.json"], [path.name for path in self.root().glob("*.json")])

    def test_the_lease_keeper_starts_one_thread(self):
        keeper = lease.LeaseKeeper(self.manager, "s", os.getpid(), 60)
        keeper.start()
        first = keeper._thread
        keeper.start()
        self.assertIs(first, keeper._thread)
        keeper.stop()

    def test_an_entry_above_the_size_limit_is_ignored(self):
        found = self.plan()
        path, _ = unitycache.store(self.root(), found, PASSING)
        with mock.patch.object(unitycache, "MAX_ENTRY_BYTES", 10):
            self.assertIn("larger than", unitycache.lookup(self.root(), found)[1])


class RunTests(CacheTestCase):
    """`unityrun.run` with a plan, as `unity run` calls it."""

    def run_unity(self, *fake_args, read_cache=True, results=None, plan=None, **options):
        args = self.args(*fake_args, results=results)
        out, err = io.StringIO(), io.StringIO()
        options.setdefault("lease_wait_sec", 0)
        options.setdefault("lfs_materialize", False)
        cache = plan or unityrun.cache_plan(args, None, sys.executable, home=self.home)
        self.assertIsNotNone(cache.key, cache.reason)
        code = unityrun.run(args, unity=sys.executable, home=self.home, out=out, err=err, poll_sec=0.1, cancel_poll_sec=0.1,
                            probe=FakeProbe(), guard_poll_sec=0.1, cache=cache, read_cache=read_cache, **options)
        return code, out.getvalue().splitlines(), err.getvalue()

    def entries(self):
        root = unitycache.cache_root(self.home)
        return sorted(root.glob("*.json")) if root.is_dir() else []

    def no_unity(self):
        return mock.patch.object(unityrun.supervise, "supervise", side_effect=AssertionError("Unity was started"))

    def test_a_pass_is_stored_and_the_same_run_is_answered_without_unity(self):
        code, lines, err = self.run_unity("--results", "pass")
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS"), (code, lines[-1]), err)
        self.assertIn("cache: stored PASS under key", err)
        self.assertEqual(1, len(self.entries()))
        elsewhere = self.base / "second" / "results.xml"
        with self.no_unity():
            code, lines, err = self.run_unity("--results", "pass", results=elsewhere)
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS"), (code, lines[-1]), err)
        self.assertTrue(lines[-2].startswith("cached: PASS of key "), lines)
        self.assertEqual(lines[-2] + "\n", (self.base / "Editor.log").read_text(encoding="utf-8"))  # the log of the earlier run is replaced
        self.assertEqual(self.results.read_bytes(), elsewhere.read_bytes())  # the file of the first run, byte for byte
        self.assertEqual([], list(self.manager.directory.glob("*.lock")))

    def test_a_failure_is_not_stored_and_runs_again(self):
        code, lines, err = self.run_unity("--results", "fail", "--exit", "2")
        self.assertEqual(constants.EXIT_FAIL, code, err)
        self.assertEqual([], self.entries())
        code, _, err = self.run_unity("--results", "fail", "--exit", "0")
        self.assertEqual(constants.EXIT_PASS, code)
        self.assertIn("cache: not stored", err)
        self.assertEqual([], self.entries())

    def test_a_result_file_this_run_did_not_write_is_not_stored(self):
        write(self.results, PASSING)
        code, _, err = self.run_unity()
        self.assertEqual(constants.EXIT_PASS, code, err)
        self.assertIn("not written by this run", err)
        self.assertEqual([], self.entries())

    def test_inputs_that_change_while_unity_runs_are_not_stored(self):
        code, _, err = self.run_unity("--results", "pass", "--append", str(self.project / "Assets" / "Game" / "Player.cs"))
        self.assertEqual(constants.EXIT_PASS, code, err)
        self.assertIn("the inputs changed while Unity ran", err)
        self.assertEqual([], self.entries())

    def test_no_cache_runs_unity_and_still_stores(self):
        self.run_unity("--results", "pass")
        stored = self.entries()[0].read_text(encoding="utf-8")
        with mock.patch.object(unityrun.supervise, "supervise", wraps=unityrun.supervise.supervise) as started:
            code, lines, err = self.run_unity("--results", "pass", read_cache=False)
        self.assertEqual((constants.EXIT_PASS, 1), (code, started.call_count), err)
        self.assertNotIn("cached:", "\n".join(lines))
        self.assertNotEqual(json.loads(stored)["storedUtc"], json.loads(self.entries()[0].read_text(encoding="utf-8"))["storedUtc"])

    def test_a_corrupt_entry_is_a_miss_and_is_replaced(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        root = unitycache.cache_root(self.home)
        root.mkdir(parents=True)
        write(root / f"{cache.key}.json", "{broken")
        code, _, err = self.run_unity("--results", "pass")
        self.assertEqual(constants.EXIT_PASS, code, err)
        self.assertIsNotNone(unitycache.lookup(root, cache)[0])

    def test_a_live_holder_of_the_key_is_waited_for_and_its_result_used(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        held = self.manager.try_acquire(cache.marker, OTHER, "other", 300, "computing", os.getpid())
        self.assertTrue(held.acquired)

        def finish():
            time.sleep(2)
            unitycache.store(unitycache.cache_root(self.home), cache, PASSING)
            self.manager.release(cache.marker, "other", os.getpid())

        helper = threading.Thread(target=finish)
        helper.start()
        with self.no_unity():
            code, lines, err = self.run_unity("--results", "pass", lease_wait_sec=20, plan=cache)
        helper.join()
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS"), (code, lines[-1]), err)
        self.assertIn("is being computed by another session", err)
        self.assertIn(OTHER, err)
        self.assertTrue(lines[-2].startswith("cached: PASS"))
        self.assertTrue(self.results.is_file())

    def test_a_change_while_the_run_waits_is_a_miss(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        self.assertTrue(self.manager.try_acquire(cache.marker, OTHER, "other", 300, "computing", os.getpid()).acquired)

        def finish():
            time.sleep(2)
            write(self.project / "Assets" / "Game" / "Player.cs", "class Player { int changed; }\n")
            unitycache.store(unitycache.cache_root(self.home), cache, PASSING)  # the result of the content before the change
            self.manager.release(cache.marker, "other", os.getpid())

        helper = threading.Thread(target=finish)
        helper.start()
        with mock.patch.object(unityrun.supervise, "supervise", wraps=unityrun.supervise.supervise) as started:
            code, lines, err = self.run_unity("--results", "pass", lease_wait_sec=20, plan=cache)
        helper.join()
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS", 1), (code, lines[-1], started.call_count), err)
        self.assertIn("the inputs changed while the run waited", err)
        self.assertNotIn("cached:", "\n".join(lines))
        self.assertEqual(2, len(self.entries()))
        self.assertEqual([], list(self.manager.directory.glob("*.lock")))

    def test_a_hit_is_answered_only_under_the_project_lease(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        unitycache.store(unitycache.cache_root(self.home), cache, PASSING)
        project = lease.unity_project_resource(self.project)
        self.assertTrue(self.manager.try_acquire(project, OTHER, "other", 300, "a run in progress", os.getpid()).acquired)
        out = io.StringIO()
        self.assertIsNone(unityrun.answer_from_cache(cache, self.home, out))
        with self.no_unity():
            code, lines, _ = self.run_unity("--results", "pass", plan=cache)
        self.assertEqual(constants.EXIT_BLOCKED, code)  # it waited for the project lease (no time here), and wrote nothing
        self.assertFalse(self.results.exists())
        self.manager.release(project, "other", os.getpid())
        self.assertEqual(constants.EXIT_PASS, unityrun.answer_from_cache(cache, self.home, out))
        self.assertIn("cached: PASS", out.getvalue())
        self.assertEqual([], list(self.manager.directory.glob("*.lock")))

    def test_a_live_holder_that_does_not_finish_in_time_lets_unity_run_here(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        self.assertTrue(self.manager.try_acquire(cache.marker, OTHER, "other", 300, "computing", os.getpid()).acquired)
        with mock.patch.object(unityrun.supervise, "supervise", wraps=unityrun.supervise.supervise) as started:
            code, lines, err = self.run_unity("--results", "pass", lease_wait_sec=0.3, plan=cache)
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS", 1), (code, lines[-1], started.call_count), err)
        self.assertIn("Unity runs here", err)
        held = [path.name for path in self.manager.directory.glob("*.lock")]
        self.assertEqual([lease.lease_file_name(cache.marker)], held)  # the other session's marker stays

    def test_the_marker_is_taken_after_the_project_lease_and_before_the_slot(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        order = []
        original = lease.LeaseManager.try_acquire

        def noted(manager, resource, *args, **kwargs):
            order.append(resource.split(":")[0])
            return original(manager, resource, *args, **kwargs)

        with mock.patch.object(lease.LeaseManager, "try_acquire", noted):
            code, _, err = self.run_unity("--results", "pass", plan=cache)
        self.assertEqual(constants.EXIT_PASS, code, err)
        self.assertEqual(["unity-project", "unity-result", "unity-editor"], [name for index, name in enumerate(order) if name not in order[:index]])

    def test_no_cache_does_not_wait_for_a_live_holder(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        self.assertTrue(self.manager.try_acquire(cache.marker, OTHER, "other", 300, "computing", os.getpid()).acquired)
        code, lines, err = self.run_unity("--results", "pass", read_cache=False, plan=cache)
        self.assertEqual(constants.EXIT_PASS, code, err)
        self.assertIn("--no-cache: Unity runs anyway", err)
        self.assertEqual(1, len(self.entries()))

    def test_a_dead_holder_of_the_key_is_taken_over(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
        dead = lease.LeaseRecord(cache.marker, OTHER, "other", int(finished.stdout), "this-machine", "2020-01-01T00:00:00.000Z",
                                 "2020-01-01T00:00:00.000Z", 300, "crashed")
        self.manager.directory.mkdir(parents=True, exist_ok=True)
        core.atomic_write_json(self.manager.path_for(cache.marker), dead.to_document())
        code, lines, err = self.run_unity("--results", "pass", plan=cache)
        self.assertEqual(constants.EXIT_PASS, code, err)
        self.assertNotIn("being computed", err)
        self.assertEqual(1, len(self.entries()))
        self.assertEqual([], list(self.manager.directory.glob("*.lock")))

    def test_the_marker_is_held_while_unity_runs_and_losing_it_does_not_stop_unity(self):
        cache = unityrun.cache_plan(self.args("--results", "pass", "--sleep", "2"), None, sys.executable)
        seen = []

        def remove():
            path = self.manager.path_for(cache.marker)
            for _ in range(100):
                if path.exists():
                    try:
                        path.unlink()
                    except PermissionError:  # Windows: the lock file is being read or written this instant
                        time.sleep(0.05)
                        continue
                    seen.append(True)
                    return
                time.sleep(0.05)

        helper = threading.Thread(target=remove)
        helper.start()
        code, lines, err = self.run_unity("--results", "pass", "--sleep", "2", plan=cache, ttl_sec=3)
        helper.join()
        self.assertEqual([True], seen)
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS"), (code, lines[-1]), err)
        self.assertNotIn("were lost", "\n".join(lines))

    def test_an_offloaded_run_is_not_cached(self):
        cache = unityrun.cache_plan(self.args("--results", "pass"), None, sys.executable)
        unitycache.store(unitycache.cache_root(self.home), cache, PASSING)
        with mock.patch.object(unityrun.supervise, "supervise", wraps=unityrun.supervise.supervise) as started:
            code, _, err = self.run_unity("--results", "pass", plan=cache, prepare=lambda: None)
        self.assertEqual((constants.EXIT_PASS, 1), (code, started.call_count), err)
        self.assertNotIn("cache:", err)


class CommandLineTests(HostTestCase):
    def setUp(self):
        super().setUp()
        package = make_local_package(self.base / "LocalPkg")
        self.project = make_project(self.base / "A" / "Proj", package)
        self.results = self.base / "out" / "results.xml"

    def cli(self, *options, extra=("--results", "pass")):
        return run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--no-offload", *options, "--",
                       FAKE, "-batchmode", "-projectPath", str(self.project), "-runTests", "-testPlatform", "EditMode", "-buildTarget", "Win64",
                       "-assemblyNames", "Game.Tests", "-testResults", str(self.results), *extra)

    def test_cache_status_reports_miss_then_hit_and_runs_nothing(self):
        with mock.patch.object(unityrun.supervise, "supervise", side_effect=AssertionError("Unity was started")):
            code, out, err = self.cli("--cache-status")
        self.assertEqual(0, code, err)
        status = json.loads(out)
        self.assertEqual((False, None), (status["cached"], status["inProgress"]))
        self.assertEqual(64, len(status["key"]))
        self.assertIn("<project>/Assets/Game/Player.cs", status["inputs"]["files"])
        self.assertFalse(self.results.exists())
        code, out, err = self.cli()
        self.assertEqual((0, "STATUS PASS"), (code, out.strip().splitlines()[-1]), err)
        code, out, err = self.cli("--cache-status")
        again = json.loads(out)
        self.assertEqual((status["key"], True), (again["key"], again["cached"]))

    def test_cache_status_names_the_session_that_computes_the_key(self):
        code, out, _ = self.cli("--cache-status")
        key = json.loads(out)["key"]
        manager = lease.LeaseManager()
        self.assertTrue(manager.try_acquire(unitycache.MARKER_PREFIX + key, OTHER, "other", 300, "computing", os.getpid()).acquired)
        code, out, _ = self.cli("--cache-status")
        self.assertEqual(OTHER, json.loads(out)["inProgress"]["owner"])

    def test_cache_status_without_a_key_says_why(self):
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--cache-status", "--", FAKE, "-projectPath", str(self.project))
        self.assertEqual(0, code, err)
        status = json.loads(out)
        self.assertIsNone(status["key"])
        self.assertIn("-runTests", status["reason"])

    def test_cache_status_of_an_offloaded_run_runs_nothing(self):
        with mock.patch.object(unityrun.supervise, "supervise", side_effect=AssertionError("Unity was started")):
            code, out, err = self.cli("--cache-status", "--offload-from", "build-pc", "--offload-project", "Game", "--offload-sha", "0" * 40)
        self.assertEqual(0, code, err)
        self.assertIn("offloaded", json.loads(out)["reason"])

    def test_cache_status_with_host_is_refused(self):
        config.add_ssh_host("win", "me@win", "windows", "C:/art")
        with mock.patch.object(commands, "_remote", side_effect=AssertionError("forwarded")):
            code, out, err = run_cli("unity", "run", "--host", "win", "--remote-project", "C:/Proj", "--cache-status", "--", "-runTests")
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("without --host", out + err)

    def test_rerun_failed_is_keyed_by_the_rewritten_filter_and_nothing_to_run_stores_nothing(self):
        earlier = write(self.base / "earlier.xml", '<test-run result="Failed"><test-suite fullname="Game.Tests" result="Failed">'
                                                   '<test-case fullname="Game.Tests.PlayerTests.Breaks" result="Failed"/></test-suite></test-run>')
        code, out, err = self.cli("--rerun-failed", str(earlier), "--cache-status")
        self.assertEqual(0, code, err)
        self.assertTrue(out.strip(), (out, err))
        rerun = json.loads(out)
        self.assertIn("-testFilter", rerun["inputs"]["command"])
        self.assertTrue(any("PlayerTests" in item for item in rerun["inputs"]["command"]))
        self.assertNotEqual(json.loads(self.cli("--cache-status")[1])["key"], rerun["key"])
        code, out, err = self.cli("--rerun-failed", str(earlier))
        self.assertEqual((0, "STATUS PASS"), (code, out.strip().splitlines()[-1]), err)
        root = unitycache.cache_root()
        self.assertEqual([f"{rerun['key']}.json"], [path.name for path in root.glob("*.json")])
        with mock.patch.object(unityrun.supervise, "supervise", side_effect=AssertionError("Unity was started")):
            code, out, _ = self.cli("--rerun-failed", str(earlier))
        self.assertIn("cached: PASS", out)
        write(earlier, '<test-run result="Passed"><test-suite fullname="A" result="Passed"><test-case fullname="A.B" result="Passed"/>'
                       '</test-suite></test-run>')
        with mock.patch.object(unityrun.supervise, "supervise", side_effect=AssertionError("Unity was started")):
            code, out, _ = self.cli("--rerun-failed", str(earlier))
        self.assertEqual((4, "STATUS NOTHING_TO_RUN"), (code, out.strip().splitlines()[-1]))
        self.assertEqual(1, len(list(root.glob("*.json"))))

    def test_a_hit_and_no_cache(self):
        self.cli()
        with mock.patch.object(unityrun.supervise, "supervise", side_effect=AssertionError("Unity was started")):
            code, out, err = self.cli()
        self.assertEqual(0, code, err)
        self.assertIn("cached: PASS of key", out)
        code, out, err = self.cli("--no-cache")
        self.assertEqual(0, code, err)
        self.assertNotIn("cached:", out)
        self.assertIn("cache: stored PASS", err)


if __name__ == "__main__":
    unittest.main()
