import json
import os
import runpy
from contextlib import contextmanager
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import constants, core, jobs, lease, providers, supervise
from valtrion.providers import android
from valtrion.providers.android import AndroidAdapter
from valtrion.adapter import StepOutcome
from valtrion.schema import Validator

HERE = Path(__file__).resolve().parent
FAKE_ADB = HERE / "fake_adb.py"
SHA = "d" * 40
PACKAGE = "com.example.app"
PHONE_PROPS = {"ro.product.model": "moto g power 5G - 2024", "ro.build.version.release": "15", "ro.product.cpu.abi": "arm64-v8a",
               "ro.hardware": "mt6855"}
EMULATOR_PROPS = {"ro.product.model": "sdk_gphone64_x86_64", "ro.build.version.release": "17", "ro.product.cpu.abi": "x86_64",
                  "ro.kernel.qemu": "1"}

FAKE_UNITY = """
import os, sys
args = sys.argv[1:]
output = args[args.index('-buildOutput') + 1]
open(args[args.index('-logFile') + 1], 'a').write('building\\n')
mode = os.environ.get('FAKE_UNITY_MODE', 'ok')
if mode == 'hang':
    import time
    time.sleep(120)
if mode == 'fail':
    sys.exit(1)
if mode == 'ok':
    open(output, 'wb').write(b'apk:' + ' '.join(args).encode())
"""


class AndroidTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.state = self.base / "device"
        self.state.mkdir()
        patcher = mock.patch.dict(os.environ, {"FAKE_ADB_STATE": str(self.state), core.HOME_ENV: str(self.base / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_state(devices=[{"serial": "ZY22", "state": "device", "model": "moto_g_power_5G___2024", "props": PHONE_PROPS}])
        self.adapter = AndroidAdapter([sys.executable, str(FAKE_ADB)], sleep=lambda _: None, unity_editors=lambda: [])
        self.addCleanup(self.adapter._stop_logcat, "ZY22")  # noqa: SLF001 - never leave a fake logcat behind

    def set_state(self, **values):
        path = self.state / "state.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"package": PACKAGE}
        state.update(values)
        core.atomic_write_json(path, state)

    def read_state(self):
        return json.loads((self.state / "state.json").read_text(encoding="utf-8"))

    def calls(self):
        path = self.state / "calls.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def device(self):
        return next(d for d in self.adapter.discover() if d.id == "android:ZY22")

    def device_file(self, relative):
        return self.state / "sdcard" / "Android" / "data" / PACKAGE / "files" / relative


class FixtureStateTests(AndroidTestCase):
    def test_concurrent_readers_never_observe_partial_json_from_either_writer(self):
        fake = runpy.run_path(str(FAKE_ADB))
        real_open = open
        for generation, publish in enumerate((lambda: self.set_state(generation=1),
                                             lambda: fake["save"]({"package": PACKAGE, "generation": 2})), start=1):
            with self.subTest(generation=generation):
                before = self.read_state()
                opened = threading.Event()
                release = threading.Event()
                failures = []

                @contextmanager
                def pause_writer(file, mode="r", *args, **kwargs):
                    with real_open(file, mode, *args, **kwargs) as stream:
                        if "w" in mode:
                            opened.set()
                            if not release.wait(5):
                                raise TimeoutError("reader did not release the staged writer")
                        yield stream

                def write_state():
                    try:
                        publish()
                    except Exception as error:
                        failures.append(error)

                # Pause at the file-write boundary: a reader runs while output is
                # open but incomplete, before replacement can cause Windows sharing errors.
                with mock.patch("builtins.open", pause_writer), mock.patch("io.open", pause_writer):
                    writer = threading.Thread(target=write_state)
                    writer.start()
                    try:
                        self.assertTrue(opened.wait(5), "the state writer must reach its write boundary")
                        self.assertEqual(before, self.read_state(), "readers must retain the previous complete document")
                    finally:
                        release.set()
                        writer.join(timeout=5)
                self.assertFalse(writer.is_alive())
                self.assertEqual([], failures)
                self.assertEqual(generation, self.read_state()["generation"])


class DiscoveryTests(AndroidTestCase):
    def test_a_physical_phone_is_described_with_a_stable_class(self):
        device = self.device()
        self.assertEqual(("android", "android-moto-g-power-5g-2024", "physical"), (device.platform, device.device_class, device.evidence))
        self.assertEqual(("moto g power 5G - 2024", "Android 15", 15), (device.model, device.os_version, device.os_major))
        self.assertIn("touch", device.capabilities)
        self.assertIn("abi.arm64-v8a", device.capabilities)

    def test_an_emulator_is_labelled_emulator(self):
        self.set_state(devices=[{"serial": "emulator-5554", "state": "device", "props": EMULATOR_PROPS}])
        (device,) = self.adapter.discover()
        self.assertEqual(("android-emulator", constants.EVIDENCE_EMULATOR), (device.device_class, device.evidence))

    def test_an_unauthorized_phone_is_listed_as_waiting_for_the_owner(self):
        self.set_state(devices=[{"serial": "ZY22", "state": "unauthorized", "props": {}}])
        (device,) = self.adapter.discover()
        self.assertEqual("android-unknown", device.device_class, "real adb prints no model for an unauthorized device")
        self.assertIn("adb.unauthorized", device.capabilities)
        self.assertIn("needs-permission", device.capabilities)

    def test_a_notice_before_the_device_list_is_not_a_device(self):
        self.set_state(mismatch_notice=True)
        self.assertEqual(["android:ZY22"], [d.id for d in self.adapter.discover()])
        self.assertEqual([], android.parse_devices("adb server is out of date.  killing...\nList of devices attached\nabc\tweird\n"))

    def test_other_emulators_and_a_device_that_does_not_answer_are_not_physical_evidence(self):
        for props in ({"ro.boot.qemu": "1"}, {"ro.hardware": "vbox86"}, {"ro.build.characteristics": "emulator,nosdcard"},
                      {"ro.product.manufacturer": "Genymotion"}):
            self.set_state(devices=[{"serial": "ZY22", "state": "device", "props": {**PHONE_PROPS, **props}}])
            self.assertEqual(constants.EVIDENCE_EMULATOR, self.adapter.discover()[0].evidence, props)
        self.set_state(devices=[{"serial": "127.0.0.1:5555", "state": "device", "props": PHONE_PROPS}])
        self.assertEqual(constants.EVIDENCE_EMULATOR, self.adapter.discover()[0].evidence)
        self.set_state(devices=[{"serial": "ZY22", "state": "device", "props": {}}])
        self.assertEqual(constants.EVIDENCE_HOST, self.adapter.discover()[0].evidence)

    def test_a_restarted_adb_server_is_retried_not_reported_as_a_failure(self):
        self.set_state(down=2)
        self.assertEqual("android:ZY22", self.device().id[:12])
        self.assertEqual(0, self.read_state()["down"])
        starts = [c for c in self.calls() if c.startswith("start-server")]
        self.assertGreaterEqual(len(starts), 3, "each retry starts the server again outside the supervised call")

    def test_cuttlefish_and_virtual_device_serials_and_products_are_emulators(self):
        for serial, props in (("localhost:6520", PHONE_PROPS), ("0.0.0.0:6520", PHONE_PROPS),
                              ("ZY22", {**PHONE_PROPS, "ro.hardware": "cutf_cvm"}),
                              ("ZY22", {**PHONE_PROPS, "ro.product.device": "emu64xa"}),
                              ("ZY22", {**PHONE_PROPS, "ro.product.name": "sdk_gphone64_arm64"})):
            self.set_state(devices=[{"serial": serial, "state": "device", "props": props}])
            self.assertEqual(constants.EVIDENCE_EMULATOR, self.adapter.discover()[0].evidence, (serial, props))
        self.set_state(devices=[{"serial": "192.0.2.20:5555", "state": "device", "props": PHONE_PROPS}])
        self.assertEqual(constants.EVIDENCE_PHYSICAL, self.adapter.discover()[0].evidence, "a phone on the LAN is physical")

    def test_no_adb_means_nothing_is_attached_and_probe_says_why(self):
        with mock.patch.object(android, "find_adb", return_value=None):
            adapter = AndroidAdapter(unity_editors=lambda: [])
            self.assertEqual([], list(adapter.discover()))
            capabilities = adapter.probe()
        self.assertIn("adb", capabilities.blocked)
        self.assertIn("build", capabilities.blocked)
        self.assertFalse(capabilities.can("adb"))

    def test_probe_reports_adb_and_the_android_module(self):
        adapter = AndroidAdapter([sys.executable, str(FAKE_ADB)], unity_editors=lambda: [{"version": "6000.6.7f1", "modules": ["android"]}])
        capabilities = adapter.probe()
        self.assertTrue(capabilities.can("adb") and capabilities.can("build"), capabilities)

    def test_adb_is_found_in_the_unity_android_module(self):
        editors = [{"version": "6000.6.7f1", "path": "u", "modules": ["android"], "adb": "/opt/unity/adb"}]
        with mock.patch("shutil.which", return_value=None), mock.patch.object(android.doctor, "find_editors", return_value=editors):
            self.assertEqual(["/opt/unity/adb"], android.find_adb({}, extra_dirs=[]))
        self.assertEqual(["custom"], android.find_adb({"GDV_ADB": "custom"}))

    def test_the_registry_accepts_the_discovered_device(self):
        from valtrion import config
        registry = config.merge_discovered({"schema": "gdv.registry/1", "host": {"id": "h", "os": "windows", "roles": []}, "devices": []},
                                           self.adapter.discover(), ["android"], "h")
        self.assertEqual([], Validator().validate(registry, "gdv.registry/1"))


class HealthTests(AndroidTestCase):
    def test_battery_thermal_and_space_are_recorded(self):
        health = self.adapter.health(self.device())
        self.assertEqual((True, False, "none", 77, 78125), (health.reachable, health.locked, health.thermal_state, health.battery_percent,
                                                            health.free_space_mb))
        self.assertTrue(any("30.1" in note for note in health.notes))

    def test_unauthorized_asks_the_owner_to_allow_debugging(self):
        self.set_state(devices=[{"serial": "ZY22", "state": "unauthorized", "model": "m", "props": {}}])
        health = self.adapter.health(self.device())
        self.assertFalse(health.reachable)
        self.assertIn("Allow USB debugging", health.permission_needed)

    def test_offline_is_a_permission_problem_too(self):
        self.set_state(devices=[{"serial": "ZY22", "state": "offline", "model": "m", "props": {}}])
        self.assertIn("offline", self.adapter.health(self.device()).permission_needed)

    def test_offline_right_after_a_server_restart_settles_before_it_is_reported(self):
        self.set_state(devices=[{"serial": "ZY22", "state": "device", "model": "m", "props": PHONE_PROPS, "offline_polls": 2}])
        health = self.adapter.health(self.adapter._describe({"serial": "ZY22", "state": "device", "model": "m"}))  # noqa: SLF001
        self.assertTrue(health.reachable, health)
        self.assertIsNone(health.permission_needed)

    def test_a_phone_stuck_authorizing_asks_the_owner(self):
        self.set_state(devices=[{"serial": "ZY22", "state": "authorizing", "props": {}}])
        health = self.adapter.health(self.adapter._describe({"serial": "ZY22", "state": "authorizing", "model": ""}))  # noqa: SLF001
        self.assertIn("Allow USB debugging", health.permission_needed)

    def test_the_adb_server_is_started_once_outside_the_supervised_calls(self):
        self.adapter.discover()
        self.adapter.discover()
        starts = [c for c in self.calls() if c.startswith("start-server")]
        self.assertEqual(1, len(starts))
        self.assertTrue(self.calls()[0].startswith("start-server"))

    def test_an_unreadable_keyguard_is_unknown_not_unlocked(self):
        self.set_state(devices=[{"serial": "ZY22", "state": "device", "model": "m", "props": PHONE_PROPS, "no_keyguard_keys": True}])
        self.assertIsNone(self.adapter.health(self.device()).locked)

    def test_a_detached_phone_is_unreachable_not_a_permission_problem(self):
        device = self.device()
        self.set_state(devices=[])
        health = self.adapter.health(device)
        self.assertFalse(health.reachable)
        self.assertIsNone(health.permission_needed)

    def test_a_secure_lock_stays_locked_and_an_insecure_keyguard_is_dismissed(self):
        self.set_state(devices=[{"serial": "ZY22", "state": "device", "model": "m", "props": PHONE_PROPS, "locked": True}])
        self.assertTrue(self.adapter.health(self.device()).locked)
        self.set_state(devices=[{"serial": "ZY22", "state": "device", "model": "m", "props": PHONE_PROPS, "locked": True, "insecure": True}])
        self.assertFalse(self.adapter.health(self.device()).locked)


class DeviceOperationTests(AndroidTestCase):
    def apk(self):
        path = self.base / "app.apk"
        path.write_bytes(b"apk")
        return path

    def test_install_reports_success_and_asks_nothing_destructive(self):
        outcome = self.adapter.install(self.device(), self.apk())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(1, len([c for c in self.calls() if "install -r" in c]))
        self.assertFalse([c for c in self.calls() if "uninstall" in c or "pm clear" in c])

    def test_install_on_an_unauthorized_phone_needs_permission(self):
        device = self.device()
        self.set_state(devices=[{"serial": "ZY22", "state": "unauthorized", "model": "m", "props": {}}])
        outcome = self.adapter.install(device, self.apk())
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)
        self.assertIn("Allow USB debugging", outcome.reason)

    def test_a_phone_that_refuses_the_install_asks_the_owner(self):
        self.set_state(install_error="INSTALL_FAILED_USER_RESTRICTED")
        outcome = self.adapter.install(self.device(), self.apk())
        self.assertEqual(constants.NEEDS_PERMISSION, outcome.status)
        self.assertIn("Install via USB", outcome.reason)

    def test_a_path_that_contains_the_word_offline_is_not_a_device_state(self):
        outcome = self.adapter.classify(supervise.BoundedResult(constants.FAIL, "exit code 1", 1, r"adb: failed to stat D:\offline-buildpp.apk"),
                                        "ZY22", "adb install")
        self.assertEqual(constants.FAIL, outcome.status)

    def test_an_old_adb_that_exits_zero_after_a_failure_is_still_a_failure(self):
        reply = supervise.BoundedResult(constants.PASS, None, 0, "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]")
        device, apk = self.device(), self.apk()
        with mock.patch.object(self.adapter, "_adb", return_value=reply), mock.patch.object(self.adapter, "_ready", return_value=StepOutcome(constants.PASS)):
            outcome = self.adapter.install(device, apk)
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertIn("the owner decides", outcome.reason)

    def test_launch_arguments_reach_the_device_shell_quoted(self):
        self.set_state(installed=[PACKAGE])
        self.adapter.prepare(self.device(), PACKAGE + "/Outer$Inner", None)
        self.adapter.launch(self.device(), PACKAGE, ["--es", "x", "a; touch /sdcard/pwn $HOME"], self.base / "q" / "c.log")
        start = next(c for c in self.calls() if "'am' 'start'" in c)
        self.assertIn("'a; touch /sdcard/pwn $HOME'", start)
        self.assertIn(f"'{PACKAGE}/Outer$Inner'", start, "$ in a class name is not expanded by the device shell")

    def test_a_device_path_with_shell_characters_is_refused(self):
        self.adapter.prepare(self.device(), PACKAGE, None)
        for bad in ("/sdcard/a'b", "gdv/$(id)", "gdv/a b", "/sdcard/../data"):
            self.assertEqual(constants.BLOCKED, self.adapter.pull(self.device(), bad, self.base / "x").status, bad)

    def test_a_full_disk_while_saving_diagnostics_still_stops_logcat(self):
        self.set_state(installed=[PACKAGE])
        self.adapter.prepare(self.device(), PACKAGE, None)
        self.assertTrue(self.adapter.launch(self.device(), PACKAGE, [], self.base / "d" / "console.log").ok)
        with mock.patch.object(android.core, "atomic_write_text", side_effect=OSError("disk full")):
            outcome = self.adapter.terminate(self.device(), PACKAGE)
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual({}, self.adapter._sessions)  # noqa: SLF001

    def test_an_emulator_that_does_not_answer_getprop_is_still_labelled_emulator(self):
        self.set_state(devices=[{"serial": "emulator-5554", "state": "device", "props": {}}])
        self.assertEqual((constants.EVIDENCE_EMULATOR, "android-emulator"), (lambda d: (d.evidence, d.device_class))(self.adapter.discover()[0]))
        self.set_state(devices=[{"serial": "127.0.0.1:5555", "state": "unauthorized", "props": {}}])
        self.assertEqual(constants.EVIDENCE_EMULATOR, self.adapter.discover()[0].evidence)

    def test_a_capture_that_ended_before_the_run_is_noted_and_a_late_launch_cannot_restart_one(self):
        self.set_state(installed=[PACKAGE], no_logcat=True)
        self.adapter.prepare(self.device(), PACKAGE, None)
        console = self.base / "e" / "console.log"
        self.assertTrue(self.adapter.launch(self.device(), PACKAGE, [], console).ok)
        self.wait_for(lambda: all(p.poll() is not None for p, _ in self.adapter._sessions["ZY22"].trees))  # noqa: SLF001
        self.assertTrue(self.adapter.terminate(self.device(), PACKAGE).ok)
        self.assertIn("incomplete", (console.parent / "android" / "notes.txt").read_text(encoding="utf-8"))
        late = self.adapter._start_logcat("ZY22", self.base / "late.log", self.base / "late-full.log")  # noqa: SLF001
        self.assertEqual(constants.BLOCKED, late.status, "a launch that finishes after terminate must not leave a capture running")
        self.assertEqual({}, self.adapter._sessions)  # noqa: SLF001

    def test_the_activity_of_the_build_profile_survives_binding_the_package(self):
        self.adapter._app = (PACKAGE, "com.x.Main")  # noqa: SLF001 - what a build leaves behind
        self.adapter.prepare(self.device(), PACKAGE, None)
        self.assertEqual((PACKAGE, "com.x.Main"), self.adapter._app)  # noqa: SLF001

    def test_install_without_an_apk_is_blocked_and_a_signature_conflict_is_left_to_the_owner(self):
        self.assertEqual(constants.BLOCKED, self.adapter.install(self.device(), self.base / "missing.apk").status)
        self.set_state(install_error="INSTALL_FAILED_UPDATE_INCOMPATIBLE")
        outcome = self.adapter.install(self.device(), self.apk())
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertIn("the owner decides", outcome.reason)
        self.assertFalse([c for c in self.calls() if "uninstall" in c])

    def test_push_and_pull_use_the_application_data_root(self):
        device = self.device()
        self.assertEqual(constants.BLOCKED, self.adapter.push(device, self.apk(), "gdv/inbox/plan.json").status, "no application is bound yet")
        self.assertTrue(self.adapter.prepare(device, PACKAGE, "devdata").ok)
        plan = self.base / "plan.json"
        plan.write_text("{}", encoding="utf-8")
        self.assertTrue(self.adapter.push(device, plan, "gdv/inbox/plan.json").ok)
        self.assertEqual("{}", self.device_file("gdv/inbox/plan.json").read_text(encoding="utf-8"))
        opened = next(c for c in self.calls() if "chmod 2777" in c)
        self.assertIn(f"/files/gdv'", opened, "the application can only write into folders opened by the host")
        self.assertIn(f"/files/gdv/inbox'", opened)
        out = self.device_file("gdv/out/run-1/result.json")
        out.parent.mkdir(parents=True)
        out.write_text('{"status": "PASS"}', encoding="utf-8")
        pulled = self.base / "pulled"
        self.assertTrue(self.adapter.pull(device, "gdv/out", pulled).ok)
        self.assertEqual('{"status": "PASS"}', (pulled / "run-1" / "result.json").read_text(encoding="utf-8"))

    def test_pulling_a_folder_that_does_not_exist_yet_is_an_empty_pass(self):
        self.adapter.prepare(self.device(), PACKAGE, None)
        pulled = self.base / "empty"
        outcome = self.adapter.pull(self.device(), "gdv/out", pulled)
        self.assertTrue(outcome.ok)
        self.assertEqual("true", outcome.details["absent"])
        self.assertEqual([], list(pulled.iterdir()))

    def test_a_path_cannot_leave_the_data_root(self):
        self.adapter.prepare(self.device(), PACKAGE, None)
        self.assertEqual(constants.BLOCKED, self.adapter.pull(self.device(), "../../other", self.base / "x").status)

    def test_only_a_clean_install_plan_clears_application_data(self):
        device = self.device()
        self.set_state(installed=[PACKAGE])
        self.device_file("keep.txt").parent.mkdir(parents=True)
        self.device_file("keep.txt").write_text("user data", encoding="utf-8")
        for policy in ("devdata", "isolated", None):
            self.assertTrue(self.adapter.prepare(device, PACKAGE, policy).ok)
        self.assertFalse([c for c in self.calls() if "pm clear" in c])
        self.assertTrue(self.device_file("keep.txt").exists())
        self.assertTrue(self.adapter.prepare(device, PACKAGE, "clean-install").ok)
        self.assertEqual(1, len([c for c in self.calls() if "pm clear" in c]))
        self.assertFalse(self.device_file("keep.txt").exists())

    def test_a_clean_install_of_an_application_that_is_not_installed_clears_nothing(self):
        self.assertTrue(self.adapter.prepare(self.device(), PACKAGE, "clean-install").ok)
        self.assertFalse([c for c in self.calls() if "pm clear" in c])

    def test_an_invalid_application_name_is_rejected_before_it_reaches_a_shell(self):
        self.assertEqual(constants.BLOCKED, self.adapter.prepare(self.device(), "com.x; rm -rf /", None).status)
        self.assertEqual(constants.BLOCKED, self.adapter.launch(self.device(), "bad name", [], self.base / "c.log").status)

    def test_launch_captures_logcat_and_terminate_saves_diagnostics_and_stops_everything(self):
        device = self.device()
        self.set_state(installed=[PACKAGE], logcat=["hello", "GDV-RESULT FAIL run=r1 broken"])
        self.adapter.prepare(device, PACKAGE, None)
        console = self.base / "run" / "console.log"
        launched = self.adapter.launch(device, PACKAGE, ["--ez", "extra", "true"], console)
        self.assertTrue(launched.ok, launched.reason)
        start = next(c for c in self.calls() if "'am' 'start'" in c)
        self.assertIn(f"'-n' '{PACKAGE}/com.unity3d.player.UnityPlayerGameActivity'", start)
        self.assertIn(f"'--es' 'gdv-plan' '/sdcard/Android/data/{PACKAGE}/files/gdv/inbox/plan.json'", start)
        self.assertIn("'--ez' 'extra' 'true'", start)
        self.wait_for(lambda: supervise.read_marker(console) is not None)
        self.assertEqual(("FAIL", "run=r1 broken"), supervise.read_marker(console), "raw logcat keeps the terminal line at a line start")
        self.assertTrue(self.adapter.terminate(device, PACKAGE).ok)
        diagnostics = console.parent / "android"
        for name in ("meminfo.txt", "gfxinfo.txt", "health-before.json", "health-after.json", "logcat.log"):
            self.assertTrue((diagnostics / name).exists(), name)
        self.assertIn("Unity", (diagnostics / "logcat.log").read_text(encoding="utf-8"))
        self.assertTrue(any("force-stop" in c for c in self.calls()))
        self.assertEqual([], self.read_state()["running"])
        self.assertEqual({}, self.adapter._sessions)  # noqa: SLF001

    def test_a_launch_the_system_rejects_is_a_failure_with_the_system_message(self):
        self.set_state(installed=[PACKAGE], launch_error=True)
        outcome = self.adapter.launch(self.device(), PACKAGE + "/.Missing", [], self.base / "c.log")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertIn("Activity class does not exist", outcome.reason)
        self.assertEqual({}, self.adapter._sessions)  # noqa: SLF001 - logcat is stopped again

    def test_launch_on_a_locked_out_phone_needs_permission(self):
        device = self.device()
        self.set_state(devices=[{"serial": "ZY22", "state": "offline", "model": "m", "props": {}}])
        self.assertEqual(constants.NEEDS_PERMISSION, self.adapter.launch(device, PACKAGE, [], self.base / "c.log").status)

    def test_screenshot_is_saved_and_the_scratch_file_removed(self):
        out = self.base / "shots" / "failure.png"
        self.assertTrue(self.adapter.screenshot(self.device(), out).ok)
        self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual([], list((self.state / "tmp").iterdir()))

    @staticmethod
    def wait_for(condition, seconds=10.0):
        import time
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.05)
        raise AssertionError("condition not met in time")


class BuildTests(AndroidTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.base / "project"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.project / "validation" / "device").mkdir(parents=True)
        self.script = self.base / "fake_unity.py"
        self.script.write_text(FAKE_UNITY, encoding="utf-8")
        self.profile = {"executeMethod": "Company.Build.Android", "scriptingBackend": "Mono", "package": PACKAGE, "stallSec": 5, "timeoutSec": 60}
        self.write_profiles({"Validation": {"android": self.profile}})
        self.adapter = AndroidAdapter(
            [sys.executable, str(FAKE_ADB)], sleep=lambda _: None, unity_command=lambda editor: [sys.executable, str(self.script)],
            unity_editors=lambda: [{"version": "6000.6.7f1", "path": "unused", "modules": ["android"]}])

    def write_profiles(self, document):
        (self.project / "validation" / "device" / "profiles.json").write_text(json.dumps({"profiles": document}), encoding="utf-8")

    def test_a_build_runs_the_consumer_hook_and_returns_the_apk(self):
        out = self.base / "out"
        outcome = self.adapter.build(self.project, "Validation", out)
        self.assertTrue(outcome.ok, outcome.reason)
        (apk,) = outcome.artifacts
        arguments = apk.read_bytes().decode()
        for expected in ("-buildTarget Android", "-executeMethod Company.Build.Android", "-gdvProfile Validation", "-gdvBackend Mono",
                         f"-buildOutput {apk}", "-batchmode"):
            self.assertIn(expected, arguments)
        self.assertEqual("PASS", outcome.details["layer.nativeBuild"])

    def test_the_adb_server_outlives_a_build_install_launch_terminate_sequence(self):
        """A server started by `adb start-server` stays up while the supervised calls (each ends by killing its process tree) run
        and finish; the sequence is the one `run --build` does in one job."""
        self.set_state(daemon=True, installed=[PACKAGE])
        pid_file = self.state / "server.pid"

        def servers():
            return [int(line) for line in pid_file.read_text(encoding="utf-8").split()] if pid_file.exists() else []

        def alive(pid):
            if not core.process_alive(pid):
                return False
            if os.name == "nt":
                return True
            # A killed process nobody has reaped yet still has a pid: it is a zombie, not a server.
            state = ""
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
                    state = handle.read().rsplit(")", 1)[-1].split()[0]  # the field after the command name, which may hold spaces
            except (OSError, IndexError):
                try:  # no /proc (macOS)
                    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
                except OSError:
                    return True  # the state cannot be read: trust core.process_alive
            return not state.startswith("Z")

        def stop_servers():
            for pid in servers():
                if alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
            deadline = time.monotonic() + 10
            while any(alive(pid) for pid in servers()) and time.monotonic() < deadline:
                time.sleep(0.05)

        self.addCleanup(stop_servers)
        outcome = self.adapter.build(self.project, "Validation", self.base / "out")
        self.assertTrue(outcome.ok, outcome.reason)
        (apk,) = outcome.artifacts
        device = self.device()
        self.assertTrue(self.adapter.install(device, apk).ok)
        self.assertEqual(1, len(servers()), "the server is started once, by the first adb call")
        (server,) = servers()
        self.assertTrue(alive(server))
        self.assertTrue(self.adapter.prepare(device, PACKAGE, None).ok)
        self.assertTrue(self.adapter.launch(device, PACKAGE, [], self.base / "run" / "console.log").ok)
        self.assertTrue(alive(server), "supervised calls ended and killed their trees, not the server")
        self.assertTrue(self.adapter.terminate(device, PACKAGE).ok)
        self.assertTrue(alive(server), "terminate stops logcat and the app, not the server")
        self.assertEqual([server], servers(), "no call restarted the server")
        stop_servers()
        self.assertFalse(alive(server))

    def test_a_performance_profile_builds_with_il2cpp(self):
        self.profile["scriptingBackend"] = "IL2CPP"
        self.write_profiles({"Perf": {"android": self.profile}})
        (apk,) = self.adapter.build(self.project, "Perf", self.base / "out").artifacts
        self.assertIn("-gdvBackend IL2CPP", apk.read_bytes().decode())

    def test_missing_pieces_are_blocked_with_the_reason(self):
        self.assertEqual(constants.BLOCKED, self.adapter.build(self.project, "Nope", self.base / "o1").status)
        (self.project / "validation" / "device" / "profiles.json").write_text("{", encoding="utf-8")
        self.assertIn("not valid JSON", self.adapter.build(self.project, "Validation", self.base / "o2").reason)
        (self.project / "validation" / "device" / "profiles.json").unlink()
        self.assertIn("no build profiles", self.adapter.build(self.project, "Validation", self.base / "o3").reason)

    def test_an_unsuitable_editor_is_blocked(self):
        for editors, text in (([], "not installed"), ([{"version": "6000.6.7f1", "path": "u", "modules": ["ios"]}], "no Android module")):
            adapter = AndroidAdapter([sys.executable, str(FAKE_ADB)], unity_editors=lambda e=editors: e)
            outcome = adapter.build(self.project, "Validation", self.base / "o")
            self.assertEqual(constants.BLOCKED, outcome.status)
            self.assertIn(text, outcome.reason)

    def test_a_failed_build_is_a_failure_and_a_hook_that_writes_nothing_is_too(self):
        for mode, expected in (("fail", "exit code 1"), ("nowrite", "wrote no")):
            with mock.patch.dict(os.environ, {"FAKE_UNITY_MODE": mode}):
                outcome = self.adapter.build(self.project, "Validation", self.base / mode)
            self.assertEqual(constants.FAIL, outcome.status)
            self.assertIn(expected, outcome.reason)
            self.assertEqual((), outcome.artifacts)
            self.assertTrue((self.base / "build-logs" / "unity-build.log").exists(), "the log survives the removal of the build folder")

    def test_a_silent_unity_is_a_stall_and_its_process_is_killed(self):
        with mock.patch.dict(os.environ, {"FAKE_UNITY_MODE": "hang"}):
            outcome = self.adapter.build(self.project, "Validation", self.base / "hang")
        self.assertEqual(constants.STALL, outcome.status)

    def test_an_invalid_package_or_backend_is_blocked_before_unity_starts(self):
        self.profile["package"] = "not a package"
        self.write_profiles({"Validation": {"android": self.profile}})
        self.assertEqual(constants.BLOCKED, self.adapter.build(self.project, "Validation", self.base / "o").status)
        self.profile.update(package=PACKAGE, scriptingBackend="Clang")
        self.write_profiles({"Validation": {"android": self.profile}})
        self.assertEqual(constants.BLOCKED, self.adapter.build(self.project, "Validation", self.base / "o").status)


PLAN = {"schema": "gdv.plan/1", "id": "smoke.android", "kind": "smoke", "requires": {"platforms": ["android"], "capabilities": ["touch"]},
        "dataPolicy": "devdata", "budgetSeconds": 60, "steps": [{"type": "wait", "timeoutSec": 5}], "metrics": {}}
RESULT = {"status": "PASS", "reason": None, "steps": [{"type": "wait", "status": "PASS", "startMs": 0, "durationMs": 5}], "metrics": [],
          "unverified": []}


class PipelineTests(AndroidTestCase):
    """The adapter driven by the real pipeline against the fake device."""

    def setUp(self):
        super().setUp()
        self.root, self.work = self.base / "artifacts", self.base / "work"
        (self.work / "project" / "ProjectSettings").mkdir(parents=True)
        (self.work / "project" / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.work / "plans").mkdir()
        self.apk = self.work / "app.apk"
        self.apk.write_bytes(b"apk")
        self.write_plan(PLAN)
        self.set_state(installed=[PACKAGE], logcat=["booting"], on_launch={
            "gdv/out/r1/heartbeat.json": {"seq": 1}, "gdv/out/r1/result.json": RESULT})
        providers.register("android", lambda: self.adapter)
        self.addCleanup(providers.unregister, "android")

    def write_plan(self, plan):
        (self.work / "plans" / "smoke.plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def run_pipeline(self, run_args=None, device=None):
        stages = [{"stage": "install", "args": {"artifact": str(self.apk)}},
                  {"stage": "run", "plan": "smoke.plan.json", "timeoutSec": 30,
                   "args": {"app": PACKAGE, "stallSec": 1, **(run_args or {})}}]
        document = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": SHA, "profile": "Validation"},
                    "device": device or {"platform": "android"}, "stages": stages}
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "projectDir": str(self.work / "project"),
                   "baseDir": str(self.work / "plans"), "devicePollSec": 0.05}
        request = jobs.JobRequest(kind="pipeline", project="Sample", sha=SHA, label="p1", payload=payload,
                                  host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=120, owner="tester")
        info = jobs.start_job(request, self.root, self.base / "home", foreground=True)
        store = jobs.JobStore(Path(info["dir"]))
        result = json.loads(store.result.read_text(encoding="utf-8")) if store.result.exists() else None
        if result is not None:
            self.assertEqual([], Validator().validate(result, "gdv.result/1"))
        return store, result

    def test_the_smoke_plan_passes_with_physical_evidence_and_leaves_nothing_running(self):
        store, result = self.run_pipeline()
        self.assertEqual("PASS", store.read_job()["status"], store.read_job().get("reason"))
        self.assertEqual(("physical", "android-moto-g-power-5g-2024", 15), (result["target"]["evidence"], result["target"]["deviceClass"],
                                                                         result["target"]["osMajor"]))
        paths = {a["path"] for a in result["artifacts"]}
        self.assertTrue(any(p.endswith("android/logcat.log") for p in paths), paths)
        self.assertTrue(any(p.endswith("android/meminfo.txt") for p in paths), paths)
        self.assertEqual([], self.read_state()["running"])
        self.assertEqual("free", lease.LeaseManager(self.base / "home").status("device:android:ZY22").state)
        self.assertEqual({}, self.adapter._sessions)  # noqa: SLF001

    def test_an_app_that_dies_mid_run_is_a_stall_with_logcat_and_a_screenshot_saved(self):
        self.set_state(on_launch={"gdv/out/r1/heartbeat.json": {"seq": 1}}, logcat=["Unity crash marker"])
        store, result = self.run_pipeline()
        job = store.read_job()
        self.assertEqual("STALL", job["status"], job.get("reason"))
        self.assertIn("heartbeat did not advance", job["reason"])
        paths = [a["path"] for a in result["artifacts"]]
        self.assertTrue(any(p.endswith("android/logcat.log") for p in paths), paths)
        self.assertTrue(any(a["kind"] == "screenshot" for a in result["artifacts"]), result["artifacts"])
        console = next(store.directory.glob("runs/*/console.log"))
        self.assertIn("Unity crash marker", console.read_text(encoding="utf-8"))
        self.assertEqual([], self.read_state()["running"], "the application is force-stopped")

    def test_the_applications_own_failure_line_ends_the_run_without_waiting_for_the_stall_window(self):
        self.set_state(on_launch={"gdv/out/r1/heartbeat.json": {"seq": 1}}, logcat=["GDV-RESULT FAIL run=r1 assertion"])
        store, _ = self.run_pipeline(run_args={"stallSec": 20})
        self.assertEqual("FAIL", store.read_job()["status"])

    def test_a_second_request_for_the_phone_is_blocked_naming_the_holder(self):
        lease.LeaseManager(self.base / "home").acquire("device:android:ZY22", "alice", "session-7", 600, "series", os.getpid())
        store, _ = self.run_pipeline()
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("alice", job["reason"])
        self.assertFalse([c for c in self.calls() if "'am' 'start'" in c])

    def test_an_unplugged_phone_is_blocked_and_an_unauthorized_one_needs_permission(self):
        self.set_state(devices=[])
        store, _ = self.run_pipeline()
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.set_state(devices=[{"serial": "ZY22", "state": "unauthorized", "model": "moto_g_power_5G___2024", "props": {}}])
        store, _ = self.run_pipeline(device={"platform": "android", "class": "android-moto-g-power-5g-2024"})
        job = store.read_job()
        self.assertEqual("NEEDS_PERMISSION", job["status"], "a pinned class must not hide the permission problem")
        self.assertIn("Allow USB debugging", job["reason"])
        self.assertEqual(constants.EXIT_BLOCKED, constants.exit_code_for_status(job["status"]))

    def test_a_phone_that_drops_off_during_the_run_is_reported_not_hung(self):
        original = self.adapter.pull
        calls = {"n": 0}

        def pull(device, remote, local):
            calls["n"] += 1
            if calls["n"] == 3:
                self.set_state(devices=[])  # the cable is pulled while the plan runs
            return original(device, remote, local)

        self.set_state(on_launch={"gdv/out/r1/heartbeat.json": {"seq": 1}})
        with mock.patch.object(self.adapter, "pull", pull):
            store, _ = self.run_pipeline()
        self.assertIn(store.read_job()["status"], ("BLOCKED", "STALL"))
        self.assertNotEqual("PASS", store.read_job()["status"])

    def test_a_performance_plan_refuses_the_emulator(self):
        self.write_plan({**PLAN, "kind": "performance", "metrics": {"x": {"unit": "ms", "direction": "lower", "regressionPct": 10}}})
        self.set_state(devices=[{"serial": "emulator-5554", "state": "device", "props": EMULATOR_PROPS}])
        store, _ = self.run_pipeline()
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("no attached device satisfies", store.read_job()["reason"])

    def test_a_launch_the_system_rejects_still_force_stops_the_application(self):
        self.set_state(launch_error=True)
        store, _ = self.run_pipeline()
        self.assertEqual("FAIL", store.read_job()["status"])
        self.assertTrue(any("force-stop" in c for c in self.calls()))
        self.assertEqual({}, self.adapter._sessions)  # noqa: SLF001

    def test_a_clean_install_plan_resets_data_before_the_plan_is_pushed(self):
        self.write_plan({**PLAN, "dataPolicy": "clean-install"})
        self.run_pipeline()
        calls = self.calls()
        clear = next(i for i, c in enumerate(calls) if "pm clear" in c)
        push = next(i for i, c in enumerate(calls) if " push " in f" {c} ")
        self.assertLess(clear, push, "clearing after the push would delete the plan")


if __name__ == "__main__":
    unittest.main()
