"""`valtrion unity run` with a fake Unity (tests/fake_unity.py): statuses and exit codes, leases and their order, the watchdog, the
Accelerator switches, the Editor lookup and the remote forward. No Unity Editor is started."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, commands, config, constants, core, lease, remote, supervise, unityrun  # noqa: F401  (cli first: it registers the commands)
from valtrion.transport import CommandResult, Transport

from .test_host import HostTestCase, run as run_cli
from .test_platform_editor import make_hub

FAKE = str(Path(__file__).with_name("fake_unity.py"))
OTHER = "other@host"


class UnityRunTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.project = self.base / "Proj"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.manager = lease.LeaseManager(self.home)
        patch = mock.patch.dict(os.environ, {"FAKE_UNITY_LEASES": str(self.manager.directory)})
        patch.start()
        self.addCleanup(patch.stop)
        host = mock.patch.object(core, "local_host_id", return_value="this-machine")
        host.start()
        self.addCleanup(host.stop)

    def unity(self, *fake_args, log="Editor.log", project=True, **options):
        """Run the fake as Unity; returns (exit code, stdout lines, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        args = [FAKE, "-batchmode"]
        if project:
            args += ["-projectPath", str(self.project)]
        if log:
            args += ["-logFile", str(self.logs / log)]
        args += list(fake_args)
        options.setdefault("unity", sys.executable)
        options.setdefault("lease_wait_sec", 0)
        code = unityrun.run(args, home=self.home, out=out, err=err, poll_sec=0.1, cancel_poll_sec=0.1, **options)
        return code, out.getvalue().splitlines(), err.getvalue()

    def occupy(self, resource, slot=False):
        """Another live holder (this process, another session) of a lease."""
        if slot:
            result = self.manager.acquire_slot(lease.UNITY_EDITOR_RESOURCE, 1, OTHER, "other", 300, "busy", os.getpid())
        else:
            result = self.manager.try_acquire(resource, OTHER, "other", 300, "busy", os.getpid())
        self.assertTrue(result.acquired)
        return result.lease.resource

    def assertNoLeases(self):
        self.assertEqual([], [p.name for p in self.manager.directory.glob("*.lock")] if self.manager.directory.is_dir() else [])


class StatusTests(UnityRunTestCase):
    def test_exit_zero_is_pass_and_the_last_stdout_line_is_the_status(self):
        code, lines, _ = self.unity()
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS"), (code, lines[-1]))

    def test_tests_failed_and_launch_errors_are_fail(self):
        for exit_code in (2, 3):
            code, lines, _ = self.unity("--exit", str(exit_code))
            self.assertEqual((constants.EXIT_FAIL, "STATUS FAIL"), (code, lines[-1]), exit_code)

    def test_exit_one_is_fail_unless_the_log_names_a_project_or_license_problem(self):
        code, lines, _ = self.unity("--exit", "1")
        self.assertEqual((constants.EXIT_FAIL, "STATUS FAIL"), (code, lines[-1]))
        for text in ("Another Unity instance is running with this project open.", "Cannot open the same project twice",
                     "No valid Unity Editor license found", "Unable to activate", "License activation failed",
                     "license activation error 5"):
            code, lines, _ = self.unity("--exit", "1", "--text", text)
            self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]), text)
            self.assertIn("log says", lines[-2])

    def test_the_license_text_does_not_turn_another_exit_code_into_blocked(self):
        code, lines, _ = self.unity("--exit", "2", "--text", "unable to activate")
        self.assertEqual("STATUS FAIL", lines[-1])

    def test_without_logfile_the_console_output_is_used_and_echoed(self):
        code, lines, err = self.unity("--exit", "1", "--text", "No valid Unity Editor license", "--print", log=None)
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertIn("No valid Unity Editor license", err)

    def test_an_editor_that_cannot_start_is_blocked(self):
        code, lines, _ = self.unity(unity=str(self.base / "missing"))
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
        self.assertTrue(any("does not exist" in line for line in lines))
        self.assertNoLeases()

    def test_a_file_that_is_not_executable_is_blocked_as_cannot_start(self):
        plain = self.base / "Unity"
        plain.write_text("not a program", encoding="utf-8")
        if os.name != "nt":
            plain.chmod(0o644)
        code, lines, _ = self.unity(unity=str(plain))
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertNoLeases()

    def test_a_missing_project_is_blocked_before_any_lease(self):
        code, lines, _ = self.unity(project=False)
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertIn("no Unity project", lines[0])
        self.assertNoLeases()


class WatchdogTests(UnityRunTestCase):
    def test_a_log_that_stops_growing_is_a_stall(self):
        started = time.monotonic()
        code, lines, _ = self.unity("--sleep", "30", stall_sec=1.0)
        self.assertEqual((constants.EXIT_STALL, "STATUS STALL"), (code, lines[-1]))
        self.assertLess(time.monotonic() - started, 20)
        self.assertNoLeases()

    def test_a_growing_log_is_not_a_stall_but_the_timeout_ends_the_run(self):
        code, lines, _ = self.unity("--sleep", "30", "--grow", stall_sec=1.0, timeout_sec=2.0)
        self.assertEqual((constants.EXIT_STALL, "STATUS TIMEOUT"), (code, lines[-1]))

    def test_without_logfile_the_stall_check_is_off(self):
        code, lines, _ = self.unity("--sleep", "2", log=None, stall_sec=0.5)
        self.assertEqual("STATUS PASS", lines[-1])

    def test_a_zero_stall_window_turns_it_off(self):
        code, lines, _ = self.unity("--sleep", "2", stall_sec=0)
        self.assertEqual("STATUS PASS", lines[-1])

    def test_the_whole_process_is_killed_on_timeout(self):
        dump = self.base / "dump"
        code, lines, _ = self.unity("--sleep", "30", "--dump", str(dump), "--grow", timeout_sec=1.5)
        self.assertEqual("STATUS TIMEOUT", lines[-1])
        pid = int((dump / "pid").read_text())
        for _ in range(50):
            if not core.process_alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(core.process_alive(pid))


class LeaseTests(UnityRunTestCase):
    def test_both_leases_are_held_while_unity_runs_and_released_after(self):
        dump = self.base / "dump"
        code, lines, _ = self.unity("--dump", str(dump))
        self.assertEqual("STATUS PASS", lines[-1])
        names = json.loads((dump / "leases.json").read_text())
        project = lease.lease_file_name(lease.unity_project_resource(self.project))
        slot = lease.lease_file_name(lease.slot_resource(lease.UNITY_EDITOR_RESOURCE, 1))
        self.assertIn(project, names)
        self.assertIn(slot, names)
        self.assertNoLeases()

    def test_the_project_is_leased_before_the_slot_and_released_in_reverse(self):
        events = []
        real_acquire, real_slot, real_release = (
            lease.LeaseManager.acquire, lease.LeaseManager.acquire_slot, lease.LeaseManager.release)

        def acquire(manager, resource, *a, **k):
            events.append(("acquire", resource))
            return real_acquire(manager, resource, *a, **k)

        def acquire_slot(manager, name, *a, **k):
            events.append(("acquire", name))
            return real_slot(manager, name, *a, **k)

        def release(manager, resource, *a, **k):
            events.append(("release", resource))
            return real_release(manager, resource, *a, **k)

        with mock.patch.object(lease.LeaseManager, "acquire", acquire), mock.patch.object(lease.LeaseManager, "acquire_slot", acquire_slot), \
                mock.patch.object(lease.LeaseManager, "release", release):
            self.unity()
        project = lease.unity_project_resource(self.project)
        slot = lease.slot_resource(lease.UNITY_EDITOR_RESOURCE, 1)
        self.assertEqual([("acquire", project), ("acquire", lease.UNITY_EDITOR_RESOURCE), ("release", slot), ("release", project)], events)

    def test_a_busy_project_is_blocked_naming_the_holder_not_a_timeout(self):
        self.occupy(lease.unity_project_resource(self.project))
        dump = self.base / "dump"
        started = time.monotonic()
        code, lines, _ = self.unity("--dump", str(dump), lease_wait_sec=0.6)
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
        self.assertIn(OTHER, "\n".join(lines))
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertFalse(dump.exists(), "Unity must not start without the lease")

    def test_a_busy_slot_is_blocked_and_the_project_lease_is_given_back(self):
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 1}), encoding="utf-8")
        self.occupy(None, slot=True)
        code, lines, _ = self.unity(lease_wait_sec=0.5)
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertIn(OTHER, "\n".join(lines))
        self.assertEqual([lease.lease_file_name(lease.slot_resource(lease.UNITY_EDITOR_RESOURCE, 1))],
                         [p.name for p in self.manager.directory.glob("*.lock")])

    def test_a_run_waits_for_a_busy_project_and_goes_on_when_it_is_freed(self):
        resource = self.occupy(lease.unity_project_resource(self.project))
        threading.Timer(0.6, lambda: self.manager.release(resource, "other", os.getpid())).start()
        code, lines, err = self.unity(lease_wait_sec=10)
        self.assertEqual("STATUS PASS", lines[-1])
        self.assertIn("lease waiting", err)
        self.assertNoLeases()

    def test_the_leases_are_released_when_supervision_raises(self):
        with mock.patch.object(supervise, "supervise", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.unity()
        self.assertNoLeases()

    def test_the_leases_are_released_and_the_run_is_blocked_on_ctrl_c_and_on_a_termination_signal(self):
        for error in (KeyboardInterrupt(), unityrun.Interrupted("signal 15")):
            with mock.patch.object(supervise, "supervise", side_effect=error):
                code, lines, _ = self.unity()
            self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
            self.assertNoLeases()

    def test_an_interrupt_while_waiting_for_the_slot_gives_the_project_back(self):
        with mock.patch.object(lease.LeaseManager, "acquire_slot", side_effect=KeyboardInterrupt()):
            code, lines, _ = self.unity(lease_wait_sec=5)
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertNoLeases()

    def test_a_lost_lease_kills_unity_and_is_blocked(self):
        dump = self.base / "dump"
        project = self.manager.path_for(lease.unity_project_resource(self.project))

        def steal():
            for _ in range(100):
                if (dump / "pid").exists():
                    break
                time.sleep(0.1)
            project.unlink()

        thread = threading.Thread(target=steal)
        thread.start()
        started = time.monotonic()
        code, lines, _ = self.unity("--sleep", "60", "--dump", str(dump), "--grow", ttl_sec=3)
        thread.join()
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
        self.assertIn("were lost", "\n".join(lines))
        self.assertLess(time.monotonic() - started, 30)
        pid = int((dump / "pid").read_text())
        for _ in range(50):
            if not core.process_alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(core.process_alive(pid))


class AcceleratorTests(UnityRunTestCase):
    def argv(self, *extra):
        dump = self.base / "dump"
        code, lines, _ = self.unity("--dump", str(dump), *extra)
        self.assertEqual("STATUS PASS", lines[-1])
        return json.loads((dump / "argv.json").read_text())

    def configure(self):
        (self.home / "host.json").write_text(json.dumps({"unityAccelerator": {"endpoint": "127.0.0.1:10080"}}), encoding="utf-8")

    def test_the_accelerator_of_host_json_is_added(self):
        self.configure()
        argv = self.argv()
        self.assertIn("-EnableCacheServer", argv)
        self.assertEqual("127.0.0.1:10080", argv[argv.index("-cacheServerEndpoint") + 1])

    def test_without_a_configured_accelerator_nothing_is_added(self):
        self.assertFalse([a for a in self.argv() if a.lower().startswith(("-enablecacheserver", "-cacheserver"))])

    def test_the_users_own_cache_server_arguments_win(self):
        self.configure()
        argv = self.argv("-cacheServerEndpoint", "10.0.0.1:1")
        self.assertEqual(["10.0.0.1:1"], [argv[i + 1] for i, a in enumerate(argv) if a == "-cacheServerEndpoint"])
        self.assertNotIn("-EnableCacheServer", argv)
        self.assertNotIn("127.0.0.1:10080", argv)

    def test_a_broken_host_json_is_blocked_before_any_lease(self):
        (self.home / "host.json").write_text("{not json", encoding="utf-8")
        code, lines, _ = self.unity()
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertNoLeases()


class EditorLookupTests(UnityRunTestCase):
    def test_the_editor_of_the_project_version_is_found_in_the_hub(self):
        hub = make_hub(self.base / "hub")
        with mock.patch.dict(os.environ, hub):
            code, lines, _ = self.unity(unity=None)
        # The hub holds an empty file as the Editor, so Unity cannot start; finding it is what is tested.
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertIn("cannot start", "\n".join(lines))

    def test_no_editor_for_the_version_is_blocked_with_the_reason_and_takes_no_lease(self):
        with mock.patch.dict(os.environ, {"GDV_UNITY_HUB_PATHS": str(self.base / "empty-hub")}):
            code, lines, _ = self.unity(unity=None)
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
        self.assertIn("6000.6.7f1 is not installed", lines[0])
        self.assertNoLeases()

    def test_an_unreadable_project_version_is_blocked(self):
        (self.project / "ProjectSettings" / "ProjectVersion.txt").unlink()
        code, lines, _ = self.unity(unity=None)
        self.assertIn("ProjectVersion.txt", lines[0])
        self.assertEqual("STATUS BLOCKED", lines[-1])


class CommandLineTests(HostTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.base / "Proj"
        (self.project / "ProjectSettings").mkdir(parents=True)

    def test_the_command_runs_the_arguments_after_double_dash(self):
        dump = self.base / "dump"
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--",
                                 FAKE, "-batchmode", "-projectPath", str(self.project), "--dump", str(dump))
        self.assertEqual((0, "STATUS PASS"), (code, out.strip().splitlines()[-1]), err)
        self.assertIn("-batchmode", json.loads((dump / "argv.json").read_text()))

    def test_project_dir_becomes_the_project_path(self):
        dump = self.base / "dump"
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--project-dir", str(self.project), "--",
                                 FAKE, "--dump", str(dump))
        self.assertEqual(0, code, err)
        argv = json.loads((dump / "argv.json").read_text())
        self.assertEqual(str(self.project.resolve()), argv[argv.index("-projectPath") + 1])

    def test_a_relative_project_path_is_taken_from_the_working_directory(self):
        dump = self.base / "dump"
        cwd = os.getcwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, cwd)
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--", FAKE, "-projectPath", "Proj", "--dump", str(dump))
        self.assertEqual(0, code, err)

    def test_options_are_validated(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["unity", "run", "--timeout-sec", "-1", "--", "-batchmode"])


class RemoteTests(HostTestCase):
    class FakeHost(Transport):
        kind = "ssh"

        def __init__(self, result):
            self.calls = []
            self.result = result

        def run(self, command, timeout_sec, cwd=None):
            self.calls.append((list(command), timeout_sec, cwd))
            return self.result

        def put(self, *_):
            return CommandResult(0)

        def get(self, *_):
            return CommandResult(0)

    def run_remote(self, result, *extra):
        config.add_ssh_host("win", "me@win", "windows", "C:/art")
        host = self.FakeHost(result)
        retries = []

        def remote_to_host(entry, arguments, retry=True):
            retries.append(retry)
            return remote.Remote(host, ("valtrion",), getattr(arguments, "remote_project", None))

        with mock.patch.object(commands, "_remote", side_effect=remote_to_host):
            code, out, err = run_cli("unity", "run", "--host", "win", "--remote-project", "C:/Proj", "--timeout-sec", "100",
                                     "--lease-wait", "50", *extra, "--", "-batchmode", "-projectPath", ".")
        return code, out, err, host, retries

    def test_the_command_runs_on_the_host_without_retries_and_its_output_and_code_come_back(self):
        code, out, err, host, retries = self.run_remote(CommandResult(1, "tests failed\nSTATUS FAIL\n", ""))
        self.assertEqual((1, "tests failed\nSTATUS FAIL\n"), (code, out))
        command, timeout, cwd = host.calls[0]
        self.assertEqual(["valtrion", "unity", "run", "--timeout-sec", "100", "--lease-wait", "50", "--", "-batchmode", "-projectPath", "."],
                         command)
        self.assertEqual("C:/Proj", cwd)
        self.assertGreater(timeout, 150)
        self.assertEqual([False], retries)

    def test_an_unreachable_host_is_blocked_not_silently_ignored(self):
        code, out, err, host, _ = self.run_remote(CommandResult(remote.SSH_FAILURE, "", "ssh: connect failed"))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("unreachable", err)


if __name__ == "__main__":
    unittest.main()
