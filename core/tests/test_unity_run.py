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
        options.setdefault("guard_poll_sec", 0.1)
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
        code, lines, _ = self.unity("--sleep", "30", stall_sec=3.0)
        self.assertEqual((constants.EXIT_STALL, "STATUS STALL"), (code, lines[-1]))
        self.assertLess(time.monotonic() - started, 20)
        self.assertNoLeases()

    def test_a_growing_log_is_not_a_stall_but_the_timeout_ends_the_run(self):
        code, lines, _ = self.unity("--sleep", "30", "--grow", stall_sec=4.0, timeout_sec=5.0)
        self.assertEqual((constants.EXIT_STALL, "STATUS TIMEOUT"), (code, lines[-1]))

    def test_without_logfile_the_stall_check_is_off(self):
        code, lines, _ = self.unity("--sleep", "2", log=None, stall_sec=0.5)
        self.assertEqual("STATUS PASS", lines[-1])

    def test_a_zero_stall_window_turns_it_off(self):
        code, lines, _ = self.unity("--sleep", "2", stall_sec=0)
        self.assertEqual("STATUS PASS", lines[-1])

    def test_the_whole_process_is_killed_on_timeout(self):
        dump = self.base / "dump"
        code, lines, _ = self.unity("--sleep", "30", "--dump", str(dump), "--grow", timeout_sec=2.0, stall_sec=0)
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
        self.assertEqual([("acquire", project), ("acquire", lease.UNITY_EDITOR_RESOURCE)], events[:2])
        released = [resource for kind, resource in events if kind == "release"]  # also names the slots it did not get: harmless
        self.assertEqual(project, released[-1])
        self.assertLess(released.index(slot), released.index(project))

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
        code, lines, _ = self.unity("--sleep", "60", "--dump", str(dump), "--grow", ttl_sec=9)
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


class CleanupTests(UnityRunTestCase):
    def test_an_interrupt_right_after_a_lease_was_created_does_not_leak_it(self):
        real = lease.LeaseManager.acquire

        def acquire_then_interrupt(manager, *a, **k):
            real(manager, *a, **k)
            raise unityrun.Interrupted("signal 15")  # between the lease file and the run noting it

        with mock.patch.object(lease.LeaseManager, "acquire", acquire_then_interrupt):
            code, lines, _ = self.unity()
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertNoLeases()

    def test_the_leases_are_given_back_through_the_keeper_under_its_lock(self):
        released = []
        real = lease.LeaseKeeper.release_one

        def spy(keeper, resource):
            released.append(resource)
            return real(keeper, resource)

        with mock.patch.object(lease.LeaseKeeper, "release_one", spy):
            self.unity()
        self.assertIn(lease.unity_project_resource(self.project), released)
        self.assertIn(lease.slot_resource(lease.UNITY_EDITOR_RESOURCE, 1), released)
        self.assertNoLeases()

    def test_a_signal_during_the_release_changes_nothing(self):
        handlers = unityrun._Handlers()
        with handlers:
            handlers.cleaning = True
            handlers._raise(15, None)  # would raise Interrupted while the leases are being given back
        handlers.cleaning = False
        with handlers:
            with self.assertRaises(unityrun.Interrupted):
                handlers._raise(15, None)

    def test_the_signal_handlers_stay_installed_until_the_cleanup_is_done(self):
        seen = []
        real = lease.LeaseKeeper.release_one

        def spy(keeper, resource):
            import signal

            seen.append(signal.getsignal(signal.SIGINT) is not signal.default_int_handler)
            return real(keeper, resource)

        with mock.patch.object(lease.LeaseKeeper, "release_one", spy):
            self.unity()
        self.assertTrue(seen and all(seen))

    def test_a_lost_lease_after_unity_ended_does_not_hide_its_result(self):
        keepers = []
        real_init = lease.LeaseKeeper.__init__

        def init(keeper, *a, **k):
            real_init(keeper, *a, **k)
            keepers.append(keeper)

        def fake_supervise(argv, watchdog, log, *a, **k):
            keepers[0].lost.append("unity-editor:slot1")  # lost while Unity ran, but Unity finished by itself
            return supervise.SuperviseResult(constants.FAIL, "exit code 2", 2, 1.0, 0.0)

        with mock.patch.object(lease.LeaseKeeper, "__init__", init), mock.patch.object(supervise, "supervise", fake_supervise):
            code, lines, _ = self.unity()
        self.assertEqual((constants.EXIT_FAIL, "STATUS FAIL"), (code, lines[-1]))
        self.assertIn("exit code 2", lines[0])
        self.assertIn("note: the lease(s) unity-editor:slot1 were lost while Unity ran", lines[0])


class LogFreshnessTests(UnityRunTestCase):
    def test_a_license_text_in_the_log_of_an_earlier_run_does_not_block(self):
        (self.logs / "Editor.log").write_text("Unable to activate\n", encoding="utf-8")
        code, lines, _ = self.unity("--exit", "1", "--no-log")  # Unity failed before it opened the log
        self.assertEqual((constants.EXIT_FAIL, "STATUS FAIL"), (code, lines[-1]))

    def test_a_license_text_written_by_this_run_into_an_existing_log_blocks(self):
        (self.logs / "Editor.log").write_text("earlier run\n", encoding="utf-8")
        code, lines, _ = self.unity("--exit", "1", "--text", "Unable to activate")
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))


class SubprocessTests(unittest.TestCase):
    """The real command in a child process, with real signals and a real closed input (POSIX only)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.home = self.base / "home"
        self.home.mkdir()
        (self.home / "host.json").write_text(json.dumps({"unityGuard": {"orphanEditors": False, "minFreeMemoryMiB": 0}}), encoding="utf-8")
        self.project = self.base / "Proj"
        (self.project / "ProjectSettings").mkdir(parents=True)
        self.dump = self.base / "dump"
        self.core = str(Path(__file__).resolve().parent.parent)

    def start(self, *options, sleep="60", stdin=None):
        import subprocess

        env = dict(os.environ, VALTRION_HOME=str(self.home), PYTHONPATH=self.core, GDV_OWNER="test@host")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "valtrion", "unity", "run", "--unity", sys.executable, "--lease-wait", "0", *options, "--",
             FAKE, "-batchmode", "-projectPath", str(self.project), "-logFile", str(self.base / "e.log"), "--grow", "--sleep", sleep,
             "--dump", str(self.dump)],
            stdin=subprocess.PIPE if stdin is None else stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.core)
        self.addCleanup(lambda: self.process.poll() is None and self.process.kill())
        self.addCleanup(lambda: [pipe.close() for pipe in (self.process.stdin, self.process.stdout, self.process.stderr) if pipe and not pipe.closed])
        for _ in range(150):
            if (self.dump / "pid").exists() and (self.dump / "leases.json").exists():
                return int((self.dump / "pid").read_text())
            time.sleep(0.1)
        self.fail("the fake Unity did not start")

    def leases(self):
        folder = self.home / "leases"
        return sorted(p.name for p in folder.glob("*.lock")) if folder.is_dir() else []

    def assertGone(self, pid):
        for _ in range(100):
            if not core.process_alive(pid):
                return
            time.sleep(0.1)
        self.fail(f"process {pid} is still alive")

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_stops_the_whole_tree_releases_the_leases_and_ends_blocked(self):
        import signal

        pid = self.start()
        self.assertEqual(2, len(self.leases()))
        self.process.send_signal(signal.SIGTERM)
        out, _ = self.process.communicate(timeout=60)
        self.assertEqual(constants.EXIT_BLOCKED, self.process.returncode)
        self.assertEqual("STATUS BLOCKED", out.decode().splitlines()[-1])
        self.assertGone(pid)
        self.assertEqual([], self.leases())

    @unittest.skipIf(os.name == "nt", "POSIX pipes")
    def test_the_end_of_the_input_stops_a_run_that_asked_for_it(self):
        pid = self.start("--stop-on-stdin-eof")
        self.process.stdin.write(b".")  # the caller's heartbeat: the input is a live pipe
        self.process.stdin.flush()
        time.sleep(0.6)
        self.process.stdin.close()  # what the host sees when the SSH connection ends
        self.process.wait(timeout=60)  # not communicate(): after the manual close it would flush the closed stdin (ValueError on 3.12)
        out = self.process.stdout.read()
        self.assertEqual(constants.EXIT_BLOCKED, self.process.returncode)
        self.assertEqual("STATUS BLOCKED", out.decode().splitlines()[-1])
        self.assertGone(pid)
        self.assertEqual([], self.leases())

    @unittest.skipIf(os.name == "nt", "POSIX pipes")
    def test_an_input_that_was_never_a_live_pipe_does_not_stop_the_run(self):
        import subprocess

        # /dev/null: the end of the input at once, with no byte before it (a wrapper without a live pipe)
        self.start("--stop-on-stdin-eof", sleep="2", stdin=subprocess.DEVNULL)
        self.process.wait(timeout=60)
        out = self.process.stdout.read().decode()
        self.assertEqual(constants.EXIT_PASS, self.process.returncode, self.process.stderr.read().decode())
        self.assertEqual("STATUS PASS", out.splitlines()[-1])
        self.assertEqual([], self.leases())

    @unittest.skipIf(os.name == "nt", "POSIX pipes")
    def test_a_run_that_ends_with_its_input_still_open_exits_cleanly(self):
        # Regression: a watcher blocked in the buffered sys.stdin made the interpreter abort at exit (SIGABRT, "Fatal Python error").
        self.start("--stop-on-stdin-eof", sleep="2")
        self.process.wait(timeout=60)  # the input stays open: the caller is still there
        out = self.process.stdout.read().decode()
        err = self.process.stderr.read().decode()
        self.assertEqual(constants.EXIT_PASS, self.process.returncode, err)  # not a negative signal number, not 134
        self.assertEqual("STATUS PASS", out.splitlines()[-1])
        self.assertNotIn("Fatal Python error", err)
        self.assertEqual([], self.leases())

    @unittest.skipIf(os.name == "nt", "POSIX pipes")
    def test_without_the_option_a_closed_input_changes_nothing(self):
        self.start(sleep="3")
        self.process.stdin.close()
        self.process.wait(timeout=60)
        out = self.process.stdout.read()
        self.assertEqual(constants.EXIT_PASS, self.process.returncode)
        self.assertEqual("STATUS PASS", out.decode().splitlines()[-1])

    @unittest.skipIf(os.name == "nt", "POSIX pipes")
    def test_a_closed_output_does_not_turn_the_end_of_the_run_into_a_traceback(self):
        self.start("--stop-on-stdin-eof", sleep="3")
        self.process.stdout.close()  # the reader is gone before the result is written
        self.process.wait(timeout=60)
        err = self.process.stderr.read()
        self.assertNotIn(b"Traceback", err)
        self.assertEqual([], self.leases())


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


class ProgressFileTests(UnityRunTestCase):
    def test_unity_is_told_where_to_report_its_progress(self):
        dump = self.base / "dump"
        code, out, err = self.unity("--dump", str(dump))
        self.assertEqual(0, code, err)
        given = Path((dump / "progress-env").read_text(encoding="utf-8"))
        self.assertEqual(self.home / "progress", given.parent)
        self.assertRegex(given.name, r"^unity-run-\d+\.json$")

    def test_the_report_is_gone_when_the_run_has_ended(self):
        dump = self.base / "dump"
        code, out, err = self.unity("--dump", str(dump), "--progress-report")
        self.assertEqual(0, code, err)
        given = Path((dump / "progress-env").read_text(encoding="utf-8"))
        self.assertEqual(4, json.loads((dump / "progress-seen").read_text(encoding="utf-8"))["done"])
        self.assertFalse(given.exists(), "a finished run leaves no report behind")

    def test_a_report_left_by_an_earlier_run_with_the_same_process_id_is_not_shown(self):
        stale = self.home / "progress" / f"unity-run-{os.getpid()}.json"
        stale.parent.mkdir()
        stale.write_text("old", encoding="utf-8")
        dump = self.base / "dump"
        self.unity("--dump", str(dump))
        self.assertFalse(stale.exists())


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

    def test_the_progress_window_is_off_unless_asked_for(self):
        with mock.patch.object(unityrun.progresswindow, "start", return_value=None) as start:
            run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--", FAKE, "-projectPath", str(self.project))
            run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--progress-window", "--", FAKE, "-projectPath", str(self.project))
        self.assertEqual([False, True], [call.args[3] for call in start.call_args_list])

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

    def test_project_path_wins_over_a_broken_project_file_and_without_it_the_file_is_needed(self):
        (self.base / "valtrion.json").write_text("{broken", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, cwd)
        args = ("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--", FAKE)
        code, out, err = run_cli(*args, "-projectPath", str(self.project))
        self.assertEqual((0, "STATUS PASS"), (code, out.strip().splitlines()[-1]), err)
        code, out, err = run_cli(*args)
        self.assertEqual(constants.EXIT_BLOCKED, code)

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

        def remote_to_host(entry, arguments, retry=True, lifeline=False):
            retries.append((retry, lifeline))
            return remote.Remote(host, ("valtrion",), getattr(arguments, "remote_project", None))

        with mock.patch.object(commands, "_remote", side_effect=remote_to_host):
            code, out, err = run_cli("unity", "run", "--host", "win", "--remote-project", "C:/Proj", "--timeout-sec", "100",
                                     "--lease-wait", "50", *extra, "--", "-batchmode", "-projectPath", ".")
        return code, out, err, host, retries

    def test_the_command_runs_on_the_host_without_retries_and_its_output_and_code_come_back(self):
        code, out, err, host, retries = self.run_remote(CommandResult(1, "tests failed\nSTATUS FAIL\n", ""))
        self.assertEqual((1, "tests failed\nSTATUS FAIL\n"), (code, out))
        command, timeout, cwd = host.calls[0]
        self.assertEqual(["valtrion", "unity", "run", "--timeout-sec", "100", "--lease-wait", "50", "--stop-on-stdin-eof", "--", "-batchmode", "-projectPath", "."],
                         command)
        self.assertEqual("C:/Proj", cwd)
        self.assertGreater(timeout, 150)
        self.assertEqual([(False, True)], retries)  # no retries, and an open input for the host to watch

    def test_the_real_transport_is_made_with_the_option_for_this_command_only(self):
        config.add_ssh_host("win", "me@win", "windows", "C:/art")
        entry = config.find_host("win")
        self.assertTrue(remote.make_transport(entry, retry=False, lifeline=True).lifeline)
        self.assertFalse(remote.make_transport(entry, retry=False).lifeline)

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
        self.assertIn("lease waiting", err)
        self.assertIn("outside any lease", err)
        self.assertNoLeases()

    def test_the_slot_is_free_while_the_run_waits_for_an_editor_outside_the_leases(self):
        slots_held_while_waiting = []

        class WatchingProbe(FakeProbe):
            def processes(inner):
                slots_held_while_waiting.append(sorted(p.name for p in self.manager.directory.glob("unity-editor*.lock")))
                return super().processes()

        probe = WatchingProbe([self.EDITOR])
        threading.Timer(0.8, lambda: setattr(probe, "process_list", [])).start()
        code, lines, _ = self.unity(probe=probe, lease_wait_sec=10)
        self.assertEqual("STATUS PASS", lines[-1])
        self.assertGreaterEqual(len(slots_held_while_waiting), 2)
        self.assertEqual([[]], [held for held in slots_held_while_waiting[:1]])
        self.assertTrue(all(held == [] for held in slots_held_while_waiting[:-1]), slots_held_while_waiting)
        self.assertNoLeases()

    def test_another_run_can_take_the_free_slot_while_this_one_waits_for_the_editor(self):
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 1}), encoding="utf-8")
        probe = FakeProbe([self.EDITOR])
        results = []
        waiter = threading.Thread(target=lambda: results.append(self.unity(probe=probe, lease_wait_sec=2.5)[1][-1]))
        waiter.start()
        time.sleep(0.6)  # the first run is waiting for the Editor and holds no slot
        taken = self.manager.acquire_slot(lease.UNITY_EDITOR_RESOURCE, 1, OTHER, "other", 300, "a run that is not guarded", os.getpid())
        self.assertTrue(taken.acquired, taken.reason)
        self.manager.release(taken.lease.resource, "other", os.getpid())
        waiter.join(10)
        self.assertEqual(["STATUS BLOCKED"], results)

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


@unittest.skipIf(os.name == "nt", "POSIX pipes")
class InputWatcherTests(unittest.TestCase):
    """`_watch_input` on real descriptors: only the end of a live pipe (one that delivered a byte) means the caller is gone."""

    def watch(self, prepare):
        reader, writer = os.pipe()
        notes = []
        prepare(reader, writer)
        stop = threading.Event()
        with mock.patch.object(unityrun._thread, "interrupt_main") as interrupt:
            thread = threading.Thread(target=unityrun._watch_input, args=(reader, stop, notes.append))
            thread.start()
            thread.join(10)
            stop.set()
            self.assertFalse(thread.is_alive())
        for fd in (reader, writer):
            try:
                os.close(fd)
            except OSError:
                pass
        return interrupt.called, notes

    def test_a_byte_and_then_the_end_is_a_lost_caller(self):
        def prepare(reader, writer):
            os.write(writer, b".")
            os.close(writer)

        interrupted, notes = self.watch(prepare)
        self.assertTrue(interrupted)
        self.assertEqual([], notes)

    def test_the_end_without_any_byte_switches_the_lifeline_off_with_a_note(self):
        interrupted, notes = self.watch(lambda reader, writer: os.close(writer))
        self.assertFalse(interrupted)
        self.assertEqual(1, len(notes))
        self.assertIn("not a live pipe", notes[0])

    def test_a_descriptor_that_is_closed_switches_the_lifeline_off_with_a_note(self):
        def prepare(reader, writer):
            os.close(writer)
            os.close(reader)

        notes = []
        stop = threading.Event()
        with mock.patch.object(unityrun._thread, "interrupt_main") as interrupt:
            unityrun._watch_input(987654, stop, notes.append)  # not an open descriptor
        self.assertFalse(interrupt.called)
        self.assertEqual(1, len(notes))

    def test_the_watcher_ends_when_asked_even_if_nothing_arrives(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        stop = threading.Event()
        thread = threading.Thread(target=unityrun._watch_input, args=(reader, stop))
        thread.start()
        stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())


class SignalTests(unittest.TestCase):
    def test_only_the_first_signal_unwinds_the_run_later_ones_change_nothing(self):
        handlers = unityrun._Handlers()
        with self.assertRaises(unityrun.Interrupted):
            handlers._raise(15, None)
        handlers._raise(15, None)  # inside the except body or the release: no second Interrupted
        handlers._raise(2, None)

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


class LifelineTransportTests(unittest.TestCase):
    """The pipe that lets the host see the end of the connection: open for the whole call, closed after it, absent unless asked for."""

    def test_the_remote_command_gets_an_open_input_only_when_asked_for(self):
        from valtrion.transport import SshTransport

        seen = []

        def runner(argv, timeout, cwd, env, stdin=None):
            if stdin is not None:
                os.fstat(stdin)  # still open during the call
                seen.append(stdin)
            else:
                seen.append(None)
            return CommandResult(0)

        plain = SshTransport("me@host", runner=runner)
        plain.run(["valtrion", "version"], 5)
        self.assertEqual([None], seen)
        attached = SshTransport("me@host", runner=runner)
        attached.lifeline = True
        attached.LIFELINE_BEAT_SEC = 0.05
        beats = []

        def reading_runner(argv, timeout, cwd, env, stdin=None):
            time.sleep(0.4)  # the heartbeat writes a byte at once and then again and again
            beats.append(os.read(stdin, 100))
            return runner(argv, timeout, cwd, env, stdin)

        attached._runner = reading_runner
        attached.run(["valtrion", "version"], 5)
        self.assertGreaterEqual(len(beats[0]), 3)
        self.assertEqual({46}, set(beats[0]))  # only "." bytes
        self.assertIsInstance(seen[1], int)
        with self.assertRaises(OSError):
            os.fstat(seen[1])  # closed once the call is over, which is what the host sees as the end of input

    def test_a_runner_with_four_parameters_still_works_without_a_lifeline(self):
        from valtrion.transport import SshTransport

        calls = []
        transport = SshTransport("me@host", runner=lambda argv, timeout, cwd, env: calls.append(argv) or CommandResult(0))
        transport.run(["valtrion", "version"], 5)
        self.assertEqual(1, len(calls))


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

        with mock.patch.object(commands, "_remote", side_effect=lambda entry, arguments, retry=True, lifeline=False: remote.Remote(Lost(), ("valtrion",), None)):
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
