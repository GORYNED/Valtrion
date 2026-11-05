"""Git LFS preflight of `valtrion unity run` (`lfs.py`): pointer files in a real git repository are materialized from the shared cache,
the repository's LFS store or a fake `git lfs fetch`. No network is used: `git lfs` is replaced by `FakeLfs`, git's clean filter by
`fake_lfs_clean.py`, and the global and system git configuration are switched off."""

import _thread
import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, config, constants, lease, lfs  # noqa: F401  (cli first: it registers the commands)

from .test_unity_run import FAKE, FakeProbe, UnityRunTestCase

HAVE_GIT = shutil.which("git") is not None
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
CLEAN = Path(__file__).with_name("fake_lfs_clean.py")
ATTRIBUTES = "*.bin filter=lfs diff=lfs merge=lfs -text\n"


def git(folder, *arguments):
    done = subprocess.run(["git", *arguments], cwd=folder, capture_output=True, text=True, check=True, env={**os.environ, **IDENTITY})
    return done.stdout.strip()


def pointer(data: bytes) -> bytes:
    return (f"version https://git-lfs.github.com/spec/v1\noid sha256:{hashlib.sha256(data).hexdigest()}\nsize {len(data)}\n"
            .encode("ascii"))


def oid(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_filter(folder, wrong=False):
    command = f'"{Path(sys.executable).as_posix()}" "{CLEAN.as_posix()}"' + (" --wrong" if wrong else "")
    git(folder, "config", "filter.lfs.clean", command)
    git(folder, "config", "filter.lfs.required", "true")


def make_repo(folder, assets, attributes=ATTRIBUTES, filter_set=True):
    """A repository with a Unity project in `Game/`, whose LFS files are committed as pointers (as a clone without smudge has them)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q", "-b", "main")
    git(folder, "config", "core.autocrlf", "false")
    if filter_set:
        clean_filter(folder)
    if attributes:
        (folder / ".gitattributes").write_text(attributes, encoding="utf-8")
    (folder / "Game" / "ProjectSettings").mkdir(parents=True)
    (folder / "Game" / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
    for name, data in assets.items():
        path = folder / "Game" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pointer(data) if attributes else data)
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "assets")
    return folder / "Game"


class FakeLfs:
    """`git lfs` without a network: `version`, and `fetch --include` that writes the objects of `remote` into the repository's store."""

    def __init__(self, remote=None, missing_tool=False, fail_text=None, corrupt=False, on_fetch=None):
        self.remote = dict(remote or {})
        self.on_fetch = on_fetch
        self.missing_tool = missing_tool
        self.fail_text = fail_text
        self.corrupt = corrupt
        self.calls = []
        self.fetched = []

    @property
    def fetch_calls(self):
        return [call for call in self.calls if call[:1] == ["fetch"]]

    def __call__(self, cwd, argv):
        self.calls.append(list(argv))
        if self.missing_tool:
            return 1, "git: 'lfs' is not a git command. See 'git --help'."
        if argv[0] == "version":
            return 0, "git-lfs/3.5.1 (fake)"
        if argv[0] != "fetch":
            return 1, f"unexpected git lfs {argv}"
        if self.on_fetch is not None:
            self.on_fetch()
        if self.fail_text:
            return 2, self.fail_text
        patterns = argv[argv.index("--include") + 1].split(",")
        common = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        problems = []
        for name in git(cwd, "ls-files").splitlines():
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
                continue
            parsed = lfs.parse_pointer((Path(cwd) / name).read_bytes())
            if parsed is None:
                continue
            data = self.remote.get(parsed[0])
            if data is None:
                problems.append(f"[{parsed[0]}] Object does not exist on the server: [404] Object does not exist on the server")
                continue
            target = lfs.object_path(common / "lfs" / "objects", parsed[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data[::-1] if self.corrupt else data)
            self.fetched.append(parsed[0])
        return (2, "\n".join(problems)) if problems else (0, "")


class CountingGit:
    def __init__(self):
        self.calls = []

    def __call__(self, cwd, argv, stdin=None):
        self.calls.append(list(argv))
        return lfs.run_git(cwd, argv, stdin)


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class LfsTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        empty = self.base / "gitconfig"
        empty.write_text("", encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.cache = self.base / "work" / "lfs-cache"
        self.texture = b"texture bytes " * 200
        self.model = b"model bytes " * 300

    def seed_cache(self, data, content=None):
        path = lfs.object_path(self.cache / "objects", oid(data))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if content is None else content)
        return path

    def run_lfs(self, project, fake, **options):
        return lfs.materialize(project, self.cache, lfs=fake, **options)

    def assertClean(self, folder):
        self.assertEqual("", git(folder, "status", "--porcelain", "--untracked-files=all"))


class DetectionTests(LfsTestCase):
    def test_pointers_of_lfs_files_are_found_and_other_files_are_left_alone(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture, "Assets/b.bin": self.model})
        (project / "Assets" / "note.txt").write_bytes(pointer(b"not lfs"))  # pointer text in a file without the attribute
        git(project.parent, "add", "-A")
        git(project.parent, "commit", "-q", "-m", "note")
        self.seed_cache(self.texture)
        self.seed_cache(self.model)
        outcome = self.run_lfs(project, FakeLfs())
        self.assertEqual((lfs.OK, 2), (outcome.code, outcome.pointers), outcome.reason)
        self.assertEqual(sorted(["Game/Assets/a.bin", "Game/Assets/b.bin"]), sorted(outcome.materialized))
        self.assertEqual(pointer(b"not lfs"), (project / "Assets" / "note.txt").read_bytes())

    def test_parse_pointer(self):
        self.assertEqual((oid(b"x"), 1, False), lfs.parse_pointer(pointer(b"x")))
        self.assertIsNone(lfs.parse_pointer(b"version https://git-lfs.github.com/spec/v1\nsize 3\n"))
        self.assertIsNone(lfs.parse_pointer(pointer(b"x") + b"junk\n"))
        self.assertIsNone(lfs.parse_pointer(b"plain text"))
        self.assertTrue(lfs.parse_pointer(pointer(b"x").replace(b"oid", b"ext-0-foo sha256:" + b"0" * 64 + b"\noid"))[2])

    def test_include_pattern_neutralises_pattern_characters(self):
        self.assertEqual("Game/a?b?c?.bin", lfs.include_pattern("Game/a,b*c[.bin"))


class CacheTests(LfsTestCase):
    def test_everything_cached_means_no_git_lfs_call_at_all(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        self.seed_cache(self.texture)
        fake = FakeLfs()
        outcome = self.run_lfs(project, fake)
        self.assertEqual(lfs.OK, outcome.code, outcome.reason)
        self.assertEqual([], fake.calls, "no fetch, no network: git lfs is never called")
        self.assertEqual((1, 0, 0, 0), (outcome.from_cache, outcome.from_repository, outcome.fetched, outcome.fetch_calls))
        self.assertEqual(self.texture, (project / "Assets" / "a.bin").read_bytes())
        self.assertClean(project.parent)
        # the object is also linked into the repository's store, so git-lfs's clean filter has nothing to copy
        self.assertTrue(lfs.object_path(project.parent / ".git" / "lfs" / "objects", oid(self.texture)).is_file())

    def test_only_missing_objects_are_fetched_and_then_cached_for_every_checkout(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture, "Assets/b.bin": self.model})
        self.seed_cache(self.texture)
        fake = FakeLfs({oid(self.texture): self.texture, oid(self.model): self.model})
        outcome = self.run_lfs(project, fake)
        self.assertEqual(lfs.OK, outcome.code, outcome.reason)
        self.assertEqual([oid(self.model)], fake.fetched)
        self.assertEqual(1, len(fake.fetch_calls))
        self.assertEqual("Game/Assets/b.bin", fake.fetch_calls[0][fake.fetch_calls[0].index("--include") + 1])
        self.assertEqual((1, 1), (outcome.from_cache, outcome.fetched))
        self.assertTrue(lfs.object_path(self.cache / "objects", oid(self.model)).is_file())
        self.assertClean(project.parent)
        # a second clone (another checkout of the host) needs no network now
        other = self.base / "other"
        git(self.base, "clone", "-q", str(project.parent), str(other))
        clean_filter(other)
        second = FakeLfs()
        outcome = self.run_lfs(other / "Game", second)
        self.assertEqual((lfs.OK, 2, []), (outcome.code, outcome.from_cache, second.calls), outcome.reason)
        self.assertEqual(self.model, (other / "Game" / "Assets" / "b.bin").read_bytes())

    def test_a_second_run_finds_nothing_to_do(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        self.seed_cache(self.texture)
        self.run_lfs(project, FakeLfs())
        outcome = self.run_lfs(project, FakeLfs())
        self.assertEqual((lfs.OK, 0), (outcome.code, outcome.pointers))

    def test_a_linked_worktree_uses_the_store_of_the_common_git_dir(self):
        repo = self.base / "repo"
        make_repo(repo, {"Assets/a.bin": self.texture})
        store = lfs.object_path(repo / ".git" / "lfs" / "objects", oid(self.texture))
        store.parent.mkdir(parents=True)
        store.write_bytes(self.texture)
        tree = self.base / "tree"
        # As a worktree made without the smudge filter (GIT_LFS_SKIP_SMUDGE, an agent's worktree): the pointers stay in the tree.
        git(repo, "-c", "filter.lfs.required=false", "worktree", "add", "-q", "--detach", str(tree))
        self.assertTrue((tree / ".git").is_file())
        fake = FakeLfs()
        outcome = self.run_lfs(tree / "Game", fake)
        self.assertEqual((lfs.OK, 1, []), (outcome.code, outcome.from_repository, fake.calls), outcome.reason)
        self.assertEqual(self.texture, (tree / "Game" / "Assets" / "a.bin").read_bytes())
        self.assertTrue(lfs.object_path(self.cache / "objects", oid(self.texture)).is_file(), "the store's object is now cached")
        self.assertClean(tree)


class FailureTests(LfsTestCase):
    def assertUntouched(self, project, name, data):
        self.assertEqual(pointer(data), (project / name).read_bytes())

    def test_a_damaged_cache_object_is_dropped_and_fetched_again(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        damaged = self.seed_cache(self.texture, self.texture[::-1])
        fake = FakeLfs({oid(self.texture): self.texture})
        outcome = self.run_lfs(project, fake)
        self.assertEqual((lfs.OK, 1), (outcome.code, outcome.fetched), outcome.reason)
        self.assertEqual(self.texture, damaged.read_bytes())
        self.assertEqual(self.texture, (project / "Assets" / "a.bin").read_bytes())

    def test_a_hash_mismatch_leaves_the_pointer_and_names_the_file(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        self.seed_cache(self.texture, self.texture[::-1])
        outcome = self.run_lfs(project, FakeLfs({oid(self.texture): self.texture}, corrupt=True))
        self.assertEqual(lfs.CODE_HASH_MISMATCH, outcome.code)
        self.assertEqual([("Game/Assets/a.bin", lfs.CODE_HASH_MISMATCH)], outcome.failed)
        self.assertIn("[lfs.hash-mismatch]", outcome.reason)
        self.assertUntouched(project, "Assets/a.bin", self.texture)
        self.assertClean(project.parent)

    def test_without_git_lfs_set_up_nothing_is_changed(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture}, filter_set=False)
        self.seed_cache(self.texture)
        outcome = self.run_lfs(project, FakeLfs())
        self.assertEqual(lfs.CODE_GIT_LFS_MISSING, outcome.code)
        self.assertIn("git lfs install", outcome.reason)
        self.assertUntouched(project, "Assets/a.bin", self.texture)

    def test_a_missing_git_lfs_program_cannot_fetch(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        fake = FakeLfs(missing_tool=True)
        outcome = self.run_lfs(project, fake)
        self.assertEqual(lfs.CODE_GIT_LFS_MISSING, outcome.code)
        self.assertEqual([], fake.fetch_calls)
        self.assertUntouched(project, "Assets/a.bin", self.texture)

    def test_offline_auth_and_missing_object_have_their_own_codes(self):
        cases = (("fatal: Could not resolve host: github.com", lfs.CODE_NETWORK),
                 ("batch response: Authentication required: Authorization error", lfs.CODE_AUTH),
                 ("something else broke", lfs.CODE_FETCH_FAILED))
        for index, (text, code) in enumerate(cases):
            project = make_repo(self.base / f"repo{index}", {"Assets/a.bin": self.texture})
            outcome = self.run_lfs(project, FakeLfs(fail_text=text))
            self.assertEqual(code, outcome.code, text)
            self.assertEqual([("Game/Assets/a.bin", code)], outcome.failed)
            self.assertIn(text, outcome.reason)
            self.assertUntouched(project, "Assets/a.bin", self.texture)
        project = make_repo(self.base / "missing", {"Assets/a.bin": self.texture})
        outcome = self.run_lfs(project, FakeLfs({}))
        self.assertEqual(lfs.CODE_OBJECT_MISSING, outcome.code)

    def test_too_little_disk_space_is_refused_before_anything_is_written(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        self.seed_cache(self.texture)
        fake = FakeLfs()
        outcome = self.run_lfs(project, fake, free=lambda path: 1024)
        self.assertEqual(lfs.CODE_DISK_SPACE, outcome.code)
        self.assertIn("MiB free", outcome.reason)
        self.assertEqual([], fake.calls)
        self.assertUntouched(project, "Assets/a.bin", self.texture)

    def test_a_clean_filter_that_disagrees_gets_the_pointer_back(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        clean_filter(project.parent, wrong=True)
        self.seed_cache(self.texture)
        outcome = self.run_lfs(project, FakeLfs())
        self.assertEqual(lfs.CODE_STATUS_DIRTY, outcome.code)
        self.assertEqual([], outcome.materialized)
        self.assertUntouched(project, "Assets/a.bin", self.texture)
        clean_filter(project.parent)
        self.assertClean(project.parent)


class NoLfsTests(LfsTestCase):
    def test_a_project_without_lfs_attributes_is_untouched_and_needs_no_git_lfs(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": b"plain"}, attributes=None, filter_set=False)
        counting, fake = CountingGit(), FakeLfs(missing_tool=True)
        outcome = lfs.materialize(project, self.cache, git=counting, lfs=fake)
        self.assertEqual((lfs.SKIPPED, 0), (outcome.code, outcome.pointers))
        self.assertTrue(outcome.ok)
        self.assertEqual([], fake.calls)
        self.assertLessEqual(len(counting.calls), 4, counting.calls)
        self.assertFalse(any(call[0] in ("ls-files", "check-attr") and "-s" in call for call in counting.calls))
        self.assertFalse(self.cache.exists())

    def test_a_folder_outside_git_is_skipped(self):
        folder = self.base / "plain"
        folder.mkdir()
        outcome = lfs.materialize(folder, self.cache, lfs=FakeLfs())
        self.assertEqual(lfs.SKIPPED, outcome.code)


class GitProblemTests(LfsTestCase):
    def test_a_git_too_old_for_the_answer_is_a_git_failure_not_a_skip(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})

        def old_git(cwd, argv, stdin=None):
            if argv[:1] == ["rev-parse"]:  # git before 2.13 echoes an option it does not know
                return 0, lfs.run_git(cwd, ["rev-parse", "--show-toplevel"], None)[1] + b"--absolute-git-dir\n.git\n", ""
            return lfs.run_git(cwd, argv, stdin)

        outcome = lfs.materialize(project, self.cache, git=old_git, lfs=FakeLfs())
        self.assertEqual(lfs.CODE_GIT_FAILED, outcome.code)
        self.assertFalse(outcome.ok)
        self.assertIn(lfs.MIN_GIT_VERSION, outcome.reason)

    def test_git_refusing_a_repository_is_a_git_failure_not_a_skip(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})

        def refusing(cwd, argv, stdin=None):
            if argv[:1] == ["rev-parse"]:
                return 128, b"", "fatal: detected dubious ownership in repository"
            return lfs.run_git(cwd, argv, stdin)

        outcome = lfs.materialize(project, self.cache, git=refusing, lfs=FakeLfs())
        self.assertEqual(lfs.CODE_GIT_FAILED, outcome.code)
        self.assertIn("dubious ownership", outcome.reason)
        self.assertEqual(pointer(self.texture), (project / "Assets" / "a.bin").read_bytes())

    def test_a_staged_change_made_meanwhile_is_kept(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture, "Assets/b.bin": self.model})
        self.seed_cache(self.texture)
        other = b"staged by someone " * 50

        def stage():
            (project / "Assets" / "a.bin").write_bytes(other)
            git(project.parent, "add", "Game/Assets/a.bin")

        outcome = self.run_lfs(project, FakeLfs({oid(self.model): self.model}, on_fetch=stage))
        self.assertEqual(lfs.CODE_CHANGED, outcome.code)
        self.assertEqual([("Game/Assets/a.bin", lfs.CODE_CHANGED)], outcome.failed)
        self.assertEqual(other, (project / "Assets" / "a.bin").read_bytes())
        self.assertEqual("M  Game/Assets/a.bin", git(project.parent, "status", "--porcelain"), "the staged change survives")


class InterruptTests(LfsTestCase):
    """An interrupt of the process (`unity run`'s lifeline or a console event arrive as one) is handled while a child still runs."""

    def interrupt_after(self, seconds):
        timer = threading.Timer(seconds, _thread.interrupt_main)
        timer.start()
        self.addCleanup(timer.cancel)

    def test_a_slow_fetch_is_stopped_at_once_by_an_interrupt(self):
        marker = self.base / "survived"
        slow = [sys.executable, "-c", "import sys, time; time.sleep(3); open(sys.argv[1], 'w').write('x')", str(marker)]
        started = time.monotonic()
        with mock.patch.object(lfs, "LFS_COMMAND", tuple(slow)):
            self.interrupt_after(0.5)
            with self.assertRaises(KeyboardInterrupt):
                lfs.run_lfs(self.base, ["fetch"])
        self.assertLess(time.monotonic() - started, 2.5, "the interrupt did not wait for the child")
        time.sleep(3.5)
        self.assertFalse(marker.exists(), "the fetch was stopped, not left running")

    def test_an_index_writer_is_finished_not_killed_by_an_interrupt(self):
        marker = self.base / "finished"
        slow = (sys.executable, "-c", f"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('x')")
        started = time.monotonic()
        with mock.patch.object(lfs, "GIT_COMMAND", slow):
            self.interrupt_after(0.3)
            with self.assertRaises(KeyboardInterrupt):
                lfs.run_git(self.base, ["update-index", "-q", "--refresh"])
        self.assertTrue(marker.exists(), "git update-index ran to its end before the interrupt was raised")
        self.assertGreaterEqual(time.monotonic() - started, 1.4)

    def test_an_interrupt_right_after_a_file_is_renamed_into_place_still_syncs_the_index(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        self.seed_cache(self.texture)
        real = os.replace
        # Compared resolved: git names the tree by its real path (on macOS /private/var/... for a /var/... temporary folder).
        target = os.path.normcase(os.path.realpath(project / "Assets" / "a.bin"))

        def replace(source, destination):
            real(source, destination)
            if os.path.normcase(os.path.realpath(destination)) == target:
                raise KeyboardInterrupt

        with mock.patch.object(lfs.os, "replace", replace), self.assertRaises(KeyboardInterrupt):
            self.run_lfs(project, FakeLfs())
        self.assertEqual(self.texture, (project / "Assets" / "a.bin").read_bytes())
        self.assertClean(project.parent)

    def test_a_lock_of_another_git_is_named_as_the_cause(self):
        project = make_repo(self.base / "repo", {"Assets/a.bin": self.texture})
        self.seed_cache(self.texture)
        lock = project.parent / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        try:
            outcome = self.run_lfs(project, FakeLfs())
        finally:
            lock.unlink()
        self.assertEqual(lfs.CODE_GIT_FAILED, outcome.code)
        self.assertIn("could not be materialized (Game/Assets/a.bin): git update-index --index-info failed", outcome.reason)
        self.assertEqual(pointer(self.texture), (project / "Assets" / "a.bin").read_bytes())
        self.assertClean(project.parent)


#: The PATH the preflight itself gives git (with the Homebrew folders on macOS, where a non-interactive shell lacks them), so git-lfs is
#: found for these tests exactly where `unity run` finds it.
LFS_PATH = lfs._environment()["PATH"]  # noqa: SLF001


def lfs_available():
    if not HAVE_GIT:
        return False
    done = subprocess.run(["git", "lfs", "version"], capture_output=True, text=True, env={**os.environ, "PATH": LFS_PATH})
    return done.returncode == 0


@unittest.skipUnless(lfs_available(), "git-lfs is not installed")
class RealGitLfsTests(unittest.TestCase):
    """The real git-lfs filter and transfer, without a network: a source repository pushes its LFS objects to a local bare repository
    (`file://`), and every test materializes a fresh copy of a clone made without the smudge filter."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls._folder.name)
        empty = cls.root / "gitconfig"
        empty.write_text("", encoding="utf-8")
        cls._patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "PATH": LFS_PATH, **IDENTITY})
        cls._patch.start()
        source = cls.root / "src"
        source.mkdir()
        git(source, "init", "-q", "-b", "main")
        git(source, "lfs", "install", "--local")
        (source / ".gitattributes").write_text(ATTRIBUTES, encoding="utf-8")
        (source / "P" / "Assets").mkdir(parents=True)
        cls.a, cls.b = os.urandom(5000), os.urandom(7000)
        (source / "P" / "Assets" / "a.bin").write_bytes(cls.a)
        (source / "P" / "Assets" / "b.bin").write_bytes(cls.b)
        git(source, "add", "-A")
        git(source, "commit", "-q", "-m", "assets")
        bare = cls.root / "bare.git"
        git(cls.root, "clone", "-q", "--bare", str(source), str(bare))
        git(source, "remote", "add", "origin", bare.as_uri())
        git(source, "lfs", "push", "--all", "origin")
        cls.clone = cls.root / "clone"
        git(cls.root, "clone", "-q", bare.as_uri(), str(cls.clone))  # no filter configured yet: the files stay pointers
        git(cls.clone, "lfs", "install", "--local")
        assert lfs.parse_pointer((cls.clone / "P" / "Assets" / "a.bin").read_bytes()) is not None

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        cls._folder.cleanup()

    def setUp(self):
        self.name = self.id().rsplit(".", 1)[-1]
        self.tree = self.root / self.name
        shutil.copytree(self.clone, self.tree, symlinks=True)
        self.cache = self.root / f"cache-{self.name}"
        self.calls = []

    def lfs(self, cwd, argv):
        self.calls.append(list(argv))
        return lfs.run_lfs(cwd, argv)

    def seed(self, data):
        target = lfs.object_path(self.cache / "objects", oid(data))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def status(self, folder=None):
        return git(folder or self.tree, "status", "--porcelain")

    def test_fetch_then_cache_with_a_clean_status(self):
        outcome = lfs.materialize(self.tree / "P", self.cache, lfs=self.lfs)
        self.assertEqual((lfs.OK, 2, 1), (outcome.code, outcome.fetched, outcome.fetch_calls), outcome.reason)
        self.assertEqual(self.a, (self.tree / "P" / "Assets" / "a.bin").read_bytes())
        self.assertEqual("", self.status())
        second = self.root / f"{self.name}-second"
        shutil.copytree(self.clone, second)
        self.calls.clear()
        outcome = lfs.materialize(second / "P", self.cache, lfs=self.lfs)
        self.assertEqual((lfs.OK, 2, []), (outcome.code, outcome.from_cache, self.calls), outcome.reason)
        self.assertEqual("", self.status(second))

    def test_a_custom_lfs_storage_is_where_the_fetched_objects_are_found(self):
        store = self.root / f"store-{self.name}"
        git(self.tree, "config", "lfs.storage", str(store))
        outcome = lfs.materialize(self.tree / "P", self.cache, lfs=self.lfs)
        self.assertEqual((lfs.OK, 2), (outcome.code, outcome.fetched), outcome.reason)
        self.assertTrue(lfs.object_path(store / "objects", oid(self.b)).is_file())
        self.assertEqual("", self.status())

    def test_an_interrupt_after_the_first_file_leaves_the_index_consistent(self):
        self.seed(self.a)

        def interrupted(cwd, argv):
            if argv[:1] == ["fetch"]:
                raise KeyboardInterrupt
            return lfs.run_lfs(cwd, argv)

        with self.assertRaises(KeyboardInterrupt):
            lfs.materialize(self.tree / "P", self.cache, lfs=interrupted)
        self.assertEqual(self.a, (self.tree / "P" / "Assets" / "a.bin").read_bytes())
        self.assertEqual("", self.status(), "the placed file is not left 'modified'")
        outcome = lfs.materialize(self.tree / "P", self.cache, lfs=self.lfs)
        self.assertEqual((lfs.OK, 1), (outcome.code, outcome.fetched), outcome.reason)
        self.assertEqual("", self.status())

    def test_skip_worktree_and_assume_unchanged_flags_are_kept(self):
        git(self.tree, "update-index", "--skip-worktree", "P/Assets/b.bin")
        git(self.tree, "update-index", "--assume-unchanged", "P/Assets/a.bin")
        outcome = lfs.materialize(self.tree / "P", self.cache, lfs=self.lfs)
        self.assertEqual(lfs.OK, outcome.code, outcome.reason)
        tags = dict(line.split(" ", 1)[::-1] for line in git(self.tree, "ls-files", "-v", "--", "P").splitlines())
        self.assertEqual({"P/Assets/a.bin": "h", "P/Assets/b.bin": "S"}, tags)
        self.assertEqual(self.b, (self.tree / "P" / "Assets" / "b.bin").read_bytes())

    def test_an_edit_made_during_the_fetch_is_never_overwritten(self):
        self.seed(self.a)
        edited = b"USER EDIT " * 500

        def edit(cwd, argv):
            if argv[:1] == ["fetch"]:
                (self.tree / "P" / "Assets" / "a.bin").write_bytes(edited)
            return lfs.run_lfs(cwd, argv)

        outcome = lfs.materialize(self.tree / "P", self.cache, lfs=edit)
        self.assertEqual(lfs.CODE_CHANGED, outcome.code, outcome.reason)
        self.assertIn(("P/Assets/a.bin", lfs.CODE_CHANGED), outcome.failed)
        self.assertEqual(edited, (self.tree / "P" / "Assets" / "a.bin").read_bytes())
        self.assertEqual(self.b, (self.tree / "P" / "Assets" / "b.bin").read_bytes())

    def test_a_linked_parent_folder_that_leads_outside_is_not_written_through(self):
        outside = self.root / f"outside-{self.name}"
        shutil.move(str(self.tree / "P" / "Assets"), str(outside))
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(outside), str(self.tree / "P" / "Assets"))
            else:
                os.symlink(outside, self.tree / "P" / "Assets", target_is_directory=True)
        except OSError as error:
            self.skipTest(f"cannot create a directory link here: {error}")
        outcome = lfs.materialize(self.tree / "P", self.cache, lfs=self.lfs)
        self.assertEqual(lfs.CODE_OUTSIDE, outcome.code, outcome.reason)
        self.assertEqual([], self.calls)
        self.assertIsNotNone(lfs.parse_pointer((outside / "a.bin").read_bytes()), "the file outside the tree is untouched")


class SettingTests(unittest.TestCase):
    def test_lfs_materialize_of_host_json(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.assertTrue(config.lfs_materialize(home))
            for value, expected in (({"materialize": False}, False), ({}, True), ({"materialize": True}, True)):
                (home / "host.json").write_text(json.dumps({"lfs": value}), encoding="utf-8")
                self.assertEqual(expected, config.lfs_materialize(home))
            for value in ({"materialize": "no"}, {"other": 1}, []):
                (home / "host.json").write_text(json.dumps({"lfs": value}), encoding="utf-8")
                with self.assertRaises(config.ConfigError):
                    config.lfs_materialize(home)


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class UnityRunLfsTests(UnityRunTestCase):
    def setUp(self):
        super().setUp()
        empty = self.base / "gitconfig"
        empty.write_text("", encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.data = b"scene bytes " * 100
        self.project = make_repo(self.base / "repo", {"Assets/scene.bin": self.data})
        cached = lfs.object_path(lfs.cache_root(self.home / "work") / "objects", oid(self.data))
        cached.parent.mkdir(parents=True)
        cached.write_bytes(self.data)

    def preflight(self, lines):
        found = [line for line in lines if line.startswith("LFS-PREFLIGHT ")]
        return json.loads(found[0][len("LFS-PREFLIGHT "):]) if found else None

    def test_pointers_are_materialized_before_unity_starts(self):
        fake = FakeLfs()
        code, lines, err = self.unity(lfs_call=fake)
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS"), (code, lines[-1]), err)
        self.assertEqual(self.data, (self.project / "Assets" / "scene.bin").read_bytes())
        self.assertEqual({"code": "ok", "pointers": 1, "materialized": 1, "fromCache": 1, "fromRepository": 0, "fetched": 0,
                          "fetchCalls": 0}, self.preflight(lines))
        self.assertEqual([], fake.calls)
        self.assertNoLeases()

    def test_a_failure_is_blocked_with_its_code_and_unity_does_not_start(self):
        (lfs.object_path(lfs.cache_root(self.home / "work") / "objects", oid(self.data))).unlink()
        code, lines, err = self.unity("--text", "unity ran", lfs_call=FakeLfs(fail_text="Could not resolve host: github.com"))
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
        record = self.preflight(lines)
        self.assertEqual("lfs.network-failed", record["code"])
        self.assertEqual([{"path": "Game/Assets/scene.bin", "code": "lfs.network-failed"}], record["failed"])
        self.assertIn("[lfs.network-failed]", lines[-2])
        self.assertFalse((self.logs / "Editor.log").exists(), "Unity was not started")
        self.assertNoLeases()

    def test_host_json_and_the_option_turn_it_off(self):
        (self.home / "host.json").write_text(json.dumps({"lfs": {"materialize": False}}), encoding="utf-8")
        code, lines, _ = self.unity(lfs_call=FakeLfs())
        self.assertEqual("STATUS PASS", lines[-1])
        self.assertIsNone(self.preflight(lines))
        self.assertEqual(pointer(self.data), (self.project / "Assets" / "scene.bin").read_bytes())
        code, lines, _ = self.unity(lfs_call=FakeLfs(), lfs_materialize=True)
        self.assertEqual(self.data, (self.project / "Assets" / "scene.bin").read_bytes())

    def test_the_preflight_time_does_not_use_up_the_wait_for_a_slot(self):
        (lfs.object_path(lfs.cache_root(self.home / "work") / "objects", oid(self.data))).unlink()
        resource = self.occupy(None, slot=True)

        def slow_fetch():  # longer than the whole lease wait; the slot is given back only afterwards
            time.sleep(2.5)
            threading.Timer(0.5, lambda: self.manager.release(resource, "other", os.getpid())).start()

        fake = FakeLfs({oid(self.data): self.data}, on_fetch=slow_fetch)
        with mock.patch.object(lease, "unity_editor_capacity", return_value=1):
            code, lines, err = self.unity(lfs_call=fake, lease_wait_sec=2.0)
        self.assertEqual((constants.EXIT_PASS, "STATUS PASS"), (code, lines[-1]), err)
        self.assertEqual(1, self.preflight(lines)["fetched"])
        self.assertNoLeases()

    def test_a_git_failure_in_a_repository_blocks_instead_of_starting_unity_on_pointers(self):
        def refusing(cwd, argv, stdin=None):
            if argv[:1] == ["rev-parse"]:
                return 128, b"", "fatal: detected dubious ownership in repository"
            return lfs.run_git(cwd, argv, stdin)

        with mock.patch.object(lfs, "run_git", refusing), mock.patch.object(lfs.materialize, "__kwdefaults__",
                                                                            {**lfs.materialize.__kwdefaults__, "git": refusing}):
            code, lines, _ = self.unity(lfs_call=FakeLfs())
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertEqual("lfs.git-failed", self.preflight(lines)["code"])
        self.assertFalse((self.logs / "Editor.log").exists(), "Unity was not started")

    def test_the_cli_option_is_passed_through(self):
        from valtrion import unityrun
        for flag, expected in (("--no-lfs-materialize", False), ("--lfs-materialize", True), (None, None)):
            with mock.patch.object(unityrun, "run", return_value=0) as run:
                with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                    cli.main(["unity", "run", *([flag] if flag else []), "--unity", sys.executable, "--lease-wait", "0", "--",
                              FAKE, "-batchmode", "-projectPath", str(self.project)])
            self.assertEqual(expected, run.call_args.kwargs["lfs_materialize"], flag)


if __name__ == "__main__":
    unittest.main()
