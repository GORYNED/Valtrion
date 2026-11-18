"""The result cache for the runners a check map writes: a relative `-testResults`, a host default `-buildTarget` (host.json
`unityBuildTarget`) and a run that selects by `-testFilter` only (no `-assemblyNames`)."""

import io
import json
import sys
from pathlib import Path
from unittest import mock

from valtrion import config, constants, unitycache, unityrun

from .test_unity_cache import FAKE, CacheTestCase, FakeProbe


class DefaultsTests(CacheTestCase):
    def host(self, **settings):
        (self.home / "host.json").write_text(json.dumps(settings), encoding="utf-8")

    def test_a_relative_test_results_path_is_the_path_under_the_project(self):
        args = unityrun.with_host_defaults([FAKE, "-testResults", "Builds/editmode.xml"], self.project, self.home)
        self.assertEqual(str(self.project / "Builds" / "editmode.xml"), args[args.index("-testResults") + 1])
        absolute = str(self.base / "x.xml")
        self.assertEqual(absolute, unityrun.with_host_defaults([FAKE, "-testResults", absolute], self.project, self.home)[2])

    def test_the_host_build_target_is_added_only_when_the_command_has_none(self):
        self.host(unityBuildTarget="StandaloneWindows64")
        args = unityrun.with_host_defaults([FAKE, "-runTests"], self.project, self.home)
        self.assertEqual(["-buildTarget", "StandaloneWindows64"], args[-2:])
        self.assertEqual([FAKE, "-buildTarget", "iOS"], unityrun.with_host_defaults([FAKE, "-buildTarget", "iOS"], self.project, self.home))
        profile = [FAKE, "-runTests", "-activeBuildProfile", "Assets/Profile.asset"]
        self.assertEqual(profile, unityrun.with_host_defaults(profile, self.project, self.home))
        build = [FAKE, "-executeMethod", "Build.Run"]  # not a test run: it chooses its platform itself
        self.assertEqual(build, unityrun.with_host_defaults(build, self.project, self.home))

    def test_without_the_setting_the_command_is_unchanged(self):
        self.assertEqual([FAKE, "-runTests"], unityrun.with_host_defaults([FAKE, "-runTests"], self.project, self.home))

    def test_a_bad_setting_is_a_config_error(self):
        for value in ("", "Win 64", 5, "-quit"):
            with self.subTest(value):
                self.host(unityBuildTarget=value)
                with self.assertRaises(config.ConfigError):
                    config.unity_build_target(self.home)

    def test_a_map_runner_with_a_relative_result_and_no_build_target_gets_a_key_from_the_host_setting(self):
        args = self.args(results="Builds/editmode.xml", target=None)
        self.assertIn("relative -testResults", unitycache.plan(args, self.project, str(self.editor)).reason)  # plan alone keeps refusing
        self.assertIn("-buildTarget", unityrun.cache_plan(args, None, str(self.editor), home=self.home).reason)  # resolved, no target
        self.host(unityBuildTarget="StandaloneWindows64")
        found = unityrun.cache_plan(args, None, str(self.editor), home=self.home)
        self.assertIsNotNone(found.key, found.reason)
        self.assertEqual(self.project / "Builds" / "editmode.xml", found.result_path)
        self.assertIn("StandaloneWindows64", found.components["command"])
        self.host(unityBuildTarget="iOS")
        self.assertNotEqual(found.key, unityrun.cache_plan(args, None, str(self.editor), home=self.home).key)

    def test_a_bad_setting_gives_no_key_and_blocks_the_run(self):
        self.host(unityBuildTarget="not a target")
        args = self.args(target=None)
        self.assertIn("unityBuildTarget", unityrun.cache_plan(args, None, str(self.editor), home=self.home).reason)
        out, err = io.StringIO(), io.StringIO()
        code = unityrun.run(args, unity=sys.executable, home=self.home, out=out, err=err, lease_wait_sec=0, lfs_materialize=False,
                            probe=FakeProbe(), poll_sec=0.1, cancel_poll_sec=0.1, guard_poll_sec=0.1)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("unityBuildTarget", out.getvalue())

    def test_unity_gets_the_host_build_target_and_the_absolute_result_path(self):
        self.host(unityBuildTarget="StandaloneWindows64")
        dump = self.base / "dump"
        args = self.args("--dump", str(dump), results="Builds/editmode.xml", target=None)
        out, err = io.StringIO(), io.StringIO()
        code = unityrun.run(args, unity=sys.executable, home=self.home, out=out, err=err, lease_wait_sec=0, lfs_materialize=False,
                            probe=FakeProbe(), poll_sec=0.1, cancel_poll_sec=0.1, guard_poll_sec=0.1)
        self.assertEqual(constants.EXIT_PASS, code, out.getvalue() + err.getvalue())
        argv = json.loads((dump / "argv.json").read_text())
        self.assertEqual("StandaloneWindows64", argv[argv.index("-buildTarget") + 1])
        self.assertEqual(str(self.project / "Builds" / "editmode.xml"), argv[argv.index("-testResults") + 1])


class OffloadPathTests(CacheTestCase):
    def test_an_offloaded_run_gets_the_result_path_under_the_project(self):
        from valtrion import commands
        from .test_host import run as cli

        seen = {}

        def offload(arguments, unity_args, found):
            seen["args"] = list(unity_args)
            return 0

        with mock.patch.object(commands, "_unity_offload", side_effect=offload), \
                mock.patch.object(unityrun, "answer_from_cache", return_value=None):
            cli("unity", "run", "--no-cache", "--", "-batchmode", "-projectPath", str(self.project), "-runTests",
                "-testResults", "Builds/editmode.xml")
        args = seen["args"]
        self.assertEqual(str(self.project / "Builds" / "editmode.xml"), args[args.index("-testResults") + 1])
        self.assertNotIn("-buildTarget", args)  # the receiving host adds its own


class ChangedInputTests(CacheTestCase):
    def settings(self):
        return self.project / "ProjectSettings" / "ProjectSettings.asset"

    def test_a_settings_file_that_unity_re_saved_with_lf_gets_the_gitattributes_hint(self):
        self.settings().write_bytes(b"%YAML 1.1\r\nPlayerSettings:\r\n  a: 1\r\n")
        before = self.plan()
        self.settings().write_bytes(b"%YAML 1.1\nPlayerSettings:\n  a: 1\n")
        why = unitycache.changed_inputs(before, self.plan())
        self.assertIn("<project>/ProjectSettings/ProjectSettings.asset", why)
        self.assertIn("only their line endings changed", why)
        self.assertIn("eol=lf", why)

    def test_a_real_change_names_the_file_without_the_hint(self):
        before = self.plan()
        self.settings().write_bytes(b"%YAML 1.1\nPlayerSettings:\n  a: 2\n")
        (self.project / "Assets" / "Game" / "Player.cs").write_text("class Player { int x; }\n", encoding="utf-8")
        why = unitycache.changed_inputs(before, self.plan())
        self.assertIn("ProjectSettings.asset", why)
        self.assertIn("Player.cs", why)
        self.assertNotIn("eol=lf", why)

    def test_a_mixed_change_marks_the_line_ending_only_files(self):
        self.settings().write_bytes(b"a\r\nb\r\n")
        before = self.plan()
        self.settings().write_bytes(b"a\nb\n")
        (self.project / "Assets" / "Game" / "Player.cs").write_text("class Player { int y; }\n", encoding="utf-8")
        why = unitycache.changed_inputs(before, self.plan())
        self.assertIn("line endings only", why)
        self.assertIn("Player.cs", why)


class FilterOnlyTests(CacheTestCase):
    def test_a_run_without_assembly_names_is_keyed_on_every_assembly_and_its_filter(self):
        found = self.plan("-testFilter", r"\AGame\.Tests", assemblies=None)
        self.assertIsNotNone(found.key, found.reason)
        self.assertEqual([], found.components["assemblies"])
        self.assertIn("Other", found.components["closure"])
        self.assertNotEqual(found.key, self.plan("-testFilter", r"\AGame\.Other", assemblies=None).key)
        self.assertNotEqual(found.key, self.plan("-testFilter", r"\AGame\.Tests").key)

    def test_a_scene_or_resource_outside_every_assembly_folder_changes_its_key(self):
        resource = self.project / "Assets" / "Resources" / "Config.asset"
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.write_text("a: 1\n", encoding="utf-8")
        before = self.key("-testFilter", "x", assemblies=None)
        named = self.key("-testFilter", "x")
        resource.write_text("a: 2\n", encoding="utf-8")
        self.assertNotEqual(before, self.key("-testFilter", "x", assemblies=None))  # every test of the project may load it
        self.assertEqual(named, self.key("-testFilter", "x"))  # the documented limit of a run that names its assemblies

    def test_no_lock_file_with_a_git_dependency_gives_no_key(self):
        manifest = self.project / "Packages" / "manifest.json"
        lock = self.project / "Packages" / "packages-lock.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["dependencies"]["com.example.git"] = "https://example.com/pkg.git#main"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        if lock.exists():
            self.assertIsNotNone(self.plan().key)
            lock.rename(lock.with_name("lock.bak"))
        self.assertIn("packages-lock.json does not exist", self.plan().reason)

    def test_any_asset_file_of_any_assembly_changes_its_key(self):
        before = self.key("-testFilter", "x", assemblies=None)
        (self.project / "Assets" / "Other" / "icon.png").write_bytes(b"new")
        self.assertNotEqual(before, self.key("-testFilter", "x", assemblies=None))
        self.assertTrue(Path(self.project / "Assets" / "Other" / "icon.png").exists())
