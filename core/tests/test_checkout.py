"""Managed build checkouts and the commit check at build time (`checkout.py`, issue #15).

`CheckoutTests` use a real git repository in a temporary folder; `QueuedJobsTests` run jobs with the fake adapter of
`test_pipeline_leases` that share one folder."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from unittest import mock

from valtrion import cli  # noqa: F401 - cli must be imported before commands
from valtrion import checkout, commands, compare, config, core, diskspace, doctor, incremental, jobs, lease, providers
from tests import test_pipeline_leases as leases

HAVE_GIT = shutil.which("git") is not None
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(folder, *arguments):
    done = subprocess.run(["git", *arguments], cwd=folder, capture_output=True, text=True, check=True, env={**os.environ, **IDENTITY})
    return done.stdout.strip()


def commit(folder, name, text):
    (Path(folder) / name).parent.mkdir(parents=True, exist_ok=True)
    (Path(folder) / name).write_text(text, encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", f"{name} {text}")
    return git(folder, "rev-parse", "HEAD")


def make_repo(folder):
    """A Unity-project-shaped repository with two commits; returns (first, second)."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q", "-b", "main")
    git(folder, "config", "core.autocrlf", "false")
    commit(folder, "ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.6.7f1\n")
    first = commit(folder, "Assets/data.txt", "one\n")
    second = commit(folder, "Assets/data.txt", "two\n")
    return first, second


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class CheckoutTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.origin = self.base / "origin"
        self.first, self.second = make_repo(self.origin)
        self.repo = self.base / "checkout"
        git(self.base, "clone", "-q", str(self.origin), str(self.repo))

    def head(self, folder=None):
        return git(folder or self.repo, "rev-parse", "HEAD")

    def test_a_checkout_is_reset_to_the_commit_detached_and_clean(self):
        (self.repo / "Assets" / "data.txt").write_text("edited", encoding="utf-8")
        (self.repo / "Assets" / "leftover.txt").write_text("untracked", encoding="utf-8")
        result = checkout.ensure(self.repo, self.first, compare.run_git)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual((True, self.second, self.first), (result.switched, result.previous, self.head()))
        self.assertEqual("one\n", (self.repo / "Assets" / "data.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / "Assets" / "leftover.txt").exists())
        self.assertEqual("HEAD", git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "detached")
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        self.assertEqual({"folder": str(self.repo), "sha": self.first, "switched": True, "fetched": False, "id": "queue",
                          "previousSha": self.second}, result.describe("queue"))

    def test_a_checkout_already_at_the_commit_is_not_switched(self):
        result = checkout.ensure(self.repo, self.second, compare.run_git)
        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.switched)

    def test_the_folders_the_run_keeps_survive_the_clean_even_with_glob_characters(self):
        for name in ("keep", "work [x]/j", "#hash", "bang!"):
            kept = self.repo / name
            kept.mkdir(parents=True)
            (kept / "run.json").write_text("{}", encoding="utf-8")
        gone = self.repo / "Assets" / "leftover.txt"
        gone.write_text("untracked", encoding="utf-8")
        keep = [self.repo / name for name in ("keep", "work [x]/j", "#hash", "bang!")]
        result = checkout.ensure(self.repo, self.first, compare.run_git, keep=keep)
        self.assertTrue(result.ok, result.reason)
        for path in keep:
            self.assertTrue((path / "run.json").exists(), f"{path} is kept")
        self.assertFalse(gone.exists())

    def test_ignored_files_survive_but_stray_build_output_is_removed(self):
        target = commit(self.origin, ".gitignore", "Library/\nBuilds/\n")
        git(self.repo, "pull", "-q")
        library = self.repo / "Library"
        library.mkdir()
        (library / "cache.bin").write_text("x", encoding="utf-8")
        for name in ("iOS", "iOS-cancelled-1"):
            (self.repo / "Builds" / name).mkdir(parents=True)
            (self.repo / "Builds" / name / "big.bin").write_text("x", encoding="utf-8")
        result = checkout.ensure(self.repo, target, compare.run_git)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue((library / "cache.bin").exists(), "the Library cache is not thrown away")
        self.assertFalse((self.repo / "Builds").exists() and list((self.repo / "Builds").iterdir()), "Builds/* is removed although it is ignored")
        self.assertTrue(result.removed, "the removal is reported")

    def test_a_missing_checkout_is_created_from_the_remote(self):
        created = self.base / "pool" / "queue"
        result = checkout.ensure(created, self.first, compare.run_git, remote=str(self.origin))
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.created)
        self.assertEqual(self.first, self.head(created))
        self.assertTrue(result.describe("queue")["created"])

    def test_a_missing_checkout_without_a_remote_fails_with_the_reason(self):
        result = checkout.ensure(self.base / "pool" / "queue", self.first, compare.run_git)
        self.assertFalse(result.ok)
        self.assertIn("no remote", result.reason)

    def test_a_folder_that_is_in_the_way_is_not_overwritten(self):
        taken = self.base / "taken"
        taken.mkdir()
        (taken / "mine.txt").write_text("x", encoding="utf-8")
        result = checkout.ensure(taken, self.first, compare.run_git, remote=str(self.origin))
        self.assertFalse(result.ok)
        self.assertTrue((taken / "mine.txt").exists())

    def test_a_missing_commit_is_fetched_from_the_remote(self):
        newer = commit(self.origin, "Assets/data.txt", "three\n")
        result = checkout.ensure(self.repo, newer, compare.run_git)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.fetched)
        self.assertEqual(newer, self.head())

    def test_an_unknown_commit_fails_with_a_reason_and_changes_nothing(self):
        result = checkout.ensure(self.repo, "f" * 40, compare.run_git)
        self.assertFalse(result.ok)
        self.assertIn("could not be fetched", result.reason)
        self.assertEqual(self.second, self.head())

    def test_a_cancelled_job_does_not_start_the_checkout(self):
        result = checkout.ensure(self.repo, self.first, compare.run_git, cancelled=lambda: True)
        self.assertFalse(result.ok)
        self.assertEqual(self.second, self.head())

    def test_lfs_is_pulled_when_any_gitattributes_of_the_repository_uses_it(self):
        calls = []

        def spy(folder, arguments):
            calls.append(list(arguments))
            return (0, "") if arguments[:1] == ["lfs"] else compare.run_git(folder, arguments)

        checkout.ensure(self.repo, self.first, spy)
        self.assertNotIn(["lfs", "pull"], calls)
        uses_lfs = commit(self.origin, "Assets/.gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text"+chr(10))  # below the root
        commit(self.origin, "Assets/more.txt", "x")
        calls.clear()
        result = checkout.ensure(self.repo, uses_lfs, spy)
        self.assertTrue(result.ok, result.reason)
        self.assertIn(["lfs", "pull"], calls)

    def test_an_unmanaged_folder_is_only_verified_naming_both_commits(self):
        self.assertIsNone(checkout.verify_head(self.repo, self.second, compare.run_git))
        problem = checkout.verify_head(self.repo, self.first, compare.run_git)
        self.assertIn(self.second[:12], problem)
        self.assertIn(self.first[:12], problem)
        self.assertEqual(self.second, self.head(), "nothing was switched")

    def test_host_json_projects_are_read_and_checked(self):
        home = self.base / "home"
        home.mkdir()
        document = {"projects": {"Game": {"remote": "git@example.com:o/g.git", "unityPath": "Unity",
                                          "checkouts": [{"id": "queue", "path": str(self.repo)}, {"id": "reference", "path": str(self.origin)}],
                                          "default": "queue"}}}
        (home / "host.json").write_text(json.dumps(document), encoding="utf-8")
        project = config.load_host_settings(home).project("Game")
        self.assertEqual(["queue", "reference"], [c.id for c in project.checkouts])
        self.assertEqual("reference", project.pick("reference").id)
        self.assertEqual("queue", project.pick(None).id)
        self.assertIsNone(project.pick("nope"))
        self.assertEqual(self.repo / "Unity", project.project_dir(project.pick("queue")))
        for broken in ({"Game": {"checkouts": []}}, {"Game": {"checkouts": [{"id": "a b", "path": str(self.repo)}]}},
                       {"Game": {"checkouts": [{"id": "a", "path": "relative"}]}},
                       {"Game": {"checkouts": [{"id": "a", "path": str(self.repo)}, {"id": "a", "path": str(self.origin)}]}},
                       {"Game": {"checkouts": [{"id": "a", "path": str(self.repo)}], "unityPath": "../x"}},
                       {"Game": {"checkouts": [{"id": "a", "path": str(self.repo)}], "default": "z"}}, []):
            (home / "host.json").write_text(json.dumps({"projects": broken}), encoding="utf-8")
            with self.assertRaises(config.ConfigError, msg=broken):
                config.load_host_settings(home)

    def test_disk_cleanup_removes_only_idle_incremental_folders(self):
        home = self.base / "home"
        home.mkdir()
        manager = lease.LeaseManager(home)
        idle_lease, busy_lease = "unity-project:idle", "unity-project:busy"
        for name, resource in (("idle", idle_lease), ("busy", busy_lease)):
            plan = incremental.Plan(incremental.folder_of(home, "Game", name), "k")
            incremental.prepare(plan, resource)
            (plan.folder / "DerivedData").mkdir(exist_ok=True)
            (plan.folder / "DerivedData" / "big.o").write_text("x" * 1000, encoding="utf-8")
            incremental.commit(plan)
        manager.acquire(busy_lease, "me", "s", 600, "build", os.getpid())
        removed = diskspace.release_idle_incremental(home)
        self.assertEqual(1, len(removed))
        self.assertIn("idle", removed[0])
        self.assertFalse((incremental.folder_of(home, "Game", "idle") / "DerivedData").exists())
        self.assertTrue((incremental.folder_of(home, "Game", "busy") / "DerivedData" / "big.o").exists())

    def test_doctor_lists_managed_checkouts_and_foreign_project_leases(self):
        home = self.base / "home"
        home.mkdir()
        project = config.ManagedProject("Game", (config.ManagedCheckout("queue", self.repo), config.ManagedCheckout("new", self.base / "none")))
        manager = lease.LeaseManager(home)
        stranger = self.base / "elsewhere"
        stranger.mkdir()
        manager.acquire(lease.unity_project_resource(stranger), "someone", "s1", 600, "editor run", os.getpid())
        manager.acquire(lease.unity_project_resource(self.repo), "me", "s2", 600, "valtrion build", os.getpid())
        report = doctor.checkouts_report([project], home)
        by_id = {item["id"]: item for item in report["managed"]}
        self.assertEqual((self.second, True), (by_id["queue"]["commit"], by_id["queue"]["exists"]))
        self.assertEqual("me", by_id["queue"]["lease"]["owner"])
        self.assertFalse(by_id["new"]["exists"])
        self.assertEqual(["someone"], [item["owner"] for item in report["unmanagedLeases"]])


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class PreflightTests(unittest.TestCase):
    """What is found at submit, before the job waits for any lease (`commands._preflight`)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.origin = self.base / "origin"
        self.first, self.second = make_repo(self.origin)
        self.repo = self.base / "queue"
        git(self.base, "clone", "-q", str(self.origin), str(self.repo))
        providers.register("android", leases.adapter_class(leases.World(self.base)))
        self.addCleanup(providers.unregister, "android")
        (self.base / "plans").mkdir()
        (self.base / "plans" / "open.plan.json").write_text(json.dumps(leases.PLAN), encoding="utf-8")

    def pipeline(self, sha, stages=None):
        return {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": sha, "profile": "Validation"},
                "device": {"platform": "android"}, "stages": stages or [{"stage": "build"}]}

    def payload(self, root=None, remote=None):
        return {"checkoutRoot": str(root or self.repo), "checkoutRemote": remote, "projectDir": str(root or self.repo)}

    def check(self, pipeline, payload, project=True):
        return commands._preflight(pipeline, self.base / "plans", payload, self.repo if project else None)  # noqa: SLF001

    def test_a_good_job_passes(self):
        self.assertIsNone(self.check(self.pipeline(self.first), self.payload()))

    def test_a_commit_missing_locally_is_fetched_at_submit(self):
        newer = commit(self.origin, "Assets/data.txt", "three\n")
        self.assertIsNone(self.check(self.pipeline(newer), self.payload()))
        self.assertEqual(newer, git(self.repo, "rev-parse", newer))

    def test_a_commit_that_is_not_on_the_remote_is_blocked_at_submit(self):
        problem = self.check(self.pipeline("e" * 40), self.payload())
        self.assertIn("not on the remote", problem)

    def test_a_checkout_that_does_not_exist_yet_needs_a_remote_that_answers(self):
        missing = self.base / "pool" / "new"
        self.assertIn("no remote", self.check(self.pipeline(self.first), self.payload(missing)))
        self.assertIn("cannot be reached", self.check(self.pipeline(self.first), self.payload(missing, str(self.base / "nowhere"))))
        self.assertIsNone(self.check(self.pipeline(self.first), self.payload(missing, str(self.origin))))

    def test_an_unreadable_or_invalid_plan_is_found_at_submit(self):
        stages = [{"stage": "build"}, {"stage": "run", "plan": "missing.plan.json", "args": {"app": "a"}}]
        self.assertIn("cannot read the plan", self.check(self.pipeline(self.first, stages), self.payload()))

    def test_a_build_without_a_project_is_found_at_submit(self):
        self.assertIn("--project-dir", self.check(self.pipeline(self.first), {}, project=False))


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class QueuedJobsTests(unittest.TestCase):
    """Jobs of different commits that share one folder, run by the pipeline with a fake adapter."""
    # The harness of the lease tests, borrowed without running their tests.
    setUp_lease = vars(leases.ScopedLeaseTests)["setUp"]
    open_all_gates = vars(leases.ScopedLeaseTests)["open_all_gates"]
    wait_for = vars(leases.ScopedLeaseTests)["wait_for"]
    events_of = vars(leases.ScopedLeaseTests)["events_of"]
    document = vars(leases.ScopedLeaseTests)["document"]
    full_stages = vars(leases.ScopedLeaseTests)["full_stages"]

    def setUp(self):
        self.setUp_lease()
        self.origin = self.base / "origin"
        self.first, self.second = make_repo(self.origin)
        self.shared = self.base / "pool" / "queue"
        git(self.base, "clone", "-q", str(self.origin), str(self.shared))
        self.builds = []
        self.world.on_build = self.on_build
        self.world.gate("queue", "build").set()  # the label of every job here: the folder's name
        self.world.gate("queue", "launch").set()

    def on_build(self, label, project, job):
        holders = [r for r in self.world.held() if r.startswith("unity-project:")]
        self.builds.append((job, git(project, "rev-parse", "HEAD"), holders))

    def project(self, label):
        return self.shared

    def run_job(self, label, document, managed=True, remote=None, root=None, lease_wait=60.0):
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "baseDir": str(self.base / "plans"), "devicePollSec": 0.02,
                   "projectDir": str(root or self.shared), "shaPinned": True}
        if managed:
            payload.update({"checkoutId": "queue", "checkoutRoot": str(root or self.shared), "checkoutRemote": remote})
        request = jobs.JobRequest(
            kind="pipeline", project="Sample", sha=document["candidate"]["sha"], label=label, payload=payload,
            host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=120, owner="tester",
            lease_wait_sec=lease_wait, tick_sec=0.1)
        info = jobs.start_job(request, self.root, self.home, foreground=True)
        return info, jobs.JobStore(Path(info["dir"]))

    def build_only(self, sha):
        return self.document(stages=[{"stage": "build"}], sha=sha)

    def head(self):
        return git(self.shared, "rev-parse", "HEAD")

    def test_queued_jobs_on_different_commits_each_build_their_own_commit_under_the_lease(self):
        results = {}

        def go(label, sha):
            results[label] = self.run_job(label, self.build_only(sha))

        threads = [threading.Thread(target=go, args=(label, sha), daemon=True) for label, sha in (("A", self.first), ("B", self.second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(leases.WAIT)
        self.assertEqual({"A", "B"}, set(results), "both jobs finished")
        self.assertEqual({self.first, self.second}, {sha for _, sha, _ in self.builds}, self.builds)
        self.assertTrue(all(holders for _, _, holders in self.builds), "the project lease is held while the build runs")
        for label, sha in (("A", self.first), ("B", self.second)):
            _, store = results[label]
            self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
            result = core.read_json(store.result)
            self.assertEqual(sha, result["candidate"]["sha"])
            self.assertFalse(result["candidate"]["dirty"], "the tree is read again after the checkout")
            self.assertEqual(("queue", sha, str(self.shared)), (result["checkout"]["id"], result["checkout"]["sha"], result["checkout"]["folder"]))
            self.assertEqual([sha], [head for job, head, _ in self.builds if label in job], f"job {label} built the commit it asked for")
            self.assertTrue(any(e["type"] == "log" and "reset to" in e["message"] for e in self.events_of(store)) or
                            result["checkout"]["switched"] is False)

    def test_builds_in_one_managed_checkout_share_one_persistent_folder_until_the_key_changes(self):
        self.run_job("A", self.build_only(self.first))
        self.run_job("B", self.build_only(self.second))
        self.assertEqual(2, len(self.world.plans))
        (_, first, lease_a), (_, second, _) = self.world.plans
        self.assertEqual(self.home / "incremental" / "Sample" / "queue", first.folder)
        self.assertEqual((first.folder, first.key, False), (second.folder, second.key, second.clean),
                         "a different commit of the same toolchain and profile keeps the folder")
        self.assertTrue(lease_a.startswith("unity-project:"))
        self.world.toolchain["xcode"] = "17.0"
        self.run_job("C", self.build_only(self.second))
        self.assertNotEqual(first.key, self.world.plans[2][1].key, "a new toolchain is a new key (a clean rebuild)")
        stage = [{"stage": "build", "args": {"clean": True, "reuse": False}}]
        self.run_job("D", self.document(stages=stage, sha=self.second))
        self.assertTrue(self.world.plans[3][1].clean, "the build stage can ask for a clean rebuild")

    def test_an_unmanaged_build_gets_no_persistent_folder(self):
        self.run_job("A", self.build_only(self.second), managed=False)
        self.assertEqual([None], [plan for _, plan, _ in self.world.plans])

    def test_a_dirty_managed_checkout_is_reset_not_refused(self):
        (self.shared / "Assets" / "data.txt").write_text("edited", encoding="utf-8")
        _, store = self.run_job("A", self.build_only(self.first))
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertEqual([self.first], [sha for _, sha, _ in self.builds])

    def test_a_missing_managed_checkout_is_created_by_the_build_stage(self):
        created = self.base / "pool" / "reference"
        _, store = self.run_job("A", self.build_only(self.first), remote=str(self.origin), root=created)
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertEqual(self.first, git(created, "rev-parse", "HEAD"))
        self.assertTrue(core.read_json(store.result)["checkout"]["created"])

    def test_an_unmanaged_folder_whose_head_moved_blocks_naming_both_commits(self):
        _, store = self.run_job("A", self.build_only(self.first), managed=False)
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn(self.first[:12], job["reason"])
        self.assertIn(self.second[:12], job["reason"])
        self.assertEqual([], self.builds)
        self.assertEqual(self.second, self.head(), "an unmanaged folder is never switched")

    def test_an_unmanaged_folder_at_the_commit_builds_and_reports_it(self):
        _, store = self.run_job("A", self.build_only(self.second), managed=False)
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        record = core.read_json(store.result)["checkout"]
        self.assertEqual((self.second, False), (record["sha"], record["switched"]))
        self.assertNotIn("id", record)

    def test_an_unknown_commit_blocks_fast_and_releases_the_leases(self):
        _, store = self.run_job("A", self.build_only("e" * 40))
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("fetched", job["reason"])
        self.assertEqual([], self.builds)
        self.assertEqual([], self.world.held())

    def test_a_managed_checkout_needs_a_build_stage(self):
        stages = [{"stage": "install"}, {"stage": "run", "plan": "open.plan.json", "args": {"app": "a", "stallSec": 5}, "timeoutSec": 30}]
        _, store = self.run_job("A", self.document(stages=stages, sha=self.first))
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("build stage", store.read_job()["reason"])

    def test_a_pinned_pipeline_without_a_build_stage_still_needs_the_commit_checked_out(self):
        stages = [{"stage": "install"}, {"stage": "run", "plan": "open.plan.json", "args": {"app": "a", "stallSec": 5}, "timeoutSec": 30}]
        _, store = self.run_job("A", self.document(stages=stages, sha=self.first), managed=False)
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("--sha must name the commit that is checked out", store.read_job()["reason"])


if __name__ == "__main__":
    unittest.main()
