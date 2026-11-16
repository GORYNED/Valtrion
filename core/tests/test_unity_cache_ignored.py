"""Git-ignored build files that change while Unity runs do not stop a result from being stored (it is stored under the key of the state the
run started from; ignored files stay in the key), and `cache: not stored` says when a run was the first Valtrion saw with the copy's Library."""

import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import unitycache, unityrun

from .test_unity_cache import PASSING, CacheTestCase, write


def git(folder: Path, *arguments: str) -> None:
    subprocess.run(["git", "-c", "init.defaultBranch=main", *arguments], cwd=folder, check=True, capture_output=True)


@unittest.skipUnless(shutil.which("git"), "needs git")
class BuildOutputTests(CacheTestCase):
    def setUp(self):
        super().setUp()
        git(self.project, "init", "-q")
        (self.project / ".gitignore").write_text("Assets/Generated/\nAssets/link.xml\nAssets/link.xml.meta\nAssets/Gen.cs\n", encoding="utf-8")
        write(self.results, PASSING)
        self.root = unitycache.cache_root(self.home)

    def plan(self, *extra, **options):
        return super().plan("-testFilter", "x", *extra, assemblies=None, **options)

    def test_ignored_files_stay_in_the_key(self):
        before = self.plan()
        write(self.project / "Assets" / "Generated" / "Data.asset", "a: 1\n")
        self.assertNotEqual(before.key, self.plan().key)  # an ignored asset the tests may read is an input

    def test_a_build_file_written_during_the_run_is_stored_under_the_starting_key(self):
        before = self.plan()
        write(self.project / "Assets" / "link.xml", "<linker/>\n")  # what a build step writes while Unity runs
        write(self.project / "Assets" / "link.xml.meta", "guid: 0\n")
        line = unityrun.keep_result(before, self.root, True)
        self.assertIn(f"stored PASS under key {before.key}", line)
        self.assertIn("files git ignores changed during the run", line)
        self.assertIn("<project>/Assets/link.xml", line)

    def test_a_build_file_removed_during_the_run_is_stored_too(self):
        write(self.project / "Assets" / "link.xml", "<linker/>\n")
        before = self.plan()
        (self.project / "Assets" / "link.xml").unlink()
        self.assertIn("stored PASS", unityrun.keep_result(before, self.root, True))

    def test_a_change_that_is_not_ignored_is_not_stored(self):
        before = self.plan()
        write(self.project / "Assets" / "link.xml", "<linker/>\n")
        write(self.project / "Assets" / "Data.asset", "a: 1\n")
        line = unityrun.keep_result(before, self.root, True)
        self.assertIn("not stored", line)
        self.assertIn("Data.asset", line)

    def test_ignored_code_that_changed_is_not_tolerated(self):
        before = self.plan()
        write(self.project / "Assets" / "Gen.cs", "class Gen {}\n")
        self.assertIn("not stored", unityrun.keep_result(before, self.root, True))

    def test_without_git_nothing_is_tolerated(self):
        before = self.plan()
        write(self.project / "Assets" / "link.xml", "<linker/>\n")
        with mock.patch.object(unitycache.subprocess, "run", side_effect=OSError("no git")):
            self.assertIn("not stored", unityrun.keep_result(before, self.root, True))

    def test_a_tracked_file_that_matches_an_ignore_rule_is_not_build_output(self):
        write(self.project / "Assets" / "link.xml", "<linker/>\n")
        git(self.project, "add", "-f", "Assets/link.xml")
        before = self.plan()
        write(self.project / "Assets" / "link.xml", "<linker changed='1'/>\n")
        self.assertIn("not stored", unityrun.keep_result(before, self.root, True))


class FirstRunTests(CacheTestCase):
    def root(self):
        return unitycache.cache_root(self.home)

    def test_a_new_library_is_a_first_run_until_it_was_seen(self):
        (self.project / "Library").mkdir()
        self.assertTrue(unitycache.first_with_library(self.root(), self.project))
        unitycache.remember_library(self.root(), self.project)
        self.assertFalse(unitycache.first_with_library(self.root(), self.project))
        with mock.patch.object(unitycache, "library_identity", return_value="another-library"):
            self.assertTrue(unitycache.first_with_library(self.root(), self.project))

    def test_the_not_stored_line_says_so_once(self):
        (self.project / "Library").mkdir()
        write(self.results, PASSING)
        settings = self.project / "ProjectSettings" / "ProjectSettings.asset"
        plan = self.plan()
        settings.write_text("changed: 1\n", encoding="utf-8")
        line = unityrun.keep_result(plan, self.root(), True)
        self.assertIn("not stored", line)
        self.assertIn("first run Valtrion saw", line)
        plan = self.plan()
        settings.write_text("changed: 2\n", encoding="utf-8")
        line = unityrun.keep_result(plan, self.root(), True)
        self.assertIn("not stored", line)
        self.assertNotIn("first run Valtrion saw", line)

    def test_a_stored_run_records_the_library_and_old_records_are_pruned(self):
        (self.project / "Library").mkdir()
        old = self.root() / unitycache.LIBRARIES / "gone.txt"
        write(old, "x\n")
        past = time.time() - (unitycache.LIBRARY_KEEP_DAYS + 1) * 86400
        os.utime(old, (past, past))
        write(self.results, PASSING)
        self.assertIn("stored PASS", unityrun.keep_result(self.plan(), self.root(), True))
        self.assertFalse(unitycache.first_with_library(self.root(), self.project))
        self.assertFalse(old.exists())
