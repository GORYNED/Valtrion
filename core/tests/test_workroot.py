import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from valtrion import config, core, doctor, jobs, remote, workroot

PYTHON = sys.executable


def git(args, cwd):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


class DefaultLocationTests(unittest.TestCase):
    HOME = Path("/Users/someone") if os.name != "nt" else Path("C:/Users/someone")

    def test_macos_default_is_in_library_caches_outside_protected_folders(self):
        root = workroot.default_work_root("macos", {}, self.HOME)
        self.assertEqual(self.HOME / "Library" / "Caches" / "com.valtrion" / "work", root)
        self.assertIsNone(workroot.protected_location(root, "macos", self.HOME))

    def test_windows_default_is_short_and_outside_local_app_data(self):
        root = workroot.default_work_root("windows", {"LOCALAPPDATA": "D:/Local"}, self.HOME)
        self.assertEqual(self.HOME / ".valtrion" / "work", root)
        self.assertIsNone(workroot.virtualized_location(root, "windows", {"LOCALAPPDATA": "D:/Local"}, self.HOME, packaged=True))
        self.assertIsNone(workroot.protected_location(root, "windows", self.HOME))

    def test_linux_default_honours_xdg_cache_home(self):
        self.assertEqual(Path("/xdg/valtrion/work"), workroot.default_work_root("linux", {"XDG_CACHE_HOME": "/xdg"}, self.HOME))
        self.assertEqual(self.HOME / ".cache" / "valtrion" / "work", workroot.default_work_root("linux", {}, self.HOME))

    def test_environment_override_and_isolated_gdv_home(self):
        self.assertEqual(Path("/custom"), workroot.default_work_root("macos", {workroot.WORK_ROOT_ENV: "/custom"}, self.HOME))
        self.assertEqual(Path("/scratch/gdv/work"), workroot.default_work_root("macos", {core.HOME_ENV: "/scratch/gdv"}, self.HOME))


class ProtectedLocationTests(unittest.TestCase):
    HOME = Path("/Users/someone")

    def reason(self, path, os_name="macos"):
        return workroot.protected_location(path, os_name, self.HOME)

    def test_macos_protected_folders(self):
        for folder in ("Documents", "Desktop", "Downloads"):
            self.assertIn(folder, self.reason(f"/Users/someone/{folder}/Projects/x"), folder)
        self.assertIn("iCloud", self.reason("/Users/someone/Library/Mobile Documents/com~apple~CloudDocs/x"))
        self.assertIn("removable", self.reason("/Volumes/External/x"))

    def test_macos_unprotected_locations(self):
        for path in ("/Users/someone/Library/Caches/com.valtrion/work", "/Users/someone/.valtrion", "/Users/someone/gdv",
                     "/Users/someone/DocumentsOther/x", "/tmp/x"):
            self.assertIsNone(self.reason(path), path)

    def test_macos_matching_is_case_insensitive_but_whole_name(self):
        self.assertIsNone(self.reason("/Users/someone/Documents-old/x"))
        self.assertIsNotNone(self.reason("/users/someone/documents/x"))

    def test_windows_onedrive_and_known_folders(self):
        home = Path("C:/Users/someone")
        self.assertIn("OneDrive", workroot.protected_location(Path("C:/Users/someone/OneDrive/Projects"), "windows", home))
        self.assertIn("Documents", workroot.protected_location(Path("C:/Users/someone/Documents/x"), "windows", home))
        self.assertIsNone(workroot.protected_location(Path("C:/Users/someone/AppData/Local/Example"), "windows", home))
        self.assertIsNone(workroot.protected_location(Path("/home/x/Documents"), "linux", Path("/home/x")))


class WindowsWorkRootTests(unittest.TestCase):
    HOME = Path("C:/Users/someone")
    LIMIT = workroot.MAX_WORK_ROOT_LENGTH

    def test_virtualized_packaged_app_location_is_reported(self):
        for path in ("C:/Users/someone/AppData/Local/Packages/Vendor.App_abc123/LocalCache/Local/Valtrion/work",
                     "c:/users/someone/appdata/local/PACKAGES/Vendor.App_abc123/localcache/Local/Valtrion/work"):
            reason = workroot.virtualized_location(Path(path), "windows", {}, self.HOME, packaged=False)
            self.assertIn("LocalCache", reason, path)
        for path in ("C:/Users/someone/AppData/Local/Valtrion/work", "C:/gdw", "C:/Packages/Vendor/Cache",
                     "D:/Packages/x/LocalCache/work", "D:/Packages/x/LocalCache"):
            self.assertIsNone(workroot.virtualized_location(Path(path), "windows", {}, self.HOME, packaged=False), path)
        self.assertIsNone(workroot.virtualized_location(Path("/home/x/AppData/Local/Packages/a/LocalCache"), "linux", {}, Path("/home/x")))

    def test_a_packaged_interpreter_virtualizes_the_nominal_local_app_data(self):
        environ = {"LOCALAPPDATA": "C:/Users/someone/AppData/Local"}
        nominal = Path("C:/Users/someone/AppData/Local/Valtrion/work")  # does not exist: the real path cannot show a redirect
        self.assertIn("packaged app", workroot.virtualized_location(nominal, "windows", environ, self.HOME, packaged=True))
        self.assertIsNone(workroot.virtualized_location(nominal, "windows", environ, self.HOME, packaged=False))
        self.assertIsNone(workroot.virtualized_location(Path("C:/gdw"), "windows", environ, self.HOME, packaged=True))
        self.assertIsNotNone(workroot.virtualized_location(nominal, "windows", {}, self.HOME, packaged=True), "default AppData\\Local")

    @unittest.skipUnless(os.name == "nt", "the real path is only resolved on the host the path belongs to")
    def test_an_existing_root_that_resolves_to_itself_is_not_reported_for_the_assumption(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            real = os.path.realpath(folder)
            environ = {"LOCALAPPDATA": os.path.dirname(real)}
            self.assertIsNone(workroot.virtualized_location(Path(real), "windows", environ, self.HOME, packaged=True))
            missing = Path(real) / "not-yet"
            reason = workroot.virtualized_location(missing, "windows", {"LOCALAPPDATA": real}, self.HOME, packaged=True)
            self.assertIn("may see", reason)

    def test_packaged_interpreter_is_told_from_its_paths(self):
        store = "C:/Program Files/WindowsApps/PythonSoftwareFoundation.Python.3.12_x64/python.exe"
        self.assertTrue(workroot.packaged_interpreter(store, "C:/Python312"))
        self.assertTrue(workroot.packaged_interpreter("C:/Python312/python.exe", "C:/Users/a/AppData/Local/Packages/Vendor.App_1/LocalCache/Local/py"))
        self.assertFalse(workroot.packaged_interpreter("C:/Python312/python.exe", "C:/Python312"))
        self.assertFalse(workroot.packaged_interpreter("D:/Packages/x/python.exe", "D:/Packages/x"))

    def test_virtualization_is_no_protected_location_reason_any_more(self):
        path = Path("C:/Users/someone/AppData/Local/Packages/Vendor.App_abc123/LocalCache/Local/Valtrion/work")
        self.assertIsNone(workroot.protected_location(path, "windows", self.HOME))

    def test_threshold_is_what_the_reserve_leaves(self):
        self.assertEqual(260 - 1 - workroot.CHECKOUT_PATH_RESERVE, self.LIMIT)
        self.assertEqual(len("\\jobs\\") + workroot.MAX_JOB_ID_LENGTH + len("\\checkout\\") + workroot.DEEPEST_PROJECT_PATH,
                         workroot.CHECKOUT_PATH_RESERVE)
        stamp = jobs.new_job_id("x" * 100, "a" * 8)  # the longest id `new_job_id` makes
        self.assertEqual(workroot.MAX_JOB_ID_LENGTH, len(stamp))

    def test_the_path_as_given_is_counted_not_its_target(self):
        shortened = Path("S:/work")  # a subst drive or junction the owner chose
        with mock.patch.object(workroot.os.path, "realpath", return_value="C:/" + "a" * 200):
            self.assertIsNone(workroot.path_length_warning(shortened, "windows", False))

    def test_a_long_root_warns_only_when_long_paths_are_off(self):
        short = Path("C:/" + "a" * (self.LIMIT - 3))
        long = Path("C:/" + "a" * (self.LIMIT - 2))
        self.assertEqual(self.LIMIT, len(str(short).replace("/", "\\")))
        self.assertIsNone(workroot.path_length_warning(short, "windows", False))
        warning = workroot.path_length_warning(long, "windows", False)
        self.assertIn(f"{self.LIMIT + 1} characters", warning)
        self.assertIn("`workRoot` in host.json", warning)
        self.assertIn("GDV_WORK_ROOT", warning)
        self.assertIsNone(workroot.path_length_warning(long, "windows", True))
        self.assertIsNone(workroot.path_length_warning(long, "windows", None))

    def test_a_known_job_id_uses_its_own_length(self):
        job_id = "20261031T182754Z-build-0123abcd"  # 30 characters, 36 shorter than the worst case
        room = self.LIMIT + (workroot.MAX_JOB_ID_LENGTH - len(job_id))
        fits = Path("C:/" + "a" * (room - 3))
        over = Path("C:/" + "a" * (room - 2))
        self.assertIsNone(workroot.path_length_warning(fits, "windows", False, job_id))
        self.assertIsNotNone(workroot.path_length_warning(over, "windows", False))  # the worst case still warns
        self.assertIn(f"within {room} characters", workroot.path_length_warning(over, "windows", False, job_id))

    def test_start_warnings_repeat_the_doctor_text_on_windows_only(self):
        job_id = "20261031T182754Z-build-0123abcd"
        long = Path("C:/" + "a" * 120)
        warnings = workroot.start_warnings(long, job_id, "windows", long_paths_enabled=lambda: False)
        self.assertEqual(1, len(warnings))
        self.assertTrue(warnings[0].startswith(f"work root {long} is 123 characters long"))
        self.assertIn("GDV_WORK_ROOT", warnings[0])
        self.assertEqual([], workroot.start_warnings(long, job_id, "windows", long_paths_enabled=lambda: True))
        self.assertEqual([], workroot.start_warnings(long, job_id, "windows", long_paths_enabled=lambda: None))
        self.assertEqual([], workroot.start_warnings(long, job_id, "macos", long_paths_enabled=lambda: False))

    def test_an_android_build_cannot_fit_whatever_the_root_while_long_paths_are_off(self):
        short = Path("C:/w")
        warning = workroot.path_length_warning(short, "windows", False, android=True)
        self.assertIn(str(workroot.ANDROID_DEEPEST_PROJECT_PATH), warning)
        self.assertIn("LongPathsEnabled", warning)
        self.assertGreater(workroot.JOB_FOLDER_RESERVE + workroot.ANDROID_DEEPEST_PROJECT_PATH,
                           workroot.WINDOWS_MAX_PATH - 1 - 3)
        self.assertIsNone(workroot.path_length_warning(short, "windows", True, android=True))
        self.assertIsNone(workroot.path_length_warning(short, "windows", None, android=True))
        self.assertIsNone(workroot.path_length_warning(short, "macos", False, android=True))
        self.assertEqual(1, len(workroot.start_warnings(short, "20261031T182754Z-a-0123abcd", "windows", True, lambda: False)))

    def test_other_systems_never_get_a_length_warning(self):
        long = Path("/" + "a" * 200)
        self.assertIsNone(workroot.path_length_warning(long, "macos", False))
        self.assertIsNone(workroot.path_length_warning(long, "linux", False))

    def test_the_extended_length_prefix_is_not_counted(self):
        path = Path("//?/C:/" + "a" * (self.LIMIT - 3))
        self.assertIsNone(workroot.path_length_warning(path, "windows", False))

    def test_registry_value_one_zero_and_absent(self):
        on, off = lambda: True, lambda: False  # noqa: E731
        self.assertIs(True, workroot.windows_long_paths_enabled(lambda: 1, on))
        self.assertIs(False, workroot.windows_long_paths_enabled(lambda: 0, on))
        self.assertIs(False, workroot.windows_long_paths_enabled(lambda: None, on), "an absent value is the Windows default: off")
        self.assertIsNone(workroot.windows_long_paths_enabled(lambda: 1, off), "not a Windows host")

    def test_an_unreadable_registry_is_silent(self):
        def denied():
            raise PermissionError("denied")

        def missing_module():
            raise ImportError("winreg")

        self.assertIsNone(workroot.windows_long_paths_enabled(denied, lambda: True))
        self.assertIsNone(workroot.windows_long_paths_enabled(missing_module, lambda: True))

    def test_doctor_lists_the_length_warning_and_one_combined_virtualization_warning(self):
        long = Path("C:/" + "a" * self.LIMIT)
        self.assertEqual(1, len(doctor.work_root_length_warnings("windows", long, False)))
        self.assertTrue(doctor.work_root_length_warnings("windows", long, False)[0].startswith("work root "))
        self.assertEqual([], doctor.work_root_length_warnings("windows", long, True))
        self.assertEqual([], doctor.work_root_length_warnings("windows", long, False, virtualized=True))
        virtual = Path("C:/Users/someone/AppData/Local/Packages/Vendor.App_abc123/LocalCache/Local/Valtrion/work")
        warnings = doctor.protected_path_warnings("windows", {"work root": virtual}, self.HOME, {}, False)
        self.assertEqual(1, len(warnings))
        self.assertIn("GDV_WORK_ROOT", warnings[0])
        self.assertIn("LocalCache", warnings[0])

    def test_probe_local_wires_the_windows_work_root_warnings_in(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        home = Path(folder.name) / "home"
        home.mkdir()
        environ = {doctor.HUB_PATHS_ENV: str(home / "none")}

        def report(work_root, long_paths, packaged):
            with mock.patch.dict(os.environ, {core.HOME_ENV: str(home), "LOCALAPPDATA": "C:/Users/someone/AppData/Local"}), \
                    mock.patch.object(workroot, "windows_long_paths_enabled", return_value=long_paths), \
                    mock.patch.object(workroot, "packaged_interpreter", return_value=packaged):
                return [w for w in doctor.probe_local("windows", lambda argv: (1, "x"), home / "art", environ, work_root=work_root)["warnings"]
                        if w.startswith("work root ")]

        long = Path("C:/" + "a" * self.LIMIT)
        (length,) = report(long, False, False)
        self.assertIn("long paths are off", length)
        self.assertEqual([], report(long, True, False))
        (virtual,) = report(Path("C:/Users/someone/AppData/Local/Valtrion/work"), False, True)
        self.assertIn("packaged app", virtual)
        (combined,) = report(Path("C:/Users/someone/AppData/Local/" + "a" * self.LIMIT), False, True)
        self.assertIn("packaged app", combined, "a virtualized and long root gets one warning")


class MeasuredPathTests(unittest.TestCase):
    def test_longest_path_counts_the_whole_path_of_the_deepest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a" / "bb").mkdir(parents=True)
            (base / "a" / "bb" / "file.txt").write_text("x", encoding="utf-8")
            (base / "short.txt").write_text("x", encoding="utf-8")
            expected = len(str(base)) + len("\\a\\bb\\file.txt") if os.name == "nt" else len(str(base)) + len("/a/bb/file.txt")
            self.assertEqual(expected, workroot.longest_path(base))
            self.assertEqual(0, workroot.longest_path(base / "missing"))

    def test_warning_names_the_exact_overflow_only_with_long_paths_off_on_windows(self):
        warning = workroot.measured_path_warning(270, "windows", False)
        self.assertIn("270 characters", warning)
        self.assertIn("11 characters too many", warning)
        self.assertIn("LongPathsEnabled", warning)
        self.assertIsNone(workroot.measured_path_warning(259, "windows", False))
        self.assertIsNone(workroot.measured_path_warning(270, "windows", True))
        self.assertIsNone(workroot.measured_path_warning(270, "windows", None))
        self.assertIsNone(workroot.measured_path_warning(270, "macos", False))

    def test_the_tree_is_not_walked_unless_it_can_matter(self):
        def forbidden(_):
            raise AssertionError("walked")
        self.assertEqual([], workroot.checkout_warnings(Path("C:/x"), "windows", lambda: True, forbidden))
        self.assertEqual([], workroot.checkout_warnings(Path("C:/x"), "windows", lambda: None, forbidden))
        self.assertEqual([], workroot.checkout_warnings(Path("/x"), "macos", lambda: False, forbidden))
        self.assertEqual(1, len(workroot.checkout_warnings(Path("C:/x"), "windows", lambda: False, lambda _: 300)))
        self.assertEqual([], workroot.checkout_warnings(Path("C:/x"), "windows", lambda: False, lambda _: 200))


class JobDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.roots = workroot.WorkRoots(Path(self._folder.name) / "work")

    def test_layout_and_environment_name_every_directory(self):
        dirs = self.roots.job("20261029T1-ios-abc")
        dirs.create()
        self.assertEqual(self.roots.jobs / "20261029T1-ios-abc", dirs.base)
        for name in ("build", "export", "derived-data", "tmp"):
            self.assertTrue((dirs.base / name).is_dir(), name)
        self.assertFalse(dirs.checkout.exists(), "git creates the checkout directory")
        environment = dirs.environment(self.roots, Path("/runs/x"))
        self.assertEqual(
            {"GDV_WORK_ROOT", "GDV_JOB_DIR", "GDV_JOB_CHECKOUT", "GDV_JOB_BUILD", "GDV_JOB_EXPORT", "GDV_JOB_DERIVED_DATA",
             "GDV_JOB_TMP", "GDV_JOB_ID", "GDV_RUN_DIR"}, set(environment))
        self.assertEqual("20261029T1-ios-abc", environment["GDV_JOB_ID"])

    def test_a_job_id_cannot_leave_the_jobs_folder(self):
        self.assertEqual(self.roots.jobs, self.roots.job("../../etc").base.parent)
        self.assertEqual(self.roots.mirrors, self.roots.mirror("../../x").parent)


class StageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.roots = workroot.WorkRoots(self.base / "work")

    def test_staged_copy_is_content_addressed_and_idempotent(self):
        first = workroot.stage_runtime(self.roots, remote.CLI_ROOT)
        second = workroot.stage_runtime(self.roots, remote.CLI_ROOT)
        self.assertEqual(first, second)
        self.assertEqual(self.roots.tools, first.parent)
        self.assertTrue((first / "valtrion" / "cli.py").is_file())
        self.assertTrue((first / "valtrion" / "_schemas" / "plan.schema.json").is_file())
        self.assertEqual([first.name], [p.name for p in self.roots.tools.iterdir()], "no scratch copy is left behind")

    def test_a_changed_source_gets_a_new_directory(self):
        source = self.base / "cli"
        (source / "valtrion" / "_schemas").mkdir(parents=True)
        (source / "valtrion" / "__init__.py").write_text("a = 1\n", encoding="utf-8")
        first = workroot.stage_runtime(self.roots, source)
        (source / "valtrion" / "__init__.py").write_text("a = 2\n", encoding="utf-8")
        self.assertNotEqual(first, workroot.stage_runtime(self.roots, source))

    def test_the_staged_cli_runs_without_the_original_folder(self):
        staged = workroot.stage_runtime(self.roots, remote.CLI_ROOT)
        done = subprocess.run([PYTHON, "-I", "-c", remote.LAUNCHER, str(staged), "version"], capture_output=True, text=True, check=False)
        self.assertEqual(0, done.returncode, done.stderr)

    def test_macos_runner_argv_uses_the_staged_cli_and_the_interpreter_that_runs_valtrion(self):
        staged = workroot.stage_runtime(self.roots, remote.CLI_ROOT)
        python = jobs.stable_python("macos", lambda path, os_name: None)
        self.assertEqual(sys.executable, python, "never the system /usr/bin/python3, which may be older than valtrion supports")
        argv = jobs.runner_argv(Path("/runs/x"), staged, python)
        self.assertEqual([sys.executable, "-I", "-c"], argv[:3])
        self.assertEqual([str(staged), "job", "_run", str(Path("/runs/x"))], argv[4:])
        self.assertEqual(sys.executable, jobs.stable_python("windows", lambda path, os_name: "protected"))

    def test_macos_interpreter_inside_a_protected_folder_is_refused_with_the_action(self):
        with self.assertRaises(jobs.JobError) as caught:
            jobs.stable_python("macos", lambda path, os_name: "inside the Documents folder")
        self.assertIn("pipx", str(caught.exception))


class CheckoutTests(unittest.TestCase):
    """Real git against a local repository: the checkout must hang off the mirror, never off the source."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        base = Path(self._folder.name)
        self.source = base / "source"
        self.source.mkdir()
        git(["init", "-q", "-b", "main"], self.source)
        git(["config", "user.email", "t@example.com"], self.source)
        git(["config", "user.name", "T"], self.source)
        (self.source / "file.txt").write_text("one\n", encoding="utf-8")
        git(["add", "."], self.source)
        git(["commit", "-q", "-m", "one"], self.source)
        self.sha = git(["rev-parse", "HEAD"], self.source)
        self.roots = workroot.WorkRoots(base / "work")

    def test_checkout_comes_from_the_mirror_and_is_removed_with_its_registration(self):
        path = workroot.add_checkout(self.roots, "proj", str(self.source), self.sha, "job-1")
        self.assertEqual("one\n", (path / "file.txt").read_text(encoding="utf-8"))
        pointer = (path / ".git").read_text(encoding="utf-8")
        self.assertTrue(pointer.startswith("gitdir: "))
        git_directory = Path(pointer.removeprefix("gitdir: ").strip()).resolve()
        # Git may expand a Windows short-name TEMP path; compare filesystem locations.
        self.assertTrue(git_directory.is_relative_to(self.roots.mirrors.resolve()), "the worktree points into the work root")
        self.assertFalse(git_directory.is_relative_to(self.source.resolve()))
        removed = workroot.cleanup_job(self.roots, "job-1")
        self.assertEqual([str(self.roots.job("job-1").base)], removed)
        self.assertFalse(self.roots.job("job-1").base.exists())
        self.assertNotIn("job-1", git(["worktree", "list"], self.roots.mirror("proj")), "the registration is pruned")
        self.assertTrue(self.roots.mirror("proj").is_dir(), "the mirror stays as the cache of the next checkout")

    def test_second_checkout_reuses_the_mirror_without_fetching(self):
        calls = []

        def spy(argv, cwd=None):
            calls.append(list(argv))
            return workroot.run_git(argv, cwd)

        workroot.add_checkout(self.roots, "proj", str(self.source), self.sha, "job-1", spy)
        workroot.add_checkout(self.roots, "proj", str(self.source), self.sha, "job-2", spy)
        self.assertEqual(1, sum(1 for argv in calls if argv[0] == "fetch" and self.sha in argv))

    def test_the_checkout_sees_the_default_branch_as_origin_main(self):
        path = workroot.add_checkout(self.roots, "proj", str(self.source), self.sha, "job-1")
        self.assertEqual(self.sha, git(["rev-parse", "origin/main"], path))
        # the exact check `valtrion baseline promote` makes
        git(["merge-base", "--is-ancestor", self.sha, "origin/main"], path)

    def test_a_non_default_default_branch_is_recorded_under_its_own_name(self):
        git(["branch", "-m", "trunk"], self.source)
        path = workroot.add_checkout(self.roots, "proj", str(self.source), self.sha, "job-1")
        self.assertEqual(self.sha, git(["rev-parse", "origin/trunk"], path))

    def test_a_commit_off_the_default_branch_is_not_an_ancestor_of_origin_main(self):
        git(["checkout", "-q", "-b", "feature"], self.source)
        (self.source / "file.txt").write_text("two\n", encoding="utf-8")
        git(["commit", "-q", "-am", "two"], self.source)
        feature = git(["rev-parse", "HEAD"], self.source)
        git(["checkout", "-q", "main"], self.source)
        path = workroot.add_checkout(self.roots, "proj", str(self.source), feature, "job-1")
        done = subprocess.run(["git", "merge-base", "--is-ancestor", feature, "origin/main"], cwd=path, check=False)
        self.assertEqual(1, done.returncode)

    def test_a_later_checkout_refreshes_origin_main_when_the_new_commit_is_on_it(self):
        workroot.add_checkout(self.roots, "proj", str(self.source), self.sha, "job-1")
        (self.source / "file.txt").write_text("two\n", encoding="utf-8")
        git(["commit", "-q", "-am", "two"], self.source)
        newer = git(["rev-parse", "HEAD"], self.source)
        path = workroot.add_checkout(self.roots, "proj", str(self.source), newer, "job-2")
        self.assertEqual(newer, git(["rev-parse", "origin/main"], path))

    def test_an_unreadable_default_branch_does_not_fail_the_checkout(self):
        def fake(argv, cwd=None):
            if argv[:1] == ["ls-remote"]:
                return 128, "fatal: unreachable"
            return workroot.run_git(argv, cwd)

        path = workroot.add_checkout(self.roots, "proj", str(self.source), self.sha, "job-1", fake)
        self.assertTrue((path / "file.txt").is_file())

    def test_a_short_or_unknown_commit_is_refused_before_git_runs(self):
        with self.assertRaises(workroot.WorkRootError):
            workroot.add_checkout(self.roots, "proj", str(self.source), self.sha[:8], "job-1")
        with self.assertRaises(workroot.WorkRootError):
            workroot.add_checkout(self.roots, "proj", str(self.source), "f" * 40, "job-2")


class LfsCheckoutTests(unittest.TestCase):
    def test_lfs_files_are_fetched_after_the_checkout_with_the_smudge_filter_switched_off(self):
        calls = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            roots = workroot.WorkRoots(Path(folder) / "work")
            source = Path(folder) / "src"
            source.mkdir()

            def fake(argv, cwd=None):
                calls.append(list(argv))
                if argv[:1] == ["worktree"] or argv[:6] == ["-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false"]:
                    checkout = Path(argv[-2])
                    checkout.mkdir(parents=True)
                if argv[:2] == ["lfs", "ls-files"]:  # rules in a nested .gitattributes are found too: no root file exists here
                    return 0, "Project/Assets/big.bin"
                return 0, ""

            workroot.add_checkout(roots, "proj", str(source), "a" * 40, "job-1", fake)
        add = next(argv for argv in calls if "worktree" in argv and "add" in argv)
        self.assertEqual(["-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false"], add[:6])
        self.assertIn(["lfs", "fetch", str(source), "a" * 40], calls)
        self.assertLess(calls.index(["lfs", "fetch", str(source), "a" * 40]), calls.index(["lfs", "checkout"]))


    def test_no_lfs_files_or_no_git_lfs_means_no_lfs_commands(self):
        for listing in ((0, ""), (1, "git: 'lfs' is not a git command")):
            calls = []
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                roots = workroot.WorkRoots(Path(folder) / "work")

                def fake(argv, cwd=None, listing=listing):
                    calls.append(list(argv))
                    if "worktree" in argv:
                        Path(argv[-2]).mkdir(parents=True)
                    if argv[:1] == ["grep"]:
                        return 1, ""  # no tracked .gitattributes mentions LFS
                    return listing if argv[:2] == ["lfs", "ls-files"] else (0, "")

                workroot.add_checkout(roots, "proj", "url", "a" * 40, "job-1", fake)
            self.assertFalse([argv for argv in calls if argv[:1] == ["lfs"] and argv[1] != "ls-files"], listing)

    def test_an_lfs_tree_on_a_host_without_git_lfs_is_refused_not_left_as_pointer_files(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            roots = workroot.WorkRoots(Path(folder) / "work")

            def fake(argv, cwd=None):
                if "worktree" in argv:
                    Path(argv[-2]).mkdir(parents=True)
                if argv[:2] == ["lfs", "ls-files"]:
                    return 1, "git: 'lfs' is not a git command"
                return 0, "Project/.gitattributes" if argv[:1] == ["grep"] else ""

            with self.assertRaises(workroot.WorkRootError):
                workroot.add_checkout(roots, "proj", "url", "a" * 40, "job-1", fake)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.roots = workroot.WorkRoots(self.base / "work")

    def test_cleanup_removes_everything_of_the_job_and_nothing_else(self):
        mine, other = self.roots.job("mine"), self.roots.job("other")
        for dirs in (mine, other):
            dirs.create()
            (dirs.build / "player.bin").write_bytes(b"x")
        (self.roots.mirrors).mkdir(parents=True)
        self.assertEqual([str(mine.base)], workroot.cleanup_job(self.roots, "mine", lambda a, c=None: (0, "")))
        self.assertFalse(mine.base.exists())
        self.assertTrue((other.build / "player.bin").is_file())
        self.assertTrue(self.roots.mirrors.is_dir())
        self.assertEqual([], workroot.cleanup_job(self.roots, "mine", lambda a, c=None: (0, "")), "idempotent")

    def test_sweep_keeps_active_and_recent_job_directories(self):
        for name in ("dead", "active", "recent"):
            self.roots.job(name).create()
        old = time.time() - 7 * 3600
        for name in ("dead", "active"):
            for path in [self.roots.job(name).base, *self.roots.job(name).base.iterdir()]:
                os.utime(path, (old, old))
        removed = workroot.sweep(self.roots, ["active"], run=lambda a, c=None: (0, ""))
        self.assertEqual([str(self.roots.job("dead").base)], removed)
        self.assertTrue(self.roots.job("active").base.exists())
        self.assertTrue(self.roots.job("recent").base.exists())

    def test_sweep_removes_abandoned_launchd_files_but_not_an_active_jobs(self):
        self.roots.launchd.mkdir(parents=True)
        old = time.time() - 7 * 3600
        names = ["com.valtrion.dead-job.plist", "com.valtrion.dead-job.log", "com.valtrion.live-job.plist", "other.plist"]
        for name in names:
            path = self.roots.launchd / name
            path.write_text("x", encoding="utf-8")
            os.utime(path, (old, old))
        removed = workroot.sweep(self.roots, ["live-job"], run=lambda a, c=None: (0, ""))
        self.assertEqual(sorted(str(self.roots.launchd / n) for n in names[:2]), sorted(removed))
        self.assertEqual({"com.valtrion.live-job.plist", "other.plist"}, {p.name for p in self.roots.launchd.iterdir()})

    def test_sweep_prunes_unused_staged_cli_copies_only_when_no_job_is_active(self):
        old = time.time() - 7 * 3600
        for name in ("valtrion-aaaa", ".stage-1-2", "keep-me"):
            folder = self.roots.tools / name
            folder.mkdir(parents=True)
            (folder / "f").write_text("x", encoding="utf-8")
            for path in (folder / "f", folder):
                os.utime(path, (old, old))
        self.assertEqual([], workroot.sweep(self.roots, ["busy"], run=lambda a, c=None: (0, "")))
        removed = workroot.sweep(self.roots, [], run=lambda a, c=None: (0, ""))
        self.assertEqual(sorted(str(self.roots.tools / n) for n in ("valtrion-aaaa", ".stage-1-2")), sorted(removed))
        self.assertTrue((self.roots.tools / "keep-me").exists())

    def test_sweep_dry_run_removes_nothing(self):
        dirs = self.roots.job("dead")
        dirs.create()
        old = time.time() - 7 * 3600
        for path in [dirs.base, *dirs.base.iterdir()]:
            os.utime(path, (old, old))
        self.assertEqual([str(dirs.base)], workroot.sweep(self.roots, [], dry_run=True))
        self.assertTrue(dirs.base.exists())
        self.assertEqual([dirs.base.name], [p.name for p in workroot.stale_job_directories(self.roots)])


class JobIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.home = self.base / "home"
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.roots = workroot.WorkRoots(self.base / "work")

    def request(self, argv):
        return jobs.JobRequest(
            kind="build", project="proj", sha="b" * 40, label="t", payload={"type": "command", "argv": argv, "cwd": None},
            host={"id": "test-host", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=60, poll_sec=0.05,
            tick_sec=0.5, owner="tester")

    def test_a_command_job_gets_its_directories_and_they_are_gone_when_it_ends(self):
        code = ("import os, json, pathlib\n"
                "build = pathlib.Path(os.environ['GDV_JOB_BUILD'])\n"
                "(build / 'out.bin').write_bytes(b'x')\n"
                "(pathlib.Path(os.environ['GDV_RUN_DIR']) / 'diag.txt').write_text('kept')\n"
                "print(json.dumps({k: v for k, v in os.environ.items() if k.startswith('GDV_JOB')}))\n")
        info = jobs.start_job(self.request([PYTHON, "-c", code]), self.base / "art", self.home, foreground=True, work_roots=self.roots)
        run_folder = Path(info["dir"])
        job_id = info["jobId"]
        self.assertEqual(0, info["exitCode"])
        self.assertEqual("kept", (run_folder / "diag.txt").read_text(encoding="utf-8"), "diagnostics are written to the run folder")
        printed = json.loads((run_folder / "console.log").read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(str(self.roots.job(job_id).build), printed["GDV_JOB_BUILD"])
        self.assertFalse(self.roots.job(job_id).base.exists(), "the job's work directories are removed")
        events = (run_folder / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("work cleanup", events)

    def test_adapter_build_logs_in_the_job_directory_are_copied_to_the_run_folder_before_cleanup(self):
        code = ("import os, pathlib\n"
                "for name in ('logs', 'build-logs'):\n"
                "    logs = pathlib.Path(os.environ['GDV_JOB_DIR']) / name\n"
                "    logs.mkdir()\n"
                "    (logs / 'unity-build.log').write_text('compile error ' + name, encoding='utf-8')\n")
        info = jobs.start_job(self.request([PYTHON, "-c", code]), self.base / "art", self.home, foreground=True, work_roots=self.roots)
        for name in ("logs", "build-logs"):
            self.assertEqual("compile error " + name, (Path(info["dir"]) / name / "unity-build.log").read_text(encoding="utf-8"))
        self.assertFalse(self.roots.job(info["jobId"]).base.exists())

    def test_a_failed_job_is_cleaned_too_after_its_results_are_saved(self):
        info = jobs.start_job(self.request([PYTHON, "-c", "import os, sys; open(os.path.join(os.environ['GDV_JOB_BUILD'], 'x'), 'w').close(); sys.exit(1)"]),
                              self.base / "art", self.home, foreground=True, work_roots=self.roots)
        run_folder = Path(info["dir"])
        job = json.loads((run_folder / "job.json").read_text(encoding="utf-8"))
        self.assertEqual("FAIL", job["status"])
        self.assertFalse(self.roots.job(info["jobId"]).base.exists())
        self.assertTrue((run_folder / "console.log").is_file() and (run_folder / "events.jsonl").is_file())

    def test_protected_and_retained_runs_keep_their_work_directories(self):
        request = self.request([PYTHON, "-c", "import os; open(os.path.join(os.environ['GDV_JOB_EXPORT'], 'x'), 'w').close()"])
        layout, spec = jobs.create_job(request, self.base / "art", self.home, self.roots)
        spec["protectedPaths"] = [str(self.roots.job(spec["jobId"]).export)]
        core.atomic_write_json(layout.spec, spec)
        jobs.run_job(layout.directory, self.home)
        self.assertTrue((self.roots.job(spec["jobId"]).export / "x").exists(), "a protected path keeps the job directory")
        self.assertIn("kept", (layout.directory / "events.jsonl").read_text(encoding="utf-8"))
        layout2, spec2 = jobs.create_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, self.roots)
        (layout2.directory / ".retained").write_text("x", encoding="utf-8")
        jobs.run_job(layout2.directory, self.home)
        self.assertTrue(self.roots.job(spec2["jobId"]).base.exists(), "a retained run keeps its work directories")

    def test_a_live_runner_older_than_the_startup_grace_is_active_and_the_sweep_keeps_retained_work(self):
        layout, spec = jobs.create_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, self.roots)
        job = core.read_json(layout.job_json)
        job.update({"state": "running", "pid": os.getpid(), "updatedUtc": core.format_utc(core.now() - timedelta(hours=3))})
        core.atomic_write_json(layout.job_json, job)
        core.atomic_write_json(layout.directory / "runner.json", {"pid": os.getpid(), "token": core.process_start_token(os.getpid())})
        self.assertEqual([spec["jobId"]], jobs.active_job_ids(self.home))
        job["state"] = "finished"
        core.atomic_write_json(layout.job_json, job)
        self.assertEqual([], jobs.active_job_ids(self.home))
        (layout.directory / ".retained").write_text("x", encoding="utf-8")
        self.assertIn("retained", jobs.kept_work_reason(spec["jobId"], self.home))
        old = time.time() - 7 * 3600
        for path in [self.roots.job(spec["jobId"]).base, *self.roots.job(spec["jobId"]).base.iterdir()]:
            os.utime(path, (old, old))
        removed = workroot.sweep(self.roots, [], run=lambda a, c=None: (0, ""), keep=lambda job_id: jobs.kept_work_reason(job_id, self.home))
        self.assertEqual([], removed)
        self.assertTrue(self.roots.job(spec["jobId"]).base.exists())

    def test_the_sweep_keeps_the_inbox_of_a_running_job_and_removes_an_idle_one(self):
        request = self.request([PYTHON, "-c", "pass"])
        request.payload["inbox"] = "inbox-used"
        layout, spec = jobs.create_job(request, self.base / "art", self.home, self.roots)
        job = core.read_json(layout.job_json)
        job.update({"state": "running", "pid": os.getpid(), "updatedUtc": core.format_utc(core.now() - timedelta(hours=3))})
        core.atomic_write_json(layout.job_json, job)
        core.atomic_write_json(layout.directory / "runner.json", {"pid": os.getpid(), "token": core.process_start_token(os.getpid())})
        old = time.time() - 7 * 3600
        for name in ("inbox-used", "inbox-idle"):
            folder = self.roots.jobs / name
            folder.mkdir(parents=True)
            (folder / "plan.json").write_text("{}", encoding="utf-8")
            os.utime(folder / "plan.json", (old, old))
            os.utime(folder, (old, old))
        removed = workroot.sweep(self.roots, jobs.active_job_ids(self.home), run=lambda a, c=None: (0, ""),
                                 keep=lambda name: jobs.kept_work_reason(name, self.home))
        self.assertEqual([str(self.roots.jobs / "inbox-idle")], removed)
        self.assertTrue((self.roots.jobs / "inbox-used").is_dir())

    def test_cleaning_up_a_job_removes_the_inbox_it_was_started_with(self):
        request = self.request([PYTHON, "-c", "pass"])
        inbox = self.roots.inbox()
        request.payload["inbox"] = inbox.name
        info = jobs.start_job(request, self.base / "art", self.home, foreground=True, work_roots=self.roots)
        self.assertFalse(inbox.exists(), info)
        (self.roots.jobs / "inbox-notmine").mkdir()
        (self.roots.jobs / "other-job").mkdir()
        for bad in ("../escape", "inbox-notmine", "other-job", "inbox-0123456789abcdef/../x", "inbox-"):
            with self.assertRaises(workroot.WorkRootError, msg=bad):
                workroot.cleanup_inbox(self.roots, bad)
        self.assertTrue((self.roots.jobs / "inbox-notmine").is_dir() and (self.roots.jobs / "other-job").is_dir())

    def test_a_job_whose_runner_died_is_not_active_but_a_starting_one_is(self):
        layout, spec = jobs.create_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, self.roots)
        job = core.read_json(layout.job_json)
        job["state"], job["pid"] = "running", 2 ** 22 + 12345  # a pid that is not alive
        job["updatedUtc"] = core.format_utc(core.now() - timedelta(minutes=10))
        core.atomic_write_json(layout.job_json, job)
        self.assertEqual([], jobs.active_job_ids(self.home), "running on paper, runner gone, past the startup grace")
        job["updatedUtc"] = core.utc_now()
        core.atomic_write_json(layout.job_json, job)
        self.assertEqual([spec["jobId"]], jobs.active_job_ids(self.home), "created moments ago: still starting")

    def test_cancel_of_a_dead_runner_keeps_retained_work_and_removes_the_rest(self):
        for retained in (True, False):
            layout, spec = jobs.create_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, self.roots)
            if retained:
                (layout.directory / ".retained").write_text("x", encoding="utf-8")
            store = jobs.JobStore(layout.directory)
            jobs._close_orphaned(store, store.read_job())
            self.assertEqual(retained, self.roots.job(spec["jobId"]).base.exists(), retained)

    def test_cancel_of_a_dead_runner_also_saves_adapter_logs(self):
        layout, spec = jobs.create_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, self.roots)
        logs = self.roots.job(spec["jobId"]).base / "logs"
        logs.mkdir()
        (logs / "unity-build.log").write_text("license error", encoding="utf-8")
        store = jobs.JobStore(layout.directory)
        jobs._close_orphaned(store, store.read_job())
        self.assertEqual("license error", (layout.directory / "logs" / "unity-build.log").read_text(encoding="utf-8"))
        self.assertFalse(self.roots.job(spec["jobId"]).base.exists())

    def test_a_pipeline_job_keeps_its_work_until_its_result_is_saved(self):
        request = self.request([PYTHON, "-c", "pass"])
        request.payload = {"type": "pipeline", "file": "p.json"}
        layout, spec = jobs.create_job(request, self.base / "art", self.home, self.roots)
        self.assertIn("result.json", jobs.work_cleanup_blocker(spec))
        core.atomic_write_json(layout.result, {})
        self.assertIsNone(jobs.work_cleanup_blocker(spec))

    def test_newest_mtime_tolerates_vanished_entries_and_plain_files(self):
        folder = self.base / "d"
        folder.mkdir()
        (folder / "a").write_text("x", encoding="utf-8")
        self.assertGreater(workroot.newest_mtime(folder), 0)
        self.assertGreater(workroot.newest_mtime(folder / "a"), 0)
        self.assertEqual(0.0, workroot.newest_mtime(folder / "missing"))

    def test_without_a_work_root_nothing_is_created(self):
        info = jobs.start_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, foreground=True)
        self.assertFalse((self.base / "work").exists())
        spec = json.loads((Path(info["dir"]) / "spec.json").read_text(encoding="utf-8"))
        self.assertNotIn("workRoot", spec)

    def test_active_job_ids_lists_only_running_jobs(self):
        layout, spec = jobs.create_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, self.roots)
        self.assertEqual([spec["jobId"]], jobs.active_job_ids(self.home))
        info = jobs.start_job(self.request([PYTHON, "-c", "pass"]), self.base / "art", self.home, foreground=True)
        self.assertNotIn(info["jobId"], jobs.active_job_ids(self.home))

    def test_macos_launch_keeps_plist_log_and_cwd_in_the_work_root(self):
        commands = []
        request = self.request([PYTHON, "-c", "pass"])
        layout, spec = jobs.create_job(request, self.base / "art", self.home, self.roots)
        jobs.launch_detached(layout.directory, "macos", "gui", spec, lambda command: (commands.append(list(command)) or (0, "")),
                             work_roots=self.roots)
        label = spec["launchd"]["label"]
        plist = self.roots.launchd / f"{label}.plist"
        self.assertEqual(["launchctl", "bootstrap", f"gui/{os.getuid() if hasattr(os, 'getuid') else 0}", str(plist)], commands[0])
        import plistlib
        document = plistlib.loads(plist.read_bytes())
        self.assertEqual(str(self.roots.launchd), document["WorkingDirectory"])
        self.assertEqual(str(self.roots.launchd / f"{label}.log"), document["StandardOutPath"])
        program = document["ProgramArguments"]
        self.assertIn(str(self.roots.tools), program[4], "the job runs the staged CLI, not the repository copy")
        self.assertEqual(str(layout.directory), program[-1])
        self.assertEqual([str(plist), str(self.roots.launchd / f"{label}.log")], spec["launchd"]["files"])

    def test_retiring_the_launchd_files_keeps_the_runner_log_with_the_run(self):
        self.roots.launchd.mkdir(parents=True)
        plist, log = self.roots.launchd / "a.plist", self.roots.launchd / "a.log"
        plist.write_bytes(b"x")
        log.write_text("runner output", encoding="utf-8")
        run = self.base / "art" / "proj" / ("b" * 8) / "job-1"
        run.mkdir(parents=True)
        spec = {"artifactsRoot": str(self.base / "art"), "project": "proj", "sha8": "b" * 8, "jobId": "job-1"}
        jobs._retire_launchd_files([str(plist), str(log)], spec)
        self.assertEqual("runner output", (run / "runner.log").read_text(encoding="utf-8"))
        self.assertFalse(plist.exists() or log.exists())


class ConfigAndDoctorTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.home = Path(self._folder.name) / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_work_root_follows_an_isolated_home(self):
        self.assertEqual(self.home / "work", config.load_host_settings(self.home).work_root)

    def test_host_json_sets_the_work_root(self):
        target = Path(self._folder.name) / "elsewhere"
        core.atomic_write_json(config.host_json_path(self.home), {"workRoot": str(target)})
        settings = config.load_host_settings(self.home)
        self.assertEqual(target, settings.work_root)
        self.assertEqual(target / "jobs", settings.roots.jobs)

    def test_environment_override_beats_host_json_and_must_be_absolute(self):
        core.atomic_write_json(config.host_json_path(self.home), {"workRoot": str(Path(self._folder.name) / "from-file")})
        target = Path(self._folder.name) / "from-env"
        with mock.patch.dict(os.environ, {workroot.WORK_ROOT_ENV: str(target)}):
            self.assertEqual(target, config.load_host_settings(self.home).work_root)
        with mock.patch.dict(os.environ, {workroot.WORK_ROOT_ENV: "relative"}):
            with self.assertRaises(config.ConfigError):
                config.load_host_settings(self.home)

    def test_a_relative_or_empty_work_root_is_rejected(self):
        for value in ("relative/dir", ""):
            core.atomic_write_json(config.host_json_path(self.home), {"workRoot": value})
            with self.assertRaises(config.ConfigError):
                config.load_host_settings(self.home)

    def test_doctor_warns_about_a_work_root_in_a_protected_folder(self):
        protected = Path("/Users/someone/Documents/gdv-work")
        home = Path("/Users/someone")
        warnings = doctor.protected_path_warnings(
            "macos", {"work root": protected, "artifacts root": Path("/Users/someone/.valtrion")}, home)
        self.assertEqual(1, len(warnings))
        self.assertIn("work root", warnings[0])
        self.assertIn("Documents", warnings[0])
        self.assertEqual([], doctor.protected_path_warnings("macos", {"work root": Path("/Users/someone/Library/Caches/x")}, home))

    def test_doctor_report_has_the_work_root_section_and_warnings(self):
        report = doctor.probe_local("windows", lambda argv: (1, "x"), self.home / "art", {doctor.HUB_PATHS_ENV: str(self.home / "none")},
                                    work_root=self.home / "w")
        self.assertEqual(str(self.home / "w"), report["workRoot"]["path"])
        self.assertEqual(0, report["workRoot"]["usage"]["jobDirectories"])
        self.assertIsInstance(report["warnings"], list)
        self.assertIn("work root:", doctor.render(report))


if __name__ == "__main__":
    unittest.main()
