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

from valtrion import cli, commands, config, constants, core, lease, remote, supervise, unityguard, unityrun  # noqa: F401  (cli first: it registers the commands)
from valtrion.transport import CommandResult, Transport

from .test_host import HostTestCase, run as run_cli
from .test_platform_editor import make_hub

FAKE = str(Path(__file__).with_name("fake_unity.py"))
OTHER = "other@host"


class FakeProbe(unityguard.Probe):
    """A machine without Editors and with plenty of memory, unless a test says otherwise."""

    def __init__(self, processes=(), memory=64000.0, os_name="macos"):
        self.os_name = os_name
        self.process_list = None if processes is None else list(processes)
        self.memory = memory
        self.reads = 0

    def processes(self):
        self.reads += 1
        return self.process_list

    def free_memory_mib(self):
        return self.memory


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
        options.setdefault("probe", FakeProbe())
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


class GuardTests(UnityRunTestCase):
    EDITOR = unityguard.Proc(4242, 1, "/Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity",
                             "/Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity -batchmode")

    def test_an_editor_outside_any_lease_blocks_naming_it_and_unity_does_not_start(self):
        dump = self.base / "dump"
        probe = FakeProbe([self.EDITOR])
        code, lines, _ = self.unity("--dump", str(dump), probe=probe)
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
        self.assertIn("pid 4242", lines[0])
        self.assertIn("outside any lease", lines[0])
        self.assertFalse(dump.exists())
        self.assertNoLeases()

    def test_an_editor_below_the_holder_of_a_live_lease_is_not_an_orphan(self):
        other = self.occupy("other-resource")  # a live lease held by this process
        child = unityguard.Proc(4243, os.getpid(), self.EDITOR.path, self.EDITOR.command)
        code, lines, _ = self.unity(probe=FakeProbe([unityguard.Proc(os.getpid(), 1, "valtrion", "valtrion"), child]))
        self.assertEqual("STATUS PASS", lines[-1])

    def test_the_run_waits_for_the_editor_to_go_away_within_the_lease_wait(self):
        probe = FakeProbe([self.EDITOR])
        threading.Timer(0.6, lambda: setattr(probe, "process_list", [])).start()
        code, lines, err = self.unity(probe=probe, lease_wait_sec=10)
        self.assertEqual("STATUS PASS", lines[-1])
        self.assertIn("waiting:", err)
        self.assertNoLeases()

    def test_too_little_free_memory_blocks_with_the_numbers(self):
        code, lines, _ = self.unity(probe=FakeProbe(memory=512.0))
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertIn("only 512 MiB of memory are free, 2048 MiB are needed", lines[0])
        self.assertNoLeases()

    def test_host_json_sets_the_threshold_and_can_turn_both_checks_off(self):
        (self.home / "host.json").write_text(json.dumps({"unityGuard": {"minFreeMemoryMiB": 100}}), encoding="utf-8")
        self.assertEqual("STATUS PASS", self.unity(probe=FakeProbe(memory=512.0))[1][-1])
        (self.home / "host.json").write_text(json.dumps({"unityGuard": {"orphanEditors": False, "minFreeMemoryMiB": 0}}), encoding="utf-8")
        probe = FakeProbe([self.EDITOR], memory=1.0)
        self.assertEqual("STATUS PASS", self.unity(probe=probe)[1][-1])
        self.assertEqual(0, probe.reads)

    def test_a_bad_unity_guard_setting_is_blocked_before_any_lease(self):
        for bad in ({"orphanEditors": "yes"}, {"minFreeMemoryMiB": -1}, {"minFreeMemoryMiB": True}, {"other": 1}, []):
            (self.home / "host.json").write_text(json.dumps({"unityGuard": bad}), encoding="utf-8")
            code, lines, _ = self.unity()
            self.assertEqual("STATUS BLOCKED", lines[-1], bad)
            self.assertNoLeases()

    def test_what_cannot_be_read_is_a_note_not_a_block(self):
        probe = FakeProbe(None, memory=None)
        code, lines, err = self.unity(probe=probe)
        self.assertEqual("STATUS PASS", lines[-1])
        self.assertIn("process list could not be read", err)
        self.assertIn("free memory could not be read", err)

    def test_the_wait_budget_is_shared_with_the_leases(self):
        resource = self.occupy(lease.unity_project_resource(self.project))
        threading.Timer(0.5, lambda: self.manager.release(resource, "other", os.getpid())).start()
        started = time.monotonic()
        code, lines, _ = self.unity(probe=FakeProbe([self.EDITOR]), lease_wait_sec=1.5)
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertLess(time.monotonic() - started, 5)


class AssemblyNamesTests(UnityRunTestCase):
    def test_a_united_comma_list_reaches_unity_as_one_semicolon_list(self):
        # Regression: `valtrion select` substitutes {assemblies} as `A,B`; Unity reads `-assemblyNames` as `A;B` and took `A,B` as one name.
        dump = self.base / "dump"
        code, lines, _ = self.unity("-assemblyNames", "Game.Runtime.Tests,Game.Playback.Tests", "--dump", str(dump))
        self.assertEqual("STATUS PASS", lines[-1])
        argv = json.loads((dump / "argv.json").read_text())
        self.assertEqual("Game.Runtime.Tests;Game.Playback.Tests", argv[argv.index("-assemblyNames") + 1])

    def test_separators_spaces_case_and_empty_names_are_handled(self):
        for given, expected in (("A;B", "A;B"), ("A, B", "A;B"), ("A,,B;", "A;B"), ("A", "A")):
            self.assertEqual(["-AssemblyNames", expected], unityrun.normalize_assembly_names(["-AssemblyNames", given]), given)

    def test_other_options_are_untouched(self):
        args = ["-testFilter", "Ns.Test(1,2);Ns.Other", "-runTests", "-assemblyNames"]  # a trailing option without a value
        self.assertEqual(args, unityrun.normalize_assembly_names(args))

    def test_the_runner_command_of_select_carries_the_comma_list_that_unity_run_accepts(self):
        from valtrion import select

        runner = "valtrion unity run -- -assemblyNames {assemblies}"
        selection = select.Selection.__new__(select.Selection)
        selection.checks = [select.Check("editmode:B", select.EDITMODE, "B", "L2", runner), select.Check("editmode:A", select.EDITMODE, "A", "L2", runner)]
        command = selection.editor_commands()[0]["command"]
        self.assertTrue(command.endswith("-assemblyNames A,B"), command)


class SignalTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(__import__("signal"), "SIGHUP") and os.name != "nt", "POSIX signals")
    def test_a_hangup_and_a_termination_signal_unwind_through_the_cleanup(self):
        import signal

        for number in (signal.SIGHUP, signal.SIGTERM):
            handlers = unityrun._Handlers()
            with self.assertRaises(unityrun.Interrupted):
                with handlers:
                    os.kill(os.getpid(), number)
                    time.sleep(2)

    def test_the_windows_console_events_interrupt_the_main_thread_and_wait_for_the_cleanup(self):
        installed, removed, interrupts = [], [], []
        handlers = unityrun._Handlers(windows=True, install=lambda handler: (installed.append(handler), lambda: removed.append(True))[1],
                                      interrupt=lambda: interrupts.append(True))
        with handlers:
            (handler,) = installed
            self.assertFalse(handler(0))  # Ctrl+C belongs to Python's KeyboardInterrupt
            self.assertEqual([], interrupts)
            handlers.finished.set()  # the cleanup is done, so the handler does not hold the process
            for event in (1, 2, 5, 6):
                self.assertTrue(handler(event))
            self.assertEqual(4, len(interrupts))
        self.assertEqual([True], removed)

    def test_a_console_handler_holds_the_process_until_the_cleanup_is_done(self):
        finished = threading.Event()
        handle = unityrun.console_stop_handler(lambda: None, finished, wait_sec=0.3)
        started = time.monotonic()
        self.assertTrue(handle(2))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        finished.set()
        started = time.monotonic()
        handle(2)
        self.assertLess(time.monotonic() - started, 0.25)

    def test_a_host_without_a_console_still_runs(self):
        def refuse(handler):
            raise OSError("no console")

        with unityrun._Handlers(windows=True, install=refuse):
            pass

    def test_other_platforms_do_not_install_a_console_handler(self):
        def fail(handler):
            raise AssertionError("not on this platform")

        with unityrun._Handlers(windows=False, install=fail):
            pass


class ConnectionLossTests(HostTestCase):
    """The caller's view of a `--host` run whose connection ends: the result is unknown and the message says what the host does."""

    def run_with(self, result):
        config.add_ssh_host("win", "me@win", "windows", "C:/art")

        class Lost(Transport):
            kind = "ssh"

            def run(self, command, timeout_sec, cwd=None):
                return result

            def put(self, *_):
                return CommandResult(0)

            def get(self, *_):
                return CommandResult(0)

        with mock.patch.object(commands, "_remote", side_effect=lambda entry, arguments, retry=True: remote.Remote(Lost(), ("valtrion",), None)):
            return run_cli("unity", "run", "--host", "win", "--", "-batchmode")

    def test_a_dropped_connection_is_blocked_with_the_hint_not_a_pass_or_a_fail(self):
        code, out, err = self.run_with(CommandResult(remote.SSH_FAILURE, "partial output\n", "Connection reset"))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("partial output", out)
        self.assertNotIn("STATUS", out)
        self.assertIn("result is unknown", err)
        self.assertIn("lease list --host", err)

    def test_an_answer_that_never_came_is_blocked_with_the_same_hint(self):
        code, out, err = self.run_with(CommandResult(None, "", "", timed_out=True))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("did not answer", err)
        self.assertIn("result is unknown", err)


if __name__ == "__main__":
    unittest.main()
