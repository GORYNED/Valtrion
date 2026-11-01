"""The commit and the dirty flag a result carries (`valtrion.gitstate`), against real temporary repositories and injected runners."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import compare, gitstate

GIT = shutil.which("git")
SHA = "a" * 40


def git(folder: Path, *arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    done = subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                           *arguments], capture_output=True, text=True, check=True, env=environment)
    return done.stdout.strip()


def write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


@unittest.skipUnless(GIT, "git is not installed")
class RealRepositoryTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.repo = Path(self._folder.name).resolve() / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        self.project = self.repo / "app"
        write(self.project / "ProjectSettings" / "ProjectVersion.txt", "m_EditorVersion: 6000.0.1f1\n")
        write(self.repo / "docs" / "notes.txt")
        write(self.repo / ".gitignore", "Library/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "first")
        self.head = git(self.repo, "rev-parse", "HEAD")

    def state(self, exclude=()):
        return gitstate.read_state(self.project, compare.run_git, exclude)

    def test_a_clean_project_in_a_subfolder_has_the_repository_head_and_is_not_dirty(self):
        state = self.state()
        self.assertEqual((self.head, False, True, []), (state.sha, state.dirty, state.known, state.notes))

    def test_changes_outside_the_project_folder_do_not_make_it_dirty(self):
        write(self.repo / "docs" / "notes.txt", "edited\n")
        write(self.repo / "docs" / "new.txt")
        state = self.state()
        self.assertEqual((self.head, False), (state.sha, state.dirty))

    def test_a_tracked_edit_inside_the_project_makes_it_dirty_and_says_which_path(self):
        write(self.project / "ProjectSettings" / "ProjectVersion.txt", "m_EditorVersion: 6000.0.2f1\n")
        state = self.state()
        self.assertTrue(state.dirty)
        self.assertTrue(state.known)
        self.assertEqual(self.head, state.sha)
        self.assertTrue(any("ProjectVersion.txt" in note for note in state.notes), state.notes)

    def test_a_new_source_file_inside_the_project_makes_it_dirty(self):
        write(self.project / "Assets" / "New.cs")
        self.assertTrue(self.state().dirty)

    def test_ignored_files_do_not_make_it_dirty(self):
        write(self.project / "Library" / "cache.bin")
        self.assertFalse(self.state().dirty)

    def test_the_folders_of_the_run_itself_are_not_dirt_even_inside_the_project(self):
        run_folder = self.project / ".valtrion-artifacts" / "App" / "aaaaaaaa" / "job-1"
        write(run_folder / "job.json", "{}")
        write(self.project / ".work" / "jobs" / "job-1" / "build" / "app.bin")
        self.assertTrue(self.state().dirty, "without the exclusion the run's own files look like untracked sources")
        state = self.state([self.project / ".valtrion-artifacts", self.project / ".work"])
        self.assertEqual((self.head, False), (state.sha, state.dirty))

    def test_an_excluded_folder_hides_only_its_own_files(self):
        write(self.project / ".work" / "x.bin")
        write(self.project / "Assets" / "New.cs")
        self.assertTrue(self.state([self.project / ".work"]).dirty)

    def test_a_tracked_file_under_an_excluded_folder_is_still_dirt(self):
        write(self.project / "Assets" / "Kept.cs", "a\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "assets")
        write(self.project / "Assets" / "Kept.cs", "edited\n")
        self.assertTrue(self.state([self.project / "Assets"]).dirty)

    def test_a_root_that_is_a_source_folder_or_an_ancestor_does_not_hide_tracked_edits(self):
        write(self.project / "ProjectSettings" / "ProjectVersion.txt", "changed\n")
        for root in (self.project / "ProjectSettings", self.project):
            self.assertTrue(self.state([root]).dirty, root)
        self.assertTrue(gitstate.read_state(self.project, compare.run_git, [self.repo]).dirty)

    def test_relative_excludes_are_taken_against_the_project_folder(self):
        write(self.project / ".work" / "x.bin")
        other = Path(self._folder.name).resolve() / "cwd"
        other.mkdir()
        previous = Path.cwd()
        os.chdir(other)
        self.addCleanup(os.chdir, previous)
        self.assertFalse(self.state([Path(".work")]).dirty)

    def test_a_symlinked_exclude_root_is_recognised(self):
        real = Path(self._folder.name).resolve() / "real-out"
        write(real / "x.bin")
        link = self.project / "out"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        self.assertTrue(self.state().dirty, "the link itself is an untracked path")
        self.assertFalse(self.state([link]).dirty)

    def test_a_staged_only_change_is_dirt(self):
        write(self.project / "ProjectSettings" / "ProjectVersion.txt", "staged\n")
        git(self.repo, "add", "-A")
        self.assertTrue(self.state().dirty)

    def test_a_deleted_tracked_file_is_dirt(self):
        (self.project / "ProjectSettings" / "ProjectVersion.txt").unlink()
        self.assertTrue(self.state().dirty)

    def test_a_project_at_the_repository_root(self):
        state = gitstate.read_state(self.repo, compare.run_git, [self.repo / ".work"])
        self.assertEqual((self.head, False), (state.sha, state.dirty))
        write(self.repo / ".work" / "x.bin")
        write(self.repo / "docs" / "notes.txt", "edited\n")
        state = gitstate.read_state(self.repo, compare.run_git, [self.repo / ".work"])
        self.assertTrue(state.dirty)
        self.assertEqual(["docs/notes.txt"], state.changed)

    def test_a_detached_head_with_an_edit_is_dirty_and_keeps_the_commit(self):
        git(self.repo, "checkout", "--quiet", "--detach")
        write(self.project / "ProjectSettings" / "ProjectVersion.txt", "changed\n")
        state = self.state()
        self.assertEqual((self.head, True), (state.sha, state.dirty))

    def test_a_moved_submodule_pointer_is_dirt_but_its_untracked_files_are_not(self):
        sub = Path(self._folder.name).resolve() / "sub"
        sub.mkdir()
        git(sub, "init", "--quiet")
        write(sub / "f.txt")
        git(sub, "add", "-A")
        git(sub, "commit", "--quiet", "-m", "s1")
        try:
            git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(sub), "app/vendor")
        except subprocess.CalledProcessError:
            self.skipTest("submodules are not available")
        git(self.repo, "commit", "--quiet", "-m", "add submodule")
        write(self.project / "vendor" / "untracked.txt")
        self.assertFalse(self.state().dirty)
        write(self.project / "vendor" / "f2.txt")
        git(self.project / "vendor", "add", "-A")
        git(self.project / "vendor", "commit", "--quiet", "-m", "s2")
        self.assertTrue(self.state().dirty)

    def test_an_excluded_folder_outside_the_project_is_ignored(self):
        state = self.state([Path(self._folder.name).resolve() / "elsewhere"])
        self.assertEqual((self.head, False, True), (state.sha, state.dirty, state.known))

    def test_a_detached_head_is_still_the_commit(self):
        git(self.repo, "checkout", "--quiet", "--detach")
        state = self.state()
        self.assertEqual((self.head, False), (state.sha, state.dirty))

    def test_a_linked_worktree_reads_its_own_head_not_the_main_checkouts(self):
        write(self.repo / "docs" / "second.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "second")
        linked = Path(self._folder.name).resolve() / "linked"
        git(self.repo, "worktree", "add", "--quiet", "--detach", str(linked), self.head)
        state = gitstate.read_state(linked / "app", compare.run_git)
        self.assertEqual((self.head, False), (state.sha, state.dirty))
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD"), state.sha)

    def test_an_inherited_git_environment_does_not_redirect_the_read_to_another_repository(self):
        other = Path(self._folder.name).resolve() / "other"
        other.mkdir()
        git(other, "init", "--quiet")
        write(other / "f.txt")
        git(other, "add", "-A")
        git(other, "commit", "--quiet", "-m", "other")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            state = self.state()
        self.assertEqual((self.head, False), (state.sha, state.dirty))

    def test_a_folder_that_is_not_a_repository_has_no_sha_and_says_why(self):
        loose = Path(self._folder.name).resolve() / "loose"
        loose.mkdir()
        state = gitstate.read_state(loose, compare.run_git)
        self.assertEqual((None, True, False), (state.sha, state.dirty, state.known))
        self.assertTrue(any("could not be read" in note and "not a git repository" in note for note in state.notes), state.notes)

    def test_run_git_keeps_the_leading_space_of_the_first_porcelain_entry(self):
        write(self.project / "ProjectSettings" / "ProjectVersion.txt", "changed\n")
        code, output = compare.run_git(self.project, ["status", "--porcelain"])
        self.assertEqual(0, code)
        self.assertTrue(output.startswith(" M "), repr(output))

    def test_run_git_returns_the_error_text_of_a_failed_command(self):
        code, output = compare.run_git(self.repo, ["checkout", "no-such-ref"])
        self.assertNotEqual(0, code)
        self.assertIn("no-such-ref", output)


class InjectedRunnerTests(unittest.TestCase):
    def runner(self, status, head):
        def run(folder, arguments):
            return status if arguments[0] == "status" else head
        return run

    def test_a_failed_status_is_an_unknown_state_with_a_note_not_a_silent_dirty(self):
        state = gitstate.read_state(Path("."), self.runner((128, "fatal: Unable to create '.git/index.lock': File exists."), (0, SHA)))
        self.assertEqual((SHA, True, False), (state.sha, state.dirty, state.known))
        self.assertEqual(1, len(state.notes))
        self.assertIn("index.lock", state.notes[0])
        self.assertIn("cannot become a baseline", state.notes[0])

    def test_a_timeout_or_missing_git_is_reported_the_same_way(self):
        state = gitstate.read_state(Path("."), self.runner((127, "[WinError 2] cannot find git"), (127, "[WinError 2] cannot find git")))
        self.assertEqual((None, True, False), (state.sha, state.dirty, state.known))
        self.assertEqual(2, len(state.notes))

    def test_a_head_that_is_not_a_commit_hash_is_not_a_sha(self):
        state = gitstate.read_state(Path("."), self.runner((0, ""), (0, "HEAD")))
        self.assertEqual((None, True), (state.sha, state.dirty))
        self.assertTrue(any("could not be read" in note for note in state.notes), state.notes)

    def test_status_entries_are_counted_with_renames_as_one(self):
        output = " M a.cs\0R  new.cs\0old.cs\0?? b.cs\0"
        state = gitstate.read_state(Path("."), self.runner((0, output), (0, SHA)))
        self.assertTrue(state.dirty)
        self.assertEqual(["a.cs", "new.cs", "b.cs"], state.changed)

    def test_many_changes_are_summarised(self):
        output = "".join(f" M f{n}.cs\0" for n in range(30))
        state = gitstate.read_state(Path("."), self.runner((0, output), (0, SHA)))
        self.assertEqual(30, len(state.changed))
        self.assertIn("and 25 more", state.notes[0])


class GitEnvironmentTests(unittest.TestCase):
    def test_the_work_root_git_runner_also_drops_the_inherited_repository_variables(self):
        from valtrion import workroot
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.dict(os.environ, {"GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/elsewhere"}):
            with mock.patch("subprocess.run", fake_run):
                workroot.run_git(["status"])
        self.assertNotIn("GIT_DIR", seen["env"])
        self.assertNotIn("GIT_WORK_TREE", seen["env"])
        self.assertEqual("0", seen["env"]["GIT_TERMINAL_PROMPT"])

    def test_git_is_started_without_the_inherited_repository_variables_and_never_takes_optional_locks(self):
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.dict(os.environ, {"GIT_DIR": "/elsewhere/.git", "GIT_INDEX_FILE": "/elsewhere/index", "GIT_WORK_TREE": "/elsewhere"}):
            with mock.patch("subprocess.run", fake_run):
                compare.run_git(Path("."), ["status"])
        environment = seen["env"]
        for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
            self.assertNotIn(name, environment)
        self.assertEqual("0", environment["GIT_OPTIONAL_LOCKS"])
        self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])


if __name__ == "__main__":
    unittest.main()
