"""Declared job outputs (`job start --cleanup`, run stage `cleanup`): what is removed, kept and refused when the job ends (#75)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, constants, core, jobs, lease, outputs
from valtrion.schema import Validator

PYTHON = sys.executable


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def _git(folder: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=folder, capture_output=True, check=True)


def _link_folder(link: Path, target: Path) -> bool:
    """A directory link: a junction on Windows (no privilege needed), a symlink elsewhere. False when the platform refuses."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, ImportError, AttributeError):
        return False
    return True


class OutputTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.tmp = Path(self._folder.name)
        self.base = self.tmp / "project"
        self.base.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("not ours", encoding="utf-8")
        self.home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_outputs(self):
        (self.base / "DerivedData" / "Build").mkdir(parents=True)
        (self.base / "DerivedData" / "Build" / "a.o").write_bytes(b"x" * 64)
        (self.base / "out").mkdir()
        (self.base / "out" / "tool").write_bytes(b"bin")
        (self.base / "helper.env").write_text("TEAM=example\n", encoding="utf-8")


class DeclarationTests(OutputTestCase):
    def test_absolute_parent_and_empty_paths_are_refused_with_an_explicit_error(self):
        for text in ("/etc/passwd", "C:\\Windows", "C:relative", "\\server\\share", "../escape", "out/../../escape", "a\\..\\..\\b", "",
                     ".", "./", ".git", "sub/.git/config", "~/x",
                     # names Windows resolves to another path: a stream, trailing dots and spaces, devices
                     "Assets::$INDEX_ALLOCATION", "out:stream", "ProjectSettings.", "ProjectSettings ", "out/sub.", "CON", "nul.txt",
                     "out/COM1", "lpt9.log", "Aux"):
            with self.subTest(text=text):
                with self.assertRaises(outputs.OutputError) as raised:
                    outputs.declare(self.base, [text])
                self.assertIn("cleanup path", str(raised.exception))

    def test_relative_paths_are_normalized_and_deduplicated(self):
        self.assertEqual(["out", "Derived/Data"], outputs.declare(self.base, ["./out", "out/", "Derived\\Data"]))

    def test_paths_need_a_working_directory(self):
        with self.assertRaises(outputs.OutputError):
            outputs.declare(None, ["out"])
        self.assertEqual([], outputs.declare(None, []))

    def test_a_link_that_escapes_the_working_directory_is_refused_at_declaration(self):
        if not _link_folder(self.base / "escape", self.outside):
            self.skipTest("this platform cannot create a directory link")
        with self.assertRaises(outputs.OutputError) as raised:
            outputs.declare(self.base, ["escape"])
        self.assertIn("outside the working directory", str(raised.exception))
        with self.assertRaises(outputs.OutputError):
            outputs.declare(self.base, ["escape/keep.txt"])  # through the link

    def test_a_working_directory_that_does_not_exist_yet_is_checked_by_form_only(self):
        self.assertEqual(["out"], outputs.declare(self.tmp / "not-yet", ["out"]))

    def test_cleanup_is_accepted_on_run_stages_only(self):
        self.assertEqual(["out", "x"], outputs.stage_paths([{"stage": "run", "cleanup": ["out"]}, {"stage": "collect"},
                                                           {"stage": "run", "cleanup": ["x"]}]))
        with self.assertRaises(outputs.OutputError) as raised:
            outputs.stage_paths([{"stage": "build", "cleanup": ["out"]}])
        self.assertIn("run stages only", str(raised.exception))


@unittest.skipUnless(_git_available(), "git is not installed")
class TrackedTests(OutputTestCase):
    def setUp(self):
        super().setUp()
        _git(self.base, "init", "-q")
        (self.base / "Tools").mkdir()
        (self.base / "Tools" / "build.sh").write_text("echo\n", encoding="utf-8")
        (self.base / "README.md").write_text("readme\n", encoding="utf-8")
        _git(self.base, "add", "Tools/build.sh", "README.md")

    def test_a_tracked_file_or_a_folder_with_tracked_files_is_refused_at_declaration(self):
        for text in ("README.md", "Tools", "Tools/build.sh", "readme.MD", "tools"):
            with self.subTest(text=text):
                with self.assertRaises(outputs.OutputError) as raised:
                    outputs.declare(self.base, [text])
                self.assertIn("tracked by git", str(raised.exception))
        self.assertEqual(["out"], outputs.declare(self.base, ["out"]))  # untracked (an output that does not exist yet)

    def assert_aliases_never_remove_tracked_files(self, declared, setup=None):
        (self.base / "Assets" / "Textures").mkdir(parents=True, exist_ok=True)
        (self.base / "Assets" / "Textures" / "t.png").write_text("x", encoding="utf-8")
        _git(self.base, "add", "Assets")
        if setup is not None and not setup():
            self.skipTest("this platform cannot create the link")
        with self.assertRaises(outputs.OutputError):
            outputs.declare(self.base, [declared])
        record = outputs.remove_declared(self.base, [declared]).to_document()
        self.assertEqual([], record["removed"], record)
        self.assertEqual(1, len(record["refused"]), record)
        for tracked in ("Tools/build.sh", "README.md", "Assets/Textures/t.png"):
            self.assertTrue((self.base / tracked).is_file(), tracked)

    def test_trailing_dots_spaces_streams_and_devices_never_reach_a_tracked_folder(self):
        for declared in ("Tools.", "Tools ", "Tools..", "Assets/Textures.", "CON", "aux.txt"):
            with self.subTest(declared=declared):
                self.assert_aliases_never_remove_tracked_files(declared)

    @unittest.skipUnless(os.name == "nt", "NTFS streams are a Windows name form")
    def test_an_ntfs_index_stream_never_reaches_a_tracked_folder(self):
        self.assert_aliases_never_remove_tracked_files("Assets::$INDEX_ALLOCATION")

    @unittest.skipUnless(os.name == "nt", "8.3 short names are a Windows name form")
    def test_a_short_8_3_name_never_reaches_a_tracked_folder(self):
        import ctypes
        folder = self.base / "LongTrackedFolderName"
        folder.mkdir()
        (folder / "f.txt").write_text("x", encoding="utf-8")
        _git(self.base, "add", "LongTrackedFolderName")
        buffer = ctypes.create_unicode_buffer(512)
        try:
            ctypes.windll.kernel32.GetShortPathNameW(str(folder), buffer, 512)
        except (AttributeError, OSError):
            self.skipTest("GetShortPathNameW is not available")
        short = Path(buffer.value).name
        if not short or short.casefold() == folder.name.casefold():
            self.skipTest("8.3 names are switched off on this volume")
        with self.assertRaises(outputs.OutputError) as raised:
            outputs.declare(self.base, [short])
        self.assertIn("different path on disk", str(raised.exception))
        record = outputs.remove_declared(self.base, [short]).to_document()
        self.assertEqual([], record["removed"])
        self.assertIn("different path on disk", record["refused"][0]["reason"])
        self.assertTrue((folder / "f.txt").is_file())

    def test_a_link_on_the_way_into_a_tracked_folder_is_refused(self):
        self.assert_aliases_never_remove_tracked_files(
            "jn/Textures", lambda: _link_folder(self.base / "jn", self.base / "Assets"))

    def test_a_symlink_on_the_way_into_a_tracked_folder_is_refused(self):
        def symlink():
            try:
                os.symlink(self.base / "Assets", self.base / "lnk", target_is_directory=True)
            except (OSError, NotImplementedError):
                return False
            return True
        self.assert_aliases_never_remove_tracked_files("lnk/Textures", symlink)

    def test_a_path_that_became_tracked_is_refused_when_the_job_ends_and_stays(self):
        self.make_outputs()
        _git(self.base, "add", "helper.env")
        record = outputs.remove_declared(self.base, ["out", "helper.env"]).to_document()
        self.assertEqual(["out"], record["removed"])
        self.assertEqual("helper.env", record["refused"][0]["path"])
        self.assertIn("tracked by git", record["refused"][0]["reason"])
        self.assertTrue((self.base / "helper.env").is_file())
        self.assertTrue((self.base / "README.md").is_file())

    def test_a_folder_holding_another_working_copy_is_refused(self):
        nested = self.base / "out" / "vendor"
        nested.mkdir(parents=True)
        _git(nested, "init", "-q")
        record = outputs.remove_declared(self.base, ["out"]).to_document()
        self.assertEqual([], record["removed"])
        self.assertIn("working copy", record["refused"][0]["reason"])
        self.assertTrue((nested / ".git").exists())


class RemovalTests(OutputTestCase):
    def test_declared_outputs_are_removed_and_a_missing_one_is_reported_absent(self):
        self.make_outputs()
        record = outputs.remove_declared(self.base, ["DerivedData", "out", "helper.env", "logs/missing.log"]).to_document()
        self.assertEqual(["DerivedData", "out", "helper.env"], record["removed"])
        self.assertEqual([{"path": "logs/missing.log", "reason": outputs.ABSENT}], record["kept"])
        self.assertEqual([], record["refused"])
        self.assertEqual(str(self.base), record["base"])
        for name in ("DerivedData", "out", "helper.env"):
            self.assertFalse((self.base / name).exists(), name)
        self.assertEqual([], Validator().validate(
            {"schema": "gdv.job/1", "jobId": "j1", "kind": "run", "state": "finished", "status": "PASS", "reason": None,
             "createdUtc": "2026-11-04T15:30:00Z", "updatedUtc": "2026-11-04T15:30:01Z", "host": "h", "command": ["x"],
             "watchdog": {"stallSec": 1, "timeoutSec": 1, "lastProgressUtc": "2026-11-04T15:30:01Z"}, "eventsOffset": 0,
             "cleanup": record}, "gdv.job/1"))

    def test_a_path_in_use_is_kept_with_the_reason(self):
        self.make_outputs()
        record = outputs.remove_declared(self.base, ["out", "DerivedData"],
                                         in_use=lambda path: "a build holds it" if path.name == "out" else None).to_document()
        self.assertEqual(["DerivedData"], record["removed"])
        self.assertEqual([{"path": "out", "reason": "in use: a build holds it"}], record["kept"])
        self.assertTrue((self.base / "out" / "tool").is_file())

    def test_a_keep_reason_keeps_every_existing_path(self):
        self.make_outputs()
        record = outputs.remove_declared(self.base, ["out"], keep_reason="the run is retained").to_document()
        self.assertEqual([{"path": "out", "reason": "the run is retained"}], record["kept"])
        self.assertTrue((self.base / "out").is_dir())

    def test_a_protected_path_is_kept(self):
        self.make_outputs()
        record = outputs.remove_declared(self.base, ["out"], protected=[self.base / "out" / "tool"]).to_document()
        self.assertEqual("out", record["kept"][0]["path"])
        self.assertIn("protected", record["kept"][0]["reason"])

    def test_a_link_that_escapes_is_refused_at_the_end_and_its_target_survives(self):
        if not _link_folder(self.base / "out", self.outside):
            self.skipTest("this platform cannot create a directory link")
        record = outputs.remove_declared(self.base, ["out", "out/keep.txt"]).to_document()
        self.assertEqual([], record["removed"])
        self.assertEqual(["out", "out/keep.txt"], [item["path"] for item in record["refused"]])
        self.assertIn("outside the working directory", record["refused"][0]["reason"])
        self.assertIn("is a link or junction", record["refused"][1]["reason"])
        self.assertTrue((self.outside / "keep.txt").is_file())

    def test_a_link_inside_a_removed_folder_is_unlinked_and_its_target_survives(self):
        (self.base / "out").mkdir()
        if not _link_folder(self.base / "out" / "cache", self.outside):
            self.skipTest("this platform cannot create a directory link")
        record = outputs.remove_declared(self.base, ["out"]).to_document()
        self.assertEqual(["out"], record["removed"])
        self.assertFalse((self.base / "out").exists())
        self.assertTrue((self.outside / "keep.txt").is_file(), "the link was followed")

    def test_a_link_inside_the_folder_that_points_inside_is_removed_as_a_link(self):
        (self.base / "real").mkdir()
        (self.base / "real" / "data.bin").write_bytes(b"1")
        if not _link_folder(self.base / "alias", self.base / "real"):
            self.skipTest("this platform cannot create a directory link")
        record = outputs.remove_declared(self.base, ["alias"]).to_document()
        self.assertEqual(["alias"], record["removed"])
        self.assertFalse(outputs.is_link(self.base / "alias") or (self.base / "alias").exists())
        self.assertTrue((self.base / "real" / "data.bin").is_file(), "only the link goes")

    def test_a_removal_failure_is_kept_not_reported_removed(self):
        self.make_outputs()
        with mock.patch.object(outputs.artifacts, "_remove", side_effect=PermissionError("in use by another process")):
            record = outputs.remove_declared(self.base, ["out"]).to_document()
        self.assertEqual([], record["removed"])
        self.assertIn("could not be removed", record["kept"][0]["reason"])


class JobOutputTests(OutputTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "artifacts"
        hostname = mock.patch.object(core, "local_host_id", return_value="this-machine")
        hostname.start()
        self.addCleanup(hostname.stop)

    def request(self, argv, paths, **options):
        values = dict(kind="build", project="proj", sha="a" * 40, label="helper",
                      payload={"type": "command", "argv": argv, "cwd": str(self.base)},
                      host={"id": "test-host", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=60, poll_sec=0.05,
                      tick_sec=0.5, owner="tester", cleanup_paths=tuple(paths), cleanup_base=str(self.base))
        values.update(options)
        return jobs.JobRequest(**values)

    def test_a_command_job_removes_its_declared_outputs_and_records_them_in_job_json(self):
        code = "import pathlib; p = pathlib.Path('out'); p.mkdir(); (p / 'tool').write_text('x'); pathlib.Path('DerivedData').mkdir()"
        info = jobs.start_job(self.request([PYTHON, "-c", code], ["out", "DerivedData", "never-written"]), self.root, self.home,
                              foreground=True)
        store = jobs.JobStore(Path(info["dir"]))
        job = store.read_job()
        self.assertEqual(("finished", "PASS"), (job["state"], job["status"]), job["reason"])
        self.assertEqual({"base": str(self.base), "removed": ["out", "DerivedData"],
                          "kept": [{"path": "never-written", "reason": outputs.ABSENT}], "refused": []}, job["cleanup"])
        self.assertFalse((self.base / "out").exists())
        self.assertFalse((self.base / "DerivedData").exists())
        self.assertEqual([], Validator().validate(job, "gdv.job/1"))
        events = [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]
        logged = [event for event in events if event.get("message", "").startswith("output cleanup")]
        self.assertEqual("output cleanup: 2 removed, 1 kept, 0 refused", logged[0]["message"])
        self.assertLess(events.index(logged[0]), [event["type"] for event in events].index("job.finished"))

    def test_a_failed_command_still_removes_its_outputs(self):
        code = "import pathlib, sys; pathlib.Path('out').mkdir(); sys.exit(1)"
        info = jobs.start_job(self.request([PYTHON, "-c", code], ["out"]), self.root, self.home, foreground=True)
        job = jobs.JobStore(Path(info["dir"])).read_job()
        self.assertEqual("FAIL", job["status"])
        self.assertEqual(["out"], job["cleanup"]["removed"])

    def test_a_job_without_declared_outputs_has_no_cleanup_record(self):
        info = jobs.start_job(self.request([PYTHON, "-c", "pass"], []), self.root, self.home, foreground=True)
        self.assertNotIn("cleanup", jobs.JobStore(Path(info["dir"])).read_job())

    def test_an_output_under_a_lease_held_by_someone_else_is_kept(self):
        self.make_outputs()
        manager = lease.LeaseManager(self.home)
        held = manager.acquire(lease.unity_project_resource(self.base), "someone", "other-session", 300, "a build", os.getpid())
        self.assertTrue(held.acquired)
        self.addCleanup(manager.release, lease.unity_project_resource(self.base), "other-session", os.getpid())
        spec = {"cleanupPaths": {"base": str(self.base), "paths": ["out"]}, "artifactsRoot": str(self.root), "project": "proj",
                "sha8": "aaaaaaaa", "protectedPaths": []}
        record = jobs.clean_declared_outputs(spec, "this-job", self.home)
        self.assertEqual([], record["removed"])
        self.assertTrue(record["kept"][0]["reason"].startswith("in use: the lease unity-project:"), record)
        self.assertTrue((self.base / "out").is_dir())

    def test_an_output_another_running_job_works_in_is_kept(self):
        self.make_outputs()
        layout, _ = jobs.create_job(self.request([PYTHON, "-c", "pass"], [], payload={"type": "command", "argv": ["x"],
                                                                                      "cwd": str(self.base / "out")}), self.root, self.home)
        job = jobs.JobStore(layout.directory).read_job()
        job["state"] = "running"
        core.atomic_write_json(layout.job_json, job)  # just created: active during the start-up grace
        spec = {"cleanupPaths": {"base": str(self.base), "paths": ["out", "DerivedData"]}, "artifactsRoot": str(self.root),
                "project": "proj", "sha8": "aaaaaaaa", "protectedPaths": []}
        record = jobs.clean_declared_outputs(spec, "this-job", self.home)
        self.assertEqual(["DerivedData"], record["removed"])
        self.assertIn(f"the running job {layout.directory.name} works in", record["kept"][0]["reason"])
        self.assertTrue((self.base / "out").is_dir())

    def test_an_output_inside_the_folder_another_running_job_works_in_is_kept(self):
        self.make_outputs()
        layout, _ = jobs.create_job(self.request([PYTHON, "-c", "pass"], [], payload={"type": "command", "argv": ["x"],
                                                                                      "cwd": str(self.base)}), self.root, self.home)
        job = jobs.JobStore(layout.directory).read_job()
        job["state"] = "running"
        core.atomic_write_json(layout.job_json, job)
        spec = {"cleanupPaths": {"base": str(self.base), "paths": ["out/tool"]}, "artifactsRoot": str(self.root),
                "project": "proj", "sha8": "aaaaaaaa", "protectedPaths": []}
        record = jobs.clean_declared_outputs(spec, "this-job", self.home)
        self.assertEqual([], record["removed"])
        self.assertIn(f"the running job {layout.directory.name} works in", record["kept"][0]["reason"])
        self.assertTrue((self.base / "out" / "tool").is_file())
        # The finishing job's own working directory does not count.
        self.assertEqual(["out/tool"], jobs.clean_declared_outputs(spec, layout.directory.name, self.home)["removed"])

    def test_a_retained_run_keeps_its_outputs(self):
        self.make_outputs()
        layout, spec = jobs.create_job(self.request([PYTHON, "-c", "pass"], ["out"]), self.root, self.home)
        (layout.directory / ".retained").write_text("kept\n", encoding="utf-8")
        record = jobs.clean_declared_outputs(spec, spec["jobId"], self.home)
        self.assertEqual([{"path": "out", "reason": "the run is retained"}], record["kept"])


class JobStartCliTests(OutputTestCase):
    def setUp(self):
        super().setUp()
        hostname = mock.patch.object(core, "local_host_id", return_value="this-machine")
        hostname.start()
        self.addCleanup(hostname.stop)

    def start(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["job", "start", "--timeout-sec", "30", "--foreground", *options])
        return code, out.getvalue(), err.getvalue()

    def test_a_refused_path_stops_the_job_before_it_starts(self):
        for options, expected in ((("--cwd", str(self.base), "--cleanup", "../outside"), "parent folder"),
                                  (("--cwd", str(self.base), "--cleanup", str(self.outside)), "not absolute"),
                                  (("--cleanup", "out"), "needs --cwd"),
                                  (("--cwd", str(self.tmp / "missing"), "--cleanup", "out"), "is not a folder")):
            with self.subTest(options=options):
                code, out, err = self.start(*options, "--", PYTHON, "-c", "pass")
                self.assertEqual(constants.EXIT_BLOCKED, code, out + err)
                self.assertIn(expected, out + err)
                self.assertFalse((self.home / "jobs").is_dir() and any((self.home / "jobs").iterdir()), "no job was created")
        self.assertTrue((self.outside / "keep.txt").is_file())

    @unittest.skipUnless(_git_available(), "git is not installed")
    def test_a_tracked_path_stops_the_job_before_it_starts(self):
        _git(self.base, "init", "-q")
        (self.base / "tracked.txt").write_text("t\n", encoding="utf-8")
        _git(self.base, "add", "tracked.txt")
        code, out, err = self.start("--cwd", str(self.base), "--cleanup", "tracked.txt", "--", PYTHON, "-c", "pass")
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("tracked by git", out + err)
        self.assertTrue((self.base / "tracked.txt").is_file())

    def test_declared_outputs_of_a_command_are_gone_after_the_job(self):
        code = "import pathlib; pathlib.Path('build-products').mkdir(); pathlib.Path('build-products/x').write_text('1')"
        exit_code, out, err = self.start("--cwd", str(self.base), "--cleanup", "build-products", "--", PYTHON, "-c", code)
        self.assertEqual(constants.EXIT_PASS, exit_code, out + err)
        info = json.loads(out[out.index("{"):out.rindex("}") + 1])
        job = jobs.JobStore(Path(info["dir"])).read_job()
        self.assertEqual(["build-products"], job["cleanup"]["removed"])
        self.assertFalse((self.base / "build-products").exists())


if __name__ == "__main__":
    unittest.main()
