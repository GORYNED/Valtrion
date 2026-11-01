import unittest
from pathlib import Path

from valtrion import constants
from valtrion.adapter import DeviceInfo
from valtrion.resolver import (
    SELECTED_CAPABILITY, SELECTED_LOCAL, SELECTED_PINNED, HostCapabilities, HostInfo, HostResolver, Request, missing)
from valtrion.transport import CommandResult, LocalTransport, SshTransport

UNITY = "6000.6.7f1"


def ipad() -> DeviceInfo:
    return DeviceInfo("ios:1", "ios", "ipad-a14", "physical", capabilities=("camera.120fps", "touch"))


def phone() -> DeviceInfo:
    return DeviceInfo("android:1", "android", "phone-mid", "physical", capabilities=("touch",))


WINDOWS = HostInfo("win", "local", "windows")
MAC = HostInfo("mac", "ssh", "macos", ssh_target="builder@mac")
WINDOWS_REMOTE = HostInfo("win", "ssh", "windows", ssh_target="me@win")
MAC_LOCAL = HostInfo("mac", "local", "macos")

WINDOWS_CAPS = HostCapabilities(
    unity_editors={UNITY: ("android", "windows-il2cpp")}, build_platforms=("windows", "android", "editor"), adb=True, devices=(phone(),))
MAC_CAPS = HostCapabilities(
    unity_editors={UNITY: ("ios",)}, build_platforms=("ios", "macos", "editor"), xcode=True, ios_signing=True, devices=(ipad(),))

IOS = Request("ios", build=True, unity_version=UNITY, device=True, device_class="ipad-a14", device_capabilities=("camera.120fps",))
ANDROID = Request("android", build=True, unity_version=UNITY, device=True)


def resolver(local: HostInfo, remotes, caps: dict) -> HostResolver:
    return HostResolver(local, remotes, lambda host: caps.get(host.id))


class SelectionMatrixTests(unittest.TestCase):
    def test_windows_agent_with_ios_uses_the_mac_over_ssh(self):
        result = resolver(WINDOWS, [MAC], {"win": WINDOWS_CAPS, "mac": MAC_CAPS}).resolve(IOS)
        self.assertEqual(("mac", "ssh", SELECTED_CAPABILITY), (result.host.id, result.transport, result.selected_by))
        self.assertEqual(0, result.exit_code)

    def test_mac_agent_with_ios_runs_locally(self):
        result = resolver(MAC_LOCAL, [WINDOWS_REMOTE], {"win": WINDOWS_CAPS, "mac": MAC_CAPS}).resolve(IOS)
        self.assertEqual(("mac", "local", SELECTED_LOCAL), (result.host.id, result.transport, result.selected_by))

    def test_android_device_on_windows_runs_locally_on_windows(self):
        result = resolver(WINDOWS, [MAC], {"win": WINDOWS_CAPS, "mac": MAC_CAPS}).resolve(ANDROID)
        self.assertEqual(("win", "local", SELECTED_LOCAL), (result.host.id, result.transport, result.selected_by))

    def test_mac_agent_reaches_windows_for_android_when_configured(self):
        result = resolver(MAC_LOCAL, [WINDOWS_REMOTE], {"win": WINDOWS_CAPS, "mac": MAC_CAPS}).resolve(ANDROID)
        self.assertEqual(("win", "ssh", SELECTED_CAPABILITY), (result.host.id, result.transport, result.selected_by))

    def test_mac_agent_without_a_windows_host_is_blocked_for_android(self):
        result = resolver(MAC_LOCAL, [], {"mac": MAC_CAPS}).resolve(ANDROID)
        self.assertEqual(constants.BLOCKED, result.status)
        self.assertEqual(2, result.exit_code)
        self.assertIsNone(result.host)
        self.assertTrue(any("mac" in reason and "adb" in reason for reason in result.reasons))

    def test_local_wins_when_both_satisfy(self):
        both = HostCapabilities(
            unity_editors={UNITY: ()}, build_platforms=("editor",), devices=())
        result = resolver(WINDOWS, [MAC], {"win": both, "mac": both}).resolve(Request("editor", build=True, unity_version=UNITY))
        self.assertEqual(("win", SELECTED_LOCAL), (result.host.id, result.selected_by))

    def test_remotes_are_tried_in_id_order(self):
        other = HostInfo("alpha", "ssh", "macos", ssh_target="a@alpha")
        result = resolver(WINDOWS, [MAC, other], {"win": WINDOWS_CAPS, "mac": MAC_CAPS, "alpha": MAC_CAPS}).resolve(IOS)
        self.assertEqual("alpha", result.host.id)

    def test_selection_is_repeatable(self):
        instance = resolver(WINDOWS, [MAC], {"win": WINDOWS_CAPS, "mac": MAC_CAPS})
        self.assertEqual(instance.resolve(IOS), instance.resolve(IOS))

    def test_unreachable_remote_is_skipped_and_reported(self):
        other = HostInfo("beta", "ssh", "macos", ssh_target="b@beta")
        result = resolver(WINDOWS, [MAC, other], {"win": WINDOWS_CAPS, "beta": MAC_CAPS}).resolve(IOS)
        self.assertEqual("beta", result.host.id)
        blocked = resolver(WINDOWS, [MAC], {"win": WINDOWS_CAPS}).resolve(IOS)
        self.assertEqual(constants.BLOCKED, blocked.status)
        self.assertIn("mac: unreachable", blocked.reasons)

    def test_every_host_contributes_a_reason_when_blocked(self):
        result = resolver(WINDOWS, [MAC], {"win": WINDOWS_CAPS, "mac": MAC_CAPS}).resolve(Request("ios", device=True, device_class="ipad-m4"))
        self.assertEqual(constants.BLOCKED, result.status)
        self.assertEqual(2, len(result.reasons))

    def test_pinned_host_is_used_when_it_satisfies(self):
        pinned = Request("editor", build=True, unity_version=UNITY, pinned_host="mac")
        caps = HostCapabilities(unity_editors={UNITY: ()}, build_platforms=("editor",))
        result = resolver(WINDOWS, [MAC], {"win": caps, "mac": caps}).resolve(pinned)
        self.assertEqual(("mac", SELECTED_PINNED), (result.host.id, result.selected_by))

    def test_pinned_host_that_cannot_serve_is_blocked_not_replaced(self):
        result = resolver(WINDOWS, [MAC], {"win": WINDOWS_CAPS, "mac": MAC_CAPS}).resolve(
            Request("android", build=True, pinned_host="mac"))
        self.assertEqual(constants.BLOCKED, result.status)

    def test_unknown_pinned_host_is_blocked(self):
        result = resolver(WINDOWS, [], {"win": WINDOWS_CAPS}).resolve(Request("android", pinned_host="ghost"))
        self.assertEqual(constants.BLOCKED, result.status)
        self.assertIn("ghost", result.reasons[0])

    def test_request_without_needs_runs_locally(self):
        result = resolver(WINDOWS, [MAC], {"win": HostCapabilities()}).resolve(Request("editor"))
        self.assertEqual(SELECTED_LOCAL, result.selected_by)


class MissingTests(unittest.TestCase):
    def test_ios_build_needs_xcode_and_signing(self):
        caps = HostCapabilities(unity_editors={UNITY: ()}, build_platforms=("ios",))
        reasons = missing(Request("ios", build=True, unity_version=UNITY), caps)
        self.assertEqual(2, len(reasons))

    def test_missing_editor_version(self):
        caps = HostCapabilities(build_platforms=("editor",))
        self.assertEqual(["Unity 6000.6.7f1 is not installed"], missing(Request("editor", build=True, unity_version=UNITY), caps))

    def test_device_capabilities_must_all_be_present(self):
        caps = HostCapabilities(devices=(ipad(),))
        self.assertTrue(missing(Request("ios", device=True, device_capabilities=("ble",)), caps))
        self.assertFalse(missing(Request("ios", device=True, device_capabilities=("touch", "camera.120fps")), caps))


class ResolverConstructionTests(unittest.TestCase):
    def test_first_host_must_be_local(self):
        with self.assertRaises(ValueError):
            HostResolver(MAC, [], lambda host: None)

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            HostResolver(WINDOWS, [HostInfo("win", "ssh", "macos", ssh_target="x")], lambda host: None)

    def test_a_second_local_host_is_rejected(self):
        with self.assertRaises(ValueError):
            HostResolver(WINDOWS, [HostInfo("other", "local", "macos")], lambda host: None)

    def test_ssh_host_needs_a_target(self):
        with self.assertRaises(ValueError):
            HostInfo("x", "ssh", "macos")


class TransportTests(unittest.TestCase):
    def test_ssh_command_is_batch_mode_bounded_and_quoted(self):
        calls = []

        def runner(argv, timeout, cwd, env):
            calls.append((list(argv), timeout))
            return CommandResult(0, "ok")

        transport = SshTransport("builder@mac", 7, runner)
        result = transport.run(["python3", "tools/tool.py", "doctor", "a b"], 30, cwd="/work/my project")
        self.assertTrue(result.ok)
        argv, timeout = calls[0]
        self.assertEqual(37, timeout)
        self.assertEqual(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7", "-o", "ForwardAgent=no",
                          "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "builder@mac", "--"], argv[:-1])
        self.assertEqual("cd '/work/my project' && python3 tools/tool.py doctor 'a b'", argv[-1])

    def test_ssh_copy_uses_scp_with_the_same_options(self):
        calls = []
        transport = SshTransport("b@mac", 5, lambda argv, timeout, cwd, env: calls.append(list(argv)) or CommandResult(0))
        transport.put(Path("plan.json"), "inbox/plan.json", 20)
        transport.get("out/run-1", Path("artifacts/run-1"), 20)
        self.assertEqual("scp", calls[0][0])
        self.assertIn("b@mac:inbox/plan.json", calls[0])
        self.assertIn("b@mac:out/run-1", calls[1])
        self.assertIn("BatchMode=yes", calls[1])

    def test_a_windows_drive_path_is_sent_to_scp_in_the_form_sftp_finds(self):
        calls = []
        transport = SshTransport("builder@win", 5, lambda argv, timeout, cwd, env: calls.append(list(argv)) or CommandResult(0))
        transport.get("C:\\Users\\me\\.valtrion\\run-1/.", Path("artifacts/run-1"), 20)
        transport.put(Path("plan.json"), "D:/inbox/plan.json", 20)
        transport.get("/Users/me/run-1/.", Path("artifacts/run-2"), 20)
        self.assertIn("builder@win:/C:/Users/me/.valtrion/run-1/.", calls[0])
        self.assertIn("builder@win:/D:/inbox/plan.json", calls[1])
        self.assertIn("builder@win:/Users/me/run-1/.", calls[2], "a POSIX path is left as it is")

    def test_ssh_transport_validates_arguments(self):
        with self.assertRaises(ValueError):
            SshTransport("")
        with self.assertRaises(ValueError):
            SshTransport("x", 0)

    def test_ssh_target_cannot_be_an_option(self):
        with self.assertRaises(ValueError):
            SshTransport("-oProxyCommand=evil")

    def test_non_posix_host_refuses_a_working_directory(self):
        transport = SshTransport("me@win", posix=False, runner=lambda *a: CommandResult(0))
        self.assertEqual("ssh", transport.ssh_argv(["C:/tools/python.exe", "x"])[0])
        with self.assertRaises(ValueError):
            transport.ssh_argv(["x"], cwd="D:/work")

    def test_transport_kinds(self):
        self.assertEqual("local", LocalTransport().kind)
        self.assertEqual("ssh", SshTransport("x").kind)

    def test_command_result_status(self):
        self.assertEqual("PASS", CommandResult(0).status)
        self.assertEqual("FAIL", CommandResult(1).status)
        self.assertEqual("TIMEOUT", CommandResult(None, timed_out=True).status)
        self.assertFalse(CommandResult(None, timed_out=True).ok)

    def test_local_transport_runs_and_copies(self):
        import sys
        import tempfile
        result = LocalTransport().run([sys.executable, "-c", "print(1)"], 30)
        self.assertEqual(("PASS", "1"), (result.status, result.stdout.strip()))
        self.assertEqual(constants.TIMEOUT, LocalTransport().run([sys.executable, "-c", "import time; time.sleep(30)"], 1).status)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "a.txt"
            source.write_text("x", encoding="utf-8")
            self.assertTrue(LocalTransport().put(source, str(Path(folder) / "sub" / "b.txt"), 5).ok)
            self.assertEqual("x", (Path(folder) / "sub" / "b.txt").read_text(encoding="utf-8"))
            self.assertFalse(LocalTransport().get(str(Path(folder) / "missing"), Path(folder) / "c.txt", 5).ok)


if __name__ == "__main__":
    unittest.main()
