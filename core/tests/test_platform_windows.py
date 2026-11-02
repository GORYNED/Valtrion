import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, constants, core, doctor, resolver, supervise
from valtrion import commands
from valtrion.providers.unity import support
from valtrion.providers import windows
from valtrion.providers.windows import WindowsAdapter

STUB = r'''
import json, os, subprocess, sys, time
data, mode = sys.argv[1], sys.argv[2]
out = os.path.join(data, "gdv", "out", "run-1")
os.makedirs(out, exist_ok=True)
def write(name, document):
    with open(os.path.join(out, name), "w") as handle:
        json.dump(document, handle)
if mode == "pass":
    write("heartbeat.json", {"seq": 1})
    write("result.json", {"status": "PASS", "reason": None, "steps": [], "metrics": [], "unverified": []})
    time.sleep(30)
elif mode == "pass-exit":
    write("heartbeat.json", {"seq": 1})
    write("result.json", {"status": "PASS", "reason": None, "steps": [], "metrics": [], "unverified": []})
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    with open(os.path.join(data, "child.pid"), "w") as handle:
        handle.write(str(child.pid))
    write("heartbeat.json", {"seq": 1})
    time.sleep(300)
elif mode == "exit":
    print("GDV-RESULT BLOCKED camera permission" if len(sys.argv) > 3 else "bye")
    sys.exit(3)
'''


def build_hub(root: Path, version="6000.6.7f1", windows_module=True, il2cpp=False) -> dict:
    editor = root / version / "Editor"
    editor.mkdir(parents=True)
    (editor / "Unity.exe").write_bytes(b"")
    if windows_module:
        variations = editor / "Data" / "PlaybackEngines" / "windowsstandalonesupport" / "Variations"
        variations.mkdir(parents=True)
        if il2cpp:
            (variations / "win64_player_development_il2cpp").mkdir()
    return {"GDV_UNITY_HUB_PATHS": str(root)}


class WindowsAdapterTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.local_low = self.base / "LocalLow"
        self.project = self.base / "Project"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.project / "ProjectSettings" / "ProjectSettings.asset").write_text(
            "  companyName: Dev Co\n  productName: Dev Product\n", encoding="utf-8")
        self.profile(executeMethod="Dev.Build.Windows", productName="Dev Product Validation", companyName="Dev Validation")
        self.stub = self.base / "player_stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.mode = "pass"
        self.desktop = True
        self.screens = []

    def profile(self, **entry):
        target = self.project / "validation" / "device" / "profiles.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"schema": "gdv.profiles/1", "profiles": {"Validation": {"windows": entry}}}), encoding="utf-8")

    def adapter(self, environ=None, is_windows=True, run=None):
        spawn = lambda argv, cwd, env, log: supervise.spawn(  # noqa: E731
            [sys.executable, str(self.stub), str(self.local_low / "Dev Validation" / "Dev Product Validation"), self.mode,
             *(["marker"] if self.mode == "exit-marker" else [])], cwd, env, log)
        return WindowsAdapter(self.local_low, environ, spawn, is_windows, run or self.fake_screen, desktop=lambda: self.desktop)

    def fake_screen(self, argv, timeout):
        target = Path(argv[-1])
        target.write_bytes(b"png")
        self.screens.append(target)
        return supervise.BoundedResult(constants.PASS, None, 0, "")

    def fake_unity(self, company="Dev Validation", product="Dev Product Validation", write_exe=True, status=constants.PASS):
        def run(argv, log, timeout, stall, cwd=None):
            exe = Path(argv[argv.index("-buildOutput") + 1])
            self.assertEqual("Dev Product Validation", argv[argv.index("-gdvProductName") + 1])
            self.assertIn("-gdvDevelopment", argv)
            self.assertEqual("mono", argv[argv.index("-gdvBackend") + 1])
            if write_exe:
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_bytes(b"MZ")
                data = exe.parent / f"{exe.stem}_Data"
                data.mkdir()
                (data / "app.info").write_text(f"{company}\n{product}\n", encoding="utf-8")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("Build Finished\n", encoding="utf-8")
            return supervise.SuperviseResult(status, None if status == constants.PASS else "boom", 0, 1.0, 0.0)
        return mock.patch.object(support, "run_unity", run)

    # -- discovery -------------------------------------------------------------------------------------------------------------

    def test_discovers_the_host_as_host_evidence_and_nothing_elsewhere(self):
        device = self.adapter().discover()[0]
        self.assertEqual(("windows", "host", False), (device.platform, device.evidence, device.exclusive))
        self.assertEqual([], self.adapter(is_windows=False).discover())
        self.assertIn("windows", self.adapter(is_windows=False).probe().blocked)

    # -- build -----------------------------------------------------------------------------------------------------------------

    def test_build_blocks_without_a_hook_and_names_the_file(self):
        (self.project / "validation" / "device" / "profiles.json").unlink()
        outcome = self.adapter({"GDV_UNITY_HUB_PATHS": str(self.base / "hub")}).build(self.project, "Validation", self.base / "b")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("executeMethod", outcome.reason)

    def test_build_refuses_the_projects_own_identity(self):
        self.profile(executeMethod="X.Y", productName="Dev Product", companyName="Dev Co")
        outcome = self.adapter(build_hub(self.base / "hub")).build(self.project, "Validation", self.base / "b")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("developer's data", outcome.reason)

    def test_build_blocks_when_the_editor_or_modules_are_missing(self):
        none = self.adapter({"GDV_UNITY_HUB_PATHS": str(self.base / "empty")}).build(self.project, "Validation", self.base / "b")
        self.assertEqual(constants.BLOCKED, none.status)
        self.assertIn("6000.6.7f1 is not installed", none.reason)
        env = build_hub(self.base / "hub2", windows_module=False)
        self.assertIn("Windows Build Support", self.adapter(env).build(self.project, "Validation", self.base / "b").reason)

    def test_missing_il2cpp_module_is_blocked_with_the_owner_action(self):
        self.profile(executeMethod="X.Y", productName="P2", companyName="C2", scriptingBackend="il2cpp")
        env = build_hub(self.base / "hub", il2cpp=False)
        outcome = self.adapter(env).build(self.project, "Validation", self.base / "b")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("IL2CPP", outcome.reason)
        self.assertEqual(constants.BLOCKED, outcome.details["layer.export"])
        env_ok = build_hub(self.base / "hub3", il2cpp=True)
        self.assertNotIn("IL2CPP", self.adapter(env_ok).build(self.project, "Validation", self.base / "b2").reason)

    def test_successful_build_returns_the_player_and_layers_and_uses_the_hook(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        with self.fake_unity():
            outcome = adapter.build(self.project, "Validation", self.base / "job" / "build")
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        self.assertEqual("Dev Product Validation.exe", outcome.artifacts[0].name)
        self.assertEqual({"layer.compile", "layer.export", "layer.nativeBuild"}, set(outcome.details))

    def test_the_unity_build_uses_the_configured_accelerator(self):
        env = build_hub(self.base / "hub-acc")
        home = self.base / "gdv-home"
        home.mkdir()
        (home / "host.json").write_text(json.dumps({"unityAccelerator": {"endpoint": "127.0.0.1:10080", "namespacePrefix": "Game"}}),
                                        encoding="utf-8")
        seen = []

        def capture(argv, *rest, **options):
            seen.append(list(argv))
            return supervise.SuperviseResult(constants.FAIL, "stopped by the test", 1, 0.0, 0.0)

        with mock.patch.dict(os.environ, {core.HOME_ENV: str(home)}), mock.patch.object(support, "run_unity", capture):
            self.adapter(env).build(self.project, "Validation", self.base / "job-acc" / "build")
        argv = seen[0]
        self.assertEqual("127.0.0.1:10080", argv[argv.index("-cacheServerEndpoint") + 1])
        self.assertEqual("Game", argv[argv.index("-cacheServerNamespacePrefix") + 1])
        self.assertIn("-EnableCacheServer", argv)

    def test_failed_build_names_compile_errors_and_is_never_installable(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        with self.fake_unity(write_exe=False, status=constants.FAIL):
            outcome = adapter.build(self.project, "Validation", self.base / "job" / "build")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertEqual({"layer.nativeBuild": constants.FAIL}, dict(outcome.details))

    def test_wrong_identity_in_the_built_player_fails_the_build(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        with self.fake_unity(company="Dev Co", product="Dev Product"):
            outcome = adapter.build(self.project, "Validation", self.base / "job" / "build")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertIn("did not apply", outcome.reason)

    # -- install, files ---------------------------------------------------------------------------------------------------------

    def install(self, adapter):
        with self.fake_unity():
            built = adapter.build(self.project, "Validation", self.base / "job" / "build")
        device = adapter.discover()[0]
        installed = adapter.install(device, built.artifacts[0])
        self.assertEqual(constants.PASS, installed.status, installed.reason)
        return device, built.artifacts[0]

    def test_install_refuses_a_missing_or_unidentified_player(self):
        adapter = self.adapter()
        device = adapter.discover()[0]
        self.assertEqual(constants.BLOCKED, adapter.install(device, self.base / "nope.exe").status)
        bare = self.base / "bare.exe"
        bare.write_bytes(b"MZ")
        self.assertIn("app.info", adapter.install(device, bare).reason)

    def test_files_go_to_the_validation_data_folder_and_cannot_escape_it(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        device, _ = self.install(adapter)
        plan = self.base / "plan.json"
        plan.write_text("{}", encoding="utf-8")
        self.assertEqual(constants.PASS, adapter.push(device, plan, "gdv/inbox/plan.json").status)
        pushed = self.local_low / "Dev Validation" / "Dev Product Validation" / "gdv" / "inbox" / "plan.json"
        self.assertTrue(pushed.is_file())
        for bad in ("../x", "/etc/x", "C:/x", "gdv/../../x", ""):
            self.assertEqual(constants.BLOCKED, adapter.push(device, plan, bad).status, bad)
        self.assertFalse((self.local_low / "Dev Validation" / "x").exists())
        empty = self.base / "empty-out"
        self.assertEqual(constants.PASS, adapter.pull(device, "gdv/out", empty).status)
        self.assertTrue(empty.is_dir())
        self.assertEqual(constants.BLOCKED, adapter.pull(device, "gdv/missing.json", self.base / "m.json").status)

    def test_install_keeps_stale_harness_output_aside_and_touches_nothing_else(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        root = self.local_low / "Dev Validation" / "Dev Product Validation"
        (root / "gdv" / "out" / "old").mkdir(parents=True)
        (root / "other.txt").write_text("keep", encoding="utf-8")
        self.install(adapter)
        self.assertFalse((root / "gdv").exists(), "the new run starts from an empty harness area")
        kept = [p for p in root.iterdir() if p.name.startswith("gdv.retained-")]
        self.assertEqual(1, len(kept), "an earlier run's data is not deleted by another run")
        self.assertTrue((kept[0] / "out" / "old").is_dir())
        self.assertEqual("INTERRUPTED", json.loads((kept[0] / "retained.json").read_text(encoding="utf-8"))["status"])
        self.assertTrue((root / "other.txt").exists())

    def test_a_passing_run_deletes_its_data_and_a_failed_one_keeps_it_with_a_manifest(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        device, _ = self.install(adapter)
        root = self.local_low / "Dev Validation" / "Dev Product Validation"
        (root / "gdv" / "out" / "run-1").mkdir(parents=True)
        self.assertEqual(constants.PASS, adapter.cleanup_run_data(device).status)
        self.assertFalse((root / "gdv").exists())
        (root / "gdv" / "out" / "run-2").mkdir(parents=True)
        outcome = adapter.retain_run_data(device, constants.STALL, "no heartbeat")
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        kept = Path(outcome.details["retainedAt"])
        self.assertFalse((root / "gdv").exists())
        self.assertTrue((kept / "out" / "run-2").is_dir())
        manifest = json.loads((kept / "retained.json").read_text(encoding="utf-8"))
        self.assertEqual(("STALL", "no heartbeat"), (manifest["status"], manifest["reason"]))
        self.assertEqual(constants.PASS, adapter.retain_run_data(device, constants.FAIL, "x").status, "nothing left, nothing kept")
        (root / "gdv").mkdir(parents=True)
        self.assertNotIn("retainedAt", adapter.retain_run_data(device, constants.FAIL, "x").details, "an empty folder is not worth keeping")

    def test_prelaunch_files_outside_the_harness_folder_follow_the_run_data(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        device, _ = self.install(adapter)
        root = self.local_low / "Dev Validation" / "Dev Product Validation"
        config = self.base / "cameras.json"
        config.write_text('{"fps": 120}', encoding="utf-8")
        self.assertEqual(constants.PASS, adapter.push(device, config, "Video/cameras.json").status)
        self.assertTrue((root / "Video" / "cameras.json").is_file())
        self.assertEqual(constants.PASS, adapter.cleanup_run_data(device).status)
        self.assertFalse((root / "Video" / "cameras.json").exists(), "a passing run leaves no pre-launch file for the next run")
        self.assertEqual(constants.PASS, adapter.push(device, config, "Video/cameras.json").status)
        outcome = adapter.retain_run_data(device, constants.FAIL, "scenario failed")
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        kept = Path(outcome.details["retainedAt"])
        self.assertFalse((root / "Video" / "cameras.json").exists())
        self.assertEqual('{"fps": 120}', (kept / "prelaunch" / "Video" / "cameras.json").read_text(encoding="utf-8"),
                         "a failed run keeps the pre-launch file with its diagnostics")

    def test_prelaunch_files_are_not_silently_left_behind(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        device, _ = self.install(adapter)
        root = self.local_low / "Dev Validation" / "Dev Product Validation"
        config = self.base / "cameras.json"
        config.write_text("{}", encoding="utf-8")
        # A partial copy is recorded before it starts, so it is cleaned like a whole one.
        with mock.patch.object(support, "copy_tolerant", return_value="disk full"):
            self.assertEqual(constants.FAIL, adapter.push(device, config, "Video/cameras.json").status)
        (root / "Video").mkdir(parents=True, exist_ok=True)
        (root / "Video" / "cameras.json").write_text("{", encoding="utf-8")
        # A file that cannot be removed is BLOCKED, never a silent pass.
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            outcome = adapter.cleanup_run_data(device)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("locked", outcome.reason)
        self.assertEqual(constants.PASS, adapter.cleanup_run_data(device).status)
        self.assertFalse((root / "Video" / "cameras.json").exists(), "the partial copy was cleaned")
        # A player that is still running leaves the files in place and says so, also when the harness folder is empty.
        self.assertEqual(constants.PASS, adapter.push(device, config, "Video/cameras.json").status)
        adapter._process = mock.Mock(poll=mock.Mock(return_value=None))
        outcome = adapter.retain_run_data(device, constants.STALL, "no heartbeat")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("pre-launch files are left in place", outcome.reason)
        self.assertTrue((root / "Video" / "cameras.json").is_file())

    # -- run -------------------------------------------------------------------------------------------------------------------

    def pulled(self, adapter, device, name, wait=15.0):
        deadline = time.monotonic() + wait
        folder = self.base / name
        while True:
            self.assertEqual(constants.PASS, adapter.pull(device, "gdv/out", folder).status)
            result = folder / "run-1" / "result.json"
            found = result if result.exists() else next(iter(folder.glob("*/result.json")), None)
            if found is not None or time.monotonic() > deadline:
                return folder, found
            time.sleep(0.1)

    def launch(self, adapter, device, exe):
        log = self.base / "run" / "console.log"
        plan = self.base / "plan.json"
        plan.write_text("{}", encoding="utf-8")
        adapter.push(device, plan, "gdv/inbox/plan.json")
        outcome = adapter.launch(device, str(exe), [], log)
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        return log

    def test_run_reads_the_harness_result_and_terminate_ends_the_player(self):
        self.mode = "pass"
        adapter = self.adapter(build_hub(self.base / "hub"))
        device, exe = self.install(adapter)
        self.launch(adapter, device, exe)
        folder, result = self.pulled(adapter, device, "got")
        self.assertEqual("PASS", json.loads(result.read_text(encoding="utf-8"))["status"])
        self.assertEqual(constants.PASS, adapter.terminate(device, str(exe)).status)
        self.assertTrue(adapter._process.poll() is not None)  # noqa: SLF001
        self.assertEqual(constants.PASS, adapter.terminate(device, str(exe)).status, "terminate is repeatable")

    def test_terminate_kills_the_whole_tree_including_a_grandchild(self):
        self.mode = "hang"
        adapter = self.adapter(build_hub(self.base / "hub"))
        device, exe = self.install(adapter)
        self.launch(adapter, device, exe)
        pid_file = self.local_low / "Dev Validation" / "Dev Product Validation" / "child.pid"
        deadline = time.monotonic() + 15
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        child = int(pid_file.read_text())
        self.assertTrue(core.process_alive(child))
        outcome = adapter.terminate(device, str(exe))
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        deadline = time.monotonic() + 10
        while core.process_alive(child) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(core.process_alive(child), "the grandchild died with the Job Object / process group")
        shot = self.base / "shots" / "failure.png"
        self.assertEqual(constants.PASS, adapter.screenshot(device, shot).status)
        self.assertTrue(shot.is_file(), "the screen was captured before the player was killed")

    def test_a_player_that_exits_without_a_result_becomes_a_failure_at_once(self):
        self.mode = "exit"
        adapter = self.adapter(build_hub(self.base / "hub"))
        device, exe = self.install(adapter)
        self.launch(adapter, device, exe)
        folder, result = self.pulled(adapter, device, "got")
        document = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(constants.FAIL, document["status"])
        self.assertIn("code 3", document["reason"])
        adapter.terminate(device, str(exe))

    def test_a_player_that_writes_its_result_and_exits_is_a_pass(self):
        self.mode = "pass-exit"
        adapter = self.adapter(build_hub(self.base / "hub"))
        device, exe = self.install(adapter)
        self.launch(adapter, device, exe)
        for attempt in range(8):
            folder, result = self.pulled(adapter, device, f"got{attempt}")
            self.assertEqual("PASS", json.loads(result.read_text(encoding="utf-8"))["status"])
        adapter.terminate(device, str(exe))

    def test_install_refuses_a_player_built_under_the_projects_own_identity(self):
        adapter = self.adapter(build_hub(self.base / "hub"))
        adapter.use_project(self.project)
        own = self.base / "own" / "Dev Product.exe"
        (self.base / "own" / "Dev Product_Data").mkdir(parents=True)
        own.write_bytes(b"MZ")
        (self.base / "own" / "Dev Product_Data" / "app.info").write_text("Dev Co\nDev Product\n", encoding="utf-8")
        outcome = adapter.install(adapter.discover()[0], own)
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("developer's data", outcome.reason)
        self.assertFalse((self.local_low / "Dev Co").exists())

    def test_a_marker_line_from_the_player_decides_the_status(self):
        self.mode = "exit-marker"
        adapter = self.adapter(build_hub(self.base / "hub"))
        device, exe = self.install(adapter)
        log = self.launch(adapter, device, exe)
        # the stub prints to stdout; the player's own -logFile is what the marker reader looks at
        deadline = time.monotonic() + 15
        while adapter._process.poll() is None and time.monotonic() < deadline:  # noqa: SLF001
            time.sleep(0.1)
        log.write_text("GDV-RESULT NEEDS_PERMISSION camera permission\n", encoding="utf-8")
        folder, result = self.pulled(adapter, device, "got")
        document = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(constants.NEEDS_PERMISSION, document["status"])
        adapter.terminate(device, str(exe))

    def test_screenshot_falls_back_to_a_harness_picture_and_otherwise_blocks(self):
        adapter = self.adapter(build_hub(self.base / "hub"), run=lambda argv, timeout: supervise.BoundedResult(constants.FAIL, "no desktop", 1, ""))
        device, _ = self.install(adapter)
        self.assertEqual(constants.BLOCKED, adapter.screenshot(device, self.base / "s" / "a.png").status)
        shots = self.local_low / "Dev Validation" / "Dev Product Validation" / "gdv" / "out" / "r1" / "screenshots"
        shots.mkdir(parents=True)
        (shots / "end.png").write_bytes(b"png")
        self.assertEqual(constants.PASS, adapter.screenshot(device, self.base / "s" / "b.png").status)

    def test_a_session_without_a_desktop_blocks_install_and_launch_but_not_the_build(self):
        env = build_hub(self.base / "hub")
        adapter = self.adapter(env)
        with self.fake_unity():
            built = adapter.build(self.project, "Validation", self.base / "job" / "build")
        self.desktop = False
        self.assertEqual(constants.PASS, built.status)
        self.assertIn("desktop", adapter.probe().blocked)
        device = adapter.discover()[0]
        for outcome in (adapter.install(device, built.artifacts[0]), adapter.launch(device, str(built.artifacts[0]), [], self.base / "c.log")):
            self.assertEqual(constants.BLOCKED, outcome.status)
            self.assertIn("no interactive desktop", outcome.reason)

    def test_launch_refuses_without_an_installed_player(self):
        adapter = self.adapter()
        outcome = adapter.launch(adapter.discover()[0], "x", [], self.base / "c.log")
        self.assertEqual(constants.BLOCKED, outcome.status)

    def test_health_reports_free_space_and_says_what_is_not_known(self):
        health = self.adapter().health(self.adapter().discover()[0])
        self.assertTrue(health.reachable)
        self.assertIsNone(health.locked)
        self.assertGreater(health.free_space_mb, 0)


class HostSelectionTests(unittest.TestCase):
    """A request for the windows or editor target is served by the local Windows host, and by it over SSH for a Mac caller."""

    def capabilities(self, adapter):
        return resolver.HostCapabilities(
            build_platforms=("windows", "editor") if adapter.probe().can("build") else (), devices=tuple(adapter.discover()))

    def test_windows_target_stays_local_on_windows_and_goes_over_ssh_from_elsewhere(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        env = build_hub(Path(folder.name) / "hub")
        windows_host = WindowsAdapter(Path(folder.name), env, is_windows=True)
        mac_host = WindowsAdapter(Path(folder.name), env, is_windows=False)
        local_win = resolver.HostInfo("laptop", "local", "windows")
        local_mac = resolver.HostInfo("mac", "local", "macos")
        ssh_win = resolver.HostInfo("laptop", "ssh", "windows", "u@laptop")
        request = resolver.Request("windows", build=True, device=True)
        on_windows = resolver.HostResolver(local_win, [], lambda host: self.capabilities(windows_host)).resolve(request)
        self.assertEqual(("laptop", "local", resolver.SELECTED_LOCAL), (on_windows.host.id, on_windows.transport, on_windows.selected_by))
        probes = {"mac": self.capabilities(mac_host), "laptop": self.capabilities(windows_host)}
        from_mac = resolver.HostResolver(local_mac, [ssh_win], lambda host: probes[host.id]).resolve(request)
        self.assertEqual(("laptop", "ssh", resolver.SELECTED_CAPABILITY), (from_mac.host.id, from_mac.transport, from_mac.selected_by))
        nowhere = resolver.HostResolver(local_mac, [], lambda host: probes[host.id]).resolve(request)
        self.assertEqual(constants.BLOCKED, nowhere.status)

    def test_pulling_a_run_from_a_windows_host_uses_forward_slashes(self):
        from valtrion import remote
        seen = []

        class Fake:
            def get(self, source, local, timeout):
                seen.append(source)
                return None

        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        remote.pull_run(Fake(), "C:\\Users\\me\\.valtrion\\artifacts\\P\\abc\\run1", Path(folder.name) / "x")
        remote.pull_run(Fake(), "/Users/me/artifacts/P/abc/run1/", Path(folder.name) / "y")
        self.assertEqual(["C:/Users/me/.valtrion/artifacts/P/abc/run1/.", "/Users/me/artifacts/P/abc/run1/."], seen)

    def test_run_accepts_target_as_the_platform_option(self):
        import argparse
        parser = argparse.ArgumentParser()
        commands._configure_run(parser)
        arguments = parser.parse_args(["p.json", "--repo", "r", "--sha", "a" * 40, "--app", "x", "--target", "windows"])
        self.assertEqual("windows", arguments.platform)

    def test_doctor_lists_the_windows_il2cpp_module_only_when_installed(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        with_module = build_hub(Path(folder.name) / "a", il2cpp=True)
        without = build_hub(Path(folder.name) / "b", il2cpp=False)
        self.assertEqual(["windows", "windows-il2cpp"], sorted(doctor.find_editors("windows", with_module)[0]["modules"]))
        self.assertEqual(["windows"], doctor.find_editors("windows", without)[0]["modules"])


if __name__ == "__main__":
    unittest.main()
