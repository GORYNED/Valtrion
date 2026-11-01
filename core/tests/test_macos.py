import json
import os
import plistlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import constants, core, jobs, providers, supervise
from valtrion.adapter import DeviceInfo
from valtrion.providers import macos
from valtrion.schema import Validator

SHA = "d" * 40
METHOD = "Sample.Editor.ValidationBuild.BuildMacOs"
OWNER_ID = "com.Example.Sample-Project"
BUNDLE_ID = OWNER_ID + ".gdv"
EDITOR = {"version": "6000.6.7f1", "path": "/Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity",
          "modules": ["ios", "macos"], "adb": None}


def result_of(status=constants.PASS, output="", returncode=None, reason=None):
    if returncode is None:
        returncode = 0 if status == constants.PASS else 1
    return supervise.BoundedResult(status, reason, returncode, output)


def make_bundle(app: Path, bundle_id=BUNDLE_ID, executable=True, **_ignored):
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "Data").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Player", "CFBundleIdentifier": bundle_id}))
    if executable:
        binary = app / "Contents" / "MacOS" / "Player"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)


class FakeProcess:
    def __init__(self, code=None):
        self.pid = 4242
        self.code = code

    def poll(self):
        return self.code


class FakeTree(supervise.ProcessTree):
    def __init__(self):
        self.killed = False
        self.closed = False

    def cpu_seconds(self):
        return 0.0

    def alive(self):
        return not self.killed

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True


class Recorder:
    """A fake `run_bounded`: records every command and answers by tool name."""

    def __init__(self):
        self.calls = []
        self.answers = {}
        self.on_unity = None

    def __call__(self, argv, timeout_sec, stall_sec=None, **options):
        self.calls.append((list(argv), timeout_sec, options))
        name = Path(argv[0]).name
        if name == "Unity" and self.on_unity:
            return self.on_unity(argv)
        answer = self.answers.get(name)
        return answer(argv) if callable(answer) else (answer or result_of())

    def names(self):
        return [Path(c[0][0]).name for c in self.calls]


class MacOSTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True) if sys.version_info >= (3, 10) else tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        base = Path(self._folder.name)
        self.base, self.home, self.work = base, base / "home", base / "work"
        self.home.mkdir()
        self.project = self.work / "repo" / "tools" / "Sample Project"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.project / "Assets" / "Editor").mkdir(parents=True)
        profiles = self.project / "validation" / "device" / "profiles.json"
        profiles.parent.mkdir(parents=True)
        profiles.write_text(json.dumps({"schema": "gdv.profiles/1", "profiles": {"Validation": {"macos": {
            "executeMethod": METHOD, "timeoutSec": 1234, "stallSec": 99}}}}), encoding="utf-8")
        (self.work / "repo" / "tools" / "build").mkdir(parents=True)
        (self.work / "repo" / "tools" / "build" / "sign-macos-sample.py").write_text("", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home), core.HOME_ENV: str(base / "gdv")})
        environment.start()
        self.addCleanup(environment.stop)
        self.run_tool = Recorder()
        self.run_tool.answers["xattr"] = result_of(constants.FAIL, "", 1)  # no quarantine attribute
        self.tree = FakeTree()
        self.spawned = []
        self.device = DeviceInfo("macos:mac", "macos", "mac-apple-m4", "host", "Mac16,1", "27.2", ("desktop",))

    def adapter(self, **options):
        defaults = dict(run=self.run_tool, environ={}, editors=lambda: [EDITOR], spawn=self.fake_spawn, darwin=True, settle_sec=0.0,
                        sleep=lambda _: None, state_dir=self.base / "state")
        defaults.update(options)
        return macos.MacOSAdapter(**defaults)

    def fake_spawn(self, argv, cwd, env, log, grace=3.0):
        self.spawned.append(list(argv))
        return FakeProcess(), self.tree

    def built_app(self, **options):
        app = self.base / "build" / "proj-gdv.app"
        make_bundle(app, **options)
        return app

    def installed(self, adapter=None):
        adapter = adapter or self.adapter()
        app = self.built_app()
        self.assertTrue(adapter.install(self.device, app).ok)
        return adapter, app


class IdentityTests(unittest.TestCase):
    def test_identity_is_isolated_from_the_application(self):
        identity = macos.identity_for(Path("/work/Sample Project"))
        self.assertEqual(("Valtrion-Validation", "Sample-Project-gdv"), (identity.company, identity.product))
        self.assertEqual(Path.home() / "Library" / "Application Support" / BUNDLE_ID, macos.data_root_for(BUNDLE_ID))

    def test_relative_paths_cannot_leave_the_data_folder(self):
        root = Path(tempfile.gettempdir()) / "valtrion-root"
        self.assertIsNotNone(macos._relative_inside(root, "gdv/inbox/plan.json"))
        for bad in ("../x", "gdv/../../x", "/etc/passwd", "C:/x", "", "\\x"):
            self.assertIsNone(macos._relative_inside(root, bad), bad)

    def test_permission_text_is_recognised(self):
        self.assertIsNotNone(macos.permission_problem("x\nThis app has crashed because it attempted to access privacy-sensitive data "
                                                      "without a usage description.\n"))
        self.assertIsNotNone(macos.permission_problem("not authorized to use the camera"))
        self.assertIsNone(macos.permission_problem("[Subsystems] TCC database opened; NSCameraUsageDescription is set"),
                          "ordinary startup text is not a permission failure")
        self.assertIsNone(macos.permission_problem("everything is fine"))


class BuildTests(MacOSTestCase):
    def unity_builds(self, argv):
        app = Path(argv[argv.index("-buildOutput") + 1])
        make_bundle(app, product=argv[argv.index("-gdvProductName") + 1])
        return result_of()

    def test_builds_with_the_validation_identity_and_signs(self):
        self.run_tool.on_unity = self.unity_builds
        out = self.base / "run" / "build"
        outcome = self.adapter().build(self.project, "Validation", out)
        self.assertTrue(outcome.ok, outcome.reason)
        argv = self.run_tool.calls[0][0]
        self.assertEqual(EDITOR["path"], argv[0])
        for flag in ("-batchmode", "-quit"):
            self.assertIn(flag, argv)
        self.assertNotIn("-nographics", argv)
        self.assertEqual("StandaloneOSX", argv[argv.index("-buildTarget") + 1])
        self.assertEqual(METHOD, argv[argv.index("-executeMethod") + 1])
        self.assertEqual("Valtrion-Validation", argv[argv.index("-gdvCompanyName") + 1])
        self.assertEqual("Sample-Project-gdv", argv[argv.index("-gdvProductName") + 1])
        self.assertTrue(Path(argv[argv.index("-logFile") + 1]).is_relative_to(self.base / "run" / "logs"))
        self.assertEqual((out / "Sample-Project-gdv.app",), outcome.artifacts)
        self.assertEqual(3, len(self.run_tool.calls))
        self.assertEqual(("Unity", "codesign"), (self.run_tool.names()[0], self.run_tool.names()[2]))
        self.assertTrue(self.run_tool.calls[1][0][1].endswith("sign-macos-sample.py"))
        self.assertEqual({"layer.export": "PASS", "layer.sign": "PASS"}, {k: v for k, v in outcome.details.items() if k.startswith("layer.")})

    def test_the_build_leaves_the_project_settings_as_they_were(self):
        settings = self.project / "ProjectSettings" / "ProjectSettings.asset"
        settings.write_bytes(b"applicationIdentifier:\n    iPhone: x\n")

        def unity_saves_settings(argv):
            settings.write_bytes(b"applicationIdentifier:\n    Standalone: com.example\n    iPhone: x\n")
            return self.unity_builds(argv)

        self.run_tool.on_unity = unity_saves_settings
        self.assertTrue(self.adapter().build(self.project, "Validation", self.base / "run" / "build").ok)
        self.assertEqual(b"applicationIdentifier:\n    iPhone: x\n", settings.read_bytes())

    def test_a_license_failure_far_from_the_end_of_the_log_is_blocked(self):
        def no_license(argv):
            Path(argv[argv.index("-logFile") + 1]).write_text("No valid Unity Editor license found.\n" + "x" * 50000, encoding="utf-8")
            return result_of(constants.FAIL, returncode=1)

        self.run_tool.on_unity = no_license
        outcome = self.adapter().build(self.project, "Validation", self.base / "run" / "build")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("license", outcome.reason)

    def test_a_busy_editor_keeps_whatever_the_other_editor_saved(self):
        settings = self.project / "ProjectSettings" / "ProjectSettings.asset"
        settings.write_bytes(b"before\n")

        def busy(argv):
            settings.write_bytes(b"saved by the owner's Editor\n")
            Path(argv[argv.index("-logFile") + 1]).write_text("It looks like another Unity instance is running with this project open.\n")
            return result_of(constants.FAIL, returncode=1)

        self.run_tool.on_unity = busy
        self.assertEqual(constants.BLOCKED, self.adapter().build(self.project, "Validation", self.base / "run" / "build").status)
        self.assertEqual(b"saved by the owner's Editor\n", settings.read_bytes())

    def test_unity_gets_a_stall_window_and_cpu_progress(self):
        self.run_tool.on_unity = self.unity_builds
        self.adapter().build(self.project, "Validation", self.base / "run" / "build")
        _, timeout, options = self.run_tool.calls[0]
        self.assertEqual(1234, timeout, "the profile sets the build timeout")
        self.assertTrue(options["cpu_counts"])

    def test_missing_prerequisites_are_blocked_with_the_reason(self):
        out = self.base / "run" / "build"
        no_editor = self.adapter(editors=lambda: []).build(self.project, "Validation", out)
        self.assertEqual((constants.BLOCKED, True), (no_editor.status, "6000.6.7f1 is not installed" in no_editor.reason))
        no_module = self.adapter(editors=lambda: [{**EDITOR, "modules": ["ios"]}]).build(self.project, "Validation", out)
        self.assertEqual((constants.BLOCKED, True), (no_module.status, "macOS build support" in no_module.reason))
        (self.project / "validation" / "device" / "profiles.json").unlink()
        no_method = self.adapter().build(self.project, "Validation", out)
        self.assertEqual((constants.BLOCKED, True), (no_method.status, macos.BUILD_METHOD_ENV in no_method.reason))
        self.assertEqual([], self.run_tool.calls, "nothing is run when a prerequisite is missing")

    def test_build_method_can_come_from_the_environment_or_the_profile(self):
        self.assertEqual("A.B.C", macos.find_build_method(self.project, {macos.BUILD_METHOD_ENV: "A.B.C"}))
        self.assertEqual(METHOD, macos.find_build_method(self.project, {}))
        self.assertIsNone(macos.find_build_method(self.project, {}, "Other"), "a profile that is not declared has no method")

    def test_a_failed_unity_build_keeps_its_status_and_a_busy_project_is_blocked(self):
        self.run_tool.on_unity = lambda argv: result_of(constants.FAIL, returncode=1)
        failed = self.adapter().build(self.project, "Validation", self.base / "run" / "build")
        self.assertEqual((constants.FAIL, "FAIL"), (failed.status, failed.details["layer.export"]))
        self.run_tool.on_unity = lambda argv: result_of(constants.TIMEOUT, returncode=None, reason="no result")
        self.assertEqual(constants.TIMEOUT, self.adapter().build(self.project, "Validation", self.base / "run2" / "build").status)

        def busy(argv):
            Path(argv[argv.index("-logFile") + 1]).write_text("It looks like another Unity instance is running with this project open.\n")
            return result_of(constants.FAIL, returncode=1)

        self.run_tool.on_unity = busy
        outcome = self.adapter().build(self.project, "Validation", self.base / "run3" / "build")
        self.assertEqual(constants.BLOCKED, outcome.status)

    def test_success_without_a_bundle_is_a_failure(self):
        self.run_tool.on_unity = lambda argv: result_of()
        outcome = self.adapter().build(self.project, "Validation", self.base / "run" / "build")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertNotIn("codesign", self.run_tool.names())

    def test_signing_that_needs_the_keychain_is_a_permission_request(self):
        self.run_tool.on_unity = self.unity_builds
        self.run_tool.answers[Path(sys.executable).name] = result_of(constants.FAIL, "errSecInternalComponent", 1)
        outcome = self.adapter().build(self.project, "Validation", self.base / "run" / "build")
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)
        self.assertIn("--domain gui", outcome.reason)
        self.assertEqual("NEEDS_PERMISSION", outcome.details["layer.sign"])

    def test_a_bundle_that_fails_codesign_verification_is_not_passed_on(self):
        self.run_tool.on_unity = self.unity_builds
        self.run_tool.answers["codesign"] = result_of(constants.FAIL, "invalid signature", 1)
        outcome = self.adapter().build(self.project, "Validation", self.base / "run" / "build")
        self.assertEqual((constants.FAIL, "FAIL"), (outcome.status, outcome.details["layer.sign"]))

    def test_a_non_darwin_host_cannot_build(self):
        outcome = self.adapter(darwin=False).build(self.project, "Validation", self.base / "x")
        self.assertEqual(constants.BLOCKED, outcome.status)


class InstallAndFilesTests(MacOSTestCase):
    def test_install_registers_in_place_and_keeps_stale_data_aside_as_diagnostics(self):
        stale = macos.data_root_for(BUNDLE_ID)
        (stale / "gdv" / "out" / "old").mkdir(parents=True)
        adapter, app = self.installed()
        self.assertFalse(stale.exists(), "an earlier run's data does not leak into the next run")
        kept = [p for p in stale.parent.iterdir() if p.name.startswith(stale.name + ".retained-")]
        self.assertEqual(1, len(kept), "and it is not deleted by another run")
        self.assertTrue((kept[0] / "gdv" / "out" / "old").is_dir())
        self.assertEqual("INTERRUPTED", json.loads((kept[0] / "retained.json").read_text(encoding="utf-8"))["status"])
        record = json.loads((self.base / "state" / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual((str(app.resolve()), "Player", str(stale)), (record["app"], record["executable"], record["dataRoot"]))
        self.assertTrue(app.is_dir(), "the bundle stays where it was built; it is not copied")

    def test_install_refuses_a_bundle_that_is_not_a_validation_identity(self):
        outcome = self.adapter().install(self.device, self.built_app(bundle_id=OWNER_ID))
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertFalse((self.home / "Library" / "Application Support" / OWNER_ID).exists())

    def test_install_refuses_broken_bundles(self):
        app = self.base / "x.app"
        make_bundle(app, executable=False)
        self.assertEqual(constants.FAIL, self.adapter().install(self.device, app).status)
        self.run_tool.answers["codesign"] = result_of(constants.FAIL, "bad", 1)
        self.assertEqual(constants.FAIL, self.adapter().install(self.device, self.built_app()).status)

    def test_push_pull_roundtrip_stays_inside_the_data_folder(self):
        adapter, _ = self.installed()
        plan = self.base / "plan.json"
        plan.write_text('{"a": 1}', encoding="utf-8")
        self.assertTrue(adapter.push(self.device, plan, "gdv/inbox/plan.json").ok)
        self.assertEqual(constants.FAIL, adapter.push(self.device, plan, "../escape.json").status)
        root = Path(json.loads((self.base / "state" / "installed.json").read_text(encoding="utf-8"))["dataRoot"])
        self.assertTrue((root / "gdv" / "inbox" / "plan.json").is_file())
        pulled = self.base / "pulled"
        self.assertTrue(adapter.pull(self.device, "gdv/inbox", pulled).ok)
        self.assertEqual('{"a": 1}', (pulled / "plan.json").read_text(encoding="utf-8"))
        empty = self.base / "empty"
        self.assertTrue(adapter.pull(self.device, "gdv/out", empty).ok, "no output folder yet is empty, not an error")
        self.assertTrue(empty.is_dir())
        self.assertEqual(constants.FAIL, adapter.pull(self.device, "/etc", self.base / "no").status)

    def test_files_need_an_installed_build(self):
        self.assertEqual(constants.BLOCKED, self.adapter().push(self.device, self.base / "a", "x").status)
        self.assertEqual(constants.BLOCKED, self.adapter().pull(self.device, "x", self.base / "b").status)

    def test_cleanup_removes_only_the_validation_data(self):
        adapter, _ = self.installed()
        root = Path(json.loads((self.base / "state" / "installed.json").read_text(encoding="utf-8"))["dataRoot"])
        (root / "gdv").mkdir(parents=True)
        owner = self.home / "Library" / "Application Support" / OWNER_ID
        owner.mkdir(parents=True)
        self.assertTrue(adapter.cleanup_run_data(self.device).ok)
        self.assertFalse(root.exists())
        self.assertTrue(owner.exists(), "the owner's data is never touched")
        record = json.loads((self.base / "state" / "installed.json").read_text(encoding="utf-8"))
        record["dataRoot"] = str(self.home / "Library" / "Application Support" / OWNER_ID)
        (self.base / "state" / "installed.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(constants.BLOCKED, adapter.cleanup_run_data(self.device).status)


class LaunchTests(MacOSTestCase):
    def test_launches_the_binary_inside_the_bundle_with_plan_and_log(self):
        adapter, app = self.installed()
        plan = self.base / "plan.json"
        plan.write_text("{}", encoding="utf-8")
        adapter.push(self.device, plan, "gdv/inbox/plan.json")
        log = self.base / "run" / "console.log"
        outcome = adapter.launch(self.device, "proj-gdv", ["-screen-fullscreen", "0"], log)
        self.assertTrue(outcome.ok, outcome.reason)
        argv = self.spawned[0]
        self.assertEqual(str(app.resolve() / "Contents" / "MacOS" / "Player"), argv[0])
        self.assertEqual(str(Path(json.loads((self.base / "state" / "installed.json").read_text(encoding="utf-8"))["dataRoot"])
                             / "gdv" / "inbox" / "plan.json"), argv[argv.index("-gdv-plan") + 1])
        self.assertEqual(str(log), argv[argv.index("-logFile") + 1])
        self.assertNotIn("open", [Path(a).name for a in argv[:1]])
        running = json.loads((self.base / "state" / "running.json").read_text(encoding="utf-8"))
        self.assertEqual(4242, running["pid"])

    def test_unknown_application_is_blocked_and_quarantine_needs_the_owner(self):
        adapter, _ = self.installed()
        self.assertEqual(constants.BLOCKED, adapter.launch(self.device, "other", [], self.base / "c.log").status)
        self.run_tool.answers["xattr"] = result_of(constants.PASS, "0081;5f;Safari;", 0)
        outcome = adapter.launch(self.device, BUNDLE_ID, [], self.base / "c.log")
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)
        self.assertEqual([], self.spawned)
        self.assertNotIn("-d", [c[0][1] for c in self.run_tool.calls if Path(c[0][0]).name == "xattr"], "quarantine is never removed")

    def test_exit_within_the_settle_window(self):
        adapter, _ = self.installed()
        log = self.base / "run" / "console.log"

        def dies(text, code):
            def spawn(argv, cwd, env, sink, grace=3.0):
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(text, encoding="utf-8")
                return FakeProcess(code), FakeTree()
            return spawn

        adapter._spawn = dies("This app has crashed because it attempted to access privacy-sensitive data without a usage description.", 134)
        permission = adapter.launch(self.device, "proj-gdv", [], log)
        self.assertEqual(constants.NEEDS_PERMISSION, permission.status)
        self.assertIn("System Settings", permission.reason)
        log.unlink()
        adapter._spawn = dies("segfault", 139)
        crashed = adapter.launch(self.device, "proj-gdv", [], log)
        self.assertEqual(constants.FAIL, crashed.status)
        self.assertIn("139", crashed.reason)
        self.assertFalse((self.base / "state" / "running.json").exists(), "an exited process leaves no running record")

    def test_terminate_kills_the_tree_and_clears_the_record(self):
        adapter, _ = self.installed()
        adapter.launch(self.device, "proj-gdv", [], self.base / "run" / "console.log")
        self.assertTrue(adapter.terminate(self.device, "proj-gdv").ok)
        self.assertTrue(self.tree.killed)
        self.assertTrue(self.tree.closed)
        self.assertFalse((self.base / "state" / "running.json").exists())
        self.assertTrue(adapter.terminate(self.device, "proj-gdv").ok, "terminate is idempotent")

    def test_an_alive_process_that_cannot_be_identified_is_not_forgotten(self):
        adapter, _ = self.installed()
        core.atomic_write_json(self.base / "state" / "running.json", {"pid": os.getpid(), "pgid": os.getpid()})  # no start token
        outcome = adapter.terminate(self.device, "proj-gdv")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertTrue((self.base / "state" / "running.json").exists())

    @unittest.skipIf(os.name == "nt", "process groups are POSIX")
    def test_a_failed_process_listing_is_not_read_as_a_stopped_player(self):
        adapter, _ = self.installed()
        core.atomic_write_json(self.base / "state" / "running.json", {"pid": os.getpid(), "startToken": core.process_start_token(os.getpid())})
        with mock.patch.object(macos, "_group_members", return_value=None), mock.patch.object(macos.os, "killpg", create=True):
            outcome = adapter.terminate(self.device, "proj-gdv")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertTrue((self.base / "state" / "running.json").exists())

    def test_install_clears_the_preferences_left_by_an_earlier_run(self):
        prefs = self.home / "Library" / "Preferences" / f"unity.{macos.COMPANY}.proj-gdv.plist"
        prefs.parent.mkdir(parents=True)
        prefs.write_bytes(b"old")
        self.installed()
        self.assertFalse(prefs.exists())

    def test_cleanup_keeps_the_data_while_a_player_cannot_be_stopped(self):
        adapter, _ = self.installed()
        root = macos.data_root_for(BUNDLE_ID)
        (root / "gdv").mkdir(parents=True)
        core.atomic_write_json(self.base / "state" / "running.json", {"pid": os.getpid(), "pgid": os.getpid()})
        outcome = adapter.cleanup_run_data(self.device)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertTrue((root / "gdv").exists())

    def test_a_failed_run_keeps_its_data_with_a_manifest_instead_of_deleting_it(self):
        adapter, _ = self.installed()
        root = macos.data_root_for(BUNDLE_ID)
        (root / "gdv" / "out" / "run-1").mkdir(parents=True)
        (root / "gdv" / "out" / "run-1" / "result.json").write_text("{}", encoding="utf-8")
        outcome = adapter.retain_run_data(self.device, constants.STALL, "the heartbeat did not advance")
        self.assertTrue(outcome.ok, outcome.reason)
        kept = Path(outcome.details["retainedAt"])
        self.assertFalse(root.exists(), "the next run starts from an empty folder")
        self.assertTrue((kept / "gdv" / "out" / "run-1" / "result.json").is_file())
        manifest = json.loads((kept / "retained.json").read_text(encoding="utf-8"))
        self.assertEqual(("STALL", "the heartbeat did not advance"), (manifest["status"], manifest["reason"]))
        self.assertIn("processId", manifest["holder"])
        self.assertTrue(manifest["retainedUtc"])

    def test_a_failed_run_that_left_nothing_keeps_nothing(self):
        adapter, _ = self.installed()
        outcome = adapter.retain_run_data(self.device, constants.FAIL, "x")
        self.assertTrue(outcome.ok)
        self.assertNotIn("retainedAt", outcome.details)

    def test_retaining_never_touches_a_folder_that_is_not_a_validation_identity(self):
        adapter, _ = self.installed()
        record = json.loads((self.base / "state" / "installed.json").read_text(encoding="utf-8"))
        owner = self.home / "Library" / "Application Support" / OWNER_ID
        owner.mkdir(parents=True)
        record["dataRoot"] = str(owner)
        (self.base / "state" / "installed.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(constants.BLOCKED, adapter.retain_run_data(self.device, constants.FAIL, "x").status)
        self.assertTrue(owner.exists())

    def test_cleanup_also_clears_the_preferences_of_the_validation_identity(self):
        adapter, _ = self.installed()
        prefs = self.home / "Library" / "Preferences" / f"unity.{macos.COMPANY}.proj-gdv.plist"
        owner = self.home / "Library" / "Preferences" / "unity.Example.Sample-Project.plist"
        prefs.parent.mkdir(parents=True)
        prefs.write_bytes(b"x")
        owner.write_bytes(b"y")
        self.assertTrue(adapter.cleanup_run_data(self.device).ok)
        self.assertFalse(prefs.exists())
        self.assertTrue(owner.exists())

    def test_a_recorded_process_of_another_incarnation_is_not_killed(self):
        adapter, _ = self.installed()
        (self.base / "state").mkdir(exist_ok=True)
        core.atomic_write_json(self.base / "state" / "running.json", {"pid": os.getpid(), "pgid": os.getpid(), "startToken": "not-this-process"})
        with mock.patch.object(macos.os, "killpg", create=True) as killpg:
            self.assertTrue(adapter.terminate(self.device, "proj-gdv").ok)
            killpg.assert_not_called()
        self.assertFalse((self.base / "state" / "running.json").exists())


@unittest.skipIf(os.name == "nt", "process groups are POSIX")
class RealProcessGroupTests(MacOSTestCase):
    def test_terminate_kills_the_players_descendants(self):
        app = self.base / "build" / "proj-gdv.app"
        make_bundle(app)
        pid_file = self.base / "child.pid"
        script = f"#!/bin/sh\nsleep 300 &\necho $! > '{pid_file}'\nwait\n"
        (app / "Contents" / "MacOS" / "Player").write_text(script, encoding="utf-8")
        adapter = macos.MacOSAdapter(run=self.run_tool, environ={}, editors=lambda: [EDITOR], darwin=True, settle_sec=0.3,
                                     state_dir=self.base / "state")
        self.assertTrue(adapter.install(self.device, app).ok)
        outcome = adapter.launch(self.device, "proj-gdv", [], self.base / "run" / "console.log")
        self.assertTrue(outcome.ok, outcome.reason)
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pid_file.read_text(encoding="utf-8").strip())
        self.assertTrue(core.process_alive(child))
        self.assertTrue(adapter.terminate(self.device, "proj-gdv").ok)
        deadline = time.monotonic() + 5
        while core.process_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(core.process_alive(child), "the grandchild dies with the player's process group")

    def test_members_that_outlive_a_dead_leader_are_stopped(self):
        app = self.base / "build" / "proj-gdv.app"
        make_bundle(app)
        pid_file = self.base / "child.pid"
        (app / "Contents" / "MacOS" / "Player").write_text(f"#!/bin/sh\nsleep 300 &\necho $! > '{pid_file}'\nsleep 1\n", encoding="utf-8")
        adapter = macos.MacOSAdapter(run=self.run_tool, environ={}, editors=lambda: [EDITOR], darwin=True, settle_sec=0.5,
                                     state_dir=self.base / "state")
        adapter.install(self.device, app)
        adapter.launch(self.device, "proj-gdv", [], self.base / "run" / "console.log")
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pid_file.read_text(encoding="utf-8").strip())
        self.assertTrue(core.process_alive(child))
        time.sleep(1.5)  # the leader exits, its child stays in the group
        adapter._process = adapter._tree = None  # a later job: only the record remains
        self.assertTrue(adapter.terminate(self.device, "proj-gdv").ok)
        deadline = time.monotonic() + 5
        while core.process_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(core.process_alive(child))

    def test_a_second_adapter_instance_can_stop_a_recorded_player(self):
        app = self.base / "build" / "proj-gdv.app"
        make_bundle(app)
        (app / "Contents" / "MacOS" / "Player").write_text("#!/bin/sh\nsleep 300\n", encoding="utf-8")
        first = macos.MacOSAdapter(run=self.run_tool, environ={}, editors=lambda: [EDITOR], darwin=True, settle_sec=0.2,
                                   state_dir=self.base / "state")
        self.run_tool.answers["xattr"] = result_of(constants.FAIL, "", 1)
        first.install(self.device, app)
        first.launch(self.device, "proj-gdv", [], self.base / "run" / "console.log")
        pid = json.loads((self.base / "state" / "running.json").read_text(encoding="utf-8"))["pid"]
        second = macos.MacOSAdapter(run=self.run_tool, environ={}, darwin=True, state_dir=self.base / "state")
        self.assertTrue(second.terminate(self.device, "proj-gdv").ok)
        deadline = time.monotonic() + 8
        while core.process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        self.assertFalse(core.process_alive(pid))
        first._forget()


class DeviceTests(MacOSTestCase):
    def test_discover_reports_the_mac_as_a_host_device(self):
        answers = {"sysctl": lambda argv: result_of(output="Apple M4" if "machdep.cpu.brand_string" in argv else "Mac16,1"),
                   "sw_vers": result_of(output="27.2")}
        self.run_tool.answers.update(answers)
        devices = self.adapter().discover()
        self.assertEqual(1, len(devices))
        device = devices[0]
        self.assertEqual(("macos", "mac-apple-m4", "host", 27, "Mac16,1"),
                         (device.platform, device.device_class, device.evidence, device.os_major, device.model))
        self.assertTrue(device.id.startswith("macos:"))
        self.assertEqual([], self.adapter(darwin=False).discover())

    def test_health_reads_lock_thermal_and_battery(self):
        console = plistlib.dumps([{"IOConsoleUsers": [{"CGSSessionScreenIsLocked": True}]}]).decode()
        self.run_tool.answers.update({
            "ioreg": result_of(output=console),
            "pmset": lambda argv: result_of(output="CPU_Speed_Limit = 100" if "therm" in argv else "Now drawing from 'AC Power'\n -InternalBattery-0 87%; charged")})
        health = self.adapter().health(self.device)
        self.assertEqual((True, True, "nominal", 87), (health.reachable, health.locked, health.thermal_state, health.battery_percent))
        self.run_tool.answers["pmset"] = lambda argv: result_of(output="CPU_Speed_Limit = 70" if "therm" in argv else "")
        self.assertEqual("throttled", self.adapter().health(self.device).thermal_state)
        self.run_tool.answers["ioreg"] = result_of(output=plistlib.dumps([{"IOConsoleUsers": []}]).decode())
        headless = self.adapter().health(self.device)
        self.assertIsNone(headless.locked)
        self.assertTrue(any("console user" in note for note in headless.notes))

    def test_probe_lists_what_is_missing(self):
        with mock.patch.object(macos.shutil, "which", return_value="/usr/bin/codesign"):
            self.assertTrue(self.adapter().probe().can("build"))
            missing = self.adapter(editors=lambda: [{**EDITOR, "modules": []}]).probe()
        self.assertFalse(missing.can("build"))
        self.assertIn("macOS build support", missing.blocked["build"])
        off = self.adapter(darwin=False).probe()
        self.assertEqual(set(), set(off.available))
        self.assertIn("build", off.blocked)

    def test_doctor_drops_a_platform_the_adapter_cannot_build(self):
        import valtrion.cli  # noqa: F401 - the CLI module registers the commands; importing `commands` first is circular
        from valtrion import commands

        providers.register("macos", lambda: self.adapter(darwin=False))
        self.addCleanup(providers.unregister, "macos")
        report = {"capabilities": {"buildPlatforms": ["editor", "macos"]}, "blocked": {}}
        commands.apply_adapter_capabilities(report)
        self.assertEqual(["editor"], report["capabilities"]["buildPlatforms"])
        self.assertIn("macos.build", report["blocked"])
        self.assertIn("build", report["adapters"]["macos"]["blocked"])

    def test_screenshot_prefers_the_harness_capture(self):
        adapter, _ = self.installed()
        root = Path(json.loads((self.base / "state" / "installed.json").read_text(encoding="utf-8"))["dataRoot"])
        shot = root / "gdv" / "out" / "run-1" / "screenshots" / "end.png"
        shot.parent.mkdir(parents=True)
        shot.write_bytes(b"png")
        out = self.base / "run" / "screenshots" / "failure.png"
        self.assertTrue(adapter.screenshot(self.device, out).ok)
        self.assertEqual(b"png", out.read_bytes())
        self.assertNotIn("screencapture", self.run_tool.names())

    def test_screenshot_without_permission_is_a_permission_request(self):
        adapter, _ = self.installed()
        self.run_tool.answers["screencapture"] = result_of(constants.FAIL, "could not create image from display", 1)
        outcome = adapter.screenshot(self.device, self.base / "run" / "s.png")
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)


class PipelineIntegrationTests(MacOSTestCase):
    """The whole pipeline on a Windows-safe fake of the Mac: build, sign, install, run, collect, terminate and clean up."""

    def setUp(self):
        super().setUp()
        self.root = self.base / "artifacts"
        self.adapters = []

        def factory():
            created = self.adapter()
            self.adapters.append(created)
            return created

        providers.register("macos", factory)
        self.addCleanup(providers.unregister, "macos")
        plan = {"schema": "gdv.plan/1", "id": "smoke.sample", "kind": "smoke", "requires": {"platforms": ["macos"]},
                "dataPolicy": "isolated", "budgetSeconds": 30, "steps": [{"type": "wait", "timeoutSec": 5}]}
        (self.work / "plans").mkdir()
        (self.work / "plans" / "smoke.plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.run_tool.on_unity = self.unity_builds
        self.run_tool.answers["sysctl"] = lambda argv: result_of(output="Apple M4" if "machdep.cpu.brand_string" in argv else "Mac16,1")
        self.run_tool.answers["sw_vers"] = result_of(output="27.2")
        self.run_tool.answers["xattr"] = result_of(constants.FAIL, "", 1)

    def unity_builds(self, argv):
        make_bundle(Path(argv[argv.index("-buildOutput") + 1]))
        return result_of()

    def fake_spawn(self, argv, cwd, env, log, grace=3.0):
        # The player starts, writes its heartbeat and result into the isolated data folder, and keeps running.
        self.spawned.append(list(argv))
        root = Path(argv[argv.index("-gdv-plan") + 1]).parents[2]
        run = root / "gdv" / "out" / "run-1"
        run.mkdir(parents=True)
        (run / "screenshots").mkdir()
        core.atomic_write_json(run / "heartbeat.json", {"seq": 1})
        core.atomic_write_json(run / "result.json", {"status": "PASS", "reason": None, "steps": [], "metrics": [], "unverified": ["audio"]})
        return FakeProcess(), self.tree

    def run_pipeline(self, stages, **extra):
        document = {"schema": "gdv.pipeline/1", "id": "mac", "candidate": {"repo": "Sample", "sha": SHA, "profile": "Validation"},
                    "device": {"platform": "macos"}, "stages": stages}
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "projectDir": str(self.project),
                   "baseDir": str(self.work / "plans"), "devicePollSec": 0.01, **extra}
        request = jobs.JobRequest(kind="pipeline", project="Sample", sha=SHA, label="mac", payload=payload,
                                  host={"id": "mac", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=120, owner="t")
        info = jobs.start_job(request, self.root, self.base / "gdv", foreground=True)
        return jobs.JobStore(Path(info["dir"]))

    def test_smoke_run_passes_and_leaves_nothing_behind(self):
        stages = [{"stage": "build"}, {"stage": "install"},
                  {"stage": "run", "plan": "smoke.plan.json", "args": {"app": "Sample-Project-gdv", "stallSec": 5}, "timeoutSec": 30}]
        store = self.run_pipeline(stages)
        job = store.read_job()
        self.assertEqual(("finished", "PASS"), (job["state"], job["status"]), job["reason"])
        result = json.loads(store.result.read_text(encoding="utf-8"))
        self.assertEqual([], Validator().validate(result, "gdv.result/1"))
        self.assertEqual(("macos", "mac-apple-m4", "host", 27), (result["target"]["platform"], result["target"]["deviceClass"],
                                                                    result["target"]["evidence"], result["target"]["osMajor"]))
        self.assertEqual({"export", "sign", "install", "launch", "scenario"}, set(result["layers"]))
        self.assertIn("audio", result["unverified"])
        self.assertTrue(self.tree.killed, "the player's process group is killed at the end of the run")
        self.assertFalse((store.directory / "build").exists(), "the temporary build is removed once the results are saved")
        self.assertFalse(macos.data_root_for(BUNDLE_ID).exists(), "the isolated data folder is cleaned up")

    def test_the_mac_build_counts_the_slots_of_the_host_not_of_its_platform(self):
        from valtrion import lease

        asked = []
        original = lease.LeaseManager.acquire_slot

        def record(manager, name, slots, *args, **kwargs):
            asked.append((name, slots))
            return original(manager, name, slots, *args, **kwargs)

        for host_os, expected in (("macos", 1), ("windows", 2)):
            asked.clear()
            with mock.patch.object(core, "local_os", lambda host_os=host_os: host_os), \
                    mock.patch.object(lease.LeaseManager, "acquire_slot", record):
                self.run_pipeline([{"stage": "build"}])
            self.assertEqual([("unity-editor", expected)], asked, host_os)

    def test_a_launch_that_needs_permission_still_cleans_the_pushed_data(self):
        self.run_tool.answers["xattr"] = result_of(constants.PASS, "0081;5f;Safari;", 0)  # quarantined
        stages = [{"stage": "build"}, {"stage": "install"},
                  {"stage": "run", "plan": "smoke.plan.json", "args": {"app": "Sample-Project-gdv"}, "timeoutSec": 30}]
        store = self.run_pipeline(stages)
        self.assertEqual("NEEDS_PERMISSION", store.read_job()["status"])
        self.assertFalse(macos.data_root_for(BUNDLE_ID).exists(), "the plan pushed before the launch is removed")
        self.assertEqual([], self.spawned)

    def test_data_is_kept_while_the_player_could_not_be_stopped(self):
        self.tree.kill = lambda: None
        self.tree.alive = lambda: True
        stages = [{"stage": "build"}, {"stage": "install"},
                  {"stage": "run", "plan": "smoke.plan.json", "args": {"app": "Sample-Project-gdv", "stallSec": 5}, "timeoutSec": 30}]
        store = self.run_pipeline(stages)
        result = json.loads(store.result.read_text(encoding="utf-8"))
        self.assertTrue(any("still running" in note for note in result["unverified"]), result["unverified"])
        self.assertTrue(macos.data_root_for(BUNDLE_ID).exists(), "a live player's folder is not removed under it")

    def test_a_player_that_stops_reporting_is_a_stall_and_its_group_is_killed(self):
        def hang(argv, cwd, env, log, grace=3.0):
            self.spawned.append(list(argv))
            root = Path(argv[argv.index("-gdv-plan") + 1]).parents[2]
            run = root / "gdv" / "out" / "run-1"
            run.mkdir(parents=True)
            core.atomic_write_json(run / "heartbeat.json", {"seq": 3})
            return FakeProcess(), self.tree

        self.fake_spawn = hang
        stages = [{"stage": "build"}, {"stage": "install"},
                  {"stage": "run", "plan": "smoke.plan.json", "args": {"app": "Sample-Project-gdv", "stallSec": 1}, "timeoutSec": 30}]
        store = self.run_pipeline(stages)
        self.assertEqual("STALL", store.read_job()["status"])
        self.assertTrue(self.tree.killed)
        self.assertFalse((store.directory / "build").exists())


if __name__ == "__main__":
    unittest.main()
