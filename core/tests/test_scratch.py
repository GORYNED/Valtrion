"""Scratch copies under <work root>/scratch: create, release, sweep, unowned entries and adoption, with every removal guard."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from valtrion import cli, config, core, diskspace, doctor, jobs, lease, scratch, workroot

HOUR = 3600.0


def git(args, cwd):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


def make_dir_link(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (needs no privilege), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


ME = SimpleNamespace(pid=os.getpid(), ppid=0, path="python", command="python -m unittest")


def no_processes():
    """A process list in which nothing but this test process runs (a list without it counts as incomplete)."""
    return [ME]


def with_me(*others):
    return lambda: [ME, *others]


def age(path: Path, seconds: float) -> None:
    """Set the modification time of `path` and everything below it `seconds` into the past."""
    moment = time.time() - seconds
    for folder, folders, files in os.walk(path):
        for name in folders + files:
            entry = os.path.join(folder, name)
            if not scratch.is_link(Path(entry)):  # a link's target is not ours to touch
                os.utime(entry, (moment, moment))
    os.utime(path, (moment, moment))


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ScratchBase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.home = self.base / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(workroot.WORK_ROOT_ENV, None)
        os.environ.pop("GDV_OWNER", None)
        self.roots = workroot.WorkRoots(self.home / "work")
        self.roots.root.mkdir()
        self.manager = lease.LeaseManager(self.home)
        self.me = scratch.Caller("me@host", scratch.current_user(), os.getuid() if hasattr(os, "getuid") else None)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "precious.txt").write_text("keep me\n", encoding="utf-8")
        self.jobs = {}  # job id -> recorded state, for the job lookups

    def make(self, name="tmp", hours=24.0, who=None, **extra):
        extra.setdefault("job_state_of", self.jobs.get)
        folder, marker = scratch.create(self.roots, name, who or self.me, max_age_hours=hours, **extra)
        (folder / "work.txt").write_text("x\n", encoding="utf-8")
        return folder, marker

    def rewrite_marker(self, folder: Path, **changes) -> None:
        marker = core.read_json(folder / scratch.MARKER)
        for key, value in changes.items():
            if value is None:
                marker.pop(key, None)
            else:
                marker[key] = value
        core.atomic_write_json(folder / scratch.MARKER, marker)

    def release(self, scratch_id, **options):
        options.setdefault("manager", self.manager)
        options.setdefault("processes", no_processes)
        return scratch.release(self.roots, scratch_id, self.me, **options)

    def sweep(self, active=(), idle=HOUR, **options):
        options.setdefault("manager", self.manager)
        options.setdefault("processes", no_processes)
        options.setdefault("job_state_of", self.jobs.get)
        return scratch.sweep(self.roots, self.me, list(active), idle, **options)

    def expire(self, folder: Path, hours_ago: float = 2.0, idle_sec: float = 2 * HOUR) -> None:
        """Move the marker's expiry into the past and make the folder idle."""
        marker = core.read_json(folder / scratch.MARKER)
        marker["expiresUtc"] = core.format_utc(core.now() - timedelta(hours=hours_ago))
        core.atomic_write_json(folder / scratch.MARKER, marker)
        age(folder, idle_sec)


class CreateAndReleaseTests(ScratchBase):
    def test_create_writes_the_marker_and_release_removes_exactly_that_folder(self):
        folder, marker = self.make("vcheck")
        other, _ = self.make("other")
        self.assertEqual(self.roots.scratch / marker["id"], folder)
        self.assertRegex(marker["id"], r"^vcheck-[0-9a-f]{8}$")
        written = core.read_json(folder / scratch.MARKER)
        self.assertEqual(scratch.MARKER_KIND, written["marker"])
        for key in ("id", "owner", "user", "host", "command", "createdUtc", "expiresUtc"):
            self.assertIn(key, written)
        self.assertEqual("me@host", written["owner"])
        listed = {item["id"]: item for item in scratch.listing(self.roots)}
        self.assertFalse(listed[marker["id"]]["expired"])
        self.assertEqual(str(folder), self.release(marker["id"]))
        self.assertFalse(folder.exists())
        self.assertTrue(other.is_dir(), "only the named folder goes")

    def test_dry_run_release_removes_nothing(self):
        folder, marker = self.make()
        self.release(marker["id"], dry_run=True)
        self.assertTrue(folder.is_dir())

    def test_credentials_in_a_source_url_are_not_written_to_the_marker(self):
        _, marker = scratch.create(self.roots, "a", self.me, max_age_hours=1, source="https://user:token@example.com/r.git")
        self.assertEqual("https://example.com/r.git", marker["source"])

    def test_bad_names_and_lifetimes_are_refused(self):
        for name in ("", "../x", "a/b", ".hidden", "x" * 41, "a b"):
            with self.assertRaises(scratch.ScratchError, msg=name):
                scratch.create(self.roots, name, self.me, max_age_hours=1)
        for hours in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(scratch.ScratchError):
                scratch.create(self.roots, "ok", self.me, max_age_hours=hours)
        with self.assertRaises(scratch.ScratchError):
            scratch.create(self.roots, "ok", self.me, max_age_hours=1, sha="a" * 40)  # no project or source
        self.assertEqual([], scratch.listing(self.roots))

    def test_a_link_inside_is_removed_as_a_link_and_its_target_is_kept(self):
        folder, marker = self.make()
        make_dir_link(folder / "linked", self.outside)
        nested = folder / "deep" / "er"
        nested.mkdir(parents=True)
        make_dir_link(nested / "linked", self.outside)
        self.release(marker["id"])
        self.assertFalse(folder.exists())
        self.assertEqual("keep me\n", (self.outside / "precious.txt").read_text(encoding="utf-8"))


class ReleaseRefusalTests(ScratchBase):
    def assertRefused(self, scratch_id, fragment, **options):
        with self.assertRaises(scratch.ScratchError) as caught:
            self.release(scratch_id, **options)
        self.assertIn(fragment, str(caught.exception))

    def test_a_folder_without_a_marker_is_refused(self):
        folder = self.roots.scratch / "plain-0123abcd"
        folder.mkdir(parents=True)
        self.assertRefused("plain-0123abcd", "no Valtrion scratch marker")
        self.assertTrue(folder.is_dir())

    def test_a_foreign_marker_is_refused(self):
        folder = self.roots.scratch / "foreign-0123abcd"
        folder.mkdir(parents=True)
        core.atomic_write_json(folder / scratch.MARKER, {"marker": "someone.else/1", "id": "foreign-0123abcd"})
        self.assertRefused("foreign-0123abcd", "not Valtrion's")
        core.atomic_write_text(folder / scratch.MARKER, "not json")
        self.assertRefused("foreign-0123abcd", "cannot be read")
        self.assertTrue(folder.is_dir())

    def test_a_marker_for_another_id_is_refused(self):
        folder, marker = self.make()
        moved = self.roots.scratch / "copy-0123abcd"
        shutil.copytree(folder, moved)
        self.assertRefused("copy-0123abcd", "names the id")
        self.assertTrue(moved.is_dir())

    def test_a_live_unity_project_lease_on_the_folder_or_a_project_inside_is_refused(self):
        folder, marker = self.make()
        project = folder / "checkout" / "Game"
        (project / "ProjectSettings").mkdir(parents=True)
        held = self.manager.acquire(lease.unity_project_resource(project), "agent", "s1", 600, "unity run", os.getpid())
        self.assertTrue(held.acquired)
        self.assertRefused(marker["id"], "a lease refers to it")
        self.assertTrue(folder.is_dir())
        self.manager.release(lease.unity_project_resource(project), "s1", os.getpid())
        self.release(marker["id"])
        self.assertFalse(folder.exists())

    def test_a_held_lease_named_by_the_marker_or_with_a_worktree_inside_is_refused(self):
        folder, marker = self.make(lease_resource="device:abc")
        self.manager.acquire("device:abc", "agent", "s1", 600, "run", os.getpid())
        self.assertRefused(marker["id"], "device:abc")
        other, second = self.make("wt")
        self.manager.acquire("thing", "agent", "s2", 600, "run", os.getpid(), worktree=str(other / "checkout"))
        self.assertRefused(second["id"], "thing")

    def test_a_stale_lease_does_not_keep_the_folder(self):
        folder, marker = self.make()
        dead = lease.LeaseManager(self.home, clock=lambda: core.now() - timedelta(hours=5))
        dead.acquire(lease.unity_project_resource(folder), "agent", "s1", 1, "old", 2 ** 30)
        stale = lease.LeaseManager(self.home, is_alive=lambda pid: False)
        self.release(marker["id"], manager=stale)
        self.assertFalse(folder.exists())

    def test_a_live_process_that_names_the_folder_is_refused(self):
        folder, marker = self.make()
        editor = SimpleNamespace(pid=999999, ppid=1, path="Unity", command=f'Unity -batchmode -projectPath "{folder / "checkout"}"')
        self.assertRefused(marker["id"], "pid 999999", processes=with_me(editor))
        self.assertTrue(folder.is_dir())

    def test_a_process_naming_a_longer_sibling_does_not_count(self):
        folder, marker = self.make()
        sibling = SimpleNamespace(pid=999999, ppid=1, path="x", command=f"tool {folder}0-more")
        self.release(marker["id"], processes=with_me(sibling))
        self.assertFalse(folder.exists())

    def test_the_caller_and_its_ancestors_do_not_count_as_users(self):
        folder, marker = self.make()
        me = SimpleNamespace(pid=os.getpid(), ppid=os.getppid(), path="python", command=f"valtrion adopt {folder}")
        parent = SimpleNamespace(pid=os.getppid(), ppid=0, path="sh", command=f"sh -c 'valtrion adopt {folder}'")
        self.release(marker["id"], processes=lambda: [me, parent])
        self.assertFalse(folder.exists())

    def test_an_unreadable_process_list_keeps_the_folder(self):
        folder, marker = self.make()
        self.assertRefused(marker["id"], "process list cannot be read", processes=lambda: None)

        def broken():
            raise OSError("no ps")

        self.assertRefused(marker["id"], "process list cannot be read", processes=broken)
        self.assertTrue(folder.is_dir())

    def test_an_entry_that_is_a_link_to_outside_is_refused_and_the_target_kept(self):
        target = self.base / "victim"
        target.mkdir()
        core.atomic_write_json(target / scratch.MARKER, {"marker": scratch.MARKER_KIND, "id": "evil-0123abcd", "owner": "me@host"})
        (target / "data.txt").write_text("mine\n", encoding="utf-8")
        self.roots.scratch.mkdir(parents=True)
        make_dir_link(self.roots.scratch / "evil-0123abcd", target)
        self.assertRefused("evil-0123abcd", "symlink or junction")
        self.assertTrue((target / "data.txt").is_file())
        report = self.sweep(idle=0)
        self.assertEqual([], report.removed)
        self.assertTrue((target / "data.txt").is_file())

    def test_a_scratch_folder_that_is_itself_a_link_is_refused(self):
        target = self.base / "elsewhere"
        (target / "evil-0123abcd").mkdir(parents=True)
        core.atomic_write_json(target / "evil-0123abcd" / scratch.MARKER,
                               {"marker": scratch.MARKER_KIND, "id": "evil-0123abcd", "owner": "me@host"})
        make_dir_link(self.roots.scratch, target)
        self.assertRefused("evil-0123abcd", "symlink or junction")
        self.assertTrue((target / "evil-0123abcd" / scratch.MARKER).is_file())
        self.assertEqual([], self.sweep(idle=0).removed)

    def test_ids_with_path_parts_are_refused_before_any_path_is_built(self):
        for bad in ("..", "../outside", "a/../../b-0123abcd", "x-0123abcd/..", "..\\x-0123abcd", "x-0123ABCD", ""):
            self.assertRefused(bad, "is not a scratch id")
        self.assertTrue((self.outside / "precious.txt").is_file())

    def test_another_owner_needs_any_owner_and_another_host_user_is_never_removed(self):
        folder, marker = self.make(who=scratch.Caller("agent-b@host", self.me.user, self.me.uid))
        self.assertRefused(marker["id"], "--any-owner")
        stranger, second = self.make("s", who=scratch.Caller("x@host", "someone-else", self.me.uid))
        self.assertRefused(second["id"], "host user someone-else", any_owner=True)
        self.assertTrue(stranger.is_dir())
        self.release(marker["id"], any_owner=True)
        self.assertFalse(folder.exists())

    @unittest.skipUnless(hasattr(os, "getuid"), "file owners are checked on POSIX")
    def test_a_folder_of_another_posix_user_is_refused(self):
        folder, marker = self.make()
        other = scratch.Caller("me@host", self.me.user, self.me.uid + 1)
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.release(self.roots, marker["id"], other, any_owner=True, manager=self.manager, processes=no_processes)
        self.assertIn("another host user", str(caught.exception))
        self.assertTrue(folder.is_dir())


class SweepTests(ScratchBase):
    def test_sweep_removes_an_expired_idle_folder_and_keeps_the_others(self):
        expired, first = self.make("old")
        fresh, _ = self.make("fresh")
        self.expire(expired)
        age(fresh, 3 * HOUR)  # idle, but not expired
        report = self.sweep()
        self.assertEqual(1, len(report.removed))
        self.assertIn(first["id"], report.removed[0])
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(any("not expired" in reason for _, reason in report.kept))

    def test_sweep_keeps_an_expired_folder_used_recently(self):
        folder, _ = self.make()
        self.expire(folder, idle_sec=0)
        (folder / "work.txt").write_text("again\n", encoding="utf-8")
        self.assertEqual([], self.sweep().removed)
        self.assertTrue(folder.is_dir())

    def test_sweep_never_removes_an_expired_folder_with_a_live_lease_or_process(self):
        leased, first = self.make("leased")
        busy, second = self.make("busy")
        self.expire(leased)
        self.expire(busy)
        self.manager.acquire(lease.unity_project_resource(leased), "agent", "s1", 600, "build", os.getpid())
        editor = SimpleNamespace(pid=999999, ppid=1, path="Unity", command=f"Unity -projectPath {busy}")
        report = self.sweep(processes=with_me(editor))
        self.assertEqual([], report.removed)
        self.assertTrue(leased.is_dir() and busy.is_dir())
        reasons = dict(report.kept)
        self.assertIn("lease", reasons[str(leased)])
        self.assertIn("pid 999999", reasons[str(busy)])
        self.assertEqual([], self.sweep(processes=lambda: None).removed, "an unreadable process list keeps everything")

    def test_a_folder_of_an_ended_job_goes_once_idle_and_one_of_a_running_job_stays(self):
        self.jobs.update({"job-1": "finished", "job-2": "running", "job-3": "running"})
        ended, _ = self.make("ended", job="job-1")
        running, _ = self.make("running", job="job-2")
        dead_runner, _ = self.make("dead", job="job-3")  # left `running` by a runner that died: not recorded as ended
        for folder in (ended, running, dead_runner):
            age(folder, 2 * HOUR)
        report = self.sweep(active=["job-2"])
        self.assertFalse(ended.exists())
        self.assertTrue(running.is_dir())
        self.assertTrue(dead_runner.is_dir())
        self.assertIn("job job-1 has ended", report.removed[0])

    def test_an_unknown_job_is_refused_at_create_and_never_counts_as_ended(self):
        with self.assertRaises(scratch.ScratchError) as caught:
            self.make("ghost", job="no-such-job")
        self.assertIn("no job", str(caught.exception))
        self.jobs["job-1"] = "running"
        folder, _ = self.make("later", job="job-1")
        self.jobs.clear()  # the job record went away (index pruned)
        age(folder, 2 * HOUR)
        self.assertEqual([], self.sweep(active=[]).removed)
        self.assertTrue(folder.is_dir())

    def test_a_marker_without_a_readable_expiry_is_never_due(self):
        missing, _ = self.make("missing")
        broken, _ = self.make("broken")
        self.rewrite_marker(missing, expiresUtc=None)
        self.rewrite_marker(broken, expiresUtc="soon")
        age(missing, 100 * HOUR)
        age(broken, 100 * HOUR)
        self.assertEqual([], self.sweep(any_owner=True).removed)
        self.assertTrue(missing.is_dir() and broken.is_dir())
        listed = {item["path"]: item for item in scratch.listing(self.roots)}
        self.assertFalse(listed[str(missing)]["expired"])
        self.assertIn("never removes", listed[str(missing)]["problem"])

    def test_a_marker_without_owner_or_host_user_is_never_removed(self):
        for key in ("user", "owner"):
            folder, marker = self.make(key)
            self.rewrite_marker(folder, **{key: None})
            self.expire(folder)
            with self.assertRaises(scratch.ScratchError) as caught:
                self.release(marker["id"], any_owner=True)
            self.assertIn(f"names no {key}", str(caught.exception))
            self.rewrite_marker(folder, **{key: ""})
            self.assertEqual([], self.sweep(any_owner=True).removed)
            self.assertTrue(folder.is_dir())

    def test_a_change_deep_in_the_tree_keeps_an_expired_folder(self):
        folder, _ = self.make()
        deep = folder / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "Edit.cs").write_text("old\n", encoding="utf-8")
        self.expire(folder)
        os.utime(deep / "Edit.cs")  # touched now, four levels down
        self.assertEqual([], self.sweep().removed)
        self.assertTrue(folder.is_dir())
        age(folder, 2 * HOUR)
        self.assertEqual(1, len(self.sweep().removed))

    def test_a_tree_not_walked_within_the_budget_counts_as_in_use(self):
        folder, _ = self.make()
        self.expire(folder)
        report = self.sweep(walk_budget_sec=-1)
        self.assertEqual([], report.removed)
        self.assertIn("could not be walked", dict(report.kept)[str(folder)])
        self.assertIsNone(scratch.newest_change(folder, budget_sec=-1))
        self.assertIsNotNone(scratch.newest_change(folder))

    def test_an_empty_or_incomplete_process_list_keeps_everything(self):
        folder, marker = self.make()
        self.expire(folder)
        other = SimpleNamespace(pid=999999, ppid=1, path="x", command="x")
        for listed in ([], [other]):
            self.assertEqual([], self.sweep(processes=lambda: listed).removed)
            with self.assertRaises(scratch.ScratchError) as caught:
                self.release(marker["id"], processes=lambda: listed)
            self.assertIn("incomplete", str(caught.exception))
        self.assertTrue(folder.is_dir())

    def test_sweep_skips_other_owners_unless_asked_and_other_host_users_always(self):
        mine_not, _ = self.make("b", who=scratch.Caller("agent-b@host", self.me.user, self.me.uid))
        theirs, _ = self.make("c", who=scratch.Caller("c@host", "someone-else", self.me.uid))
        self.expire(mine_not)
        self.expire(theirs)
        self.assertEqual([], self.sweep().removed)
        report = self.sweep(any_owner=True)
        self.assertFalse(mine_not.exists())
        self.assertTrue(theirs.is_dir())
        self.assertIn("someone-else", dict(report.kept)[str(theirs)])

    def test_sweep_dry_run_lists_but_removes_nothing_and_invalid_entries_stay(self):
        folder, _ = self.make()
        self.expire(folder)
        plain = self.roots.scratch / "plain"
        plain.mkdir()
        age(plain, 10 * HOUR)
        report = self.sweep(dry_run=True)
        self.assertEqual(1, len(report.removed))
        self.assertTrue(folder.is_dir())
        self.sweep()
        self.assertFalse(folder.exists())
        self.assertTrue(plain.is_dir(), "a folder without a marker is never removed")

    def test_the_disk_pressure_cleanup_removes_expired_idle_scratch_of_any_owner(self):
        core.atomic_write_json(config.host_json_path(self.home), {"workRoot": str(self.roots.root), "disk": {"minFreeGiB": 10}})
        settings = config.load_host_settings(self.home)
        expired, _ = self.make("old", who=scratch.Caller("agent-b@host", self.me.user, self.me.uid))
        fresh, _ = self.make("fresh")
        self.expire(expired)
        with mock.patch.object(scratch, "system_processes", no_processes):
            outcome = diskspace.ensure_free(settings, self.home, free=lambda path: 0)
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(any("scratch" in item for item in outcome.removed))


class CheckoutTests(ScratchBase):
    def test_a_scratch_checkout_hangs_off_the_mirror_and_release_prunes_it(self):
        source = self.base / "source"
        source.mkdir()
        git(["init", "-q", "-b", "main"], source)
        git(["config", "user.email", "t@example.com"], source)
        git(["config", "user.name", "T"], source)
        (source / "file.txt").write_text("one\n", encoding="utf-8")
        git(["add", "."], source)
        git(["commit", "-q", "-m", "one"], source)
        sha = git(["rev-parse", "HEAD"], source)
        folder, marker = scratch.create(self.roots, "repro", self.me, max_age_hours=1, project="proj", source=str(source), sha=sha)
        checkout = folder / scratch.CHECKOUT
        self.assertEqual("one\n", (checkout / "file.txt").read_text(encoding="utf-8"))
        self.assertEqual(sha, marker["sha"])
        self.assertIn(marker["id"], git(["worktree", "list"], self.roots.mirror("proj")))
        self.release(marker["id"])
        self.assertFalse(folder.exists())
        self.assertNotIn(marker["id"], git(["worktree", "list"], self.roots.mirror("proj")))

    def test_a_failed_checkout_leaves_nothing(self):
        with self.assertRaises(workroot.WorkRootError):
            scratch.create(self.roots, "repro", self.me, max_age_hours=1, project="proj", source=str(self.base / "missing"),
                           sha="f" * 40)
        self.assertEqual([], list(self.roots.scratch.iterdir()))


class UnownedAndAdoptTests(ScratchBase):
    def real_case(self):
        """The two leftovers of issue #75: standalone clones directly in the work root."""
        first = self.roots.root / "vcheck-2114bfc"
        second = self.roots.root / "sdk-clean-553"
        for folder in (first, second):
            (folder / "sub").mkdir(parents=True)
            (folder / "sub" / "blob.bin").write_bytes(b"x" * 2048)
        for name in ("jobs", "mirrors", "launchd", "tools", "lfs-cache", "result-cache"):
            (self.roots.root / name).mkdir()
        return first, second

    def test_unowned_entries_are_listed_with_size_age_and_users_and_never_swept(self):
        first, second = self.real_case()
        age(first, 5 * HOUR)
        self.make("valid")
        (self.roots.scratch / "nomarker").mkdir()
        editor = SimpleNamespace(pid=4242, ppid=1, path="Unity", command=f"Unity -projectPath {second}")
        found = {item["name"]: item for item in scratch.unowned(self.roots, self.manager, with_me(editor))}
        self.assertEqual({"vcheck-2114bfc", "sdk-clean-553", "scratch/nomarker"}, set(found))
        self.assertEqual(2048, found["vcheck-2114bfc"]["bytes"])
        self.assertGreaterEqual(found["vcheck-2114bfc"]["ageHours"], 4.9)
        self.assertEqual([], found["vcheck-2114bfc"]["processes"])
        self.assertEqual([4242], found["sdk-clean-553"]["processes"])
        self.assertIn("is not a scratch id", found["scratch/nomarker"]["reason"])
        (self.roots.scratch / "nomarker-0123abcd").mkdir()
        found = {item["name"]: item for item in scratch.unowned(self.roots, self.manager, with_me(editor))}
        self.assertIn("no Valtrion scratch marker", found["scratch/nomarker-0123abcd"]["reason"])
        hint = scratch.describe_unowned(found["vcheck-2114bfc"])
        self.assertIn("never removes", hint)
        self.assertNotIn("--yes", hint, "no ready-made command that would skip the owner's own check")
        self.assertIn("at least", scratch.describe_unowned({**found["vcheck-2114bfc"], "bytesComplete": False}))
        limited = {item["name"]: item for item in scratch.unowned(self.roots, self.manager, with_me(editor), size_budget_sec=-1)}
        self.assertFalse(limited["vcheck-2114bfc"]["bytesComplete"])
        self.sweep(idle=0, any_owner=True)
        self.assertTrue(first.is_dir() and second.is_dir() and (self.roots.scratch / "nomarker").is_dir())

    def test_unowned_reports_unknown_processes_and_skips_the_probe_when_nothing_is_unowned(self):
        calls = []

        def probe():
            calls.append(1)
            return None

        self.assertEqual([], scratch.unowned(self.roots, self.manager, probe))
        self.assertEqual([], calls, "no unowned entry: the process list is not read")
        (self.roots.root / "stray").mkdir()
        self.assertIsNone(scratch.unowned(self.roots, self.manager, probe)[0]["processes"])

    def test_doctor_and_workroot_show_and_sweep_dry_run_warn_about_unowned_entries(self):
        self.real_case()
        core.atomic_write_json(config.host_json_path(self.home), {"workRoot": str(self.roots.root)})
        with mock.patch.object(scratch, "system_processes", no_processes):
            report = doctor.work_root_report(self.roots.root)
            self.assertEqual({"vcheck-2114bfc", "sdk-clean-553"}, {item["name"] for item in report["unowned"]})
            full = doctor.probe_local("windows", lambda argv: (1, "x"), self.home / "art", {doctor.HUB_PATHS_ENV: str(self.home / "none")},
                                      work_root=self.roots.root)
            self.assertEqual(2, sum("unowned work-root entry" in item for item in full["warnings"]))
            self.assertIn("sdk-clean-553", doctor.render(full))
            code, out, _ = run_cli("workroot", "show", "--json")
            self.assertEqual(0, code)
            shown = json.loads(out)
            self.assertEqual(2, len(shown["unowned"]))
            self.assertEqual(2, len(shown["warnings"]))
            code, out, _ = run_cli("workroot", "sweep", "--dry-run")
            self.assertEqual(0, code)
            self.assertIn("vcheck-2114bfc", out)
            self.assertIn("sdk-clean-553", out)
            self.assertIn("2 unowned entries (never removed)", out)
        self.assertTrue((self.roots.root / "vcheck-2114bfc").is_dir())

    def test_adopt_moves_an_unowned_folder_into_scratch_so_it_expires(self):
        first, _ = self.real_case()
        folder, marker = scratch.adopt(self.roots, first, self.me, max_age_hours=1, manager=self.manager, processes=no_processes)
        self.assertFalse(first.exists())
        self.assertTrue((folder / "sub" / "blob.bin").is_file())
        self.assertEqual(str(first), marker["adoptedFrom"])
        self.assertRegex(marker["id"], r"^vcheck-2114bfc-[0-9a-f]{8}$")
        self.assertNotIn("vcheck-2114bfc", {item["name"] for item in scratch.unowned(self.roots, self.manager, no_processes)})
        self.expire(folder)
        self.sweep()
        self.assertFalse(folder.exists())

    def test_adopt_by_name_relative_to_the_work_root(self):
        self.real_case()
        folder, _ = scratch.adopt(self.roots, Path("sdk-clean-553"), self.me, max_age_hours=1, name="sdk",
                                  manager=self.manager, processes=no_processes)
        self.assertTrue(folder.name.startswith("sdk-"))

    def test_adopt_refuses_links_valtrion_folders_escapes_and_folders_in_use(self):
        first, _ = self.real_case()
        make_dir_link(self.roots.root / "link", self.outside)
        nested = self.roots.root / "jobs" / "deep"
        nested.mkdir()
        cases = {
            self.roots.root / "link": "symlink or junction",
            self.roots.root / "jobs": "valtrion's own folders",
            self.roots.root / ".." / ".." / "outside": "not a direct entry of the work root",
            nested: "not a direct entry of the work root",
            self.outside: "not a direct entry of the work root",
            self.roots.root / "missing": "not a directory",
        }
        for path, fragment in cases.items():
            with self.assertRaises(scratch.ScratchError, msg=str(path)) as caught:
                scratch.adopt(self.roots, path, self.me, max_age_hours=1, manager=self.manager, processes=no_processes)
            self.assertIn(fragment, str(caught.exception))
        editor = SimpleNamespace(pid=4242, ppid=1, path="Unity", command=f"Unity -projectPath {first}")
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.adopt(self.roots, first, self.me, max_age_hours=1, manager=self.manager, processes=with_me(editor))
        self.assertIn("in use", str(caught.exception))
        self.assertTrue(first.is_dir())
        self.assertTrue((self.outside / "precious.txt").is_file())


class AdoptGitTests(ScratchBase):
    """Adopting a clone must never lose work that exists only in it."""

    def setUp(self):
        super().setUp()
        self.source = self.base / "origin"
        self.source.mkdir()
        git(["init", "-q", "-b", "main"], self.source)
        git(["config", "user.email", "t@example.com"], self.source)
        git(["config", "user.name", "T"], self.source)
        (self.source / "file.txt").write_text("one\n", encoding="utf-8")
        git(["add", "."], self.source)
        git(["commit", "-q", "-m", "one"], self.source)
        self.clone = self.roots.root / "vcheck-2114bfc"
        git(["clone", "-q", str(self.source), str(self.clone)], self.base)
        git(["config", "user.email", "t@example.com"], self.clone)
        git(["config", "user.name", "T"], self.clone)

    def adopt(self, **options):
        return scratch.adopt(self.roots, self.clone, self.me, max_age_hours=1, manager=self.manager, processes=no_processes, **options)

    def assertKept(self, fragment):
        with self.assertRaises(scratch.ScratchError) as caught:
            self.adopt()
        self.assertIn(fragment, str(caught.exception))
        self.assertIn("--discard-unsaved-work", str(caught.exception))
        self.assertTrue(self.clone.is_dir())

    def test_a_clean_clone_with_everything_pushed_is_adopted(self):
        folder, _ = self.adopt()
        self.assertTrue((folder / "file.txt").is_file())

    def test_uncommitted_and_untracked_changes_are_refused(self):
        (self.clone / "file.txt").write_text("changed\n", encoding="utf-8")
        self.assertKept("uncommitted or untracked changes")
        git(["checkout", "-q", "--", "file.txt"], self.clone)
        (self.clone / "new.txt").write_text("new\n", encoding="utf-8")
        self.assertKept("uncommitted or untracked changes")

    def test_stashes_and_unpushed_commits_are_refused(self):
        (self.clone / "file.txt").write_text("changed\n", encoding="utf-8")
        git(["stash", "-q"], self.clone)
        self.assertKept("stashed changes")
        git(["stash", "drop", "-q"], self.clone)
        (self.clone / "file.txt").write_text("two\n", encoding="utf-8")
        git(["commit", "-q", "-am", "local only"], self.clone)
        self.assertKept("commits that are on no remote")

    def test_a_nested_repository_is_checked_too_and_the_flag_overrides(self):
        nested = self.clone / "Dependency"
        git(["clone", "-q", str(self.source), str(nested)], self.base)
        (nested / "file.txt").write_text("changed\n", encoding="utf-8")
        (self.roots.root / "vcheck-2114bfc" / ".git" / "info").mkdir(exist_ok=True)
        (self.clone / ".git" / "info" / "exclude").write_text("Dependency/\n", encoding="utf-8")
        self.assertKept("Dependency")
        folder, _ = self.adopt(discard_unsaved_work=True)
        self.assertTrue(folder.is_dir())


class RemovalSafetyTests(ScratchBase):
    def test_a_removal_that_fails_half_way_keeps_the_id_and_marker_and_can_be_repeated(self):
        folder, marker = self.make()
        (folder / "b").mkdir()
        (folder / "b" / "x.txt").write_text("x\n", encoding="utf-8")
        original = scratch.artifacts._remove

        def failing(path):
            if path.name == "b":
                raise PermissionError("locked")
            original(path)

        with mock.patch.object(scratch.artifacts, "_remove", failing):
            with self.assertRaises(scratch.ScratchError) as caught:
                self.release(marker["id"])
        self.assertIn("release it again", str(caught.exception))
        self.assertTrue((folder / scratch.MARKER).is_file(), "renamed back with its marker")
        self.assertEqual([marker["id"]], [entry.name for entry in self.roots.scratch.iterdir()], "no tombstone is left")
        self.release(marker["id"])
        self.assertFalse(folder.exists())

    def test_a_folder_swapped_for_a_link_after_the_checks_is_not_followed(self):
        folder, marker = self.make()
        real_locate = scratch.locate
        calls = []

        def swapping(roots, scratch_id):
            found = real_locate(roots, scratch_id)
            calls.append(1)
            if len(calls) == 2:  # between the checks of release and the removal: swap in a link to outside
                aside = self.base / "aside"
                os.rename(found, aside)
                make_dir_link(found, self.outside)
                core.atomic_write_json(self.outside / scratch.MARKER, core.read_json(aside / scratch.MARKER))
            return found

        with mock.patch.object(scratch, "locate", swapping):
            with self.assertRaises(scratch.ScratchError):
                self.release(marker["id"])
        self.assertTrue((self.outside / "precious.txt").is_file())


class RealProcessTests(ScratchBase):
    """The real process probe of this host (Win32_Process in UTF-8 on Windows, ps elsewhere) with a non-ASCII path."""

    def test_a_live_process_in_a_work_root_under_a_cyrillic_folder_is_seen_and_keeps_it(self):
        # The whole work root lies below a non-ASCII folder (a Cyrillic user profile, as the default work root does): with output
        # in the console code page the command line arrives with `?` in place of every such letter and matches nothing.
        self.roots = workroot.WorkRoots(self.base / "Пользователь" / "work")
        self.roots.root.mkdir(parents=True)
        folder, marker = self.make("cyr")
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(folder)])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        listed = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            listed = scratch.system_processes()
            self.assertIsNotNone(listed, "the process list of this host must be readable")
            if any(proc.pid == holder.pid for proc in listed):
                break
            time.sleep(1)
        command = next(proc.command for proc in listed if proc.pid == holder.pid)
        self.assertIn("Пользователь", command, "the command line must arrive in UTF-8")
        self.assertIn(holder.pid, scratch.using_processes(folder, listed), "the command line must match the folder")
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.release(self.roots, marker["id"], self.me, manager=self.manager)
        self.assertIn(f"pid {holder.pid}", str(caught.exception))
        self.assertTrue(folder.is_dir())
        holder.kill()
        holder.wait()
        scratch.release(self.roots, marker["id"], self.me, manager=self.manager)
        self.assertFalse(folder.exists())


class LsofTests(unittest.TestCase):
    def test_lsof_cwd_output_is_parsed(self):
        text = "p12\nfcwd\nn/Users/a/work/scratch/x-0123abcd\np13\nfcwd\nn/\n"
        self.assertEqual({12: "/Users/a/work/scratch/x-0123abcd", 13: "/"}, scratch.parse_lsof_cwd(text))

    def test_a_working_directory_inside_the_folder_counts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as base:
            folder = Path(base) / "x-0123abcd"
            (folder / "deep").mkdir(parents=True)
            shell = SimpleNamespace(pid=77, ppid=1, path="zsh", command="-zsh", cwd=str(folder / "deep"))
            self.assertEqual([77], scratch.using_processes(folder, [ME, shell]))


class EncodingTests(unittest.TestCase):
    def test_lsof_hex_escapes_are_decoded_and_bad_bytes_refused(self):
        raw = b"p42\nfcwd\nn/Users/a/\\xd0\\x9f\\xd1\\x80\\xd0\\xbe/work/scratch/x-0123abcd\n"
        self.assertEqual({42: "/Users/a/Про/work/scratch/x-0123abcd"}, scratch.parse_lsof_cwd(raw))
        with self.assertRaises(ValueError):
            scratch.parse_lsof_cwd(b"p42\nn/Users/\\xff\\xfe\n")

    def test_posix_probes_run_in_a_utf8_locale(self):
        from valtrion import unityguard
        with mock.patch.object(unityguard, "available_locales", return_value=None):  # unknown: the per-system default
            self.assertEqual("en_US.UTF-8", unityguard.utf8_environment("macos")["LC_ALL"])
            self.assertEqual("C.UTF-8", unityguard.utf8_environment("linux")["LC_ALL"])
        self.assertIsNone(unityguard.utf8_environment("windows"))
        done = SimpleNamespace(returncode=0, stdout=b"ok")
        with mock.patch.object(unityguard, "available_locales", return_value=frozenset({"c", "posix", "cutf8"})), \
                mock.patch.object(unityguard.subprocess, "run", return_value=done) as run:
            self.assertEqual("ok", unityguard.run_text(["ps"]))
        options = run.call_args.kwargs
        if os.name == "nt":
            self.assertEqual(subprocess.CREATE_NO_WINDOW, options["creationflags"], "PowerShell gets a console of its own")
        else:
            self.assertIn("UTF-8", options["env"]["LC_ALL"])

    def listed(self, *commands, argv=("release", "x-0123abcd")):
        own = SimpleNamespace(pid=os.getpid(), ppid=1, path="python", command="python -m valtrion workroot scratch " + " ".join(argv),
                              cwd=None)
        return [own, *(SimpleNamespace(pid=900 + i, ppid=1, path="p", command=c, cwd=None) for i, c in enumerate(commands))]

    def test_a_listing_with_undecodable_bytes_or_without_the_callers_own_arguments_is_unreadable(self):
        scratch.check_readable(self.listed("/bin/sleep 9 /w/Пользователь/scratch/x"), ["release", "x-0123abcd"])
        with self.assertRaisesRegex(scratch.ProcessListUnreadable, "pid 900 is not valid UTF-8"):
            scratch.check_readable(self.listed("/bin/sleep 9 /w/��/scratch/x"), ["release", "x-0123abcd"])
        with self.assertRaisesRegex(scratch.ProcessListUnreadable, "own argument"):
            # procps in a locale it does not have prints `?` for every non-ASCII byte of the caller's own arguments too
            scratch.check_readable(self.listed(argv=("release", "/w/" + "?" * 24)), ["release", "/w/Пользователь"])
        with self.assertRaisesRegex(scratch.ProcessListUnreadable, "does not show this process"):
            scratch.check_readable(self.listed()[1:], [])
        nfd = "/w/" + __import__("unicodedata").normalize("NFD", "Ünïcode")
        scratch.check_readable(self.listed(argv=("release", nfd)), ["release", "/w/Ünïcode"])

    @contextlib.contextmanager
    def system_probe(self, processes, locales):
        """`system_processes` on a Linux host whose ps prints `processes` and that has the locales `locales`."""
        from valtrion import unityguard
        procs = [unityguard.Proc(p.pid, p.ppid, p.command, p.command) for p in processes]
        with mock.patch.object(unityguard.SystemProbe, "processes", return_value=procs), \
                mock.patch.object(scratch, "_working_directories", return_value={}), \
                mock.patch.object(unityguard, "available_locales", return_value=locales), \
                mock.patch.object(core, "local_os", return_value="linux"), \
                mock.patch.object(sys, "argv", ["valtrion", "release", "x-0123abcd"]):
            yield

    def test_the_system_listing_reports_why_it_is_unreadable(self):
        with self.system_probe(self.listed("/bin/sleep �"), frozenset({"cutf8"})):
            source = scratch._Processes(None)
            self.assertIsNone(source.get())
        self.assertIn("not valid UTF-8", source.reason)

    def test_without_a_utf8_locale_a_non_ascii_folder_is_never_ruled_free(self):
        # glibc without C.UTF-8 or en_US.UTF-8: ps prints `?` for the folder's letters, so a holder would not be found.
        base = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(base.cleanup)
        manager = lease.LeaseManager(Path(base.name))
        for locales, folder, expected in ((frozenset({"c", "posix"}), "/w/Пользователь/scratch/x-0123abcd", "no UTF-8 locale"),
                                          (frozenset({"c", "posix"}), "/w/plain/scratch/x-0123abcd", None),
                                          (frozenset({"c", "cutf8"}), "/w/Пользователь/scratch/x-0123abcd", None)):
            with self.system_probe(self.listed("/bin/sleep 9 /w/" + "?" * 24 + "/scratch/x-0123abcd"), locales):
                reason = scratch.activity(Path(folder), {}, manager, scratch._Processes(None))
            if expected is None:
                self.assertIsNone(reason)
            else:
                self.assertIn(expected, reason)

    @unittest.skipUnless(os.name == "nt", "the console code page is a Windows matter")
    def test_the_process_probe_leaves_the_callers_console_code_page_alone(self):
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        before = kernel32.GetConsoleOutputCP()
        if before == 0:
            self.skipTest("this process has no console")
        self.assertIsNotNone(scratch.system_processes())
        self.assertEqual(before, kernel32.GetConsoleOutputCP())

    def test_a_path_ends_at_any_character_a_scratch_id_cannot_hold(self):
        needle = scratch._key(r"C:\w\scratch\x-1234abcd" if os.name == "nt" else "/w/scratch/x-1234abcd")
        sep = ";" if os.name == "nt" else ":"
        for text in (f"tool --paths={needle}{sep}C:\\other", f"tool {needle},C:\\o", f"tool ({needle})", f"tool {needle}"):
            self.assertTrue(scratch._mentions(text, [needle]), text)
        for text in (f"tool {needle}0", f"tool {needle}-old", f"tool {needle}.bak"):
            self.assertFalse(scratch._mentions(text, [needle]), text)


class SweepCallerAndTombstoneTests(ScratchBase):
    def test_the_sweep_counts_the_caller_itself_but_release_does_not(self):
        folder, marker = self.make()
        self.expire(folder)
        caller = SimpleNamespace(pid=os.getpid(), ppid=0, path="python", command=f"valtrion build --project-dir {folder}")
        report = self.sweep(processes=lambda: [caller])
        self.assertEqual([], report.removed)
        self.assertIn(f"pid {os.getpid()}", dict(report.kept)[str(folder)])
        self.release(marker["id"], processes=lambda: [caller])
        self.assertFalse(folder.exists())

    def test_the_sweep_keeps_a_folder_that_holds_or_lies_in_a_protected_path_or_the_working_directory(self):
        holds, _ = self.make("holds")
        inside, _ = self.make("inside")
        cwd, _ = self.make("cwd")
        for folder in (holds, inside, cwd):
            (folder / "checkout").mkdir()
            self.expire(folder)
        here = os.getcwd()
        os.chdir(cwd / "checkout")
        self.addCleanup(os.chdir, here)
        report = self.sweep(protected=[holds / "checkout", self.roots.scratch / inside.name])
        self.assertEqual([], report.removed)
        for folder in (holds, inside, cwd):
            self.assertIn("overlaps", dict(report.kept)[str(folder)])

    def interrupted(self, name="tmp"):
        folder, marker = self.make(name)
        tombstone = self.roots.scratch / f".removing-{marker['id']}-0123abcd"
        os.rename(folder, tombstone)
        return tombstone, marker

    def test_an_interrupted_removal_is_finished_by_release_or_sweep_and_never_reported_as_adoptable(self):
        first, one = self.interrupted("one")
        second, _ = self.interrupted("two")
        listed = {item["name"]: item for item in scratch.listing(self.roots)}
        self.assertEqual(one["id"], listed[first.name]["interruptedRemovalOf"])
        self.assertEqual([], scratch.unowned(self.roots, self.manager, no_processes), "a valid tombstone is valtrion's")
        self.assertEqual(str(first), self.release(one["id"]))
        self.assertFalse(first.exists())
        report = self.sweep()
        self.assertFalse(second.exists())
        self.assertIn("interrupted removal", report.removed[0])

    def test_an_interrupted_removal_still_passes_every_guard(self):
        tombstone, marker = self.interrupted()
        editor = SimpleNamespace(pid=999999, ppid=1, path="Unity", command=f"Unity -projectPath {tombstone}")
        with self.assertRaises(scratch.ScratchError):
            self.release(marker["id"], processes=with_me(editor))
        self.assertTrue(tombstone.is_dir())
        foreign = self.roots.scratch / ".removing-evil-0123abcd-0123abcd"
        foreign.mkdir()
        core.atomic_write_json(foreign / scratch.MARKER, {"marker": "other/1", "id": "evil-0123abcd"})
        self.sweep()
        self.assertTrue(foreign.is_dir(), "a tombstone without a valid marker is never removed")
        found = {item["name"]: item for item in scratch.unowned(self.roots, self.manager, no_processes)}
        hint = scratch.describe_unowned(found[f"scratch/{foreign.name}"])
        self.assertNotIn("adopt", hint)


class DeepAdoptGitTests(AdoptGitTests):
    def test_a_repository_two_levels_down_with_untracked_work_is_refused(self):
        stray = self.roots.root / "stray"
        repo = stray / "group" / "repo"
        git(["clone", "-q", str(self.source), str(repo)], self.base)
        (repo / "notes.txt").write_text("mine\n", encoding="utf-8")
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.adopt(self.roots, stray, self.me, max_age_hours=1, manager=self.manager, processes=no_processes)
        self.assertIn("uncommitted or untracked", str(caught.exception))
        self.assertTrue((repo / "notes.txt").is_file())

    def test_a_bare_repository_with_branches_on_no_remote_or_without_remote_is_refused(self):
        stray = self.roots.root / "stray"
        stray.mkdir()
        git(["clone", "-q", "--bare", str(self.source), str(stray / "bare.git")], self.base)
        git(["remote", "remove", "origin"], stray / "bare.git")
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.adopt(self.roots, stray, self.me, max_age_hours=1, manager=self.manager, processes=no_processes)
        self.assertIn("bare repository without any remote", str(caught.exception))
        git(["init", "-q", "--bare", str(stray / "empty.git")], self.base)
        git(["remote", "add", "origin", str(self.source)], stray / "bare.git")
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.adopt(self.roots, stray, self.me, max_age_hours=1, manager=self.manager, processes=no_processes)
        self.assertIn("on no remote", str(caught.exception))

    def test_a_tree_not_searched_within_the_budget_is_refused(self):
        self.assertIsNone(scratch.find_repositories(self.clone, budget_sec=-1))
        self.assertTrue(scratch.unsaved_work(self.clone, budget_sec=-1))


class ActiveJobTests(ScratchBase):
    def index_job(self, job_id: str, payload: dict) -> None:
        directory = self.base / "runs" / job_id
        directory.mkdir(parents=True)
        core.atomic_write_json(directory / "spec.json", {"jobId": job_id, "payload": payload})
        core.atomic_write_json(jobs.index_path(job_id, self.home), {"dir": str(directory)})

    def test_an_expired_copy_of_an_active_job_is_never_due(self):
        self.jobs["job-b"] = "running"
        folder, _ = self.make("b", job="job-b")
        self.expire(folder)
        report = self.sweep(active=["job-b"])
        self.assertEqual([], report.removed)
        self.assertTrue(folder.is_dir())
        self.assertIsNone(scratch.due(core.read_json(folder / scratch.MARKER), ["job-b"], time.time() + 10 ** 6))

    def test_the_folders_of_active_jobs_are_read_from_their_specs(self):
        project, cwd = str(self.base / "x" / "project"), str(self.base / "x" / "cwd")
        self.index_job("job-b", {"projectDir": project, "cwd": cwd})
        self.index_job("job-c", {})
        self.assertEqual([project, cwd], jobs.active_job_folders(self.home, ["job-b", "job-c", "job-missing"]))

    def test_the_cli_sweep_keeps_the_project_of_another_active_job(self):
        folder, _ = self.make("other")
        (folder / "checkout").mkdir()
        self.expire(folder)
        core.atomic_write_json(config.host_json_path(self.home), {"workRoot": str(self.roots.root)})
        self.index_job("job-b", {"projectDir": str(folder / "checkout")})
        with mock.patch.object(scratch, "system_processes", no_processes), \
                mock.patch.object(jobs, "active_job_ids", return_value=["job-b"]), \
                mock.patch.dict(os.environ, {"GDV_OWNER": "me@host"}):
            code, out, err = run_cli("workroot", "sweep")
        self.assertEqual(0, code, err)
        self.assertTrue(folder.is_dir(), out)
        self.assertIn("overlaps", out)

    def test_a_project_lease_deeper_than_the_search_is_seen_through_its_worktree(self):
        folder, marker = self.make("deep")
        project = folder / "checkout" / "a" / "b" / "c" / "Proj"
        (project / "ProjectSettings").mkdir(parents=True)
        held = self.manager.acquire(lease.unity_project_resource(project), "agent", "s1", 600, "build", os.getpid(), worktree=str(project))
        self.assertTrue(held.acquired)
        with self.assertRaises(scratch.ScratchError) as caught:
            self.release(marker["id"])
        self.assertIn("lease refers to it", str(caught.exception))

    def test_the_sweep_itself_protects_the_folders_of_active_jobs(self):
        folder, _ = self.make("b")
        (folder / "checkout").mkdir()
        self.expire(folder)
        report = self.sweep(active=["job-b"], job_folders_of=lambda ids: [str(folder / "checkout")] if "job-b" in ids else [])
        self.assertEqual([], report.removed)
        self.index_job("job-b", {"projectDir": str(folder / "checkout")})
        self.assertEqual([], self.sweep(active=["job-b"]).removed, "read from the job's spec by default")
        self.assertTrue(folder.is_dir())

    def test_a_project_lease_at_any_depth_is_found_without_a_worktree(self):
        folder, marker = self.make("deeper")
        project = folder / "checkout" / "a" / "b" / "c" / "d" / "Proj"
        (project / "ProjectSettings").mkdir(parents=True)
        self.manager.acquire(lease.unity_project_resource(project), "agent", "s1", 600, "build", os.getpid())
        with self.assertRaises(scratch.ScratchError) as caught:
            self.release(marker["id"])
        self.assertIn("lease refers to it", str(caught.exception))

    def test_a_tree_too_large_to_search_for_projects_counts_as_in_use(self):
        folder, marker = self.make("big")
        elsewhere = self.base / "OtherProject"
        self.manager.acquire(lease.unity_project_resource(elsewhere), "agent", "s1", 600, "by hand", os.getpid())  # no worktree
        with mock.patch.object(scratch.project_folders, "__defaults__", (-1, time.monotonic)):
            with self.assertRaises(scratch.ScratchError) as caught:
                self.release(marker["id"])
        self.assertIn("could not be searched", str(caught.exception))
        self.assertTrue(folder.is_dir())

    def test_without_a_held_project_lease_lacking_a_worktree_nothing_is_searched(self):
        folder, marker = self.make("quick")
        (self.roots.root / "stray").mkdir()
        recorded = self.base / "Recorded"
        self.manager.acquire(lease.unity_project_resource(recorded), "agent", "s1", 600, "job", os.getpid(), worktree=str(recorded))
        with mock.patch.object(scratch.project_folders, "__defaults__", (-1, time.monotonic)):
            entries = scratch.unowned(self.roots, self.manager, no_processes)
            self.assertEqual([], entries[0]["leases"], "no search, no false 'unknown'")
            self.release(marker["id"])
        self.assertFalse(folder.exists())

    def test_a_held_project_lease_under_temp_is_found(self):
        folder, marker = self.make("temp")
        project = folder / "checkout" / "Temp" / "Proj"
        (project / "ProjectSettings").mkdir(parents=True)
        self.manager.acquire(lease.unity_project_resource(project), "agent", "s1", 600, "build", os.getpid())
        with self.assertRaises(scratch.ScratchError):
            self.release(marker["id"])
        self.assertTrue(folder.is_dir())

    def test_an_active_job_with_a_broken_or_missing_spec_keeps_everything(self):
        folder, _ = self.make("x")
        self.expire(folder)
        for text in ("{not json", None):
            directory = self.base / "runs" / "job-b"
            directory.mkdir(parents=True, exist_ok=True)
            spec = directory / "spec.json"
            if text is None:
                if spec.exists():
                    os.replace(spec, directory / "spec.moved")
            else:
                spec.write_text(text, encoding="utf-8")
            core.atomic_write_json(jobs.index_path("job-b", self.home), {"dir": str(directory)})
            report = self.sweep(active=["job-b"])
            self.assertEqual([], report.removed)
            self.assertIn("nothing is swept", " ".join(reason for _, reason in report.kept))
            self.assertTrue(folder.is_dir())
        self.assertEqual(1, len(self.sweep(active=["job-gone"]).removed), "an id without an index entry has ended: skipped")

    def test_a_relative_job_cwd_resolves_against_the_jobs_own_folder(self):
        folder, _ = self.make("rel")
        (folder / "checkout").mkdir()
        self.expire(folder)
        run_dir = self.base / "runs" / "job-r"
        run_dir.mkdir(parents=True)
        relative = os.path.relpath(folder / "checkout", run_dir)
        core.atomic_write_json(run_dir / "spec.json", {"jobId": "job-r", "payload": {"cwd": relative}})
        core.atomic_write_json(jobs.index_path("job-r", self.home), {"dir": str(run_dir)})
        self.assertEqual([], self.sweep(active=["job-r"]).removed)
        self.assertTrue(folder.is_dir())

    def test_an_expired_copy_of_an_active_job_says_why_it_stays(self):
        self.jobs["job-a"] = "running"
        folder, _ = self.make("why", job="job-a")
        self.expire(folder)
        self.assertEqual("its job job-a is still active", dict(self.sweep(active=["job-a"]).kept)[str(folder)])

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_a_chmod_failure_during_adopt_is_a_warning_and_nothing_is_lost(self):
        stray = self.roots.root / "stray"
        stray.mkdir()
        warnings = []
        with mock.patch.object(scratch.os, "chmod", side_effect=PermissionError("no modes here")):
            folder, marker = scratch.adopt(self.roots, stray, self.me, max_age_hours=1, manager=self.manager,
                                           processes=no_processes, warnings=warnings)
        self.assertTrue((folder / scratch.MARKER).is_file())
        self.assertIn("owner-only", warnings[0])

    def test_the_job_runner_records_the_project_folder_on_its_project_lease(self):
        project = self.base / "proj"
        keeper = mock.Mock()
        runner = SimpleNamespace(spec={"leaseTtlSec": 60}, keeper=keeper, leases=self.manager, home=self.home, job_id="job-x",
                                 events=mock.Mock(), taken={}, _waited_sec=0.0, _wait_since=None)
        reason = jobs._Runner.acquire_one(runner, {"resource": lease.unity_project_resource(project), "worktree": str(project)})
        self.assertIsNone(reason)
        status = self.manager.status(lease.unity_project_resource(project))
        self.assertEqual(str(project), status.record.worktree)

    def test_nfd_and_nfc_spellings_match_on_macos(self):
        import unicodedata
        nfc = unicodedata.normalize("NFC", "/Users/й/work/scratch/x-0123abcd")
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfc, nfd)
        with mock.patch.object(scratch.core, "local_os", return_value="macos"):
            self.assertTrue(scratch._mentions(f"tool {nfd}", [scratch._key(nfc)]))

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX modes")
    def test_an_adopted_folder_is_owner_only(self):
        stray = self.roots.root / "stray"
        stray.mkdir(mode=0o755)
        os.chmod(stray, 0o755)
        folder, _ = scratch.adopt(self.roots, stray, self.me, max_age_hours=1, manager=self.manager, processes=no_processes)
        self.assertEqual(0o700, os.stat(folder).st_mode & 0o777)


class ConfigAndCliTests(ScratchBase):
    def test_host_json_scratch_max_age_hours(self):
        self.assertEqual(config.DEFAULT_SCRATCH_MAX_AGE_HOURS, config.load_host_settings(self.home).scratch_max_age_hours)
        core.atomic_write_json(config.host_json_path(self.home), {"scratch": {"maxAgeHours": 6}})
        self.assertEqual(6.0, config.load_host_settings(self.home).scratch_max_age_hours)
        for bad in ({"maxAgeHours": 0}, {"maxAgeHours": True}, {"maxAgeHours": "1"}, {"other": 1}, [], 5):
            core.atomic_write_json(config.host_json_path(self.home), {"scratch": bad})
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.load_host_settings(self.home)

    def test_create_list_and_release_through_the_cli_without_any_manual_deletion(self):
        core.atomic_write_json(config.host_json_path(self.home), {"workRoot": str(self.roots.root), "scratch": {"maxAgeHours": 2}})
        with mock.patch.object(scratch, "system_processes", no_processes), mock.patch.dict(os.environ, {"GDV_OWNER": "agent-a"}):
            code, out, err = run_cli("workroot", "scratch", "create", "--name", "repro")
            self.assertEqual(0, code, err)
            created = json.loads(out)
            folder = Path(created["scratch"])
            self.assertTrue(folder.is_dir())
            self.assertEqual(f"valtrion workroot scratch release {created['id']}", created["release"])
            (folder / "clone").mkdir()
            code, out, _ = run_cli("workroot", "scratch", "list")
            listed = json.loads(out)["directories"]
            self.assertEqual([created["id"]], [item["id"] for item in listed])
            self.assertEqual("agent-a", listed[0]["owner"])
            with mock.patch.dict(os.environ, {"GDV_OWNER": "agent-b"}):
                code, _, err = run_cli("workroot", "scratch", "release", created["id"])
                self.assertNotEqual(0, code)
                self.assertIn("--any-owner", err)
            code, out, err = run_cli("workroot", "scratch", "release", created["id"])
            self.assertEqual(0, code, err)
            self.assertEqual([str(folder)], json.loads(out)["removed"])
            self.assertFalse(folder.exists())
            code, _, err = run_cli("workroot", "scratch", "release", "../outside")
            self.assertNotEqual(0, code)
            self.assertIn("not a scratch id", err)

    def test_adopt_needs_yes_on_the_cli(self):
        (self.roots.root / "stray").mkdir()
        core.atomic_write_json(config.host_json_path(self.home), {"workRoot": str(self.roots.root)})
        with mock.patch.object(scratch, "system_processes", no_processes):
            code, _, err = run_cli("workroot", "adopt", "stray")
            self.assertNotEqual(0, code)
            self.assertIn("--yes", err)
            self.assertTrue((self.roots.root / "stray").is_dir())
            code, out, err = run_cli("workroot", "adopt", "stray", "--yes")
            self.assertEqual(0, code, err)
            self.assertTrue(Path(json.loads(out)["scratch"]).is_dir())


if __name__ == "__main__":
    unittest.main()
