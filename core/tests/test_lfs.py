"""Git LFS preflight of `valtrion unity run` (`lfs.py`): pointer files in a real git repository are materialized from the shared cache,
the repository's LFS store or a fake `git lfs fetch`. No network is used: `git lfs` is replaced by `FakeLfs`, git's clean filter by
`fake_lfs_clean.py`, and the global and system git configuration are switched off."""

import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, config, constants, lfs  # noqa: F401  (cli first: it registers the commands)

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

    def __init__(self, remote=None, missing_tool=False, fail_text=None, corrupt=False):
        self.remote = dict(remote or {})
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
