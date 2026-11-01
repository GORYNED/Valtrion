import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from valtrion import cli, commands, config, constants, core, jobs, lease, remote
from valtrion.schema import Validator
from valtrion.transport import CommandResult, LocalTransport, Transport

import warnings

# Detached runners are started and deliberately never waited for.
warnings.simplefilter("ignore", ResourceWarning)

PYTHON = sys.executable
HANG = [PYTHON, "-c", "import time; time.sleep(120)"]


def counting(seconds: float, interval: float = 0.3) -> list[str]:
    code = f"import time\nfor i in range({int(seconds / interval)}):\n    print('step', i, flush=True); time.sleep({interval})"
    return [PYTHON, "-u", "-c", code]


class JobTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.home = Path(self._folder.name) / "home"
        self.root = Path(self._folder.name) / "artifacts"
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The tests name remote hosts such as "mac"; the machine's own hostname must never collide with them.
        hostname = mock.patch.object(core, "local_host_id", return_value="this-machine")
        hostname.start()
        self.addCleanup(hostname.stop)
        self.validator = Validator()

    def request(self, argv, **options):
        defaults = dict(
            kind="run", project="proj", sha="a" * 40, label="t", payload={"type": "command", "argv": argv, "cwd": None},
            host={"id": "test-host", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=60, poll_sec=0.05,
            tick_sec=0.5, owner="tester")
        defaults.update(options)
        return jobs.JobRequest(**defaults)

    def run_foreground(self, argv, **options):
        info = jobs.start_job(self.request(argv, **options), self.root, self.home, foreground=True)
        return info, jobs.JobStore(Path(info["dir"]))

    def assert_contract(self, store: jobs.JobStore):
        self.assertEqual([], self.validator.validate(store.read_job(), "gdv.job/1"))
        for line in store.events.read_text(encoding="utf-8").splitlines():
            self.assertEqual([], self.validator.validate(json.loads(line), "gdv.event/1"), line)

    def wait_for(self, predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False


class RunnerTests(JobTestCase):
    def test_pass_writes_valid_state_and_events_under_the_artifact_layout(self):
        info, store = self.run_foreground([PYTHON, "-c", "print('hello')"])
        job = store.read_job()
        self.assertEqual(("finished", "PASS", None), (job["state"], job["status"], job["reason"]))
        self.assertEqual(self.root / "proj" / ("a" * 8) / info["jobId"], store.directory)
        self.assert_contract(store)
        types = [json.loads(line)["type"] for line in store.events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("job.started", types[0])
        self.assertEqual("job.finished", types[-1])
        self.assertIn("hello", store.console.read_text(encoding="utf-8"))
        self.assertEqual(store.events.stat().st_size, job["eventsOffset"])

    def test_failure_and_exit_codes(self):
        _, store = self.run_foreground([PYTHON, "-c", "raise SystemExit(5)"])
        self.assertEqual(("FAIL", "exit code 5"), (store.read_job()["status"], store.read_job()["reason"]))
        _, store = self.run_foreground([PYTHON, "-c", "raise SystemExit(2)"], contract_exit_codes=True)
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assert_contract(store)

    def test_a_hang_becomes_a_stall_job_and_leaves_no_lease(self):
        _, store = self.run_foreground(HANG, stall_sec=1.5, leases=({"resource": "device:hang"},))
        job = store.read_job()
        self.assertEqual("STALL", job["status"])
        self.assertIn("stall.detected", store.events.read_text(encoding="utf-8"))
        self.assertEqual("free", lease.LeaseManager(self.home).status("device:hang").state)
        self.assert_contract(store)

    def test_a_busy_device_blocks_the_job_naming_the_holder_and_never_takes_it(self):
        manager = lease.LeaseManager(self.home)
        manager.acquire("device:ipad", "alice", "other-session", 600, "series", os.getpid())
        _, store = self.run_foreground([PYTHON, "-c", "print('never runs')"], leases=({"resource": "device:ipad"},), lease_wait_sec=0.5)
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("alice", job["reason"])
        self.assertIn("other-session", job["reason"])
        self.assertFalse(store.console.exists(), "the step must not start without its lease")
        self.assertEqual("held", manager.status("device:ipad").state)
        self.assertEqual("other-session", manager.status("device:ipad").record.session)

    def test_waiting_for_a_lease_is_a_log_event_naming_the_holder(self):
        lease.LeaseManager(self.home).acquire("device:ipad", "alice", "other-session", 600, "series", os.getpid())
        _, store = self.run_foreground([PYTHON, "-c", "pass"], leases=({"resource": "device:ipad"},), lease_wait_sec=0.5)
        self.assert_contract(store)
        events = [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]
        waiting = [event for event in events if event["type"] == "log" and event["message"].startswith("lease waiting")]
        self.assertEqual(1, len(waiting))
        self.assertEqual("device:ipad", waiting[0]["data"]["resource"])
        self.assertIn("alice", waiting[0]["data"]["holder"])
        self.assertEqual(0, waiting[0]["data"]["waitedSec"])
        self.assertIn("alice", waiting[0]["message"])

    def test_unity_editor_without_slots_counts_the_host_total(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 1}), encoding="utf-8")
        manager = lease.LeaseManager(self.home)
        manager.acquire_slot("unity-editor", 1, "alice", "other-session", 600, "build", os.getpid())
        _, store = self.run_foreground([PYTHON, "-c", "pass"], leases=({"resource": "unity-editor"},))
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("all 1 slots of unity-editor", job["reason"])
        self.assertEqual("free", manager.status("unity-editor:slot2").state)

    def test_an_invalid_unity_slot_total_blocks_a_unity_editor_job(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 0}), encoding="utf-8")
        _, store = self.run_foreground([PYTHON, "-c", "pass"], leases=({"resource": "unity-editor"},))
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("host.json", job["reason"])
        self.assertFalse(store.console.exists())

    def test_job_holds_its_leases_while_running_and_releases_after(self):
        manager = lease.LeaseManager(self.home)
        seen = []
        code = (
            "import json, os, sys\n"
            f"print(os.path.exists(os.path.join({str(self.home / 'leases')!r}, 'device%3Aipad.lock')))")
        _, store = self.run_foreground([PYTHON, "-c", code], leases=({"resource": "device:ipad"},))
        self.assertIn("True", store.console.read_text(encoding="utf-8"))
        self.assertEqual("free", manager.status("device:ipad").state)
        types = store.events.read_text(encoding="utf-8")
        self.assertIn("lease.acquired", types)
        self.assertIn("lease.released", types)

    def test_cleanup_removes_registered_temp_paths_after_results_are_saved(self):
        scratch = Path(self._folder.name) / "export"
        scratch.mkdir()
        (scratch / "big.bin").write_bytes(b"x")
        request = self.request([PYTHON, "-c", "pass"])
        layout, spec = jobs.create_job(request, self.root, self.home)
        from valtrion import artifacts
        artifacts.register_temp(layout, scratch)
        core.atomic_write_json(layout.result, {"note": "results are saved first"})
        jobs.run_job(layout.directory, self.home)
        self.assertFalse(scratch.exists())
        self.assertTrue(layout.result.exists())


class DetachedTests(JobTestCase):
    def local_fetch(self, directory):
        return lambda offset: jobs.tail_info(directory, offset, self.root)

    def test_start_returns_at_once_and_the_job_outlives_the_caller(self):
        info = jobs.start_job(self.request(counting(2.4)), self.root, self.home)
        directory = Path(info["dir"])
        job = jobs.JobStore(directory).read_job()
        self.assertNotEqual(os.getpid(), job.get("pid"))
        self.assertIn(job["state"], ("running", "queued", "finished"))
        lines: list[str] = []
        outcome = jobs.watch(self.local_fetch(directory), lines.append, poll_sec=0.1)
        self.assertEqual("PASS", outcome.status)
        self.assertEqual("finished", jobs.JobStore(directory).read_job()["state"])
        self.assertEqual("job.started", json.loads(lines[0])["type"])
        self.assertEqual("job.finished", json.loads(lines[-1])["type"])

    def test_cancel_kills_the_step_tree_and_closes_the_job(self):
        pid_file = Path(self._folder.name) / "child.pid"
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(120)\n")
        info = jobs.start_job(self.request([PYTHON, "-c", code], leases=({"resource": "device:cancel"},)), self.root, self.home)
        directory = Path(info["dir"])
        self.assertTrue(self.wait_for(pid_file.exists))
        result = jobs.cancel_job(directory, grace_sec=20)
        job = jobs.JobStore(directory).read_job()
        self.assertEqual("cancelled", job["state"], result)
        self.assertTrue(self.wait_for(lambda: not core.process_alive(int(pid_file.read_text()))), "the step's child survived the cancel")
        self.assertEqual("free", lease.LeaseManager(self.home).status("device:cancel").state)
        self.assertEqual(constants.EXIT_BLOCKED, jobs.WatchOutcome("BLOCKED", None, 0).exit_code)
        self.assertEqual("already ended", jobs.cancel_job(directory)["note"])

    def test_cancel_never_kills_a_process_that_only_reuses_the_runners_pid(self):
        bystander = __import__("subprocess").Popen(HANG)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        layout, _ = jobs.create_job(self.request(HANG), self.root, self.home)
        job = jobs.JobStore(layout.directory).read_job()
        job.update({"state": "running", "pid": bystander.pid})  # the dead runner's pid, now owned by someone else
        core.atomic_write_json(layout.job_json, job)
        core.atomic_write_json(layout.directory / "runner.json", {"pid": bystander.pid, "token": "start-time-of-the-old-runner"})
        result = jobs.cancel_job(layout.directory, grace_sec=0.5)
        self.assertIsNone(bystander.poll(), "an unrelated process was killed")
        self.assertEqual("cancelled", result["state"])
        self.assertEqual("cancelled", jobs.JobStore(layout.directory).read_job()["state"])

    def test_cancelling_a_job_whose_runner_is_gone_removes_the_temporary_paths_it_registered(self):
        project = Path(self._folder.name) / "project"
        export = project / "Builds" / "iOS"
        export.mkdir(parents=True)
        (export / "Xcode.proj").write_text("x", encoding="utf-8")
        protected = project / "Builds" / "Keep"
        protected.mkdir()
        layout, _ = jobs.create_job(self.request(HANG, payload={"type": "command", "argv": HANG, "cwd": None, "projectDir": str(project)},
                                                 protected_paths=(str(protected),)), self.root, self.home)
        core.atomic_write_json(layout.directory / "temp-paths.json", [str(export), str(protected)])
        job = jobs.JobStore(layout.directory).read_job()
        job.update({"state": "running", "pid": 999999})
        core.atomic_write_json(layout.job_json, job)  # the runner is gone: nobody ran its cleanup and no result was saved
        jobs.cancel_job(layout.directory, grace_sec=0.3)
        self.assertFalse(export.exists(), "the export folder in the project would block the next build")
        self.assertTrue(protected.exists(), "a protected path stays")
        events = [json.loads(line) for line in (layout.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("dead runner" in e.get("message", "") for e in events))

    def test_a_job_without_a_runner_record_is_never_killed_by_pid(self):
        bystander = __import__("subprocess").Popen(HANG)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        layout, _ = jobs.create_job(self.request(HANG), self.root, self.home)
        job = jobs.JobStore(layout.directory).read_job()
        job.update({"state": "running", "pid": bystander.pid})
        core.atomic_write_json(layout.job_json, job)
        jobs.cancel_job(layout.directory, grace_sec=0.3)
        self.assertIsNone(bystander.poll())

    def test_a_dead_runner_is_a_stall_seen_by_the_watcher_from_event_age(self):
        layout, _ = jobs.create_job(self.request(HANG, stall_sec=60), self.root, self.home)  # queued, no runner ever starts
        later = core.now() + timedelta(seconds=120)
        outcome = jobs.watch(lambda offset: jobs.tail_info(layout.directory, offset, now=lambda: later), lambda _: None, poll_sec=0.01)
        self.assertEqual("STALL", outcome.status)
        self.assertEqual(constants.EXIT_STALL, outcome.exit_code)
        self.assertIn("runner may be dead", outcome.reason)

    def test_the_watcher_stall_window_can_be_narrowed(self):
        layout, _ = jobs.create_job(self.request(HANG, stall_sec=600), self.root, self.home)
        later = core.now() + timedelta(seconds=20)
        fetch = lambda offset: jobs.tail_info(layout.directory, offset, now=lambda: later)
        self.assertEqual("STALL", jobs.watch(fetch, lambda _: None, stall_sec=10, poll_sec=0.01).status)


class LaunchdTests(JobTestCase):
    def test_plist_roundtrips_arguments_that_need_xml_escaping(self):
        import plistlib
        data = jobs.launchd_plist(["python3", "-c", "a<b&c>'\""], Path("/tmp/run dir"), Path("/tmp/run dir/runner.log"), "com.valtrion.x")
        loaded = plistlib.loads(data)
        self.assertEqual("a<b&c>'\"", loaded["ProgramArguments"][2])
        self.assertTrue(loaded["RunAtLoad"])
        self.assertNotIn("KeepAlive", loaded, "a one-shot agent must not be restarted")

    def test_bootstraps_into_the_requested_domain_and_records_the_label_for_cleanup(self):
        import plistlib
        layout, spec = jobs.create_job(self.request(["x"]), self.root, self.home)
        calls = []
        info = jobs.launch_detached(layout.directory, "macos", "gui", spec, lambda command: (calls.append(list(command)), (0, ""))[1])
        self.assertEqual(["launchctl", "bootstrap"], calls[0][:2])
        self.assertTrue(calls[0][2].startswith("gui/"))
        self.assertEqual(info["launchd"]["label"], plistlib.loads((layout.directory / "launchd.plist").read_bytes())["Label"])
        self.assertEqual(info["launchd"], jobs.JobStore(layout.directory).read_spec()["launchd"])
        argv = plistlib.loads((layout.directory / "launchd.plist").read_bytes())["ProgramArguments"]
        self.assertEqual(["job", "_run", str(layout.directory)], argv[-3:])

    def test_auto_falls_back_from_the_user_domain_to_the_gui_domain(self):
        layout, spec = jobs.create_job(self.request(["x"]), self.root, self.home)
        calls = []

        def launchctl(command):
            calls.append(command[2].split("/")[0])
            return (5, "Input/output error") if command[2].startswith("user/") else (0, "")

        info = jobs.launch_detached(layout.directory, "macos", "auto", spec, launchctl)
        self.assertEqual(["user", "gui"], calls)
        self.assertTrue(info["launchd"]["domain"].startswith("gui/"))
        self.assertEqual(info["launchd"], jobs.JobStore(layout.directory).read_spec()["launchd"], "the cleanup uses the domain that worked")

    def test_a_failed_bootstrap_names_the_owner_action_for_the_gui_domain(self):
        layout, spec = jobs.create_job(self.request(["x"]), self.root, self.home)
        with self.assertRaises(jobs.JobError) as caught:
            jobs.launch_detached(layout.directory, "macos", "gui", spec, lambda command: (5, "Bootstrap failed: 5: Input/output error"))
        self.assertIn("logged-in user", str(caught.exception))
        with self.assertRaises(jobs.JobError):
            jobs.launch_detached(layout.directory, "macos", "system", spec, lambda command: (0, ""))


class DroppedConnectionTests(JobTestCase):
    """A fake SSH transport drops mid-job. The job's outcome does not change and the watcher resumes from its offset."""

    class Loopback(Transport):
        kind = "ssh"

        def __init__(self, drop_calls):
            self.drop_calls = set(drop_calls)
            self.calls = 0

        def run(self, command, timeout_sec, cwd=None):
            self.calls += 1
            if self.calls in self.drop_calls:
                return CommandResult(remote.SSH_FAILURE, "", "ssh: connection reset by peer")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(command[1:]))
            return CommandResult(code, out.getvalue(), err.getvalue())

        def put(self, local, remote_path, timeout_sec):
            return CommandResult(0)

        def get(self, remote_path, local, timeout_sec):
            return CommandResult(0)

    def watch_over(self, transport, extra=()):
        config.add_ssh_host("mac", "me@mac", "macos", str(self.root), self.home)
        target = remote.Remote(transport, ("valtrion-loopback",))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(commands, "_remote", return_value=target), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["job", "watch", "--host", "mac", "--no-collect", "--poll-sec", "0.05", *extra, self.job_id])
        return code, out.getvalue()

    def test_drops_in_the_middle_do_not_change_the_outcome_and_events_arrive_once_in_order(self):
        info = jobs.start_job(self.request(counting(3.0)), self.root, self.home)
        self.job_id = info["jobId"]
        transport = self.Loopback(drop_calls=(2, 3, 5, 6, 7))
        code, output = self.watch_over(transport)
        self.assertEqual(0, code, output)
        self.assertGreaterEqual(transport.calls, 8)
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertEqual(list(range(len(events))), [e["seq"] for e in events], "an event was lost or delivered twice")
        self.assertEqual("job.finished", events[-1]["type"])
        self.assertIn("GDV-RESULT PASS", output)
        self.assertEqual("PASS", jobs.JobStore(Path(info["dir"])).read_job()["status"])

    def test_resume_from_an_offset_continues_where_the_watcher_stopped(self):
        info = jobs.start_job(self.request(counting(1.2)), self.root, self.home)
        directory = Path(info["dir"])
        first = jobs.tail_info(directory, 0, self.root)
        self.assertTrue(self.wait_for(lambda: jobs.JobStore(directory).read_job()["state"] == "finished"))
        rest = jobs.tail_info(directory, first["nextOffset"], self.root)
        everything = jobs.tail_info(directory, 0, self.root)
        self.assertEqual(everything["lines"], first["lines"] + rest["lines"])

    def test_an_unreachable_host_is_blocked_after_the_bound_not_a_stall(self):
        info = jobs.start_job(self.request(counting(2.0)), self.root, self.home)
        self.job_id = info["jobId"]
        transport = self.Loopback(drop_calls=range(1, 10_000))
        code, output = self.watch_over(transport, ("--unreachable-sec", "0.3"))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("unreachable", output)
        self.assertIn("resume with --from", output)
        self.assertTrue(self.wait_for(lambda: jobs.JobStore(Path(info["dir"])).read_job()["state"] == "finished"))
        self.assertEqual("PASS", jobs.JobStore(Path(info["dir"])).read_job()["status"], "the job is unaffected by the lost connection")

    def test_an_unknown_job_is_a_permanent_error(self):
        code, output = self.watch_over_unknown()
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("unknown job", output)

    def watch_over_unknown(self):
        self.job_id = "no-such-job"
        return self.watch_over(self.Loopback(()))


class ForwardingTests(unittest.TestCase):
    def test_host_options_are_stripped_only_before_the_user_command(self):
        raw = ["job", "start", "--host", "mac", "--remote-project=/p", "--timeout-sec", "5", "--", "tool", "--host", "x", "--remote-project", "y"]
        self.assertEqual(["job", "start", "--timeout-sec", "5", "--", "tool", "--host", "x", "--remote-project", "y"], commands._strip_host_options(raw))

    def test_events_longer_than_a_chunk_do_not_block_the_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            long_line = json.dumps({"x": "y" * 5000})
            path.write_text(long_line + "\n" + '{"n":2}\n', encoding="utf-8")
            lines, offset = jobs.read_events(path, 0, max_bytes=1024)
            self.assertEqual([long_line, '{"n":2}'], lines)
            self.assertEqual(path.stat().st_size, offset)


class RetryingTransportTests(unittest.TestCase):
    def test_retries_transport_failures_with_backoff_and_stops_on_success(self):
        outcomes = [CommandResult(255), CommandResult(255), CommandResult(0, "ok")]
        pauses = []

        class Flaky(Transport):
            kind = "ssh"

            def run(self, command, timeout_sec, cwd=None):
                return outcomes.pop(0)

            def put(self, local, remote_path, timeout_sec):
                return CommandResult(0)

            def get(self, remote_path, local, timeout_sec):
                return CommandResult(0)

        transport = remote.RetryingTransport(Flaky(), attempts=4, backoff_sec=(1, 2, 4), sleep=pauses.append)
        self.assertEqual("ok", transport.run(["x"], 5).stdout)
        self.assertEqual([1, 2], pauses)

    def test_a_remote_command_failure_is_not_retried(self):
        calls = []

        class Failing(Transport):
            kind = "ssh"

            def run(self, command, timeout_sec, cwd=None):
                calls.append(1)
                return CommandResult(2, "", "BLOCKED")

            def put(self, *_):
                return CommandResult(0)

            def get(self, *_):
                return CommandResult(0)

        result = remote.RetryingTransport(Failing(), sleep=lambda _: None).run(["x"], 5)
        self.assertEqual(2, result.returncode)
        self.assertEqual(1, len(calls))

    def test_gives_up_after_the_attempts(self):
        class Down(Transport):
            kind = "ssh"
            count = 0

            def run(self, command, timeout_sec, cwd=None):
                Down.count += 1
                return CommandResult(None, "", "", True)

            def put(self, *_):
                return CommandResult(0)

            def get(self, *_):
                return CommandResult(0)

        result = remote.RetryingTransport(Down(), attempts=3, sleep=lambda _: None).run(["x"], 5)
        self.assertTrue(result.timed_out)
        self.assertEqual(3, Down.count)

    def test_pull_run_keeps_the_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "remote" / "proj" / "abcd1234" / "run1"
            (source / "device").mkdir(parents=True)
            (source / "result.json").write_text("{}", encoding="utf-8")
            (source / "device" / "log.txt").write_text("x", encoding="utf-8")
            target = Path(folder) / "caller" / "proj" / "abcd1234" / "run1"
            self.assertTrue(remote.pull_run(LocalTransport(), str(source), target).ok)
            self.assertTrue(remote.pull_run(LocalTransport(), str(source), target).ok, "a repeated pull is idempotent")
            self.assertEqual({"result.json", "device/log.txt"}, {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
