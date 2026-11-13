"""Delivering a working copy to another host without pushing it (`codesync.py`), on real temporary git repositories."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from valtrion import codesync, compare

HAVE_GIT = shutil.which("git") is not None
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(folder, *arguments):
    done = subprocess.run(["git", *arguments], cwd=folder, capture_output=True, text=True, check=True, env={**os.environ, **IDENTITY})
    return done.stdout.strip()


def write(folder, name, text):
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def commit(folder, name, text):
    write(folder, name, text)
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", f"{name} {text}")
    return git(folder, "rev-parse", "HEAD")


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class CodeSyncTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.origin = self.base / "origin.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.work = self.base / "work"
        git(self.base, "clone", "-q", str(self.origin), str(self.work))
        git(self.work, "config", "core.autocrlf", "false")
        git(self.work, "checkout", "-q", "-b", "main")
        write(self.work, ".gitignore", "Library/\n")
        commit(self.work, "Assets/data.txt", "one\n")
        git(self.work, "push", "-q", "-u", "origin", "main")
        self.staging = self.base / "staging"

    def snap(self, project=None, **options):
        return codesync.snapshot(project or self.work, compare.run_git, self.staging, **options)

    def receiver(self, name="host"):
        folder = self.base / name
        git(self.base, "clone", "-q", "--no-checkout", str(self.origin), str(folder))
        return folder

    def refs(self, folder):
        return git(folder, "for-each-ref", "refs/valtrion").splitlines()

    def test_a_clean_pushed_repository_needs_no_bundle(self):
        result = self.snap()
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), result.sha)
        self.assertEqual((result.base_sha, False, 0, None, 0), (result.sha, result.dirty, result.unpushed, result.bundle, result.changed))
        self.assertEqual([], self.refs(self.work))

    def test_an_unpushed_commit_travels_in_a_bundle(self):
        head = commit(self.work, "Assets/new.txt", "local\n")
        result = self.snap()
        self.assertEqual((head, head, False, 1), (result.base_sha, result.sha, result.dirty, result.unpushed))
        self.assertTrue(result.bundle and result.bundle.is_file())
        host = self.receiver()
        self.assertIsNone(codesync.import_bundle(host, result.bundle, result.sha, compare.run_git))
        self.assertEqual("commit", git(host, "cat-file", "-t", head))
        self.assertEqual([f"{head} commit\t{codesync.REF_PREFIX}{head[:8]}"], self.refs(host))
        self.assertEqual("main", git(host, "symbolic-ref", "--short", "HEAD"))  # the working tree and branches are not touched

    def test_uncommitted_work_becomes_a_snapshot_commit_and_the_sender_is_unchanged(self):
        write(self.work, "Assets/data.txt", "edited\n")
        write(self.work, "Assets/untracked.txt", "fresh\n")
        write(self.work, "Library/cache.bin", "ignored\n")
        before = (git(self.work, "status", "--porcelain=v1", "-uall"), git(self.work, "rev-parse", "HEAD"),
                  git(self.work, "branch", "-a"), git(self.work, "stash", "list"), (self.work / ".git" / "index").read_bytes())
        result = self.snap()
        after = (git(self.work, "status", "--porcelain=v1", "-uall"), git(self.work, "rev-parse", "HEAD"),
                 git(self.work, "branch", "-a"), git(self.work, "stash", "list"), (self.work / ".git" / "index").read_bytes())
        self.assertEqual(before, after)
        self.assertEqual([], self.refs(self.work))
        self.assertTrue(result.dirty)
        self.assertNotEqual(result.base_sha, result.sha)
        self.assertEqual(2, result.changed)
        self.assertEqual(result.base_sha, git(self.work, "rev-parse", result.sha + "^"))
        listed = git(self.work, "ls-tree", "-r", "--name-only", result.sha).splitlines()
        self.assertIn("Assets/untracked.txt", listed)
        self.assertNotIn("Library/cache.bin", listed)
        self.assertEqual("edited", git(self.work, "show", f"{result.sha}:Assets/data.txt"))
        self.assertEqual([], [name for name in os.listdir(self.staging) if name.startswith("index-")])
        host = self.receiver()
        self.assertIsNone(codesync.import_bundle(host, result.bundle, result.sha, compare.run_git))
        self.assertEqual("commit", git(host, "cat-file", "-t", result.sha))

    def test_the_same_content_gives_the_same_commit(self):
        write(self.work, "Assets/data.txt", "edited\n")
        self.assertEqual(self.snap().sha, self.snap().sha)

    def test_a_deleted_file_is_in_the_snapshot_as_a_deletion(self):
        (self.work / "Assets" / "data.txt").unlink()
        result = self.snap()
        self.assertTrue(result.dirty)
        self.assertNotIn("Assets/data.txt", git(self.work, "ls-tree", "-r", "--name-only", result.sha).splitlines())

    def test_an_lfs_path_is_refused_without_git_lfs(self):
        commit(self.work, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        git(self.work, "push", "-q")
        write(self.work, "Assets/x.bin", "pointer-less\n")
        with self.assertRaises(codesync.SnapshotError) as caught:
            self.snap()
        self.assertIn("Assets/x.bin", str(caught.exception))
        self.assertIn("LFS", str(caught.exception))
        self.assertEqual([], self.refs(self.work))

    def test_a_changed_submodule_is_refused(self):
        other = self.base / "other"
        git(self.base, "init", "-q", "-b", "main", str(other))
        commit(other, "a.txt", "a\n")
        git(self.work, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(other), "Packages/sub")
        git(self.work, "commit", "-q", "-m", "submodule")
        git(self.work, "push", "-q")
        commit(other, "a.txt", "b\n")
        git(self.work / "Packages" / "sub", "-c", "protocol.file.allow=always", "pull", "-q")
        with self.assertRaises(codesync.SnapshotError) as caught:
            self.snap()
        self.assertIn("submodule", str(caught.exception))
        self.assertIn("Packages/sub", str(caught.exception))

    def test_a_bundle_over_the_limit_is_refused(self):
        write(self.work, "Assets/big.txt", "x" * 20000)
        with self.assertRaises(codesync.SnapshotError) as caught:
            self.snap(max_bytes=100)
        self.assertIn("too large to send", str(caught.exception))
        self.assertEqual([], self.refs(self.work))

    def test_a_folder_that_is_not_a_repository_is_refused(self):
        with self.assertRaises(codesync.SnapshotError):
            codesync.snapshot(self.base / "staging-none", compare.run_git, self.staging)

    def test_a_project_in_a_subfolder_snapshots_the_whole_repository(self):
        project = self.work / "Unity"
        write(project, "ProjectSettings/ProjectVersion.txt", "v\n")
        write(self.work, "tools/other.txt", "outside\n")
        result = self.snap(project)
        self.assertTrue(result.dirty)
        listed = git(self.work, "ls-tree", "-r", "--name-only", result.sha).splitlines()
        self.assertIn("Unity/ProjectSettings/ProjectVersion.txt", listed)
        self.assertIn("tools/other.txt", listed)
        self.assertEqual(2, result.changed)

    def test_import_names_the_commits_the_host_lacks(self):
        git(self.work, "checkout", "-q", "-b", "side")
        commit(self.work, "Assets/side.txt", "side\n")
        write(self.work, "Assets/more.txt", "more\n")
        result = self.snap()
        stranger = self.base / "stranger"
        git(self.base, "init", "-q", "-b", "main", str(stranger))
        commit(stranger, "z.txt", "z\n")
        reason = codesync.import_bundle(stranger, result.bundle, result.sha, compare.run_git)
        self.assertIsNotNone(reason)
        self.assertIn("needs commits", reason)
        self.assertNotIn("\n", reason)

    def test_import_without_a_folder_or_a_remote_gives_a_reason_and_with_a_remote_clones(self):
        head = commit(self.work, "Assets/new.txt", "local\n")
        result = self.snap()
        host = self.base / "fresh"
        self.assertIn("no remote", codesync.import_bundle(host, result.bundle, head, compare.run_git))
        self.assertIsNone(codesync.import_bundle(host, result.bundle, head, compare.run_git, remote=str(self.origin)))
        self.assertEqual("commit", git(host, "cat-file", "-t", head))

    def test_import_keeps_only_the_newest_refs(self):
        host = self.receiver()
        shas = []
        for number in range(4):
            shas.append(commit(self.work, f"Assets/n{number}.txt", f"{number}\n"))
            result = self.snap()
            self.assertIsNone(codesync.import_bundle(host, result.bundle, result.sha, compare.run_git, keep=2))
        refs = [line.split("\t")[1] for line in self.refs(host)]
        self.assertEqual(2, len(refs))
        self.assertIn(f"{codesync.REF_PREFIX}{shas[3][:8]}", refs)


if __name__ == "__main__":
    unittest.main()
