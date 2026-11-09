import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, core, project

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
        (self.repo / "valtrion.json").write_text(json.dumps(value), encoding="utf-8")

    def test_valtrion_json_is_found_by_walking_up_from_the_working_directory(self):
        self.write_file({"unityProject": "game/UnityProject"})
        deep = self.repo / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(self.unity.resolve(), project.resolve_unity_project(None, {}, deep))

    def test_no_file_and_no_override_means_no_project(self):
        self.assertIsNone(project.resolve_unity_project(None, {}, self.base))

    def test_explicit_folder_beats_environment_beats_valtrion_json(self):
        self.write_file({"unityProject": "game/UnityProject"})
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

    def test_a_pipeline_command_reports_an_invalid_valtrion_json_as_blocked(self):
        self.write_file({"unityProject": "missing"})
        plan = self.base / "p.json"
        plan.write_text((Path(__file__).resolve().parent.parent.parent / "contracts" / "fixtures" / "valid" / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch("pathlib.Path.cwd", return_value=self.repo), mock.patch.dict(os.environ, {core.HOME_ENV: str(self.base / "h")}):
            os.environ.pop(project.PROJECT_ENV, None)
            code, _, err = run("run", "--repo", "r", "--sha", "a" * 40, "--app", "x", str(plan))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)


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
        self.lock({"version": "0.16.0", "source": "registry"})
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

    def test_supported_package_passes(self):
        self.lock({"version": "0.15.0", "source": "registry"})
        code, out, _ = self.doctor("--project", str(self.unity))
        self.assertEqual((0, "ok"), (code, json.loads(out)["project"]["status"]))


class ShippedTableTests(ProjectCase):
    def test_the_shipped_table_names_the_real_unity_package(self):
        package, ranges = project.load_compat_document()
        self.assertEqual("com.goryned.valtrion.unity", package)
        self.assertEqual("0.7.x", ranges[0]["package"])

    def test_package_0_7_x_stays_compatible_with_every_core_of_the_0_9_line_up_to_0_13(self):
        # The new optional keys of gdv.checks/1 do not change what the package reads, so core 0.9.x keeps the 0.7.x pairing.
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


@unittest.skipUnless(shutil.which("git"), "needs git")
class ChecksSettingTests(ProjectCase):
    """`checks` in valtrion.json moves the default check map; --checks still wins."""

    CHECKS = json.dumps({"schema": "gdv.checks/1", "platforms": {"required": ["ios"], "dev": ["editor"]}, "areas": [{"glob": "**", "kind": "docs"}],
                         "kinds": {"docs": {"L0": ["l0"]}}, "unclassified": "widest"})

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.nested = self.repo / "game" / "validation" / "device"
        self.nested.mkdir(parents=True)
        self.map = self.nested / "checks.json"
        self.map.write_text(self.CHECKS, encoding="utf-8")

    def write_file(self, value):
        (self.repo / "valtrion.json").write_text(json.dumps(value), encoding="utf-8")

    def check_config(self, *args, cwd=None):
        with mock.patch("pathlib.Path.cwd", return_value=cwd or self.repo):
            return run("check-config", *args)

    def test_checks_key_is_relative_to_the_file_and_used_by_default(self):
        self.write_file({"unityProject": "game/UnityProject", "checks": "game/validation/device/checks.json"})
        code, out, err = self.check_config()
        self.assertEqual(0, code, err)
        self.assertIn(str(self.map.resolve()), out)

    def test_without_the_key_the_repository_default_is_used(self):
        self.write_file({"unityProject": "game/UnityProject"})
        code, _, err = self.check_config()
        self.assertEqual(2, code)
        self.assertIn("checks.json", err, "validation/device/checks.json under the repository root does not exist here")
        default = self.repo / "validation" / "device"
        default.mkdir(parents=True)
        (default / "checks.json").write_text(self.CHECKS, encoding="utf-8")
        self.assertEqual(0, self.check_config()[0])

    def test_the_key_is_found_from_a_subfolder(self):
        self.write_file({"unityProject": "game/UnityProject", "checks": "game/validation/device/checks.json"})
        self.assertEqual(0, self.check_config(cwd=self.nested)[0])

    def test_explicit_checks_beats_the_key(self):
        self.write_file({"unityProject": "game/UnityProject", "checks": "missing/checks.json"})
        self.assertEqual(0, self.check_config("--checks", str(self.map))[0])

    def test_a_bad_checks_value_is_blocked_by_every_command(self):
        for value in (3, ""):
            self.write_file({"unityProject": "game/UnityProject", "checks": value})
            for command in (["check-config"], ["select", "--base-ref", "HEAD"], ["report", "--base-ref", "HEAD", "--results", str(self.base)]):
                with self.subTest(value=value, command=command[0]), mock.patch("pathlib.Path.cwd", return_value=self.repo):
                    code, _, err = run(*command)
                    self.assertEqual(2, code)
                    self.assertIn('"checks" must be a path', err)
                    self.assertNotIn("Traceback", err)

    def test_an_unreadable_valtrion_json_is_blocked(self):
        (self.repo / "valtrion.json").write_text("{ nope", encoding="utf-8")
        code, _, err = self.check_config()
        self.assertEqual(2, code)
        self.assertIn("valtrion.json", err)

    def test_a_valtrion_json_above_the_repository_is_ignored(self):
        (self.base / "valtrion.json").write_text(json.dumps({"checks": "elsewhere/checks.json"}), encoding="utf-8")
        default = self.repo / "validation" / "device"
        default.mkdir(parents=True)
        (default / "checks.json").write_text(self.CHECKS, encoding="utf-8")
        code, out, err = self.check_config()
        self.assertEqual(0, code, err)
        self.assertNotIn("elsewhere", out)

    def test_select_uses_the_key_too(self):
        self.write_file({"unityProject": "game/UnityProject", "checks": "game/validation/device/checks.json"})
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        for args in (["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, env={**os.environ, **env})
        with mock.patch("pathlib.Path.cwd", return_value=self.repo):
            code, out, err = run("select", "--base-ref", "HEAD", "--json")
        self.assertEqual(0, code, err)
        self.assertEqual("HEAD", json.loads(out)["baseRef"])
