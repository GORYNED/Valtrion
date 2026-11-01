import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from valtrion import constants, core, supervise
from valtrion.supervise import Watchdog, supervise as run_supervised

PYTHON = sys.executable
HANG = [PYTHON, "-c", "import time; time.sleep(120)"]


def spawn_tree_script(pid_file: Path) -> list[str]:
    """A parent that starts a long-lived child, records the child's pid, and then hangs."""
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n")
    return [PYTHON, "-c", code]


class SuperviseTests(unittest.TestCase):
    def setUp(self):
        # A killed descendant releases its inherited log handle a moment after it exits; do not fail the test on that lag.
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.folder = Path(self._folder.name)
        self.log = self.folder / "step.log"

    def run_step(self, argv, **options):
        defaults = dict(timeout_sec=30, stall_sec=30, poll_sec=0.05)
        defaults.update(options)
        return run_supervised(argv, Watchdog(**defaults), self.log)

    def test_clean_exit_is_pass(self):
        result = self.run_step([PYTHON, "-c", "print('ok')"])
        self.assertEqual((constants.PASS, 0), (result.status, result.returncode))
        self.assertIsNone(result.reason)

    def test_non_zero_exit_is_fail(self):
        result = self.run_step([PYTHON, "-c", "raise SystemExit(7)"])
        self.assertEqual(constants.FAIL, result.status)
        self.assertIn("7", result.reason)

    def test_missing_executable_is_blocked(self):
        result = self.run_step([str(self.folder / "does-not-exist.exe")])
        self.assertEqual(constants.BLOCKED, result.status)
        self.assertIn("cannot start", result.reason)

    def test_injected_hang_is_a_stall_at_the_clock_deadline(self):
        window = 2.0
        now = 0.0

        def advance(seconds):
            nonlocal now
            now += seconds

        events = []
        result = run_supervised(
            HANG, Watchdog(timeout_sec=60, stall_sec=window, poll_sec=0.05), self.log,
            on_event=lambda *event: events.append(event), monotonic=lambda: now, sleep=advance)
        self.assertEqual(constants.STALL, result.status)
        self.assertGreaterEqual(result.idle_sec, window)
        self.assertLessEqual(result.idle_sec, window + 0.05)
        self.assertEqual(constants.EXIT_STALL, result.exit_code)
        self.assertIn("stall.detected", [event[0] for event in events])

    def test_hard_timeout_is_timeout_even_when_the_log_keeps_growing(self):
        spam = [PYTHON, "-u", "-c", "import time\nwhile True:\n    print('tick'); time.sleep(0.05)"]
        started = time.monotonic()
        result = self.run_step(spam, timeout_sec=1.5, stall_sec=30)
        self.assertEqual(constants.TIMEOUT, result.status)
        self.assertLess(time.monotonic() - started, 1.5 + 8.0)  # the timeout plus slack for a slow start and reaping the tree on a loaded machine

    def test_log_growth_is_progress(self):
        # The step runs longer than the stall window; a loaded machine may start the interpreter slowly, so the window is generous.
        slow = [PYTHON, "-u", "-c", "import time\nfor i in range(40):\n    print(i); time.sleep(0.25)"]
        result = self.run_step(slow, timeout_sec=120, stall_sec=6.0)
        self.assertEqual(constants.PASS, result.status)

    def test_whole_process_tree_is_killed(self):
        pid_file = self.folder / "child.pid"
        result = self.run_step(spawn_tree_script(pid_file), timeout_sec=60, stall_sec=2.5)
        self.assertEqual(constants.STALL, result.status)
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while core.process_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(core.process_alive(child), "a grandchild survived the kill of the step")

    def test_descendants_left_after_a_normal_exit_are_killed(self):
        pid_file = self.folder / "orphan.pid"
        code = (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n")
        result = self.run_step([PYTHON, "-c", code])
        self.assertEqual(constants.PASS, result.status)
        orphan = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while core.process_alive(orphan) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(core.process_alive(orphan))

    def test_heartbeat_seq_is_the_authority_when_given(self):
        heartbeat = self.folder / "heartbeat.json"
        heartbeat.write_text(json.dumps({"seq": 1}))
        stuck_but_chatty = [PYTHON, "-u", "-c", "import time\nwhile True:\n    print('still here'); time.sleep(0.05)"]
        result = self.run_step(stuck_but_chatty, timeout_sec=60, stall_sec=1.5, heartbeat=heartbeat)
        self.assertEqual(constants.STALL, result.status)

    def test_advancing_heartbeat_keeps_a_silent_step_alive(self):
        heartbeat = self.folder / "heartbeat.json"
        heartbeat.write_text(json.dumps({"seq": 0}))
        stop = threading.Event()

        def beat():
            seq = 0
            while not stop.is_set():
                seq += 1
                heartbeat.write_text(json.dumps({"seq": seq}))
                time.sleep(0.1)

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        result = self.run_step([PYTHON, "-c", "import time; time.sleep(2.5)"], stall_sec=1.0, heartbeat=heartbeat)
        self.assertEqual(constants.PASS, result.status)

    def test_watched_file_growth_is_progress(self):
        watched = self.folder / "out.bin"
        code = f"import time\nfor i in range(40):\n    open({str(watched)!r}, 'ab').write(b'x'); time.sleep(0.25)"
        result = self.run_step([PYTHON, "-c", code], timeout_sec=120, stall_sec=6.0, progress_files=(watched,))
        self.assertEqual(constants.PASS, result.status)

    def test_cpu_of_the_tree_is_opt_in_progress(self):
        busy = [PYTHON, "-c", "while True: pass"]
        quiet = self.run_step(busy, timeout_sec=20, stall_sec=1.0, log_counts=False)
        self.assertEqual(constants.STALL, quiet.status)
        counted = self.run_step(busy, timeout_sec=9, stall_sec=4.0, log_counts=False, cpu_counts=True)  # wide: a loaded machine starves the busy loop
        self.assertEqual(constants.TIMEOUT, counted.status)

    def test_terminal_line_of_the_application_is_stricter_than_the_exit_code(self):
        permission = [PYTHON, "-c", "print('GDV-RESULT NEEDS_PERMISSION allow the camera in Settings'); raise SystemExit(1)"]
        result = self.run_step(permission)
        self.assertEqual(constants.NEEDS_PERMISSION, result.status)
        self.assertIn("allow the camera", result.reason)
        failed_but_zero = [PYTHON, "-c", "print('GDV-RESULT FAIL step 3 timed out')"]
        self.assertEqual(constants.FAIL, self.run_step(failed_but_zero).status)

    def test_contract_exit_codes(self):
        blocked = [PYTHON, "-c", "raise SystemExit(2)"]
        stalled = [PYTHON, "-c", "raise SystemExit(3)"]
        plain = run_supervised(blocked, Watchdog(timeout_sec=30), self.log)
        self.assertEqual(constants.FAIL, plain.status)
        contract = run_supervised(blocked, Watchdog(timeout_sec=30), self.log, contract_exit_codes=True)
        self.assertEqual(constants.BLOCKED, contract.status)
        self.assertEqual(constants.TIMEOUT, run_supervised(stalled, Watchdog(timeout_sec=30), self.log, contract_exit_codes=True).status)

    def test_cancel_file_stops_the_step(self):
        cancel = self.folder / "cancel"
        threading.Timer(0.5, cancel.write_text, args=("x",)).start()
        result = self.run_step(HANG, cancel_file=cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(constants.BLOCKED, result.status)

    def test_windows_cannot_be_disabled(self):
        for options in ({"timeout_sec": 0}, {"timeout_sec": 5, "stall_sec": 0}, {"timeout_sec": 5, "stall_sec": -1}):
            with self.subTest(options), self.assertRaises(ValueError):
                Watchdog(**options)

    def test_run_bounded_returns_output(self):
        result = supervise.run_bounded([PYTHON, "-c", "print('hello')"], timeout_sec=20)
        self.assertTrue(result.ok)
        self.assertIn("hello", result.output)
        hung = supervise.run_bounded(HANG, timeout_sec=20, stall_sec=1.0, poll_sec=0.05)
        self.assertEqual(constants.STALL, hung.status)

    def test_call_bounded(self):
        self.assertEqual((True, 5), supervise.call_bounded(lambda: 5, 5))
        self.assertEqual((False, None), supervise.call_bounded(lambda: time.sleep(2), 0.2))
        with self.assertRaises(KeyError):
            supervise.call_bounded(lambda: {}["x"], 5)


class ClassificationTests(unittest.TestCase):
    def test_marker_pass_with_non_zero_exit_is_fail(self):
        self.assertEqual(constants.FAIL, supervise.classify_exit(1, (constants.PASS, ""))[0])

    def test_cpu_time_parsing(self):
        self.assertEqual(65.5, supervise._parse_cpu_time("1:05.50"))
        self.assertEqual(90061.0, supervise._parse_cpu_time("1-01:01:01"))
        self.assertEqual(0.0, supervise._parse_cpu_time("garbage"))


if __name__ == "__main__":
    unittest.main()
