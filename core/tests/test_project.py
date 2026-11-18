import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import buildcache, cli, core, project

COMPAT = {"schema": "valtrion.compat/1", "package": project.UNITY_PACKAGE, "ranges": [{"package": "0.7.x", "core": ">=0.7.0,<0.8"}]}


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name).resolve()
        self.repo = self.base / "repo"
        self.unity = self.repo / "game" / "UnityProject"
        (self.unity / "Packages").mkdir(parents=True)
        self.compat = self.base / "compat.json"
        self.compat.write_text(json.dumps(COMPAT), encoding="utf-8")

    def lock(self, entry):
        dependencies = {} if entry is None else {project.UNITY_PACKAGE: entry}
        (self.unity / "Packages" / "packages-lock.json").write_text(json.dumps({"dependencies": dependencies}), encoding="utf-8")


class DiscoveryTests(ProjectCase):
    def write_file(self, value):
        target = self.repo / "tools" / "valtrion" / "valtrion.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding="utf-8")

    def test_an_old_valtrion_json_at_the_project_root_is_ignored(self):
        (self.repo / "valtrion.json").write_text(json.dumps({"unityProject": "game/UnityProject"}), encoding="utf-8")
        self.assertIsNone(project.find_project_file(self.repo))
        self.assertIsNone(project.resolve_unity_project(None, {}, self.repo))

    def test_unity_project_is_relative_to_the_file_in_tools_valtrion(self):
        self.write_file({"unityProject": "../../game/UnityProject"})
        self.assertEqual(self.repo / "tools" / "valtrion" / "valtrion.json", project.find_project_file(self.repo / "game"))

    def test_the_search_ends_at_the_first_folder_with_a_git_folder(self):
        self.write_file({"unityProject": "../../game/UnityProject"})
        nested = self.repo / "nested"
        (nested / ".git").mkdir(parents=True)
        self.assertIsNone(project.find_project_file(nested / "deep"))
        self.assertIsNone(project.find_project_file(nested))
        self.assertIsNotNone(project.find_project_file(self.repo / "game"))

    def test_valtrion_json_is_found_by_walking_up_from_the_working_directory(self):
        self.write_file({"unityProject": "../../game/UnityProject"})
        deep = self.repo / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(self.unity.resolve(), project.resolve_unity_project(None, {}, deep))

    def test_no_file_and_no_override_means_no_project(self):
        self.assertIsNone(project.resolve_unity_project(None, {}, self.base))

    def test_explicit_folder_beats_environment_beats_valtrion_json(self):
        self.write_file({"unityProject": "../../game/UnityProject"})
        other, third = self.base / "other", self.base / "third"
        self.assertEqual(other.resolve(), project.resolve_unity_project(other, {project.PROJECT_ENV: str(third)}, self.repo))
        self.assertEqual(third.resolve(), project.resolve_unity_project(None, {project.PROJECT_ENV: str(third)}, self.repo))

    def test_invalid_files_are_reported_with_the_path(self):
        for value in ({}, {"unityProject": ""}, {"unityProject": 3}, {"unityProject": "missing"}, []):
            with self.subTest(value):
                self.write_file(value)
                with self.assertRaises(project.ProjectError) as caught:
                    project.resolve_unity_project(None, {}, self.repo)
                self.assertIn("valtrion.json", str(caught.exception))

    def test_a_checks_key_is_refused_with_the_upgrade_text(self):
        self.write_file({"unityProject": "../../game/UnityProject", "checks": "validation/device/checks.json"})
        with self.assertRaises(project.ProjectError) as caught:
            project.resolve_unity_project(None, {}, self.repo)
        self.assertIn('"checks" names a check map', str(caught.exception))
        self.assertIn("Remove the", str(caught.exception))

    def test_any_other_unknown_key_is_refused_by_name(self):
        self.write_file({"unityProject": "../../game/UnityProject", "platforms": ["ios"]})
        with self.assertRaises(project.ProjectError) as caught:
            project.resolve_unity_project(None, {}, self.repo)
        self.assertIn("'platforms'", str(caught.exception))

    def test_a_leftover_checks_key_blocks_the_commands_that_find_the_project(self):
        self.write_file({"unityProject": "../../game/UnityProject", "checks": "x.json"})
        plan = self.base / "p.json"
        plan.write_text((Path(__file__).resolve().parent.parent.parent / "contracts" / "fixtures" / "valid" / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch("pathlib.Path.cwd", return_value=self.repo), mock.patch.dict(os.environ, {core.HOME_ENV: str(self.base / "h")}):
            os.environ.pop(project.PROJECT_ENV, None)
            code, _, err = run("run", "--repo", "r", "--sha", "a" * 40, "--app", "x", str(plan))
            self.assertEqual(2, code)
            self.assertIn('"checks" names a check map', err)
            code, _, err = run("doctor", "--json", "--project")
            self.assertEqual(2, code)
            self.assertIn('"checks" names a check map', err)

    def test_removed_commands_are_unknown(self):
        for command in ("select", "check-config"):
            with self.subTest(command), self.assertRaises(SystemExit) as caught:
                run(command)
            self.assertEqual(2, caught.exception.code)

    def test_a_pipeline_command_reports_an_invalid_valtrion_json_as_blocked(self):
        self.write_file({"unityProject": "missing"})
        plan = self.base / "p.json"
        plan.write_text((Path(__file__).resolve().parent.parent.parent / "contracts" / "fixtures" / "valid" / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch("pathlib.Path.cwd", return_value=self.repo), mock.patch.dict(os.environ, {core.HOME_ENV: str(self.base / "h")}):
            os.environ.pop(project.PROJECT_ENV, None)
            code, _, err = run("run", "--repo", "r", "--sha", "a" * 40, "--app", "x", str(plan))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)


class ProfilesLocationTests(ProjectCase):
    def test_profiles_are_found_in_tools_valtrion_of_the_repository_root(self):
        (self.repo / ".git").mkdir()
        target = self.repo / "tools" / "valtrion" / "profiles.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")
        self.assertEqual(target, project.profiles_file(self.unity))

    def test_the_nearest_file_wins(self):
        (self.repo / ".git").mkdir()
        for folder in (self.repo, self.unity):
            (folder / "tools" / "valtrion").mkdir(parents=True)
            (folder / "tools" / "valtrion" / "profiles.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.unity / "tools" / "valtrion" / "profiles.json", project.profiles_file(self.unity))

    def test_the_old_place_is_ignored_and_the_expected_path_is_named(self):
        (self.repo / ".git").mkdir()
        old = self.unity / "validation" / "device"
        old.mkdir(parents=True)
        (old / "profiles.json").write_text("{}", encoding="utf-8")
        found = project.profiles_file(self.unity)
        self.assertEqual(self.repo / "tools" / "valtrion" / "profiles.json", found)
        self.assertFalse(found.exists())

    def test_the_search_does_not_leave_the_repository(self):
        outer = self.base / "tools" / "valtrion"
        outer.mkdir(parents=True)
        (outer / "profiles.json").write_text("{}", encoding="utf-8")
        (self.repo / ".git").mkdir()
        self.assertEqual(self.repo / "tools" / "valtrion" / "profiles.json", project.profiles_file(self.unity))

    def test_the_build_cache_hashes_the_profile_from_tools_valtrion_only(self):
        (self.repo / ".git").mkdir()
        self.assertEqual("none", buildcache.profile_config_hash(self.unity, "Validation"))
        old = self.unity / "validation" / "device"
        old.mkdir(parents=True)
        (old / "profiles.json").write_text(json.dumps({"profiles": {"Validation": {"android": {"executeMethod": "X.Y"}}}}), encoding="utf-8")
        self.assertEqual("none", buildcache.profile_config_hash(self.unity, "Validation"))
        target = self.repo / "tools" / "valtrion" / "profiles.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"profiles": {"Validation": {"android": {"executeMethod": "X.Y"}}}}), encoding="utf-8")
        first = buildcache.profile_config_hash(self.unity, "Validation")
        target.write_text(json.dumps({"profiles": {"Validation": {"android": {"executeMethod": "X.Z"}}}}), encoding="utf-8")
        second = buildcache.profile_config_hash(self.unity, "Validation")
        self.assertNotIn("none", (first, second))
        self.assertNotEqual(first, second)

    def test_a_git_file_stops_the_search_like_a_git_folder(self):
        (self.repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
        outer = self.base / "tools" / "valtrion"
        outer.mkdir(parents=True)
        (outer / "profiles.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.repo / "tools" / "valtrion" / "profiles.json", project.profiles_file(self.unity))

    def test_a_unity_project_in_a_subfolder_reads_the_profiles_of_the_repository_root(self):
        from valtrion.providers.unity import support
        (self.repo / ".git").mkdir()
        target = self.repo / "tools" / "valtrion" / "profiles.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"schema": "gdv.profiles/1", "profiles": {"Validation": {"editor": {"planAssembly": "A"}}}}), encoding="utf-8")
        self.assertEqual({"Validation": {"editor": {"planAssembly": "A"}}}, support.load_profiles(self.unity))

    def test_a_malformed_profiles_file_is_named_by_its_path(self):
        from valtrion.providers.unity import support
        (self.repo / ".git").mkdir()
        target = self.repo / "tools" / "valtrion" / "profiles.json"
        target.parent.mkdir(parents=True)
        target.write_text("{", encoding="utf-8")
        with self.assertRaises(support.ProfileError) as caught:
            support.load_profiles(self.unity)
        self.assertIn(str(target), str(caught.exception))

    def test_without_a_repository_the_expected_path_is_below_the_project(self):
        self.assertEqual(self.unity / "tools" / "valtrion" / "profiles.json", project.profiles_file(self.unity))


class StrayFileTests(ProjectCase):
    def put(self, relative, text="{}"):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_files_in_the_right_place_give_no_warning(self):
        (self.repo / ".git").mkdir()
        self.put("tools/valtrion/valtrion.json", '{"unityProject": "../../game/UnityProject"}')
        self.put("tools/valtrion/profiles.json", '{"schema": "gdv.profiles/1", "profiles": {}}')
        self.put("tools/valtrion/plans/smoke.plan.json")
        self.put("tools/valtrion/plans/night.pipeline.json")
        self.assertEqual([], project.stray_files(self.unity))

    def test_every_stray_file_is_one_warning_that_names_where_it_belongs(self):
        (self.repo / ".git").mkdir()
        self.put("valtrion.json")
        self.put("game/UnityProject/validation/device/profiles.json", '{"schema": "gdv.profiles/1"}')
        self.put("validation/device/smoke.plan.json")
        self.put("docs/night.pipeline.json")
        self.put("validation/device/checks.json")
        warnings = project.stray_files(self.unity)
        self.assertEqual(7, len(warnings), warnings)
        joined = " | ".join(warnings)
        for expected in ("valtrion.json is not read: move it to tools/valtrion/valtrion.json",
                         "game/UnityProject/validation/device/profiles.json is not read: move it to tools/valtrion/profiles.json",
                         "validation/device/smoke.plan.json is outside tools/valtrion/: move it to tools/valtrion/plans/smoke.plan.json",
                         "docs/night.pipeline.json is outside tools/valtrion/: move it to tools/valtrion/plans/night.pipeline.json",
                         "validation/device/checks.json is a check map",
                         "validation/device/ is a Valtrion folder in the wrong place"):
            self.assertIn(expected, joined)

    def test_a_profiles_json_of_another_tool_is_not_reported(self):
        (self.repo / ".git").mkdir()
        self.put("web/profiles.json", '{"user": "x"}')
        self.assertEqual([], project.stray_files(self.unity))

    def test_cache_and_build_folders_are_skipped(self):
        (self.repo / ".git").mkdir()
        for folder in ("Library", "node_modules", "Temp", "Builds", "Logs", "obj", "game/UnityProject/Library/PackageCache/x"):
            self.put(f"{folder}/a.plan.json")
        self.assertEqual([], project.stray_files(self.unity))

    def test_the_warnings_are_sorted_and_bounded(self):
        (self.repo / ".git").mkdir()
        for index in range(project.MAX_STRAY_WARNINGS + 5):
            self.put(f"plans/p{index:03d}.plan.json")
        warnings = project.stray_files(self.unity)
        self.assertEqual(project.MAX_STRAY_WARNINGS + 1, len(warnings))
        self.assertIn("p000.plan.json", warnings[0])
        self.assertEqual("5 more Valtrion files outside tools/valtrion/", warnings[-1])

    def test_without_a_git_folder_only_the_unity_project_folder_is_searched(self):
        self.put("outside.plan.json")
        self.put("game/UnityProject/smoke.plan.json")
        warnings = project.stray_files(self.unity)
        self.assertEqual(1, len(warnings), warnings)
        self.assertIn("smoke.plan.json", warnings[0])

    def test_a_git_file_of_a_worktree_marks_the_repository_root(self):
        (self.repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
        self.put("a.plan.json")
        self.assertEqual(self.repo, project.repository_root(self.unity))
        self.assertEqual(1, len(project.stray_files(self.unity)))

    def test_doctor_lists_them_without_blocking(self):
        self.lock({"version": "1.0.0", "source": "registry"})
        (self.repo / ".git").mkdir()
        self.put("smoke.plan.json")
        environ = {core.HOME_ENV: str(self.base / "home"), "GDV_UNITY_HUB_PATHS": str(self.base / "none")}
        with mock.patch.dict(os.environ, environ):
            code, out, _ = run("doctor", "--json", "--project", str(self.unity))
        report = json.loads(out)["project"]
        self.assertEqual((0, "ok", 1), (code, report["status"], len(report["warnings"])))
        self.assertIn("tools/valtrion/plans/smoke.plan.json", report["warnings"][0])


class VersionTests(unittest.TestCase):
    def test_ranges(self):
        self.assertTrue(project.version_in_range("0.7.0", ">=0.7.0,<0.8"))
        self.assertTrue(project.version_in_range("0.7.12", ">=0.7.0,<0.8"))
        self.assertFalse(project.version_in_range("0.8.0", ">=0.7.0,<0.8"))
        self.assertFalse(project.version_in_range("0.6.9", ">=0.7.0,<0.8"))
        self.assertTrue(project.version_in_range("1.0.0-rc.1", "==1.0.0"))

    def test_package_patterns(self):
        self.assertTrue(project.package_matches("0.7.3", "0.7.x"))
        self.assertFalse(project.package_matches("0.8.0", "0.7.x"))
        self.assertTrue(project.package_matches("0.7.3", "0.7.3"))
        self.assertFalse(project.package_matches("0.7.4", "0.7.3"))

    def test_unsupported_range_syntax_is_an_error(self):
        with self.assertRaises(project.ProjectError):
            project.version_in_range("0.7.0", "~0.7")


class CheckTests(ProjectCase):
    def check(self, core_version="0.7.0"):
        return project.check_project(self.unity, core_version, self.compat)

    def test_project_without_the_package_is_skipped(self):
        self.assertEqual("skipped", self.check()["status"], "no lock file at all")
        self.lock(None)
        self.assertEqual("skipped", self.check()["status"], "a lock without the package")

    def test_matching_version_is_ok(self):
        self.lock({"version": "0.7.2", "source": "registry"})
        self.assertEqual("ok", self.check()["status"])

    def test_package_outside_the_table_is_blocked_with_an_action(self):
        self.lock({"version": "0.9.0", "source": "registry"})
        result = self.check()
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("0.9.0", result["reason"])
        self.assertIn("0.7.x", result["action"])

    def test_core_outside_the_range_is_blocked_with_an_action(self):
        self.lock({"version": "0.7.2", "source": "registry"})
        result = self.check("0.8.1")
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn(">=0.7.0,<0.8", result["reason"])
        self.assertIn("valtrion hosts sync", result["action"])

    def test_git_package_version_comes_from_the_package_cache(self):
        self.lock({"version": "https://example.invalid/x.git", "source": "git", "hash": "abcdef0123456789"})
        cache = self.unity / "Library" / "PackageCache" / f"{project.UNITY_PACKAGE}@abcdef012345"
        cache.mkdir(parents=True)
        (cache / "package.json").write_text(json.dumps({"version": "0.7.1"}), encoding="utf-8")
        self.assertEqual("ok", self.check()["status"])

    def test_git_package_that_is_not_unpacked_is_blocked(self):
        self.lock({"version": "https://example.invalid/x.git", "source": "git", "hash": "abcdef0123456789"})
        self.assertEqual("BLOCKED", self.check()["status"])

    def test_local_package_reads_its_package_json(self):
        folder = self.base / "pkg"
        folder.mkdir()
        (folder / "package.json").write_text(json.dumps({"version": "0.7.0"}), encoding="utf-8")
        self.lock({"version": "file:" + str(folder), "source": "local"})
        self.assertEqual("ok", self.check()["status"])

    def test_the_shipped_compat_table_is_valid_and_lists_this_core(self):
        self.lock({"version": "1.0.0", "source": "registry"})
        self.assertEqual("ok", project.check_project(self.unity)["status"])


class DoctorProjectTests(ProjectCase):
    def doctor(self, *args):
        environ = {core.HOME_ENV: str(self.base / "home"), "GDV_UNITY_HUB_PATHS": str(self.base / "none")}
        with mock.patch.dict(os.environ, environ):
            return run("doctor", "--json", *args)

    def test_mismatch_is_blocked_with_exit_two_and_the_action(self):
        self.lock({"version": "0.99.0", "source": "registry"})
        code, out, err = self.doctor("--project", str(self.unity))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)
        self.assertEqual("BLOCKED", json.loads(out)["project"]["status"])

    def test_project_without_the_package_is_reported_skipped(self):
        code, out, _ = self.doctor("--project", str(self.unity))
        self.assertEqual(0, code)
        self.assertEqual("skipped", json.loads(out)["project"]["status"])

    def test_a_leftover_check_map_is_a_warning_not_a_block(self):
        self.lock({"version": "1.0.0", "source": "registry"})
        (self.repo / ".git").mkdir()
        old = self.repo / "validation" / "device"
        old.mkdir(parents=True)
        (old / "checks.json").write_text("{}", encoding="utf-8")
        code, out, _ = self.doctor("--project", str(self.unity))
        project_report = json.loads(out)["project"]
        self.assertEqual((0, "ok"), (code, project_report["status"]))
        self.assertEqual(1, len(project_report["warnings"]))
        self.assertIn("checks.json", project_report["warnings"][0])

    def test_the_leftover_search_stops_at_the_repository_root(self):
        outside = self.base / "validation" / "device"
        outside.mkdir(parents=True)
        (outside / "checks.json").write_text("{}", encoding="utf-8")
        (self.repo / ".git").mkdir()
        self.assertIsNone(project.leftover_check_map(self.unity))

    def test_the_leftover_search_stops_at_valtrion_json(self):
        outside = self.base / "validation" / "device"
        outside.mkdir(parents=True)
        (outside / "checks.json").write_text("{}", encoding="utf-8")
        (self.repo / "tools" / "valtrion").mkdir(parents=True)
        (self.repo / "tools" / "valtrion" / "valtrion.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(project.leftover_check_map(self.unity))

    def test_without_a_repository_or_valtrion_json_only_the_folder_is_searched(self):
        parent = self.base / "validation" / "device"
        parent.mkdir(parents=True)
        (parent / "checks.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(project.leftover_check_map(self.unity))
        own = self.unity / "validation" / "device"
        own.mkdir(parents=True)
        (own / "checks.json").write_text("{}", encoding="utf-8")
        self.assertEqual(own / "checks.json", project.leftover_check_map(self.unity))

    def test_doctor_project_finds_the_unity_project_through_tools_valtrion(self):
        self.lock({"version": "1.0.0", "source": "registry"})
        target = self.repo / "tools" / "valtrion" / "valtrion.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"unityProject": "../../game/UnityProject"}), encoding="utf-8")
        with mock.patch("pathlib.Path.cwd", return_value=self.repo):
            os.environ.pop(project.PROJECT_ENV, None)
            code, out, _ = self.doctor("--project")
        self.assertEqual((0, "ok", str(self.unity)), (code, json.loads(out)["project"]["status"], json.loads(out)["project"]["project"]))

    def test_doctor_project_ignores_an_old_valtrion_json_and_names_the_new_path(self):
        (self.repo / "valtrion.json").write_text(json.dumps({"unityProject": "game/UnityProject"}), encoding="utf-8")
        with mock.patch("pathlib.Path.cwd", return_value=self.repo):
            os.environ.pop(project.PROJECT_ENV, None)
            _, out, _ = self.doctor("--project")
        report = json.loads(out)["project"]
        self.assertEqual("skipped", report["status"])
        self.assertIn("tools/valtrion/valtrion.json", report["reason"])

    def test_without_a_leftover_map_there_is_no_warning(self):
        self.lock({"version": "1.0.0", "source": "registry"})
        (self.repo / ".git").mkdir()
        _, out, _ = self.doctor("--project", str(self.unity))
        self.assertNotIn("warnings", json.loads(out)["project"])

    def test_supported_package_passes(self):
        self.lock({"version": "1.0.0", "source": "registry"})
        code, out, _ = self.doctor("--project", str(self.unity))
        self.assertEqual((0, "ok"), (code, json.loads(out)["project"]["status"]))


class ShippedTableTests(ProjectCase):
    def test_the_shipped_table_names_the_real_unity_package(self):
        package, ranges = project.load_compat_document()
        self.assertEqual("com.goryned.valtrion.unity", package)
        self.assertEqual("0.7.x", ranges[0]["package"])

    def test_package_0_7_x_stays_compatible_with_every_core_of_the_0_9_line_up_to_0_13(self):
        # The new optional keys of a contract do not change what the package reads, so core 0.9.x keeps the 0.7.x pairing.
        self.lock({"version": "0.7.1", "source": "registry"})
        for core_version in ("0.8.8", "0.9.0", "0.9.7", "0.13.1"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        self.assertNotEqual("ok", project.check_project(self.unity, "0.14.0")["status"])  # outside the range: reported, not silently accepted

    def test_package_0_8_x_needs_core_0_8_or_later_and_works_up_to_core_0_13(self):
        self.lock({"version": "0.8.0", "source": "registry"})
        for core_version in ("0.8.0", "0.9.0", "0.13.1"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        self.assertNotEqual("ok", project.check_project(self.unity, "0.7.4")["status"])  # the 0.8 readers are for core 0.8 and later
        self.assertNotEqual("ok", project.check_project(self.unity, "0.14.0")["status"])

    def test_package_0_9_x_works_with_core_0_8_to_0_13(self):
        self.lock({"version": "0.9.0", "source": "registry"})
        for core_version in ("0.8.0", "0.9.0", "0.10.0", "0.13.1"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        self.assertNotEqual("ok", project.check_project(self.unity, "0.7.4")["status"])
        self.assertNotEqual("ok", project.check_project(self.unity, "0.14.0")["status"])

    def test_package_0_10_x_works_with_core_0_8_to_0_13(self):
        self.lock({"version": "0.10.0", "source": "registry"})
        for core_version in ("0.8.0", "0.10.0", "0.11.0", "0.12.0", "0.13.1"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        self.assertNotEqual("ok", project.check_project(self.unity, "0.7.4")["status"])
        self.assertNotEqual("ok", project.check_project(self.unity, "0.14.0")["status"])

    def test_package_0_11_0_works_with_core_0_8_to_0_13_but_not_the_current_core(self):
        # 0.11.0 lacks the readers for cleanup, externalUi, checkout, valtrionCommand, pipelineRun and reusedFrom.
        self.lock({"version": "0.11.0", "source": "registry"})
        for core_version in ("0.8.0", "0.11.0", "0.13.0", "0.13.1"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        self.assertNotEqual("ok", project.check_project(self.unity, "0.7.4")["status"])
        for core_version in ("0.14.0", __import__("valtrion").__version__):
            result = project.check_project(self.unity, core_version)
            self.assertEqual("BLOCKED", result["status"], core_version)
            self.assertIn(">=0.8.0,<0.14", result["reason"])

    def test_package_0_11_1_works_with_core_0_8_to_0_14_but_not_0_15(self):
        self.lock({"version": "0.11.1", "source": "registry"})
        for core_version in ("0.8.0", "0.13.1", "0.14.0", "0.14.1"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        for core_version in ("0.7.4", "0.15.0"):
            self.assertNotEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)

    def test_the_exact_0_11_1_row_widens_only_that_package_version(self):
        _, ranges = project.load_compat_document()
        exact = [r for r in ranges if r["package"] == "0.11.1"]
        self.assertEqual([{"package": "0.11.1", "core": ">=0.8.0,<0.15"}], exact)
        self.assertEqual(">=0.8.0,<0.14", next(r["core"] for r in ranges if r["package"] == "0.11.x"))
        for version in ("0.11.0", "0.11.2", "0.11.12"):
            self.assertFalse(project.package_matches(version, "0.11.1"), version)
        for core_version in ("0.14.0", "0.14.9"):
            self.assertTrue(any(project.package_matches("0.11.1", str(r["package"])) and project.version_in_range(core_version, str(r["core"]))
                                for r in ranges), core_version)
            self.assertFalse(any(project.package_matches("0.11.0", str(r["package"])) and project.version_in_range(core_version, str(r["core"]))
                                 for r in ranges), core_version)

    def test_package_0_11_2_has_its_own_exact_row_like_0_11_1(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "0.11.2", "core": ">=0.8.0,<0.15"}], [r for r in ranges if r["package"] == "0.11.2"])
        for version in ("0.11.1", "0.11.3", "0.11.20"):
            self.assertFalse(project.package_matches(version, "0.11.2"), version)
        self.lock({"version": "0.11.2", "source": "registry"})
        for core_version in ("0.8.0", "0.13.1", "0.14.0", "0.14.9", "0.14.1"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        for core_version in ("0.7.4", "0.15.0"):
            self.assertNotEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        self.lock({"version": "0.11.3", "source": "registry"})
        self.assertEqual("BLOCKED", project.check_project(self.unity, "0.14.0")["status"], "a later 0.11 package needs its own row")

    def test_package_0_12_x_works_with_core_0_8_to_0_15_and_not_with_0_16(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "0.12.x", "core": ">=0.8.0,<0.16"}], [r for r in ranges if r["package"] == "0.12.x"])
        self.lock({"version": "0.12.0", "source": "registry"})
        for core_version in ("0.8.0", "0.14.1", "0.15.0", "0.15.9"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        for core_version in ("0.7.4", "0.16.0"):
            self.assertNotEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)

    def test_package_0_13_x_works_with_core_0_8_to_0_17_and_not_with_0_18(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "0.13.x", "core": ">=0.8.0,<0.18"}], [r for r in ranges if r["package"] == "0.13.x"])
        self.lock({"version": "0.13.0", "source": "registry"})
        for core_version in ("0.8.0", "0.15.1", "0.16.0", "0.17.0", "0.17.9"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        for core_version in ("0.7.4", "0.18.0"):
            self.assertNotEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)

    def test_package_0_14_x_works_with_core_0_8_to_0_17_and_not_with_0_18(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "0.14.x", "core": ">=0.8.0,<0.18"}], [r for r in ranges if r["package"] == "0.14.x"])
        self.lock({"version": "0.14.0", "source": "registry"})
        for core_version in ("0.8.0", "0.16.0", "0.17.0", "0.17.9"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        for core_version in ("0.7.4", "0.18.0"):
            self.assertNotEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)

    def test_package_0_15_x_works_with_core_0_8_to_0_19_and_not_with_0_20(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "0.15.x", "core": ">=0.8.0,<0.20"}], [r for r in ranges if r["package"] == "0.15.x"])
        self.lock({"version": "0.15.0", "source": "registry"})
        for core_version in ("0.8.0", "0.17.1", "0.18.0", "0.18.9", "0.19.0"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        for core_version in ("0.7.4", "0.20.0"):
            self.assertNotEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)

    def test_package_0_16_x_works_with_core_0_8_to_0_19_and_not_with_0_20(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "0.16.x", "core": ">=0.8.0,<0.20"}], [r for r in ranges if r["package"] == "0.16.x"])
        self.lock({"version": "0.16.0", "source": "registry"})
        for core_version in ("0.8.0", "0.18.0", "0.19.0", "0.19.9"):
            self.assertEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)
        for core_version in ("0.7.4", "0.20.0"):
            self.assertNotEqual("ok", project.check_project(self.unity, core_version)["status"], core_version)

    def test_package_0_20_x_is_paired_with_core_0_24_only_and_0_19_x_is_not(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "0.20.x", "core": ">=0.24.0,<0.26"}], [r for r in ranges if r["package"] == "0.20.x"])
        self.lock({"version": "0.20.0", "source": "registry"})
        self.assertEqual("ok", project.check_project(self.unity, "0.24.0")["status"])
        self.assertNotEqual("ok", project.check_project(self.unity, "0.23.2")["status"])
        self.lock({"version": "0.19.0", "source": "registry"})
        self.assertEqual("ok", project.check_project(self.unity, "0.23.2")["status"])
        self.assertNotEqual("ok", project.check_project(self.unity, "0.24.0")["status"])

    def test_package_1_0_x_is_paired_with_core_1_x_only_and_0_20_x_is_not(self):
        _, ranges = project.load_compat_document()
        self.assertEqual([{"package": "1.0.x", "core": ">=1.0.0,<2"}], [r for r in ranges if r["package"] == "1.0.x"])
        self.lock({"version": "1.0.0", "source": "registry"})
        self.assertEqual("ok", project.check_project(self.unity, "1.0.0")["status"])
        self.assertNotEqual("ok", project.check_project(self.unity, "0.25.0")["status"])
        self.assertNotEqual("ok", project.check_project(self.unity, "2.0.0")["status"])
        self.lock({"version": "0.20.1", "source": "registry"})
        self.assertEqual("ok", project.check_project(self.unity, "0.25.0")["status"])
        self.assertNotEqual("ok", project.check_project(self.unity, "1.0.0")["status"])

    def test_package_0_14_x_is_not_paired_with_core_0_18_that_it_cannot_read_the_keys_of(self):
        self.lock({"version": "0.14.0", "source": "registry"})
        result = project.check_project(self.unity, "0.18.0")
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn(">=0.8.0,<0.18", result["reason"])

    def test_the_package_and_the_core_of_this_checkout_are_paired(self):
        package = json.loads((Path(__file__).resolve().parents[2] / "integrations" / "unity" / "com.goryned.valtrion.unity" / "package.json")
                             .read_text(encoding="utf-8"))["version"]
        _, ranges = project.load_compat_document()
        core_version = __import__("valtrion").__version__
        self.assertTrue(any(project.package_matches(package, str(r["package"])) and project.version_in_range(core_version, str(r["core"]))
                            for r in ranges), f"package {package} and core {core_version} must be paired by compat.json")

    def test_the_package_id_comes_from_the_table(self):
        other = self.base / "other.json"
        other.write_text(json.dumps({**COMPAT, "package": "com.example.other"}), encoding="utf-8")
        (self.unity / "Packages" / "packages-lock.json").write_text(
            json.dumps({"dependencies": {"com.example.other": {"version": "0.7.1", "source": "registry"}}}), encoding="utf-8")
        self.assertEqual("ok", project.check_project(self.unity, "0.7.0", other)["status"])
        self.assertEqual("skipped", project.check_project(self.unity, "0.7.0", self.compat)["status"])

    def test_a_malformed_core_range_is_blocked_not_a_traceback(self):
        bad = self.base / "bad.json"
        bad.write_text(json.dumps({**COMPAT, "ranges": [{"package": "0.7.x", "core": "~0.7"}]}), encoding="utf-8")
        self.lock({"version": "0.7.0", "source": "registry"})
        result = project.check_project(self.unity, "0.7.0", bad)
        self.assertEqual(("BLOCKED", "reinstall valtrion"), (result["status"], result["action"]))
