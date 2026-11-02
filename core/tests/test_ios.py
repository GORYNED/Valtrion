import json
import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import config, constants, core, incremental, jobs, providers, supervise
from valtrion.adapter import DeviceInfo
from valtrion.providers import ios

# Deliberately synthetic; never use a physical device identifier in fixtures.
UDID = "00000000-0000000000000001"
DEVICE = DeviceInfo(f"ios:{UDID}", "ios", "ipad-a14", "physical", "iPad (10th generation) (iPad13,19)", "iPadOS 27.2", ("touch",))

LISTING = {"result": {"devices": [
    {"identifier": "5F3D", "hardwareProperties": {"udid": UDID, "productType": "iPad13,19", "marketingName": "iPad (10th generation)",
                                                  "reality": "physical", "platform": "iOS"},
     "deviceProperties": {"name": "iPad", "osVersionNumber": "27.2"},
     "connectionProperties": {"tunnelState": "connected", "pairingState": "paired"}},
    {"identifier": "A1", "properties": {"hardware": {"udid": "00008130-AAAA", "productType": "iPhone16,2", "marketingName": "iPhone 15 Pro Max",
                                                       "reality": "physical", "platform": "iOS"},
                                         "connection": {"state": "disconnected", "pairingState": "paired"}}},
    {"identifier": "S1", "properties": {"hardware": {"udid": "SIM-1", "productType": "iPad17,4", "marketingName": "iPad Pro",
                                                       "reality": "simulated", "platform": "iOS"},
                                         "connection": {"state": "connected", "pairingState": "paired"}}}]}}


def result(status=constants.PASS, output="", returncode=0, reason=None):
    return supervise.BoundedResult(status, reason, returncode, output)


class Recorder:
    """Stands in for `supervise.run_bounded`: records every call and answers by the tool and subcommand."""

    def __init__(self):
        self.calls = []
        self.answers = {}  # key (tool or "devicectl <sub...>") -> BoundedResult or callable(argv, kwargs)
        self.json = {"list devices": LISTING, "device info lockState": {"result": {"passcodeRequired": False}},
                     "device info apps": {"result": {"apps": [{"bundleIdentifier": "com.example.game", "url": "file:///var/App/Game.app/"}]}},
                     "device info processes": {"result": {"runningProcesses": [
                         {"processIdentifier": 77, "executable": "file:///var/App/Game.app/Game"}]}}}

    def __call__(self, argv, timeout_sec, stall_sec=None, cwd=None, env=None, log_path=None, **options):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "timeout": timeout_sec, "stall": stall_sec, "cwd": cwd, "log": log_path, "options": options})
        key = self.key(argv)
        answer = self.answers.get(key)
        if callable(answer):
            answer = answer(argv, self.calls[-1])
        if answer is not None:
            return answer
        if "--json-output" in argv:
            document = self.json.get(key)
            if document is None:
                return result(constants.FAIL, "no json", 1)
            Path(argv[argv.index("--json-output") + 1]).write_text(json.dumps(document), encoding="utf-8")
        if log_path is not None:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("", encoding="utf-8")
        return result()

    @staticmethod
    def key(argv):
        if argv[:2] == ["xcrun", "devicectl"]:
            words = []
            for word in argv[2:]:
                if word.startswith("-"):
                    break
                words.append(word)
            return " ".join(words)
        return os.path.basename(argv[0]) if not argv[0].endswith("Unity") else "unity"

    def named(self, key):
        return [c for c in self.calls if self.key(c["argv"]) == key]


class FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.killed = False

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        return self.exit_code


class FakeTree(supervise.ProcessTree):
    def __init__(self):
        self.killed = False

    def cpu_seconds(self):
        return 0.0

    def alive(self):
        return not self.killed

    def kill(self):
        self.killed = True


class IosTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="gdv-ios-test-"))  # not TemporaryDirectory(ignore_cleanup_errors=...): the Mac has Python 3.9
        self.addCleanup(shutil.rmtree, base, True)
        self.work, self.home = base / "work", base / "home"
        self.project = self.work / "project"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.project / "validation" / "device").mkdir(parents=True)
        self.editors = base / "editors"
        binary = self.editors / "6000.6.7f1" / "Unity.app" / "Contents" / "MacOS" / "Unity"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"")
        patcher = mock.patch.dict(os.environ, {ios.UNITY_EDITORS_ENV: str(self.editors), core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_profiles({"executeMethod": "My.Build.IosBuild.Internal", "buildProfile": "Assets/Internal.asset",
                             "configuration": "ReleaseForRunning"})
        self.recorder = Recorder()
        self.recorder.answers["launchctl"] = result(output="Aqua\n")
        self.adapter = ios.IosAdapter(run=self.recorder, os_name="macos", home=self.home, sleep=lambda _: None)
        self.out = base / "job" / "build"

    def write_profiles(self, entry, profile="Internal"):
        (self.project / ios.PROFILES_FILE).write_text(json.dumps({"profiles": {profile: {"ios": entry}}}), encoding="utf-8")

    # -- discovery ---------------------------------------------------------------------------------------------------------

    def test_discovery_lists_only_connected_paired_devices_from_devicectl(self):
        devices = {d.id: d for d in self.adapter.discover()}
        self.assertEqual({f"ios:{UDID}", "ios:SIM-1"}, set(devices))
        ipad = devices[f"ios:{UDID}"]
        self.assertEqual(("ipad-a14", "physical", 27), (ipad.device_class, ipad.evidence, ipad.os_major))
        self.assertEqual("emulator", devices["ios:SIM-1"].evidence, "a simulator never counts as device evidence")

    def test_registry_adds_the_class_but_never_a_device_discovery_did_not_find(self):
        registry = config.load_registry(self.home)
        registry["devices"] = [
            {"id": f"ios:{UDID}", "hostId": registry["host"]["id"], "platform": "ios", "class": "ipad-owner", "evidence": "physical",
             "capabilities": ["camera.120fps"], "exclusive": True},
            {"id": "ios:GONE", "hostId": registry["host"]["id"], "platform": "ios", "class": "old", "evidence": "physical",
             "capabilities": [], "exclusive": True}]
        config.save_registry(registry, self.home)
        devices = {d.id: d for d in self.adapter.discover()}
        self.assertEqual("ipad-owner", devices[f"ios:{UDID}"].device_class)
        self.assertIn("camera.120fps", devices[f"ios:{UDID}"].capabilities)
        self.assertNotIn("ios:GONE", devices, "a stale registry entry is never trusted over discovery")

    def test_discovery_failure_is_reported_not_swallowed(self):
        self.recorder.answers["list devices"] = result(constants.TIMEOUT, "", None, "timed out")
        with self.assertRaises(RuntimeError):
            self.adapter.discover()

    # -- build -------------------------------------------------------------------------------------------------------------

    def make_export(self, argv, call):
        export = Path(argv[argv.index("-gdv-export-path") + 1])
        (export / "Unity-iPhone.xcodeproj").mkdir(parents=True, exist_ok=True)
        return result()

    def make_app(self, argv, call):
        derived = Path(argv[argv.index("-derivedDataPath") + 1])
        (derived / "Build" / "Products" / "ReleaseForRunning-iphoneos" / "Game.app").mkdir(parents=True, exist_ok=True)
        return result()

    def test_a_good_build_exports_then_builds_for_the_discovered_device_and_returns_the_app(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        self.assertEqual("Game.app", outcome.artifacts[0].name)
        self.assertEqual({"layer.export": "PASS", "layer.nativeBuild": "PASS", "layer.sign": "PASS", "xcodebuild.container": "project"},
                         dict(outcome.details))
        unity = self.recorder.named("unity")[0]
        self.assertIn("-activeBuildProfile", unity["argv"])
        self.assertIn("My.Build.IosBuild.Internal", unity["argv"])
        export = unity["argv"][unity["argv"].index("-gdv-export-path") + 1]
        self.assertEqual(export, unity["argv"][unity["argv"].index("-buildOutput") + 1])
        xcode = self.recorder.named("xcodebuild")[0]
        self.assertIn(f"platform=iOS,id={UDID}", xcode["argv"], "the destination comes from discovery, not from a script constant")
        self.assertIn("-allowProvisioningUpdates", xcode["argv"])

    # -- incremental builds in a managed checkout (incremental.py) -------------------------------------------------------------

    def incremental_plan(self, key="k1", clean=False):
        return incremental.Plan(self.home / "incremental" / "Game" / "queue", key, clean)

    def incremental_build(self, plan):
        self.adapter.bind_incremental(plan, "unity-project:abc")
        return self.adapter.build(self.project, "Internal", self.out)

    def test_the_second_build_exports_with_append_into_the_same_folder_and_reuses_derived_data(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        plan = self.incremental_plan()
        first = self.incremental_build(plan)
        self.assertEqual(constants.PASS, first.status, first.reason)
        self.assertIn("clean build (first build here)", first.details[ios.INCREMENTAL_DETAIL])
        self.assertNotIn("-gdv-export-append", self.recorder.named("unity")[0]["argv"], "nothing to append to yet")
        self.assertEqual(self.out / "Game.app", first.artifacts[0], "the pipeline publishes a copy; the product stays in DerivedData")
        marker = plan.folder / "DerivedData" / "compiled.o"
        marker.write_text("object", encoding="utf-8")
        (plan.folder / "export" / "Classes.cpp").write_text("il2cpp", encoding="utf-8")
        second = self.incremental_build(plan)
        self.assertEqual(constants.PASS, second.status, second.reason)
        self.assertIn("reused", second.details[ios.INCREMENTAL_DETAIL])
        unity = self.recorder.named("unity")[1]["argv"]
        self.assertEqual(str(plan.folder / "export"), unity[unity.index("-gdv-export-path") + 1], "the same export folder, outside the job")
        self.assertEqual("1", unity[unity.index("-gdv-export-append") + 1], "Unity is asked to append")
        xcode = self.recorder.named("xcodebuild")[1]["argv"]
        self.assertEqual(str(plan.folder / "DerivedData"), xcode[xcode.index("-derivedDataPath") + 1])
        self.assertTrue(marker.exists(), "DerivedData of the previous build is still there")
        self.assertTrue((plan.folder / "export" / "Classes.cpp").exists(), "the export is not wiped")

    def test_a_source_unity_rewrites_with_the_same_bytes_keeps_its_mtime_across_builds(self):
        old = 1_700_000_000_000_000_000
        state = {"n": 0}

        def export_again(argv, call):
            self.make_export(argv, call)
            export = Path(argv[argv.index("-gdv-export-path") + 1])
            state["n"] += 1
            (export / "Classes.cpp").write_text("same", encoding="utf-8")  # fresh mtime, same bytes
            (export / "Other.cpp").write_text(f"v{state['n']}", encoding="utf-8")  # really different
            if state["n"] == 1:
                os.utime(export / "Classes.cpp", ns=(old, old))
            return result()

        self.recorder.answers["unity"] = export_again
        self.recorder.answers["xcodebuild"] = self.make_app
        plan = self.incremental_plan()
        self.incremental_build(plan)
        second = self.incremental_build(plan)
        self.assertEqual(old, (plan.folder / "export" / "Classes.cpp").stat().st_mtime_ns)
        self.assertIn("1 restored", second.details[ios.INCREMENTAL_EXPORT_DETAIL])
        self.assertIn("1 changed", second.details[ios.INCREMENTAL_EXPORT_DETAIL])

    def test_a_changed_key_or_a_clean_request_starts_from_nothing(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        plan = self.incremental_plan("k1")
        self.incremental_build(plan)
        (plan.folder / "DerivedData" / "compiled.o").write_text("object", encoding="utf-8")
        changed = self.incremental_build(self.incremental_plan("k2"))
        self.assertIn("toolchain, build profile or signing changed", changed.details[ios.INCREMENTAL_DETAIL])
        self.assertFalse((plan.folder / "DerivedData" / "compiled.o").exists())
        self.assertNotIn("-gdv-export-append", self.recorder.named("unity")[1]["argv"])
        (plan.folder / "DerivedData" / "compiled.o").write_text("object", encoding="utf-8")
        forced = self.incremental_build(self.incremental_plan("k2", clean=True))
        self.assertIn("clean rebuild was requested", forced.details[ios.INCREMENTAL_DETAIL])
        self.assertFalse((plan.folder / "DerivedData" / "compiled.o").exists())

    def test_a_build_killed_half_way_is_not_trusted_by_the_next_one(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        plan = self.incremental_plan()
        self.incremental_build(plan)
        self.recorder.answers["xcodebuild"] = lambda argv, call: result(constants.TIMEOUT, "", None, "timed out")
        broken = self.incremental_build(plan)
        self.assertEqual(constants.TIMEOUT, broken.status)
        self.assertFalse((plan.folder / "export").exists(), "the half-built project is removed")
        self.recorder.answers["xcodebuild"] = self.make_app
        again = self.incremental_build(plan)
        self.assertIn("clean build", again.details[ios.INCREMENTAL_DETAIL])

    def test_a_failed_build_keeps_the_folder_for_the_next_try(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        plan = self.incremental_plan()
        self.incremental_build(plan)
        self.recorder.answers["unity"] = lambda argv, call: result(constants.FAIL, "", 1, "compile error")
        failed = self.incremental_build(plan)
        self.assertEqual(constants.FAIL, failed.status)
        self.recorder.answers["unity"] = self.make_export
        again = self.incremental_build(plan)
        self.assertIn("did not finish", again.details[ios.INCREMENTAL_DETAIL], "a build that did not succeed never marks the folder as good")

    def test_an_unmanaged_build_is_not_incremental(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        self.adapter.bind_incremental(self.incremental_plan())
        self.adapter.bind_incremental(None)
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertNotIn(ios.INCREMENTAL_DETAIL, outcome.details)
        xcode = self.recorder.named("xcodebuild")[0]["argv"]
        self.assertEqual(str(self.out / "DerivedData"), xcode[xcode.index("-derivedDataPath") + 1])

    def test_an_export_without_a_workspace_is_built_with_project(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        argv = self.recorder.named("xcodebuild")[0]["argv"]
        self.assertNotIn("-workspace", argv)
        self.assertEqual("Unity-iPhone.xcodeproj", Path(argv[argv.index("-project") + 1]).name)
        self.assertEqual("project", outcome.details["xcodebuild.container"])

    def test_a_cocoapods_export_is_built_with_its_workspace_and_the_same_other_arguments(self):
        def make_pods_export(argv, call):
            export = Path(argv[argv.index("-gdv-export-path") + 1])
            (export / "Unity-iPhone.xcodeproj").mkdir(parents=True, exist_ok=True)
            (export / "Unity-iPhone.xcworkspace").mkdir()
            return result()
        self.recorder.answers["unity"] = make_pods_export
        self.recorder.answers["xcodebuild"] = self.make_app
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        self.assertEqual("workspace", outcome.details["xcodebuild.container"])
        xcode = self.recorder.named("xcodebuild")[0]
        argv = xcode["argv"]
        self.assertNotIn("-project", argv)
        self.assertEqual("Unity-iPhone.xcworkspace", Path(argv[argv.index("-workspace") + 1]).name)
        self.assertEqual(["-scheme", "Unity-iPhone", "-configuration", "ReleaseForRunning"], argv[3:7])
        self.assertIn(f"platform=iOS,id={UDID}", argv)
        self.assertIn("-allowProvisioningUpdates", argv)
        self.assertEqual(str(self.out / "DerivedData"), argv[argv.index("-derivedDataPath") + 1])
        self.assertEqual("Game.app", outcome.artifacts[0].name)

    def test_export_has_a_timeout_and_a_stall_watch_on_the_unity_log(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        self.write_profiles({"executeMethod": "M.B", "exportTimeoutSec": 1800, "buildTimeoutSec": 2400, "stallSec": 300,
                             "configuration": "ReleaseForRunning"})
        self.adapter.build(self.project, "Internal", self.out)
        unity = self.recorder.named("unity")[0]
        self.assertEqual((1800, 300), (unity["timeout"], unity["stall"]))
        self.assertEqual(self.out.parent / "build-logs" / "unity-export.log", unity["options"]["progress_files"][0],
                         "stall is judged on the Unity log the export writes")
        self.assertTrue(unity["options"]["cpu_counts"])
        xcode = self.recorder.named("xcodebuild")[0]
        self.assertEqual((2400, 300), (xcode["timeout"], xcode["stall"]))
        self.assertEqual((self.out / "DerivedData",), tuple(xcode["options"]["progress_files"]), "stall is judged on the build products")

    def test_unity_path_overrides_the_hub_editor_and_must_exist(self):
        stub = self.work / "UnityStub"
        stub.write_bytes(b"")
        self.recorder.answers["UnityStub"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        self.write_profiles({"executeMethod": "M.B", "unityPath": str(stub), "configuration": "ReleaseForRunning"})
        self.assertEqual(constants.PASS, self.adapter.build(self.project, "Internal", self.out).status)
        self.assertEqual(str(stub), self.recorder.named("UnityStub")[0]["argv"][0])
        self.write_profiles({"executeMethod": "M.B", "unityPath": str(self.work / "nope")})
        self.assertEqual(constants.BLOCKED, self.adapter.build(self.project, "Internal", self.out).status)

    def test_a_hung_export_is_a_stall_and_xcodebuild_never_runs(self):
        self.recorder.answers["unity"] = result(constants.STALL, "", None, "no progress for 600.0 s")
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.STALL, outcome.status)
        self.assertEqual("STALL", outcome.details["layer.export"])
        self.assertEqual([], self.recorder.named("xcodebuild"))

    def test_a_failed_xcodebuild_fails_the_native_build_and_returns_no_artifact(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = result(constants.FAIL, "error: Build input file cannot be found", 65, "exit code 65")
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertEqual((), outcome.artifacts, "XCODE EXIT 65 must not leave an app for install")
        self.assertEqual("FAIL", outcome.details["layer.nativeBuild"])
        self.assertIn("xcode-build.log", outcome.reason)

    def test_a_success_exit_without_an_app_is_still_a_failure(self):
        self.recorder.answers["unity"] = self.make_export
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertIn("no .app", outcome.reason)

    def test_a_signing_failure_needs_the_owner(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = result(constants.FAIL, "CodeSign failed: errSecInternalComponent", 65, "exit code 65")
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)
        self.assertEqual("FAIL", outcome.details["layer.sign"])

    def test_a_build_outside_the_gui_session_is_blocked_before_unity_starts(self):
        self.recorder.answers["launchctl"] = result(output="Background\n")
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("gui", outcome.reason)
        self.assertEqual([], self.recorder.named("unity"))

    def test_missing_or_unknown_profile_settings_are_blocked_with_the_file_named(self):
        (self.project / ios.PROFILES_FILE).unlink()
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("profiles.json", outcome.reason)
        self.write_profiles({"executeMethod": "M.B", "bogus": 1})
        self.assertIn("unknown keys", self.adapter.build(self.project, "Internal", self.out).reason)
        self.write_profiles({"executeMethod": "M.B"}, profile="Other")
        self.assertIn("no profiles.Internal.ios", self.adapter.build(self.project, "Internal", self.out).reason)

    def test_an_export_path_must_stay_inside_the_project_and_is_reported_temporary(self):
        self.write_profiles({"executeMethod": "M.B", "exportPath": "../outside"})
        self.assertEqual(constants.BLOCKED, self.adapter.build(self.project, "Internal", self.out).status)
        (self.project / "Builds").mkdir()
        self.write_profiles({"executeMethod": "M.B", "exportPath": "Builds/iOS", "configuration": "ReleaseForRunning"})

        def export(argv, call):
            (self.project / "Builds" / "iOS" / "Unity-iPhone.xcodeproj").mkdir(parents=True, exist_ok=True)
            return result()

        self.recorder.answers["unity"] = export
        self.recorder.answers["xcodebuild"] = self.make_app
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        self.assertEqual(str((self.project / "Builds" / "iOS").resolve()), outcome.details["temp.export"])

    def test_an_export_path_in_a_protected_or_existing_folder_is_refused_and_never_listed_temporary(self):
        for bad in ("Assets", "Assets/Sub", "ProjectSettings", "Packages", ".git", "validation/device"):
            self.write_profiles({"executeMethod": "M.B", "exportPath": bad})
            outcome = self.adapter.build(self.project, "Internal", self.out)
            self.assertEqual(constants.BLOCKED, outcome.status, bad)
            self.assertNotIn("temp.export", outcome.details)
        (self.project / "Builds" / "iOS").mkdir(parents=True)
        (self.project / "Builds" / "iOS" / "hand-edited.txt").write_text("keep", encoding="utf-8")
        self.write_profiles({"executeMethod": "M.B", "exportPath": "Builds/iOS"})
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("already exists", outcome.reason)
        self.assertNotIn("temp.export", outcome.details, "a folder this run did not create is never removed")
        self.assertEqual([], self.recorder.named("unity"))

    def test_xcodebuild_settings_and_the_selected_device_reach_xcodebuild(self):
        self.write_profiles({"executeMethod": "M.B", "configuration": "ReleaseForRunning",
                             "xcodebuildSettings": {"GCC_ENABLE_OBJC_EXCEPTIONS": "YES"}})
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        self.recorder.json["list devices"] = {"result": {"devices": [
            *LISTING["result"]["devices"],
            {"identifier": "Z", "hardwareProperties": {"udid": "00008999-ZZZZ", "productType": "iPad13,19", "reality": "physical", "platform": "iOS"},
             "deviceProperties": {"osVersionNumber": "27.2"}, "connectionProperties": {"tunnelState": "connected", "pairingState": "paired"}}]}}
        self.adapter.bind_device(DeviceInfo("ios:00008999-ZZZZ", "ios", "ipad-a14", "physical"))
        self.assertEqual(constants.PASS, self.adapter.build(self.project, "Internal", self.out).status)
        argv = self.recorder.named("xcodebuild")[0]["argv"]
        self.assertIn("GCC_ENABLE_OBJC_EXCEPTIONS=YES", argv)
        self.assertIn("platform=iOS,id=00008999-ZZZZ", argv, "signs for the leased device, not the first attached one")
        self.write_profiles({"executeMethod": "M.B", "xcodebuildSettings": {"lower": "x"}})
        self.assertEqual(constants.BLOCKED, self.adapter.build(self.project, "Internal", self.out).status)

    def test_the_whole_build_is_capped_and_every_tool_call_gets_the_remaining_time(self):
        self.write_profiles({"executeMethod": "M.B", "configuration": "ReleaseForRunning", "exportTimeoutSec": 50000, "buildTimeoutSec": 50000})
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        self.adapter.build(self.project, "Internal", self.out)
        for call in (self.recorder.named("unity")[0], self.recorder.named("xcodebuild")[0]):
            self.assertLessEqual(call["timeout"], ios.MAX_BUILD_SEC)

    def test_a_locked_device_during_the_native_build_needs_the_owner(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = result(constants.FAIL, "error: Device is locked. Unlock the device", 70, "exit code 70")
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)
        self.assertIn("unlock", outcome.reason)

    def test_device_or_app_not_found_is_a_failure_not_an_empty_pull_or_a_tree_retry(self):
        self.adapter.bind_application("com.example.game")
        self.recorder.answers["device copy from"] = result(constants.FAIL, "ERROR: device not found", 1, "exit code 1")
        self.assertEqual(constants.FAIL, self.adapter.pull(DEVICE, "gdv/out", self.work / "pulled").status)
        local = self.work / "plan.json"
        local.write_text("{}", encoding="utf-8")
        self.recorder.answers["device copy to"] = result(constants.FAIL, "ERROR: application not found", 1, "exit code 1")
        self.assertEqual(constants.FAIL, self.adapter.push(DEVICE, local, "gdv/inbox/plan.json").status)
        self.assertEqual(1, len(self.recorder.named("device copy to")), "no directory-tree retry for an unrelated error")

    def test_terminate_does_not_claim_success_when_the_device_cannot_be_asked(self):
        self.recorder.answers["device info processes"] = result(constants.FAIL, "tunnel closed", 1, "exit code 1")
        outcome = self.adapter.terminate(DEVICE, "com.example.game")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertEqual([], self.recorder.named("device process terminate"))

    def test_terminate_stops_the_running_pid_and_verifies_it_is_gone(self):
        running = {"result": {"runningProcesses": [{"processIdentifier": 77, "executable": "file:///private/var/App/Game.app/Game"}]}}
        gone = {"result": {"runningProcesses": []}}

        def processes(argv, call):
            Path(argv[argv.index("--json-output") + 1]).write_text(
                json.dumps(gone if self.recorder.named("device process terminate") else running), encoding="utf-8")
            return result()

        self.recorder.answers["device info processes"] = processes  # the device lists /private/var, the app url says /var
        self.assertTrue(self.adapter.terminate(DEVICE, "com.example.game").ok)
        stopped = self.recorder.named("device process terminate")
        self.assertEqual(["77"], [c["argv"][c["argv"].index("--pid") + 1] for c in stopped])

    def test_terminate_fails_when_the_application_is_still_running_afterwards(self):
        outcome = self.adapter.terminate(DEVICE, "com.example.game")  # the fake device keeps listing pid 77
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertIn("still running", outcome.reason)

    def test_export_path_case_and_missing_parent_and_immediate_registration(self):
        for bad in ("assets/Gen", "PROJECTSETTINGS/x"):
            self.write_profiles({"executeMethod": "M.B", "exportPath": bad})
            self.assertEqual(constants.BLOCKED, self.adapter.build(self.project, "Internal", self.out).status, bad)
        self.assertFalse((self.project / "assets" / "Gen").exists())
        registered = []
        self.write_profiles({"executeMethod": "M.B", "exportPath": "Builds/iOS", "configuration": "ReleaseForRunning"})
        self.adapter.bind_temporary_registrar(registered.append)

        def hung(argv, call):
            # The missing `Builds` parent is created by the adapter; the topmost folder it created is the one listed, at once.
            self.assertEqual([(self.project / "Builds").resolve()], registered, "listed before Unity even starts")
            self.assertTrue((self.project / "Builds" / "iOS").is_dir())
            return result(constants.TIMEOUT, "", None, "no result within the timeout")

        self.recorder.answers["unity"] = hung
        self.assertEqual(constants.TIMEOUT, self.adapter.build(self.project, "Internal", self.out).status)
        self.assertEqual(1, len(registered))

    def test_an_export_parent_that_is_not_a_folder_or_cannot_be_created_is_blocked_and_leaves_nothing(self):
        (self.project / "Builds").write_text("a file", encoding="utf-8")
        self.write_profiles({"executeMethod": "M.B", "exportPath": "Builds/iOS"})
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("is not a folder", outcome.reason)
        self.assertNotIn("temp.export", outcome.details)
        self.write_profiles({"executeMethod": "M.B", "exportPath": "Out/a/iOS"})
        real_mkdir = Path.mkdir

        def failing(path, *args, **kwargs):
            if path.name == "iOS":
                raise PermissionError("read-only volume")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", failing):
            outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("cannot create exportPath", outcome.reason)
        self.assertFalse((self.project / "Out").exists(), "the folders created before the failure are removed again")
        self.assertNotIn("temp.export", outcome.details)

    def test_missing_export_parents_are_created_inside_the_project_and_the_whole_chain_is_listed(self):
        self.write_profiles({"executeMethod": "M.B", "exportPath": "Builds/deeper/iOS", "configuration": "ReleaseForRunning"})

        def export(argv, call):
            (self.project / "Builds" / "deeper" / "iOS" / "Unity-iPhone.xcodeproj").mkdir(parents=True, exist_ok=True)
            return result()

        self.recorder.answers["unity"] = export
        self.recorder.answers["xcodebuild"] = self.make_app
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        self.assertEqual(str((self.project / "Builds").resolve()), outcome.details["temp.export"],
                         "the topmost created folder is removed, so no empty Builds/deeper is left behind")
        (self.project / "Builds2").mkdir()  # an existing parent is not listed: only the export itself is
        self.write_profiles({"executeMethod": "M.B", "exportPath": "Builds2/iOS", "configuration": "ReleaseForRunning"})

        def export2(argv, call):
            (self.project / "Builds2" / "iOS" / "Unity-iPhone.xcodeproj").mkdir(parents=True, exist_ok=True)
            return result()

        self.recorder.answers["unity"] = export2
        self.assertEqual(str((self.project / "Builds2" / "iOS").resolve()),
                         self.adapter.build(self.project, "Internal", self.out.parent.parent / "job2" / "build").details["temp.export"])

    def test_the_stage_timeout_caps_the_whole_build(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        self.adapter.bind_build_budget(600)
        self.write_profiles({"executeMethod": "M.B", "configuration": "ReleaseForRunning", "exportTimeoutSec": 5000})
        self.adapter.build(self.project, "Internal", self.out)
        self.assertLessEqual(self.recorder.named("unity")[0]["timeout"], 540)
        self.assertLessEqual(self.recorder.named("xcodebuild")[0]["timeout"], 540)

    def test_prepare_binds_the_application_and_refuses_clean_install(self):
        self.assertTrue(self.adapter.prepare(DEVICE, "com.example.game", "devdata").ok)
        local = self.work / "plan.json"
        local.write_text("{}", encoding="utf-8")
        self.assertTrue(self.adapter.push(DEVICE, local, "plan.json").ok, "prepare bound the application")
        outcome = self.adapter.prepare(DEVICE, "com.example.game", "clean-install")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("devicectl cannot delete", outcome.reason)

    def test_a_small_stage_timeout_is_never_exceeded_by_the_build_budget(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.answers["xcodebuild"] = self.make_app
        self.adapter.bind_build_budget(30)
        self.adapter.build(self.project, "Internal", self.out)
        self.assertLessEqual(self.recorder.named("unity")[0]["timeout"], 30)

    def test_terminate_waits_for_a_slow_process_to_leave_the_list(self):
        listings = iter([True, True, True, False])  # terminate lookup, then two lingering polls, then gone

        def processes(argv, call):
            alive = next(listings, False)
            document = {"result": {"runningProcesses": [{"processIdentifier": 77, "executable": "file:///var/App/Game.app/Game"}] if alive else []}}
            Path(argv[argv.index("--json-output") + 1]).write_text(json.dumps(document), encoding="utf-8")
            return result()

        self.recorder.answers["device info processes"] = processes
        self.assertTrue(self.adapter.terminate(DEVICE, "com.example.game").ok)

    def test_a_timed_out_push_is_not_retried_as_a_missing_folder(self):
        self.adapter.bind_application("com.example.game")
        local = self.work / "plan.json"
        local.write_text("{}", encoding="utf-8")
        self.recorder.answers["device copy to"] = result(constants.TIMEOUT, "No such file or directory", None, "timed out")
        self.assertEqual(constants.TIMEOUT, self.adapter.push(DEVICE, local, "gdv/inbox/plan.json").status)
        self.assertEqual(1, len(self.recorder.named("device copy to")))

    def test_no_connected_physical_device_blocks_the_build_for_device_destination(self):
        self.recorder.answers["unity"] = self.make_export
        self.recorder.json["list devices"] = {"result": {"devices": []}}
        outcome = self.adapter.build(self.project, "Internal", self.out)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertEqual([], self.recorder.named("xcodebuild"))

    # -- install, files, run control -------------------------------------------------------------------------------------

    def test_every_devicectl_call_is_bounded_in_time_and_carries_its_own_timeout(self):
        app = self.work / "Game.app"
        app.mkdir()
        self.adapter.bind_application("com.example.game")
        local = self.work / "plan.json"
        local.write_text("{}", encoding="utf-8")
        self.adapter.install(DEVICE, app)
        self.adapter.push(DEVICE, local, "gdv/inbox/plan.json")
        self.adapter.pull(DEVICE, "gdv/out", self.work / "pulled")
        self.adapter.screenshot(DEVICE, self.work / "shot.png")
        self.adapter.health(DEVICE)
        calls = [c for c in self.recorder.calls if c["argv"][:2] == ["xcrun", "devicectl"]]
        self.assertGreaterEqual(len(calls), 6)
        for call in calls:
            self.assertIn("--timeout", call["argv"], call["argv"])
            self.assertGreater(call["timeout"], 0)
            self.assertLessEqual(call["timeout"], ios.INSTALL_TIMEOUT_SEC)
        copy = self.recorder.named("device copy to")[0]
        self.assertLessEqual(copy["timeout"], ios.DEVICECTL_TIMEOUT_SEC, "a copy cannot hang a run")

    def test_install_refuses_something_that_is_not_a_built_app(self):
        outcome = self.adapter.install(DEVICE, self.work / "missing.app")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertEqual([], self.recorder.named("device install app"))

    def test_push_and_pull_address_the_app_container_documents_by_udid(self):
        self.adapter.bind_application("com.example.game")
        local = self.work / "plan.json"
        local.write_text("{}", encoding="utf-8")
        self.assertTrue(self.adapter.push(DEVICE, local, "gdv/inbox/plan.json").ok)
        argv = self.recorder.named("device copy to")[0]["argv"]
        self.assertEqual(UDID, argv[argv.index("--device") + 1])
        self.assertEqual(["appDataContainer", "com.example.game"], [argv[argv.index("--domain-type") + 1], argv[argv.index("--domain-identifier") + 1]])
        self.assertEqual("Documents/gdv/inbox/plan.json", argv[argv.index("--destination") + 1])
        self.assertTrue(self.adapter.pull(DEVICE, "gdv/out", self.work / "pulled").ok)
        pulled = self.recorder.named("device copy from")[0]["argv"]
        self.assertEqual("Documents/gdv/out", pulled[pulled.index("--source") + 1])

    def test_push_needs_the_application(self):
        self.assertEqual(constants.BLOCKED, self.adapter.push(DEVICE, self.work, "x").status)

    def test_a_push_into_a_missing_folder_retries_with_a_directory_tree(self):
        self.adapter.bind_application("com.example.game")
        local = self.work / "plan.json"
        local.write_text("{}", encoding="utf-8")
        trees = []

        def copy_to(argv, call):
            source = Path(argv[argv.index("--source") + 1])
            if source.is_file():
                return result(constants.FAIL, "Failed to retrieve the file node for Documents/gdv/inbox (error 7000)", 1, "exit code 1")
            trees.append(sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()))
            return result()

        self.recorder.answers["device copy to"] = copy_to
        outcome = self.adapter.push(DEVICE, local, "gdv/inbox/plan.json")
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual([["inbox/plan.json"]], trees)
        last = self.recorder.named("device copy to")[-1]["argv"]
        self.assertEqual("Documents/gdv", last[last.index("--destination") + 1])

    def test_pulling_a_folder_that_does_not_exist_yet_is_an_empty_pass(self):
        self.adapter.bind_application("com.example.game")
        self.recorder.answers["device copy from"] = result(constants.FAIL, "Failed to retrieve the file node for Documents/gdv/out", 1, "exit code 1")
        target = self.work / "pulled"
        self.assertTrue(self.adapter.pull(DEVICE, "gdv/out", target).ok)
        self.assertTrue(target.is_dir())

    def test_a_timed_out_copy_is_a_timeout_outcome(self):
        self.adapter.bind_application("com.example.game")
        self.recorder.answers["device copy from"] = result(constants.STALL, "", None, "no progress for 120 s")
        outcome = self.adapter.pull(DEVICE, "gdv/out", self.work / "pulled")
        self.assertEqual(constants.STALL, outcome.status)

    def test_launch_attaches_the_console_and_terminate_stops_it(self):
        log = self.work / "console.log"
        tree = FakeTree()
        spawned = []

        def spawn(argv, cwd, env, handle, grace=3.0):
            spawned.append(list(argv))
            handle.write(b"Launched application with com.example.game bundle identifier.\n")
            handle.flush()
            return FakeProcess(), tree

        adapter = ios.IosAdapter(run=self.recorder, spawn=spawn, os_name="macos", home=self.home, sleep=lambda _: None)
        outcome = adapter.launch(DEVICE, "com.example.game", [], log)
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(["--terminate-existing", "--console"], [a for a in spawned[0] if a.startswith("--") and a != "--device"])
        self.assertIn(UDID, spawned[0])
        self.recorder.json["device info processes"] = {"result": {"runningProcesses": []}}
        self.assertTrue(adapter.terminate(DEVICE, "com.example.game").ok)
        self.assertTrue(tree.killed, "terminate stops the console process tree")

    def test_launch_on_a_locked_device_needs_the_owner(self):
        log = self.work / "console.log"

        def spawn(argv, cwd, env, handle, grace=3.0):
            handle.write(b"ERROR: Unable to launch com.example.game because the device was not, or could not be, unlocked.\n")
            handle.flush()
            return FakeProcess(exit_code=1), FakeTree()

        adapter = ios.IosAdapter(run=self.recorder, spawn=spawn, os_name="macos", home=self.home, sleep=lambda _: None)
        outcome = adapter.launch(DEVICE, "com.example.game", [], log)
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)
        self.assertIn("unlock", outcome.reason)

    def test_health_reports_a_locked_or_missing_device(self):
        self.assertEqual((True, False), (self.adapter.health(DEVICE).reachable, self.adapter.health(DEVICE).locked))
        self.recorder.json["device info lockState"] = {"result": {"passcodeRequired": True}}
        self.assertTrue(self.adapter.health(DEVICE).locked)
        gone = DeviceInfo("ios:NOT-ATTACHED", "ios", "x", "physical")
        self.assertFalse(self.adapter.health(gone).reachable)

    def test_probe_on_a_windows_host_names_why_ios_is_blocked(self):
        windows = ios.IosAdapter(run=self.recorder, os_name="windows")
        capabilities = windows.probe()
        self.assertIn("macOS host", capabilities.blocked["ios"])
        self.assertEqual(constants.BLOCKED, windows.build(self.project, "Internal", self.out).status)

    def test_the_adapter_registers_itself_and_uses_the_gui_domain_without_its_own_slot_count(self):
        self.assertIn("ios", providers.platforms())
        self.assertEqual("gui", ios.IosAdapter.launchd_domain)
        self.assertFalse(hasattr(ios.IosAdapter, "unity_editor_slots"), "the slot total is the host's, see lease.unity_editor_capacity")

    def test_a_launchd_plist_escapes_xml_special_characters_in_arguments(self):
        argument = 'a&b<c>"d\' $HOME'
        data = jobs.launchd_plist(["/bin/echo", argument], Path("/tmp/job"), Path("/tmp/job/runner.log"), "com.valtrion.x")
        self.assertEqual(["/bin/echo", argument], plistlib.loads(data)["ProgramArguments"])


class SettingsTests(unittest.TestCase):
    def test_class_and_device_parsing(self):
        self.assertEqual("ipad-a14", ios.derive_class("iPad13,19", ""))
        self.assertEqual("iphone16-2", ios.derive_class("iPhone16,2", ""))
        self.assertEqual("DEV", ios.udid_of(DeviceInfo("ios:DEV", "ios", "c", "physical")))
        self.assertEqual(("sign", constants.NEEDS_PERMISSION), ios.classify_xcodebuild_failure("User interaction is not allowed."))
        self.assertEqual("nativeBuild", ios.classify_xcodebuild_failure("error: compile failed")[0])


if __name__ == "__main__":
    unittest.main()
