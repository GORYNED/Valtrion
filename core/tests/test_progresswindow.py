"""The progress window of `unity run`: what it reads from the Unity log, how a run describes itself, and that it never gets in the run's way."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import config, hiddendesktop, progresswindow, supervise


class PhaseTests(unittest.TestCase):
    def test_the_phase_follows_the_log(self) -> None:
        tracker = progresswindow.PhaseTracker()
        self.assertEqual(progresswindow.START_PHASE, tracker.phase)
        tracker.feed("Refreshing native plugins\n")
        self.assertEqual(progresswindow.LOADING_PHASE, tracker.phase)
        tracker.feed("[Project] Loading completed in 12.3 seconds\n")
        self.assertEqual("Project loaded", tracker.phase)
        tracker.feed("Running tests for ExecutionSettings with details:\n")
        self.assertEqual("Running tests", tracker.phase)
        tracker.feed("Cleanup mono\n")
        self.assertEqual("Shutting down", tracker.phase)

    def test_a_line_split_between_two_reads_is_read_whole(self) -> None:
        tracker = progresswindow.PhaseTracker()
        tracker.feed("Running tests for Exec")
        self.assertNotEqual("Running tests", tracker.phase)
        tracker.feed("utionSettings with details:\n")
        self.assertEqual("Running tests", tracker.phase)

    def test_machine_lines_are_not_shown_as_the_last_line(self) -> None:
        tracker = progresswindow.PhaseTracker()
        tracker.feed('Compiling scripts\n##utp:{"type":"ProjectInfo"}\n')
        self.assertEqual("Compiling scripts", tracker.last_line)

    def test_elapsed_time(self) -> None:
        self.assertEqual("00:05", progresswindow.format_elapsed(5.9))
        self.assertEqual("02:03", progresswindow.format_elapsed(123))
        self.assertEqual("1:01:01", progresswindow.format_elapsed(3661))
        self.assertEqual("00:00", progresswindow.format_elapsed(-4))


class DescribeTests(unittest.TestCase):
    def test_a_test_run_names_its_platform_and_assemblies(self) -> None:
        args = ["-batchmode", "-runTests", "-testPlatform", "PlayMode", "-assemblyNames", "A;B"]
        self.assertEqual(("Unity tests (PlayMode)", "Game: A;B"), progresswindow.describe_run(args, Path("C:/work/Game")))

    def test_a_method_run_names_the_method(self) -> None:
        args = ["-batchmode", "-executeMethod", "Build.Run"]
        self.assertEqual(("Unity batch method", "Build.Run"), progresswindow.describe_run(args, None))

    def test_anything_else_has_a_generic_title(self) -> None:
        self.assertEqual(("Unity batch run", "Game"), progresswindow.describe_run(["-batchmode", "-quit"], Path("Game")))


class StartTests(unittest.TestCase):
    def test_it_is_not_started_when_off(self) -> None:
        self.assertIsNone(progresswindow.start(Path("unity.log"), "t", "d", enabled=False))

    def test_a_start_that_fails_is_no_window_and_no_error(self) -> None:
        with mock.patch.object(progresswindow.subprocess, "Popen", side_effect=OSError("no")), \
                mock.patch.object(progresswindow.os, "name", "nt"):
            self.assertIsNone(progresswindow.start(Path("unity.log"), "t", "d"))

    def test_finish_writes_the_status_for_a_running_window(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="valtrion-progress-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        done = folder / "done.json"
        process = mock.Mock(**{"poll.return_value": None})
        progresswindow.Handle(process, done).finish("PASS", None)
        self.assertEqual({"status": "PASS", "reason": ""}, json.loads(done.read_text(encoding="utf-8")))

    def test_finish_leaves_nothing_when_the_window_is_gone(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="valtrion-progress-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        done = folder / "done.json"
        done.write_text("{}", encoding="utf-8")
        progresswindow.Handle(mock.Mock(**{"poll.return_value": 0}), done).finish("FAIL", "x")
        self.assertFalse(done.exists())


class DefaultsTests(unittest.TestCase):
    def _home(self, document) -> Path:
        home = Path(tempfile.mkdtemp(prefix="valtrion-home-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        if document is not None:
            (home / "host.json").write_text(json.dumps(document), encoding="utf-8")
        return home

    def test_the_default_is_the_callers(self) -> None:
        home = self._home(None)
        self.assertFalse(config.progress_window(home))
        self.assertTrue(config.progress_window(home, True))
        self.assertTrue(config.hidden_desktop(home, True))

    def test_host_json_wins_over_the_default(self) -> None:
        home = self._home({"progressWindow": False, "hiddenDesktop": False})
        self.assertFalse(config.progress_window(home, True))
        self.assertFalse(config.hidden_desktop(home, True))

    def test_a_non_boolean_is_an_error(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.progress_window(self._home({"progressWindow": 1}))


@unittest.skipUnless(os.name == "nt", "the hidden desktop exists on Windows only")
class FallbackTests(unittest.TestCase):
    def test_a_desktop_that_cannot_be_created_runs_the_step_on_the_current_one(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="valtrion-fallback-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        with mock.patch.object(hiddendesktop, "start", side_effect=hiddendesktop.DesktopUnavailable(5, "denied")):
            result = supervise.supervise(
                [__import__("sys").executable, "-I", "-c", "print('ran')"], supervise.Watchdog(timeout_sec=60.0, stall_sec=float("inf")),
                folder / "step.log", hidden_desktop=True)
        self.assertEqual("PASS", result.status)
        self.assertIn(b"ran", (folder / "step.log").read_bytes())


if __name__ == "__main__":
    unittest.main()
