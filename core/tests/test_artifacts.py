import os
import tempfile
import time
import unittest
from pathlib import Path

from valtrion import artifacts, constants, core
from valtrion.artifacts import RunLayout


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.root = self.base / "artifacts"
        self.layout = RunLayout(self.root, "proj", "abcd1234", "run-1")
        self.layout.directory.mkdir(parents=True)

    def make(self, relative: str, content: str = "x") -> Path:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def save_results(self):
        core.atomic_write_json(self.layout.result, {"status": "PASS"})
        self.layout.console.write_text("log", encoding="utf-8")

    def test_removes_only_what_the_run_registered_after_results_are_saved(self):
        export = self.make("export/build.bin").parent
        other = self.make("other/keep.txt").parent
        artifacts.register_temp(self.layout, export)
        self.save_results()
        report = artifacts.cleanup_run(self.layout)
        self.assertEqual((str(export.resolve()),), report.removed)
        self.assertFalse(export.exists())
        self.assertTrue(other.exists())
        self.assertTrue(self.layout.result.exists())
        self.assertTrue(self.layout.console.exists())

    def test_nothing_is_removed_before_the_results_are_saved(self):
        export = self.make("export/build.bin").parent
        artifacts.register_temp(self.layout, export)
        report = artifacts.cleanup_run(self.layout)
        self.assertIn("not saved", report.skipped_reason)
        self.assertTrue(export.exists())

    def test_cleanup_after_a_failure_once_results_exist(self):
        export = self.make("export/x").parent
        artifacts.register_temp(self.layout, export)
        core.atomic_write_json(self.layout.result, {"status": "FAIL"})
        self.assertFalse(artifacts.cleanup_run(self.layout, status=constants.FAIL).skipped_reason)
        self.assertFalse(export.exists())

    def test_never_removes_a_baseline_or_anything_around_it(self):
        baselines = self.make("repo-validation/baselines/plan/ipad.json").parent.parent
        artifacts.register_temp(self.layout, baselines)  # a parent of a protected path
        artifacts.register_temp(self.layout, baselines / "plan")  # a parent of the baseline file
        artifacts.register_temp(self.layout, baselines / "plan" / "ipad.json")  # the baseline itself
        self.save_results()
        report = artifacts.cleanup_run(self.layout, protected=[baselines / "plan" / "ipad.json"])
        self.assertEqual((), report.removed)
        self.assertEqual(3, len(report.kept))
        self.assertTrue((baselines / "plan" / "ipad.json").exists())

    def test_never_removes_a_working_copy_the_artifacts_root_or_the_run_results(self):
        worktree = self.base / "other-worktree"
        (worktree / ".git").mkdir(parents=True)
        (worktree / "file.cs").write_text("x", encoding="utf-8")
        self.save_results()
        for path in (worktree, self.root, self.layout.result, self.layout.directory, Path(self.base.anchor), Path.home()):
            artifacts.register_temp(self.layout, path)
        report = artifacts.cleanup_run(self.layout)
        self.assertEqual((), report.removed, report.kept)
        self.assertTrue((worktree / "file.cs").exists())
        self.assertTrue(self.layout.result.exists())

    def test_a_retained_run_keeps_everything(self):
        export = self.make("export/x").parent
        artifacts.register_temp(self.layout, export)
        self.save_results()
        artifacts.mark_retained(self.layout.directory)
        artifacts.register_temp(self.layout, self.layout.directory / "scratch")
        report = artifacts.cleanup_run(self.layout)
        self.assertIn(str(export.resolve()), report.removed)  # outside the run folder: still the run's own temporary
        self.assertTrue(self.layout.result.exists())

    def test_a_read_only_temp_tree_is_removed(self):
        export = self.make("export/ro.bin")
        os.chmod(export, 0o444)
        artifacts.register_temp(self.layout, export.parent)
        self.save_results()
        artifacts.cleanup_run(self.layout)
        self.assertFalse(export.parent.exists())


class GcTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.root = Path(self._folder.name) / "artifacts"

    def run_folder(self, project, sha, run, age_days, state="finished"):
        layout = RunLayout(self.root, project, sha, run)
        layout.directory.mkdir(parents=True)
        core.atomic_write_json(layout.job_json, {"state": state})
        layout.result.write_text("{}", encoding="utf-8")
        stamp = time.time() - age_days * 86400
        for path in (layout.job_json, layout.result, layout.directory):
            os.utime(path, (stamp, stamp))
        return layout.directory

    def test_removes_old_runs_only_and_tidies_empty_parents(self):
        old = self.run_folder("p", "aaaa0000", "old", 40)
        new = self.run_folder("p", "bbbb0000", "new", 1)
        gone = self.run_folder("q", "cccc0000", "ancient", 90)
        report = artifacts.gc(self.root, 30)
        self.assertEqual({str(old), str(gone)}, set(report.removed))
        self.assertFalse(old.exists())
        self.assertFalse((self.root / "q").exists())
        self.assertTrue(new.exists())

    def test_never_removes_active_retained_or_protected_runs_and_dry_run_removes_nothing(self):
        active = self.run_folder("p", "aaaa0000", "active", 99, state="running")
        retained = self.run_folder("p", "aaaa0000", "keep", 99)
        artifacts.mark_retained(retained)
        protected = self.run_folder("p", "aaaa0000", "baseline-source", 99)
        stranger = self.root / "p" / "aaaa0000" / "not-a-run"
        stranger.mkdir()
        (stranger / "user.txt").write_text("user data", encoding="utf-8")
        report = artifacts.gc(self.root, 30, protected=[protected])
        self.assertEqual((), report.removed)
        for path in (active, retained, protected, stranger / "user.txt"):
            self.assertTrue(path.exists(), path)
        dry = self.run_folder("p", "dddd0000", "dry", 99)
        self.assertEqual((str(dry),), artifacts.gc(self.root, 30, dry_run=True, protected=[protected]).removed)
        self.assertTrue(dry.exists())

    def test_only_the_three_level_layout_is_considered(self):
        loose = self.root / "stray"
        loose.mkdir(parents=True)
        (loose / "job.json").write_text("{}", encoding="utf-8")
        deep = self.root / "p" / "aaaa0000" / "r" / "nested" / "sub"
        deep.mkdir(parents=True)
        (deep.parent / "job.json").write_text("{}", encoding="utf-8")
        artifacts.gc(self.root, 0)
        self.assertTrue((loose / "job.json").exists())

    def test_negative_keep_days_is_refused(self):
        with self.assertRaises(ValueError):
            artifacts.gc(self.root, -1)


if __name__ == "__main__":
    unittest.main()
