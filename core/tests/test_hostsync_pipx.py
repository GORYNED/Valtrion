"""`hosts sync` with pipx, the virtual-environment fallback with its stable link and pruning, and the PATH diagnosis."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import __version__, config, core, hostsync, remote
from valtrion.transport import CommandResult

from .test_hostsync import HOME, FakeHost, entry, isolate_host_id, run


class SyncWithPipxTests(unittest.TestCase):
    def setUp(self):
        self.wheel = Path("valtrion-0.7.0-py3-none-any.whl")
        self.recorded = []

    def sync(self, host, e):
        return hostsync.sync_host(e, host, self.wheel, lambda h, c, p: self.recorded.append((h, c, p)))

    def test_host_with_pipx_gets_the_short_pipx_shim_and_no_version_folder(self):
        host = FakeHost(pipx=True)
        row = self.sync(host, entry())
        command = f"{HOME}/.local/bin/valtrion"
        self.assertEqual((hostsync.UPDATED, command, "pipx"), (row.status, row.command, row.method))
        self.assertEqual([("mac", command, "/opt/homebrew/bin/python3")], self.recorded)
        install = next(c for c in host.calls if "install" in c)
        self.assertEqual(["/opt/homebrew/bin/python3", "-m", "pipx", "install", "--force", "--python", "/opt/homebrew/bin/python3",
                          f"{HOME}/.valtrion/incoming/{self.wheel.name}"], install)
        self.assertFalse(any(c[1:3] == ["-m", "venv"] for c in host.calls), "no per-version environment when pipx is there")
        self.assertFalse(any(c[2:3] == ["prune"] for c in host.calls), "pipx hosts are not pruned unless asked")
        self.assertTrue(any(c[2:3] == ["relink"] for c in host.calls), "a link left from the fallback follows the new command")

    def test_prune_on_a_pipx_host_is_opt_in(self):
        host = FakeHost(pipx=True, prune_output="removed=0.8.5\n")
        row = hostsync.sync_host(entry(), host, self.wheel, lambda h, c, p: None, prune_pipx=True)
        self.assertIn("removed old versions 0.8.5", row.reason)
        prune = next(c for c in host.calls if c[2:3] == ["prune"])
        self.assertEqual([f"{HOME}/.valtrion", __version__, f"{HOME}/.local/bin/valtrion"], prune[3:])

    def test_pipx_sync_changes_nothing_when_the_idle_check_cannot_be_made(self):
        for host in (FakeHost(pipx=True, busy_fails=True), FakeHost(pipx=True, copy_fails=True)):
            row = self.sync(host, entry())
            self.assertEqual(hostsync.BLOCKED, row.status)
            self.assertIn("cannot tell", row.reason)
            self.assertFalse(any("install" in c for c in host.calls))
            self.assertEqual([], self.recorded)

    def test_half_done_pipx_install_says_so(self):
        class Broken(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                if command[-1] == "--version" and command[0].endswith("valtrion"):
                    return CommandResult(127, "", "not found")
                return super().run(command, timeout_sec, cwd)

        row = self.sync(Broken(pipx=True), entry(command=f"{HOME}/.local/bin/valtrion"))
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertIn("replaced its environment", row.reason)
        self.assertIn("by hand", row.reason)
        self.assertEqual([], self.recorded)

    def test_pipx_is_found_next_to_the_python_when_it_is_not_a_module(self):
        class NextToPython(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                if command[1:3] == ["-m", "pipx"]:
                    return CommandResult(1, "", "No module named pipx")
                if command[0] != "/opt/homebrew/bin/pipx" and command[0].endswith("pipx"):
                    return CommandResult(127, "", "not found")
                return super().run(command, timeout_sec, cwd)

        host = NextToPython(pipx=True)
        row = self.sync(host, entry(python="/opt/homebrew/bin/python3"))
        self.assertEqual("pipx", row.method)
        self.assertTrue(any(c[:2] == ["/opt/homebrew/bin/pipx", "install"] for c in host.calls))

    def test_pipx_install_failure_is_blocked_without_recording(self):
        class Failing(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                if "install" in command and "pipx" in " ".join(command):
                    return CommandResult(1, "", "boom")
                return super().run(command, timeout_sec, cwd)

        row = self.sync(Failing(pipx=True), entry())
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertIn("pipx", row.reason)
        self.assertEqual([], self.recorded)

    def test_a_running_job_in_the_pipx_environment_stops_the_sync_before_anything_is_replaced(self):
        host = FakeHost(pipx=True, busy=True, installed="0.6.0")
        row = self.sync(host, entry(command=f"{HOME}/.local/bin/valtrion"))
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertIn("not idle", row.reason)
        self.assertFalse(any("install" in c for c in host.calls))
        self.assertEqual([], self.recorded)

    def test_pipx_missing_falls_back_to_a_venv_with_a_stable_link_and_prunes_other_versions(self):
        host = FakeHost(pipx=False, prune_output="removed=0.8.5\nbusy=0.8.4\n")
        row = self.sync(host, entry())
        self.assertEqual("venv", row.method)
        link = next(c for c in host.calls if c[2:3] == ["link"])
        self.assertEqual([f"{HOME}/.valtrion/bin/valtrion", f"{HOME}/.valtrion/core/{__version__}/bin/valtrion"], link[3:])
        prune = next(c for c in host.calls if c[2:3] == ["prune"])
        self.assertEqual([f"{HOME}/.valtrion", __version__, f"{HOME}/.valtrion/bin/valtrion"], prune[3:],
                         "only versions below the new one are candidates; the recorded command is protected")
        self.assertIn("removed old versions 0.8.5", row.reason)
        self.assertIn("kept 0.8.4 (a process uses it)", row.reason)
        order = ["venv", "pip", "link", "prune"]
        steps = [c[1:3] == ["-m", "venv"] and "venv" or c[1:3] == ["-m", "pip"] and "pip" or c[2:3] == ["link"] and "link"
                 or c[2:3] == ["prune"] and "prune" for c in host.calls]
        steps = [step for step in steps if step]
        self.assertEqual(order, steps, "the link moves only after the new version works, and pruning comes last")

    def test_failed_install_leaves_the_stable_link_and_old_versions_alone(self):
        host = FakeHost(version_after="0.6.9")
        row = self.sync(host, entry())
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertFalse(any(c[2:3] in (["link"], ["prune"]) for c in host.calls))

    def test_versioned_command_at_the_current_version_is_migrated(self):
        old = f"{HOME}/.valtrion/core/{__version__}/bin/valtrion"
        row = self.sync(FakeHost(installed=__version__, pipx=True), entry(command=old))
        self.assertEqual((hostsync.UPDATED, f"{HOME}/.local/bin/valtrion"), (row.status, row.command))

    def test_short_command_at_the_current_version_is_left_alone(self):
        host = FakeHost(installed=__version__, pipx=True)
        row = self.sync(host, entry(command=f"{HOME}/.local/bin/valtrion"))
        self.assertEqual(hostsync.OK, row.status)
        self.assertEqual([], host.puts)

    def test_is_versioned_command(self):
        for command in ("/h/.valtrion/core/0.8.6/bin/valtrion", "C:\\Users\\d\\.valtrion\\core\\0.8.6\\Scripts\\valtrion.exe"):
            self.assertTrue(hostsync.is_versioned_command(command), command)
        for command in ("/h/.local/bin/valtrion", "/h/.valtrion/bin/valtrion", "C:\\Users\\d\\.valtrion\\bin\\valtrion.cmd"):
            self.assertFalse(hostsync.is_versioned_command(command), command)


class PathCheckTests(unittest.TestCase):
    command = f"{HOME}/.local/bin/valtrion"

    def check(self, host, os_name="macos"):
        return hostsync.path_check(entry(os_name=os_name, command=self.command), host)

    def test_found_by_name_is_ok(self):
        self.assertEqual("ok", self.check(FakeHost(installed=__version__))["status"])

    def test_not_found_by_name_is_a_warning_with_the_one_line_fix(self):
        host = FakeHost(installed=__version__, by_name=False)
        check = self.check(host)
        self.assertEqual("warning", check["status"])
        self.assertEqual(["valtrion", "--version"], host.calls[0], "the plain name is asked, not the recorded path")
        self.assertIn(self.command, check["reason"])
        self.assertIn(f"echo 'export PATH=\"{HOME}/.local/bin:$PATH\"' >> ~/.zshenv", check["action"])
        self.assertIn("--ensure-path", check["action"])

    def test_permission_problem_is_not_reported_as_not_found(self):
        class Denied(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                return CommandResult(126, "", "permission denied")

        self.assertIn("cannot be run", self.check(Denied())["reason"])

    def test_failure_of_the_program_itself_is_not_reported_as_not_found(self):
        class Failing(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                return CommandResult(1, "", "Traceback: something broke")

        self.assertNotIn("not found by name", self.check(Failing(), "windows")["reason"])

    def test_the_path_line_in_the_fix_is_shell_quoted(self):
        fix = hostsync.path_fix(entry(), "/Users/a b/.local/bin/valtrion")
        self.assertIn("'export PATH=\"/Users/a b/.local/bin:$PATH\"'", fix)

    def test_another_version_by_name_is_a_warning(self):
        self.assertEqual("warning", self.check(FakeHost(installed="0.1.0"))["status"])

    def test_no_recorded_command_or_unreachable_host_gives_no_verdict(self):
        self.assertIsNone(hostsync.path_check(entry(), FakeHost()))
        self.assertIsNone(self.check(FakeHost(down=True)))

    def test_fix_for_linux_and_windows_names_the_folder(self):
        self.assertIn("~/.bashrc", hostsync.path_fix(entry(os_name="linux"), self.command))
        fix = hostsync.path_fix(entry(os_name="windows"), "C:\\Users\\dev\\.local\\bin\\valtrion.exe")
        self.assertIn("C:\\Users\\dev\\.local\\bin", fix)

    def test_ensure_path_is_only_done_on_macos_and_only_through_the_helper(self):
        host = FakeHost()
        self.assertIn("added", hostsync.ensure_path(entry(), host, self.command))
        path = next(c for c in host.calls if c[2:3] == ["path"])
        self.assertEqual(["~/.zshenv", f"{HOME}/.local/bin"], path[3:])
        other = FakeHost()
        self.assertIn("only done on macOS", hostsync.ensure_path(entry(os_name="linux"), other, self.command))
        self.assertFalse(any(c[2:3] == ["path"] for c in other.calls))


class HostScriptTests(unittest.TestCase):
    """The helper that runs on the hosts, run here against a temporary folder."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.root = Path(self._folder.name)
        self.script = self.root / "helper.py"
        self.script.write_text(hostsync.HOST_SCRIPT, encoding="utf-8")

    def helper(self, *args):
        return subprocess.run([sys.executable, str(self.script), *args], capture_output=True, text=True, check=False)

    @unittest.skipIf(os.name == "nt", "a symlink needs a privilege on Windows; the .cmd form is checked below")
    def test_link_follows_the_newest_version(self):
        link = self.root / "bin" / "valtrion"
        self.helper("link", str(link), "/a/0.8.5/bin/valtrion")
        self.helper("link", str(link), "/a/0.8.6/bin/valtrion")
        self.assertEqual("/a/0.8.6/bin/valtrion", os.readlink(link))

    def test_windows_form_is_a_cmd_file(self):
        namespace: dict = {}
        exec(compile(hostsync.HOST_SCRIPT.split("mode, rest = sys.argv")[0], "helper", "exec"), namespace)
        target = self.root / "bin" / "valtrion.cmd"
        with mock.patch.object(os, "name", "nt"):
            namespace["link"](str(target), "C:\\v\\valtrion.exe")
        self.assertEqual('@echo off\r\n"C:\\v\\valtrion.exe" %*\r\n', target.read_bytes().decode())

    def test_path_line_is_added_once_and_existing_content_is_kept(self):
        rc = self.root / "zshenv"
        rc.write_text("export A=1", encoding="utf-8")
        self.assertIn("added", self.helper("path", str(rc), "/h/.local/bin").stdout)
        self.assertIn("present", self.helper("path", str(rc), "/h/.local/bin").stdout)
        self.assertEqual('export A=1\nexport PATH="/h/.local/bin:$PATH"  # valtrion\n', rc.read_text(encoding="utf-8"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.home = Path(self._folder.name) / "home"
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        isolate_host_id(self)
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/artifacts", home=self.home)

    def test_hosts_sync_records_the_pipx_shim(self):
        with mock.patch.object(remote, "make_transport", return_value=FakeHost(pipx=True)):
            code, out, _ = run("hosts", "--json", "sync")
        self.assertEqual(0, code, out)
        self.assertEqual("pipx", json.loads(out)[0]["method"])
        self.assertEqual(f"{HOME}/.local/bin/valtrion", config.find_host("mac", self.home).valtrion_command)

    def test_ensure_path_adds_the_line_only_with_the_flag(self):
        host = FakeHost(pipx=True)
        with mock.patch.object(remote, "make_transport", return_value=host):
            run("hosts", "sync")
        self.assertFalse(any(c[2:3] == ["path"] for c in host.calls))
        with mock.patch.object(remote, "make_transport", return_value=host):
            code, out, _ = run("hosts", "sync", "--ensure-path")
        self.assertEqual(0, code, out)
        self.assertTrue(any(c[2:3] == ["path"] for c in host.calls))
        self.assertIn(".zshenv", out)

    def test_doctor_host_reports_a_command_that_is_not_on_the_path_by_name(self):
        config.set_host_command("mac", "/opt/v/bin/valtrion", home=self.home)
        report = {"host": {"id": "mac", "os": "macos"}, "core": {"version": __version__}, "capabilities": {
            "unityEditors": [], "buildPlatforms": [], "iosSigning": False, "adb": False}, "disk": {"freeGb": 1, "totalGb": 2, "path": "/"},
            "githubRunner": {"state": "none"}, "blocked": {}, "devices": []}

        class Reporting(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                if command[0] == "valtrion":
                    return CommandResult(127, "", "valtrion: command not found")
                return CommandResult(0, json.dumps(report))

        with mock.patch.object(remote, "make_transport", return_value=Reporting()):
            code, out, err = run("doctor", "--host", "mac", "--json")
        self.assertEqual(0, code, "a PATH problem is a warning, not a block")
        self.assertEqual("warning", json.loads(out)["valtrionOnPath"]["status"])
        self.assertIn("not found by name", err)
        self.assertIn(".zshenv", err)


if __name__ == "__main__":
    unittest.main()
