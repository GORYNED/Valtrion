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

from valtrion import cli, config, core, diskspace, doctor, lease, scratch, workroot

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


def no_processes():
    return []


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

    def make(self, name="tmp", hours=24.0, who=None, **extra):
        folder, marker = scratch.create(self.roots, name, who or self.me, max_age_hours=hours, **extra)
        (folder / "work.txt").write_text("x\n", encoding="utf-8")
        return folder, marker

    def release(self, scratch_id, **options):
        options.setdefault("manager", self.manager)
        options.setdefault("processes", no_processes)
        return scratch.release(self.roots, scratch_id, self.me, **options)

    def sweep(self, active=(), idle=HOUR, **options):
        options.setdefault("manager", self.manager)
        options.setdefault("processes", no_processes)
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
        self.assertRefused(marker["id"], "pid 999999", processes=lambda: [editor])
        self.assertTrue(folder.is_dir())

    def test_a_process_naming_a_longer_sibling_does_not_count(self):
        folder, marker = self.make()
        sibling = SimpleNamespace(pid=999999, ppid=1, path="x", command=f"tool {folder}0-more")
        self.release(marker["id"], processes=lambda: [sibling])
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
        report = self.sweep(processes=lambda: [editor])
        self.assertEqual([], report.removed)
        self.assertTrue(leased.is_dir() and busy.is_dir())
        reasons = dict(report.kept)
        self.assertIn("lease", reasons[str(leased)])
        self.assertIn("pid 999999", reasons[str(busy)])
        self.assertEqual([], self.sweep(processes=lambda: None).removed, "an unreadable process list keeps everything")

    def test_a_folder_of_an_ended_job_goes_once_idle_and_one_of_a_running_job_stays(self):
        ended, _ = self.make("ended", job="job-1")
        running, _ = self.make("running", job="job-2")
        age(ended, 2 * HOUR)
        age(running, 2 * HOUR)
        report = self.sweep(active=["job-2"])
        self.assertFalse(ended.exists())
        self.assertTrue(running.is_dir())
        self.assertIn("job job-1 has ended", report.removed[0])

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
        for name in ("jobs", "mirrors", "launchd", "tools"):
            (self.roots.root / name).mkdir()
        return first, second

    def test_unowned_entries_are_listed_with_size_age_and_users_and_never_swept(self):
        first, second = self.real_case()
        age(first, 5 * HOUR)
        self.make("valid")
        (self.roots.scratch / "nomarker").mkdir()
        editor = SimpleNamespace(pid=4242, ppid=1, path="Unity", command=f"Unity -projectPath {second}")
        found = {item["name"]: item for item in scratch.unowned(self.roots, self.manager, lambda: [editor])}
        self.assertEqual({"vcheck-2114bfc", "sdk-clean-553", "scratch/nomarker"}, set(found))
        self.assertEqual(2048, found["vcheck-2114bfc"]["bytes"])
        self.assertGreaterEqual(found["vcheck-2114bfc"]["ageHours"], 4.9)
        self.assertEqual([], found["vcheck-2114bfc"]["processes"])
        self.assertEqual([4242], found["sdk-clean-553"]["processes"])
        self.assertIn("is not a scratch id", found["scratch/nomarker"]["reason"])
        (self.roots.scratch / "nomarker-0123abcd").mkdir()
        found = {item["name"]: item for item in scratch.unowned(self.roots, self.manager, lambda: [editor])}
        self.assertIn("no Valtrion scratch marker", found["scratch/nomarker-0123abcd"]["reason"])
        self.assertIn("never removes", scratch.describe_unowned(found["vcheck-2114bfc"]))
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
            scratch.adopt(self.roots, first, self.me, max_age_hours=1, manager=self.manager, processes=lambda: [editor])
        self.assertIn("in use", str(caught.exception))
        self.assertTrue(first.is_dir())
        self.assertTrue((self.outside / "precious.txt").is_file())


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
