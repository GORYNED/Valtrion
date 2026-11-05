"""Unity (or any step) on a desktop of its own: the process runs, its output reaches the log, the watchdog can stop it, the setting is read."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from valtrion import config, constants, hiddendesktop, supervise

WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000


@unittest.skipUnless(WINDOWS, "a hidden desktop exists on Windows only")
class StartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="valtrion-hidden-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))

    def _start(self, code: str, env=None):
        log = open(self.folder / "out.log", "ab")
        self.addCleanup(log.close)
        process = hiddendesktop.start([sys.executable, "-I", "-c", code], str(self.folder), env, log, CREATE_NO_WINDOW)
        self.addCleanup(process.close)
        return process

    def test_the_exit_code_and_the_output_are_returned(self) -> None:
        process = self._start("import sys; print('hello'); sys.exit(7)")
        self.assertEqual(7, process.wait(timeout=60))
        self.assertEqual(7, process.poll())
        self.assertIn(b"hello", (self.folder / "out.log").read_bytes())

    def test_the_environment_is_passed(self) -> None:
        environment = dict(os.environ, VALTRION_PROBE="on-the-other-desktop")
        process = self._start("import os; print(os.environ['VALTRION_PROBE'])", environment)
        self.assertEqual(0, process.wait(timeout=60))
        self.assertIn(b"on-the-other-desktop", (self.folder / "out.log").read_bytes())

    def test_a_running_process_times_out_and_can_be_killed(self) -> None:
        process = self._start("import time; time.sleep(60)")
        self.assertIsNone(process.poll())
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=0.2)
        process.kill()
        process.wait(timeout=30)
        self.assertIsNotNone(process.poll())

    def test_the_result_stays_after_close(self) -> None:
        process = self._start("import sys; sys.exit(3)")
        process.wait(timeout=60)
        process.close()
        self.assertEqual(3, process.poll())


@unittest.skipUnless(WINDOWS, "a hidden desktop exists on Windows only")
class SuperviseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="valtrion-hidden-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))

    def _supervise(self, code: str, **watchdog):
        options = dict(timeout_sec=60.0, stall_sec=float("inf"))
        options.update(watchdog)
        return supervise.supervise([sys.executable, "-I", "-c", code], supervise.Watchdog(**options), self.folder / "step.log",
                                   hidden_desktop=True)

    def test_a_passing_step_passes(self) -> None:
        result = self._supervise("print('ok')")
        self.assertEqual(constants.PASS, result.status)
        self.assertIn(b"ok", (self.folder / "step.log").read_bytes())

    def test_a_failing_step_fails_with_its_exit_code(self) -> None:
        result = self._supervise("import sys; sys.exit(5)")
        self.assertEqual(constants.FAIL, result.status)
        self.assertEqual(5, result.returncode)

    def test_the_timeout_stops_the_step_and_its_children(self) -> None:
        code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
        result = self._supervise(code, timeout_sec=2.0)
        self.assertEqual(constants.TIMEOUT, result.status)


class HostSettingTests(unittest.TestCase):
    def _home(self, document) -> Path:
        home = Path(tempfile.mkdtemp(prefix="valtrion-home-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        if document is not None:
            (home / "host.json").write_text(json.dumps(document), encoding="utf-8")
        return home

    def test_off_by_default(self) -> None:
        self.assertFalse(config.hidden_desktop(self._home(None)))
        self.assertFalse(config.hidden_desktop(self._home({})))

    def test_on_when_set(self) -> None:
        self.assertTrue(config.hidden_desktop(self._home({"hiddenDesktop": True})))

    def test_a_non_boolean_is_an_error(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.hidden_desktop(self._home({"hiddenDesktop": "yes"}))


class NonWindowsTests(unittest.TestCase):
    @unittest.skipIf(WINDOWS, "this is the refusal on other systems")
    def test_start_refuses_outside_windows(self) -> None:
        with self.assertRaises(OSError):
            hiddendesktop.start(["true"], None, None, None, 0)


if __name__ == "__main__":
    unittest.main()
