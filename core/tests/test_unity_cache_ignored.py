"""The result cache leaves git-ignored build files out of the key, and says when a run that was not stored was the first one Valtrion
saw with the working copy's Library."""

import os
import subprocess
from pathlib import Path
from unittest import mock

from valtrion import unitycache, unityrun

from .test_unity_cache import CacheTestCase, write


def git(folder: Path, *arguments: str) -> None:
    subprocess.run(["git", "-c", "init.defaultBranch=main", *arguments], cwd=folder, check=True, capture_output=True)


class IgnoredFileTests(CacheTestCase):
    def setUp(self):
        super().setUp()
        git(self.project, "init", "-q")
        (self.project / ".gitignore").write_text("Assets/Generated/\nAssets/link.xml\nAssets/link.xml.meta\nAssets/Gen.cs\n", encoding="utf-8")

    def key(self):  # every file of the project counts in a run without -assemblyNames
        found = self.plan("-testFilter", "x", assemblies=None)
        self.assertIsNotNone(found.key, found.reason)
        return found

    def test_an_ignored_build_file_that_comes_and_goes_does_not_change_the_key(self):
        before = self.key()
        write(self.project / "Assets" / "link.xml", "<linker/>\n")
        write(self.project / "Assets" / "link.xml.meta", "guid: 0\n")
        write(self.project / "Assets" / "Generated" / "Data.asset", "a: 1\n")
        after = self.key()
        self.assertEqual(before.key, after.key)
        self.assertNotIn("<project>/Assets/link.xml", after.components["files"])

    def test_a_tracked_or_untracked_but_not_ignored_asset_still_counts(self):
        before = self.key()
        write(self.project / "Assets" / "Data.asset", "a: 1\n")
        self.assertNotEqual(before.key, self.key().key)

    def test_ignored_code_stays_in_the_key(self):
        before = self.key()
        write(self.project / "Assets" / "Gen.cs", "class Gen {}\n")
        after = self.key()
        self.assertIn("<project>/Assets/Gen.cs", after.components["files"])
        self.assertNotEqual(before.key, after.key)

    def test_without_git_nothing_is_left_out(self):
        write(self.project / "Assets" / "link.xml", "<linker/>\n")
        with mock.patch.object(unitycache.subprocess, "run", side_effect=OSError("no git")):
            self.assertIn("<project>/Assets/link.xml", self.key().components["files"])

    def test_a_folder_outside_git_ignores_nothing(self):
        found = unitycache.git_ignored(self.base / "editor")  # the temporary folder is not a git working copy
        self.assertEqual((frozenset(), ()), (found.files, found.folders))


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
        results = self.results
        write(results, unitycache_passing())
        plan = self.plan()
        settings = self.project / "ProjectSettings" / "ProjectSettings.asset"
        settings.write_text("changed: 1\n", encoding="utf-8")
        line = unityrun.keep_result(plan, self.root(), True)
        self.assertIn("not stored", line)
        self.assertIn("first run Valtrion saw", line)
        plan = self.plan()
        settings.write_text("changed: 2\n", encoding="utf-8")
        line = unityrun.keep_result(plan, self.root(), True)
        self.assertIn("not stored", line)
        self.assertNotIn("first run Valtrion saw", line)

    def test_a_stored_run_records_the_library(self):
        (self.project / "Library").mkdir()
        write(self.results, unitycache_passing())
        line = unityrun.keep_result(self.plan(), self.root(), True)
        self.assertIn("stored PASS", line)
        self.assertFalse(unitycache.first_with_library(self.root(), self.project))
        self.assertTrue(os.path.isdir(self.root() / unitycache.LIBRARIES))


def unitycache_passing() -> str:
    from .test_unity_cache import PASSING
    return PASSING
