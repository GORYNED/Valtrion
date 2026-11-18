"""Completed-test counts are progress; report heartbeats and current-test changes are not."""

import json
import tempfile
import unittest
from pathlib import Path

from valtrion import supervise, testprogress


class CompletedTests(unittest.TestCase):
    def test_only_new_completions_keep_a_reported_test_run_alive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "progress.json"
            now = 0.0
            tracker = supervise.ProgressTracker(
                supervise.Watchdog(timeout_sec=60, progress_counter=lambda: testprogress.completed_count(path)),
                None, monotonic=lambda: now)
            report = {"schema": testprogress.SCHEMA, "state": "running", "total": 4, "done": 0, "current": "Game.Tests.First"}
            path.write_text(json.dumps(report), encoding="utf-8")
            now = 1.0
            self.assertTrue(tracker.poll())
            report.update(current="Game.Tests.Second", utc="later", mainUtc="later")
            path.write_text(json.dumps(report), encoding="utf-8")
            now = 2.0
            self.assertFalse(tracker.poll())
            path.write_text("{partial", encoding="utf-8")
            now = 3.0
            self.assertFalse(tracker.poll())
            path.unlink()
            now = 4.0
            self.assertFalse(tracker.poll())
            self.assertEqual(3.0, tracker.idle_sec())
            report.update(done=1)
            path.write_text(json.dumps(report), encoding="utf-8")
            now = 5.0
            self.assertTrue(tracker.poll())
            self.assertEqual(0.0, tracker.idle_sec())

    def test_a_report_must_describe_a_started_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "progress.json"
            for state in ("", "starting", "unknown"):
                path.write_text(json.dumps({"schema": testprogress.SCHEMA, "state": state, "total": 4, "done": 0}), encoding="utf-8")
                self.assertIsNone(testprogress.completed_count(path))
