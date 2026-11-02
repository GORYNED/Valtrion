import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, commands, config, constants, core, doctor, lease, remote
from valtrion.adapter import DeviceInfo
from valtrion.resolver import Request
from valtrion.schema import Validator
from valtrion.transport import CommandResult, Transport

UNITY = "6000.6.7f1"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class HostTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.home = self.base / "home"
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The tests name remote hosts such as "mac"; the machine's own hostname must never collide with them.
        hostname = mock.patch.object(core, "local_host_id", return_value="this-machine")
        hostname.start()
        self.addCleanup(hostname.stop)


class ConfigTests(HostTestCase):
    def test_the_local_host_is_added_automatically(self):
        entries = config.load_hosts()
        self.assertEqual(1, len(entries))
        self.assertEqual(("local", True, core.local_os()), (entries[0].kind, entries[0].auto, entries[0].os))
        self.assertEqual(self.home / "artifacts", entries[0].artifacts_root)

    def test_host_json_sets_the_artifacts_root_and_retention(self):
        (self.home).mkdir()
        (self.home / "host.json").write_text(json.dumps({"artifactsRoot": str(self.base / "runs"), "retention": {"keepDays": 7}}), encoding="utf-8")
        settings = config.load_host_settings()
        self.assertEqual((self.base / "runs", 7.0), (settings.artifacts_root, settings.retention_days))
        (self.home / "host.json").write_text(json.dumps({"retention": {"keepDays": -1}}), encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_host_settings()

    def test_add_ssh_host_validates_keeps_the_local_entry_and_replaces_by_id(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/artifacts", projects_root="/Users/me/Projects")
        config.add_ssh_host("mac", "me@mac2", "macos", "/Users/me/artifacts")
        entries = config.load_hosts()
        self.assertEqual(["local", "ssh"], [e.kind for e in entries])
        self.assertEqual("me@mac2", entries[1].ssh_target)
        self.assertEqual([], Validator().validate(json.loads((self.home / "hosts.json").read_text(encoding="utf-8")), "gdv.hosts/1"))
        with self.assertRaises(config.ConfigError):
            config.add_ssh_host("bad", "-oProxyCommand=x", "macos", "/x")

    def test_an_invalid_hosts_file_is_a_config_error(self):
        self.home.mkdir()
        (self.home / "hosts.json").write_text(json.dumps({"schema": "gdv.hosts/1", "hosts": [{"id": "x"}]}), encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_hosts()
        code, _, err = run("hosts")
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)

    def test_a_refresh_keeps_an_undiscovered_device_offline_with_its_settings_until_forgotten(self):
        registry = config.load_registry()
        tablet = DeviceInfo("ios:1", "ios", "tablet", "physical", "Tablet", "iOS 1.0", ("touch",))
        first = config.merge_discovered(registry, [tablet], ["ios"], "mac", "2026-11-01T10:00:00Z")
        first["devices"][0].update(alias="tab1", capabilities=["camera.fast", "perf.baseline", "touch"], **{"class": "tablet-owner"})
        away = config.merge_discovered(first, [], ["ios"], "mac", "2026-11-02T10:00:00Z")
        entry = away["devices"][0]
        self.assertEqual((False, "2026-11-01T10:00:00Z"), (entry["online"], entry["lastSeen"]), "offline, last seen when it was found")
        self.assertEqual(("tablet-owner", "tab1", ["camera.fast", "perf.baseline", "touch"]),
                         (entry["class"], entry["alias"], entry["capabilities"]), "nothing the owner set is lost")
        self.assertEqual([], Validator().validate(away, "gdv.registry/1"))
        self.assertEqual([], remote.devices_from_report({"devices": away["devices"]}), "an offline entry never routes a job as attached")
        back = config.merge_discovered(away, [tablet], ["ios"], "mac", "2026-11-02T11:00:00Z")
        entry = back["devices"][0]
        self.assertEqual((True, "2026-11-02T11:00:00Z", "tablet-owner"), (entry["online"], entry["lastSeen"], entry["class"]))
        self.assertIn("camera.fast", entry["capabilities"])
        config.save_registry(away)
        code, out, _ = run("devices")
        self.assertEqual(0, code)
        self.assertIn("OFFLINE", out)
        code, out, _ = run("devices", "--forget", "tab1")
        self.assertEqual(0, code, out)
        self.assertEqual([], config.load_registry()["devices"])
        code, _, err = run("devices", "--forget", "ios:1")
        self.assertEqual(1, code)
        self.assertIn("no registered device", err)

    def test_registry_merge_keeps_alias_and_notes_and_keeps_vanished_devices_offline(self):
        registry = config.load_registry()
        first = config.merge_discovered(registry, [DeviceInfo("ios:1", "ios", "ipad-a14", "physical", "iPad13,19", "iPadOS 27.2", ("touch",))],
                                        ["ios"], "mac")
        first["devices"][0]["alias"] = "ipad10"
        first["devices"][0]["notes"] = "no XCUITest while recording"
        android = DeviceInfo("android:1", "android", "phone-mid", "physical")
        second = config.merge_discovered(first, [android], ["android"], "mac")
        self.assertEqual(["android:1", "ios:1"], [d["id"] for d in second["devices"]], "another platform stays untouched")
        third = config.merge_discovered(second, [DeviceInfo("ios:1", "ios", "ipad-a14", "physical", "iPad13,19", "iPadOS 27.3", ("touch", "ble"))],
                                        ["ios"], "mac")
        ipad = next(d for d in third["devices"] if d["id"] == "ios:1")
        self.assertEqual(("ipad10", "no XCUITest while recording", "iPadOS 27.3"), (ipad["alias"], ipad["notes"], ipad["os"]))
        gone = config.merge_discovered(third, [], ["ios"], "mac")
        self.assertEqual(["android:1", "ios:1"], [d["id"] for d in gone["devices"]])
        self.assertEqual([True, False], [d["online"] for d in gone["devices"]], "a vanished device is kept offline, not dropped")
        self.assertEqual([], Validator().validate(config.merge_discovered(registry, [android], ["android"], "mac"), "gdv.registry/1"))


class DoctorTests(HostTestCase):
    def hub(self, os_name="windows"):
        hub = self.base / "Hub" / "Editor"
        editor = hub / UNITY
        if os_name == "windows":
            (editor / "Editor").mkdir(parents=True)
            (editor / "Editor" / "Unity.exe").write_bytes(b"")
            engines = editor / "Editor" / "Data" / "PlaybackEngines"
        else:
            (editor / "Unity.app" / "Contents" / "MacOS").mkdir(parents=True)
            (editor / "Unity.app" / "Contents" / "MacOS" / "Unity").write_bytes(b"")
            engines = editor / "PlaybackEngines"
        (engines / "iOSSupport").mkdir(parents=True)
        adb = engines / "AndroidPlayer" / "SDK" / "platform-tools"
        adb.mkdir(parents=True)
        (adb / ("adb.exe" if os_name == "windows" else "adb")).write_bytes(b"")
        (hub.parent / "Other").mkdir()
        return {doctor.HUB_PATHS_ENV: str(hub)}

    def runner(self, table):
        def run_command(argv):
            for prefix, answer in table.items():
                if " ".join(argv).startswith(prefix):
                    return answer
            return 1, "not found"
        return run_command

    def test_windows_host_with_unity_android_module_and_bundled_adb(self):
        environ = self.hub("windows")
        table = {"tasklist": (0, "Runner.Listener.exe 1234 Console\nUnity.exe 55"), f"{self.base}": (0, "Android Debug Bridge version 1.0.41")}
        with mock.patch("shutil.which", return_value=None):
            report = doctor.probe_local("windows", self.runner(table), self.base / "art", environ)
        caps = report["capabilities"]
        self.assertEqual([{"version": UNITY, "modules": ["android", "ios"]}], caps["unityEditors"])
        self.assertEqual(["android", "editor", "windows"], caps["buildPlatforms"], "ios needs a macOS host with Xcode")
        self.assertTrue(caps["adb"])
        self.assertIn("AndroidPlayer", report["tools"]["adb"], "Unity's bundled adb is found when none is on PATH")
        self.assertFalse(caps["iosSigning"])
        self.assertIn("xcode", report["blocked"])
        self.assertEqual("idle", report["githubRunner"]["state"])
        self.assertGreater(report["disk"]["freeGb"], 0)
        host_caps = doctor.to_host_capabilities(report)
        self.assertEqual(("android",), tuple(host_caps.unity_editors[UNITY])[:1])

    def test_runner_busy_idle_absent_and_never_touched(self):
        for output, state in (("Runner.Listener\nRunner.Worker", "busy"), ("Runner.Listener", "idle"), ("nothing", "absent")):
            with self.subTest(state):
                self.assertEqual(state, doctor.probe_runner("macos", lambda argv, o=output: (0, o))["state"])
        self.assertEqual("unknown", doctor.probe_runner("macos", lambda argv: (1, "denied"))["state"])
        issued = []
        doctor.probe_runner("windows", lambda argv: (issued.append(argv), (0, ""))[1])
        self.assertEqual([["tasklist", "/FO", "CSV", "/NH"]], issued, "doctor only lists processes")

    def test_mac_host_with_xcode_gui_session_and_signing(self):
        environ = self.hub("macos")
        table = {
            "xcodebuild -version": (0, "Xcode 27.1\nBuild version 27A1"), "xcrun devicectl --version": (0, "devicectl 399.1"),
            "launchctl print gui/": (0, "ok"), "security find-identity": (0, "  1) ABC \"Apple Development\"\n     1 valid identities found"),
            "security show-keychain-info": (0, "Keychain"), "ps": (0, "1 Runner.Listener"), "adb": (0, "Android Debug Bridge")}
        with mock.patch("shutil.which", return_value=None):
            report = doctor.probe_local("macos", self.runner(table), self.base / "art", environ)
        caps = report["capabilities"]
        self.assertEqual("27.1", caps["xcode"])
        self.assertTrue(caps["iosSigning"])
        self.assertIn("ios", caps["buildPlatforms"])
        self.assertTrue(report["gui"]["guiSession"])
        self.assertNotIn("xcode", report["blocked"])

    def test_mac_without_a_gui_session_blocks_signing_with_the_owner_action(self):
        table = {"xcodebuild -version": (0, "Xcode 27.1"), "launchctl print": (1, "no such domain"), "security find-identity": (0, "  1) X"),
                 "xcrun devicectl": (0, "1")}
        with mock.patch("shutil.which", return_value=None):
            report = doctor.probe_local("macos", self.runner(table), self.base, self.hub("macos"))
        self.assertFalse(report["capabilities"]["iosSigning"])
        self.assertIn("GUI session", report["blocked"]["guiSession"])
        self.assertEqual("27.1", report["capabilities"]["xcode"])

    def test_no_editor_is_blocked_with_a_reason(self):
        with mock.patch("shutil.which", return_value=None):
            report = doctor.probe_local("windows", self.runner({}), self.base, {doctor.HUB_PATHS_ENV: str(self.base / "empty")})
        self.assertEqual([], report["capabilities"]["buildPlatforms"])
        self.assertIn("unity", report["blocked"])

    def test_doctor_command_json(self):
        environ = {doctor.HUB_PATHS_ENV: str(self.base / "empty")}
        with mock.patch.dict(os.environ, environ):
            code, out, _ = run("doctor", "--json")
        self.assertEqual(0, code)
        report = json.loads(out)
        # `adapters` appears when this checkout ships platform adapters; it is not part of the host's own probe.
        self.assertEqual({"host", "capabilities", "tools", "editors", "gui", "disk", "githubRunner", "blocked", "devices",
                          "workRoot", "storage", "checkouts", "warnings", "core"}, set(report) - {"adapters"})
        self.assertEqual({"workRoot", "artifactsRoot", "buildCache", "incremental", "freeBytes"}, set(report["storage"]), "no threshold is set")


class FakeSsh(Transport):
    """The Mac as seen over SSH: answers `doctor --json` with a canned report and records every call."""

    kind = "ssh"

    def __init__(self, report):
        self.report = report
        self.calls = []

    def run(self, command, timeout_sec, cwd=None):
        self.calls.append(list(command))
        if "doctor" in command:
            return CommandResult(0, json.dumps(self.report))
        return CommandResult(2, "", "unexpected")

    def put(self, *_):
        return CommandResult(0)

    def get(self, *_):
        return CommandResult(0)


def mac_report():
    return {"host": {"id": "mac", "os": "macos"},
            "capabilities": {"unityEditors": [{"version": UNITY, "modules": ["ios"]}], "buildPlatforms": ["editor", "ios", "macos"],
                             "xcode": "27.1", "iosSigning": True, "adb": False},
            "devices": [{"id": "ios:1", "platform": "ios", "class": "ipad-a14", "evidence": "physical", "capabilities": ["touch"], "exclusive": True}]}


def windows_report(with_phone=True):
    return {"host": {"id": "win", "os": "windows"},
            "capabilities": {"unityEditors": [{"version": UNITY, "modules": ["android"]}], "buildPlatforms": ["android", "editor", "windows"],
                             "iosSigning": False, "adb": True},
            "devices": [{"id": "android:1", "platform": "android", "class": "phone-mid", "evidence": "physical", "capabilities": [], "exclusive": True}]
            if with_phone else []}


class ResolveHostTests(HostTestCase):
    def resolve(self, request, local, ssh):
        config.add_ssh_host("mac", "me@mac", "macos", "/art")
        target = remote.Remote(ssh, ("gdv",))
        with mock.patch.object(commands, "local_report", return_value=local), \
                mock.patch.object(remote, "remote_for", return_value=target):
            return commands.resolve_host(request, None)[0]

    def test_a_project_that_is_not_on_this_machine_does_not_pin_the_unity_version(self):
        pipeline = {"device": {"platform": "android"}, "stages": [{"stage": "build"}, {"stage": "run", "plan": "x.plan.json"}]}
        request = commands._request_for(pipeline, Path(tempfile.gettempdir()), Path(tempfile.gettempdir()) / "no-such-project")  # noqa: SLF001
        self.assertTrue(request.build)
        self.assertIsNone(request.unity_version, "the host that runs the job checks the editor; this machine cannot")

    def test_windows_with_everything_local_never_touches_ssh(self):
        ssh = FakeSsh(mac_report())
        resolution = self.resolve(Request("android", True, UNITY, True), windows_report(), ssh)
        self.assertEqual(("local", "local"), (resolution.selected_by, resolution.transport))
        self.assertEqual([], ssh.calls)

    def test_windows_agent_with_an_ios_request_goes_to_the_mac_over_ssh(self):
        ssh = FakeSsh(mac_report())
        resolution = self.resolve(Request("ios", True, UNITY, True, "ipad-a14"), windows_report(), ssh)
        self.assertEqual(("mac", "capability", "ssh"), (resolution.host.id, resolution.selected_by, resolution.transport))
        self.assertTrue(any("doctor" in call for call in ssh.calls))

    def test_the_remote_probe_is_cached_between_decisions(self):
        ssh = FakeSsh(mac_report())
        config.add_ssh_host("mac", "me@mac", "macos", "/art")
        target = remote.Remote(ssh, ("gdv",))
        with mock.patch.object(commands, "local_report", return_value=windows_report()), mock.patch.object(remote, "remote_for", return_value=target):
            commands.resolve_host(Request("ios", True, UNITY, True), None)
            commands.resolve_host(Request("ios", True, UNITY, True), None)
        self.assertEqual(1, len(ssh.calls))

    def test_mac_agent_with_an_android_phone_on_the_windows_host(self):
        config.add_ssh_host("win", "me@win", "windows", "C:/art")
        ssh = FakeSsh(windows_report())
        target = remote.Remote(ssh, ("python", "valtrion.py"))
        with mock.patch.object(commands, "local_report", return_value=mac_report()), mock.patch.object(remote, "remote_for", return_value=target):
            resolution = commands.resolve_host(Request("android", True, UNITY, True), None)[0]
        self.assertEqual(("win", "capability"), (resolution.host.id, resolution.selected_by))

    def test_nothing_satisfies_is_blocked_with_a_reason_per_host(self):
        resolution = self.resolve(Request("ios", True, UNITY, True), windows_report(), FakeSsh({**mac_report(), "devices": []}))
        self.assertEqual(constants.BLOCKED, resolution.status)
        self.assertEqual(2, len(resolution.reasons))
        self.assertIn("no attached ios device", resolution.reasons[1])

    def test_an_unreachable_remote_is_a_reason_not_an_error(self):
        class Down(FakeSsh):
            def run(self, command, timeout_sec, cwd=None):
                return CommandResult(remote.SSH_FAILURE, "", "no route")

        resolution = self.resolve(Request("ios", True, UNITY, True), windows_report(), Down({}))
        self.assertEqual(constants.BLOCKED, resolution.status)
        self.assertIn("unreachable", resolution.reasons[1])


class LeaseCommandTests(HostTestCase):
    def test_acquire_status_release_through_the_cli(self):
        code, out, _ = run("lease", "acquire", "device:ipad", "--session", "s1", "--holder-pid", str(os.getpid()), "--ttl", "60", "--purpose", "series")
        self.assertEqual(0, code)
        self.assertEqual("s1", json.loads(out)["lease"]["session"])
        code, _, err = run("lease", "acquire", "device:ipad", "--session", "s2", "--holder-pid", str(os.getpid()))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("leased by", err)
        self.assertIn("s1", err)
        self.assertEqual("held", json.loads(run("lease", "status", "device:ipad")[1])["state"])
        self.assertEqual(1, len(json.loads(run("lease", "list")[1])))
        self.assertEqual(constants.EXIT_BLOCKED, run("lease", "release", "device:ipad", "--session", "s2", "--holder-pid", str(os.getpid()))[0])
        self.assertEqual(0, run("lease", "release", "device:ipad", "--session", "s1", "--holder-pid", str(os.getpid()))[0])
        self.assertEqual("free", json.loads(run("lease", "status", "device:ipad")[1])["state"])

    def test_counted_slots_through_the_cli(self):
        for index in range(2):
            self.assertEqual(0, run("lease", "acquire", "unity-editor", "--slots", "2", "--session", f"s{index}", "--holder-pid", str(os.getpid()))[0])
        code, _, err = run("lease", "acquire", "unity-editor", "--slots", "2", "--session", "s3", "--holder-pid", str(os.getpid()))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("all 2 slots", err)

    def write_host_json(self, slots):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": slots}), encoding="utf-8")

    def test_unity_editor_without_slots_uses_the_host_total(self):
        self.write_host_json(2)
        for index in range(2):
            code, out, _ = run("lease", "acquire", "unity-editor", "--session", f"s{index}", "--holder-pid", str(os.getpid()))
            self.assertEqual(0, code)
            self.assertRegex(json.loads(out)["lease"]["resource"], r"^unity-editor:slot[12]$")
        code, _, err = run("lease", "acquire", "unity-editor", "--session", "s3", "--holder-pid", str(os.getpid()))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("all 2 slots", err)

    def test_unity_editor_acquired_without_slots_is_released_by_its_slot_name(self):
        self.write_host_json(2)
        pid = str(os.getpid())
        code, out, _ = run("lease", "acquire", "unity-editor", "--session", "s1", "--holder-pid", pid)
        self.assertEqual(0, code)
        resource = json.loads(out)["lease"]["resource"]
        self.assertRegex(resource, r"^unity-editor:slot[12]$")
        self.assertEqual("held", json.loads(run("lease", "status", resource)[1])["state"])
        self.assertEqual(0, run("lease", "release", resource, "--session", "s1", "--holder-pid", pid)[0])
        self.assertEqual("free", json.loads(run("lease", "status", resource)[1])["state"])

    def test_explicit_slots_override_the_host_total(self):
        self.write_host_json(3)
        self.assertEqual(0, run("lease", "acquire", "unity-editor", "--slots", "1", "--session", "s1", "--holder-pid", str(os.getpid()))[0])
        code, _, err = run("lease", "acquire", "unity-editor", "--slots", "1", "--session", "s2", "--holder-pid", str(os.getpid()))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("all 1 slots", err)

    def test_other_resources_without_slots_stay_plain_leases(self):
        code, out, _ = run("lease", "acquire", "device:ipad", "--session", "s1", "--holder-pid", str(os.getpid()))
        self.assertEqual("device:ipad", json.loads(out)["lease"]["resource"])

    def test_lease_name_unity_project_prints_the_project_lease_name(self):
        project = self.base / "MyGame"
        project.mkdir()
        code, out, _ = run("lease", "name", "unity-project", str(project))
        self.assertEqual(0, code)
        self.assertEqual({"resource": lease.unity_project_resource(project)}, json.loads(out))
        self.assertTrue(json.loads(out)["resource"].startswith("unity-project:"))
        self.assertEqual("free", lease.LeaseManager().status(json.loads(out)["resource"]).state, "a read-only command takes nothing")

    def test_waiting_for_a_busy_lease_prints_a_line_to_stderr(self):
        self.assertEqual(0, run("lease", "acquire", "device:ipad", "--session", "s1", "--holder-pid", str(os.getpid()))[0])
        code, _, err = run("lease", "acquire", "device:ipad", "--session", "s2", "--holder-pid", str(os.getpid()), "--wait", "0.3")
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("lease waiting 0 s:", err)
        self.assertIn("s1", err)

    def test_a_remote_lease_command_runs_on_the_host_that_owns_the_resource(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/art")
        ssh = FakeSsh({})
        ssh.run = lambda command, timeout, cwd=None: (ssh.calls.append(list(command)), CommandResult(0, '{"acquired": true}\n'))[1]
        with mock.patch.object(commands, "_remote", return_value=remote.Remote(ssh, ("gdv",))):
            code, out, _ = run("lease", "acquire", "device:ipad", "--host", "mac", "--session", "s1", "--wait", "5")
        self.assertEqual(0, code)
        self.assertEqual(["gdv", "lease", "acquire", "device:ipad", "--session", "s1", "--wait", "5"], ssh.calls[0])
        self.assertEqual("free", lease.LeaseManager().status("device:ipad").state, "nothing was leased on this machine")


class GcAndCompareCommandTests(HostTestCase):
    def test_gc_dry_run_and_hosts_listing(self):
        code, out, _ = run("hosts", "--json")
        self.assertEqual(0, code)
        self.assertEqual("local", json.loads(out)[0]["transport"])
        code, out, _ = run("gc", "--keep-days", "30", "--dry-run")
        self.assertEqual(0, code)
        self.assertIn("0 run(s)", out)

    def test_compare_command_exit_codes(self):
        from tests.test_compare import BASE, baseline, plan, result
        folder = self.base / "files"
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(result([5, 130, 131, 129])), encoding="utf-8")
        (folder / "plan.json").write_text(json.dumps(plan()), encoding="utf-8")
        code, out, _ = run("compare", str(folder / "result.json"), "--plan", str(folder / "plan.json"), "--baselines", str(folder / "b"))
        self.assertEqual(constants.EXIT_BLOCKED, code, "no baseline is NOT_COMPARABLE, never PASS")
        core.atomic_write_json(folder / "b" / "perf.open" / "ipad-a14.json", baseline(BASE))
        code, out, _ = run("compare", str(folder / "result.json"), "--plan", str(folder / "plan.json"), "--baselines", str(folder / "b"))
        self.assertEqual(constants.EXIT_FAIL, code)
        self.assertEqual("REGRESSION", json.loads(out)["verdict"])

    def test_compare_command_finds_the_baseline_by_key_among_several_and_says_which_file(self):
        from tests.test_compare import BASE, baseline, plan, result
        folder = self.base / "files"
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(result([5, 101, 100, 102])), encoding="utf-8")
        (folder / "plan.json").write_text(json.dumps(plan()), encoding="utf-8")
        core.atomic_write_json(folder / "b" / "perf.open" / "ipad-a14.json", baseline([999, 50, 50, 50], osMajor=26))
        core.atomic_write_json(folder / "b" / "perf.open" / "ipad-os27.json", baseline(BASE))
        code, out, _ = run("compare", str(folder / "result.json"), "--plan", str(folder / "plan.json"), "--baselines", str(folder / "b"))
        self.assertEqual(0, code, out)
        report = json.loads(out)
        self.assertEqual("PASS", report["verdict"])
        self.assertTrue(report["baseline"].endswith("ipad-os27.json"), report["baseline"])


class RunOptionTests(HostTestCase):
    def write_documents(self):
        plan = {"schema": "gdv.plan/1", "id": "p", "kind": "smoke", "requires": {"platforms": ["android"]}, "dataPolicy": "devdata",
                "budgetSeconds": 30, "steps": [{"type": "wait", "timeoutSec": 5}]}
        pipeline = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "R", "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": [{"stage": "run", "plan": "plan.json", "args": {"app": "a"}}]}
        core.atomic_write_json(self.base / "plan.json", plan)
        core.atomic_write_json(self.base / "pipeline.json", pipeline)

    def test_a_pipeline_file_needs_no_sha_and_a_malformed_sha_option_is_refused(self):
        self.write_documents()
        code, _, err = run("pipeline", str(self.base / "pipeline.json"), "--sha", "not-a-commit")
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("40-character", err)
        self.assertNotIn("is not a valid pipeline", err, "a pipeline without candidate.sha is valid")

    def test_a_run_without_a_known_commit_gets_the_unversioned_run_folder(self):
        self.write_documents()
        for sha in (None, "0" * 40):
            document = json.loads((self.base / "pipeline.json").read_text(encoding="utf-8"))
            if sha is not None:
                document["candidate"]["sha"] = sha
            core.atomic_write_json(self.base / "pipeline.json", document)
            code, out, _ = run("pipeline", str(self.base / "pipeline.json"), "--local", "--foreground")
            info = json.loads(out)
            self.assertIn("/unversioned/", info["relDir"], sha)
            self.assertNotIn("00000000", info["relDir"], sha)
            self.assertIn(code, (constants.EXIT_BLOCKED, constants.EXIT_FAIL), "no adapter or device here: the run ends, not passes")

    def test_a_malformed_prelaunch_file_option_is_refused(self):
        self.write_documents()
        for bad in ("no-delimiter", "=remote.json", "local.json="):
            code, _, err = run("run", str(self.base / "plan.json"), "--repo", "R", "--sha", "a" * 40, "--app", "a", "--prelaunch-file", bad)
            self.assertEqual(constants.EXIT_BLOCKED, code, bad)
            self.assertIn("LOCAL=REMOTE", err, bad)


if __name__ == "__main__":
    unittest.main()
