"""Free-space check before a build: cleanup under pressure, BLOCKED when it is not enough, early release of build output, doctor facts."""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from valtrion import artifacts, buildcache, config, core, diskspace, doctor, jobs, pipeline, providers, schema, workroot
from tests.test_pipeline_leases import PLAN, World, adapter_class  # noqa: E402

GIB = diskspace.GIB


class FakeVolume:
    """A volume with a fixed capacity: free space is capacity minus the bytes the tests mark as used, and removing a registered
    folder frees what it was declared to hold. It reads the real folders only to know whether they still exist."""

    def __init__(self, free_gib: float) -> None:
        self.free = free_gib * GIB
        self.sizes: dict[Path, float] = {}

    def hold(self, path: Path, gib: float) -> None:
        self.sizes[path] = gib * GIB

    def __call__(self, _path: Path) -> int:
        reclaimed = sum(size for path, size in self.sizes.items() if not path.exists())
        return int(self.free + reclaimed)


class DiskTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.home, self.root, self.work = self.base / "home", self.base / "artifacts", self.base / "work"
        self.home.mkdir()
        (self.home / "host.json").write_text(json.dumps({
            "artifactsRoot": str(self.root), "workRoot": str(self.work), "disk": {"minFreeGiB": 10, "keepDaysUnderPressure": 1}}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.volume = FakeVolume(5)  # below the 10 GiB minimum
        self.settings = config.load_host_settings(self.home)

    def old(self, path: Path, days: float = 3) -> None:
        stamp = time.time() - days * 86400
        for item in [path, *path.rglob("*")]:
            os.utime(item, (stamp, stamp))

    def abandoned_job(self, name: str, gib: float, age_days: float = 1) -> Path:
        folder = self.work / "jobs" / name
        (folder / "build").mkdir(parents=True)
        (folder / "build" / "big.bin").write_bytes(b"x")
        self.old(folder, age_days)
        self.volume.hold(folder, gib)
        return folder

    def run_folder(self, name: str, gib: float, age_days: float, state: str = "finished") -> Path:
        folder = self.root / "Sample" / "abcdef12" / name
        folder.mkdir(parents=True)
        core.atomic_write_json(folder / "job.json", {"state": state})
        (folder / "result.json").write_text("{}", encoding="utf-8")
        self.old(folder, age_days)
        self.volume.hold(folder, gib)
        return folder

    def expired_scratch(self, name: str, gib: float) -> Path:
        from valtrion import scratch
        folder, _ = scratch.create(self.settings.roots, name, scratch.caller(), max_age_hours=1)
        (folder / "checkout" / "Assets").mkdir(parents=True)
        (folder / "checkout" / "Assets" / "a.cs").write_text("x\n", encoding="utf-8")
        marker = core.read_json(folder / scratch.MARKER)
        marker["expiresUtc"] = core.format_utc(core.now() - timedelta(hours=5))
        core.atomic_write_json(folder / scratch.MARKER, marker)
        self.old(folder, 1)
        self.volume.hold(folder, gib)
        return folder

    def test_the_scratch_step_removes_an_expired_copy_but_never_the_one_the_job_builds_in(self):
        from valtrion import scratch
        building = self.expired_scratch("building", 4)
        protected = self.expired_scratch("protected", 4)
        idle = self.expired_scratch("idle", 4)
        me = [mock.Mock(pid=os.getpid(), ppid=0, path="python", command="python", cwd=None)]
        with mock.patch.object(scratch, "system_processes", lambda: me):
            outcome = diskspace.ensure_free(self.settings, self.home, protected=[str(protected / "checkout" / "Assets")],
                                            free=self.volume, busy=[str(building / "checkout")])
        self.assertFalse(idle.exists(), outcome.removed)
        self.assertTrue(building.is_dir(), "the project the running job builds in stays")
        self.assertTrue(protected.is_dir(), "a scratch copy holding a protected path stays")
        self.assertTrue(any("scratch" in item for item in outcome.removed))

    def test_the_scratch_step_keeps_a_copy_the_calling_process_runs_in(self):
        from valtrion import scratch
        folder = self.expired_scratch("mine", 4)
        me = [mock.Mock(pid=os.getpid(), ppid=0, path="python", command=f"valtrion build --project-dir {folder / 'checkout'}", cwd=None)]
        with mock.patch.object(scratch, "system_processes", lambda: me):
            diskspace.ensure_free(self.settings, self.home, free=self.volume)
        self.assertTrue(folder.is_dir(), "the sweep counts the caller itself as a user")

    def test_nothing_runs_without_a_threshold(self):
        (self.home / "host.json").write_text(json.dumps({"workRoot": str(self.work)}), encoding="utf-8")
        outcome = diskspace.ensure_free(config.load_host_settings(self.home), self.home, free=self.volume)
        self.assertEqual((True, ()), (outcome.ok, outcome.removed))

    def test_enough_space_cleans_nothing(self):
        stale = self.abandoned_job("20261101T000000Z-old", 20)
        outcome = diskspace.ensure_free(self.settings, self.home, free=FakeVolume(50))
        self.assertTrue(outcome.ok)
        self.assertTrue(stale.exists())

    def test_low_space_removes_abandoned_folders_and_expired_runs_but_not_active_or_protected_ones(self):
        abandoned = self.abandoned_job("20261101T000000Z-dead", 4)
        recent = self.run_folder("20261101T000000Z-recent", 1, age_days=0.2)
        expired = self.run_folder("20261031T000000Z-expired", 4, age_days=3)
        protected = self.run_folder("20261031T000001Z-protected", 3, age_days=3)
        running = self.run_folder("20261031T000002Z-running", 3, age_days=3, state="running")
        retained = self.run_folder("20261031T000003Z-retained", 3, age_days=3)
        artifacts.mark_retained(retained)
        self.old(retained, 3)
        outcome = diskspace.ensure_free(self.settings, self.home, protected=[str(protected)], free=self.volume)
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertFalse(abandoned.exists())
        self.assertFalse(expired.exists())
        for kept in (recent, protected, running, retained):
            self.assertTrue(kept.exists(), kept.name)
        self.assertEqual(5 + 4 + 4, round(outcome.free_after / GIB))

    def test_a_running_jobs_work_folder_is_never_swept(self):
        mine = self.abandoned_job("20261101T000000Z-mine", 9)
        with mock.patch.object(jobs, "active_job_ids", lambda home=None: ["20261101T000000Z-mine"]):
            outcome = diskspace.ensure_free(self.settings, self.home, free=self.volume)
        self.assertTrue(mine.exists())
        self.assertFalse(outcome.ok)

    def test_still_low_after_cleanup_is_blocked_with_the_numbers_and_the_largest_consumers(self):
        self.abandoned_job("20261101T000000Z-dead", 1)
        outcome = diskspace.ensure_free(self.settings, self.home, free=self.volume)
        self.assertFalse(outcome.ok)
        self.assertIn("below the 10 GiB minimum", outcome.reason)
        self.assertIn("6.0 GiB free", outcome.reason)
        self.assertIn("largest:", outcome.reason)

    def test_a_locked_file_does_not_stop_the_other_cleanup_steps_or_crash_the_stage(self):
        expired = self.run_folder("20261031T000000Z-expired", 4, age_days=3)
        with mock.patch.object(workroot, "sweep", side_effect=PermissionError("file is locked")):
            outcome = diskspace.ensure_free(self.settings, self.home, free=self.volume)
        self.assertFalse(expired.exists(), "the run folders were still cleaned")
        self.assertFalse(outcome.ok)
        self.assertIn("cleanup problems: abandoned job folders: PermissionError", outcome.reason)

    def test_the_build_cache_retention_runs_too(self):
        root = buildcache.builds_root(self.home)
        source = self.base / "out" / "app.apk"
        source.parent.mkdir()
        source.write_bytes(b"a")
        entry = buildcache.publish(root, "k1", source, out=source.parent, project="P", platform="android", sha="a" * 40, profile="X",
                                   dirty=True, toolchain={}, config_hash="n", job_id="j", cacheable=False)
        manifest = {**entry.manifest, "lastUsedUtc": core.format_utc(core.now() - timedelta(days=10)),
                    "publishedUtc": core.format_utc(core.now() - timedelta(days=10))}
        core.atomic_write_json(entry.directory / buildcache.MANIFEST, manifest)
        self.volume.hold(entry.directory, 2)
        outcome = diskspace.ensure_free(self.settings, self.home, free=self.volume)
        self.assertFalse(entry.directory.exists())
        self.assertEqual(7, round(outcome.free_after / GIB))

    def test_the_pressure_cleanup_uses_the_build_cache_limits_of_host_json(self):
        import dataclasses
        root = buildcache.builds_root(self.home)
        entries = []
        for number, days in enumerate((3, 2, 1)):
            source = self.base / f"out{number}" / "app.apk"
            source.parent.mkdir()
            source.write_bytes(b"a")
            entry = buildcache.publish(root, f"k{number}", source, out=source.parent, project="P", platform="android", sha="a" * 40,
                                       profile="X", dirty=False, toolchain={}, config_hash="n", job_id=f"j{number}", cacheable=True)
            stamp = core.format_utc(core.now() - timedelta(days=days))
            core.atomic_write_json(entry.directory / buildcache.MANIFEST, {**entry.manifest, "lastUsedUtc": stamp, "publishedUtc": stamp})
            entries.append(entry)
        self.volume.hold(entries[0].directory, 2)
        tight = dataclasses.replace(self.settings, build_cache=buildcache.Retention(max_entries=1))
        diskspace.ensure_free(tight, self.home, free=self.volume)
        self.assertEqual(["k2"], [e.key for e in buildcache.entries(root)], "only the most recently used build stays")

    def test_the_pressure_cleanup_also_applies_per_source(self):
        import dataclasses
        root = buildcache.builds_root(self.home)
        made = []
        for number, hours in enumerate((30, 20)):
            source = self.base / f"p{number}" / "app.apk"
            source.parent.mkdir()
            source.write_bytes(b"a")
            entry = buildcache.publish(root, f"s{number}", source, out=source.parent, project="P", platform="android", sha="a" * 40,
                                       profile="X", dirty=False, toolchain={}, config_hash="n", job_id=f"j{number}", cacheable=True,
                                       source="dir:/w")
            stamp = core.format_utc(core.now() - timedelta(hours=hours))
            core.atomic_write_json(entry.directory / buildcache.MANIFEST,
                                   {**entry.manifest, "lastUsedUtc": stamp, "publishedUtc": stamp, "sources": {"dir:/w": stamp}})
            made.append(entry)
        self.volume.hold(made[0].directory, 2)
        tight = dataclasses.replace(self.settings, build_cache=buildcache.Retention(per_source=1))
        diskspace.ensure_free(tight, self.home, free=self.volume)
        self.assertEqual(["s1"], [e.key for e in buildcache.entries(root)], "the older build of the same source goes")

    def test_config_rejects_bad_disk_settings(self):
        for bad in ({"minFreeGiB": 0}, {"minFreeGiB": "5"}, {"keepDaysUnderPressure": -1}, [], {"minFreeGiB": float("nan")},
                    {"minFreeGiB": float("inf")}, {"keepDaysUnderPressure": float("inf")}, {"waitSec": -1}, {"waitSec": "5"},
                    {"waitSec": True}, {"waitSec": float("inf")}):
            (self.home / "host.json").write_text(json.dumps({"disk": bad}), encoding="utf-8")
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load_host_settings(self.home)

    def test_doctor_reports_free_space_sizes_and_the_threshold(self):
        (self.work / "jobs" / "j1").mkdir(parents=True)
        (self.work / "jobs" / "j1" / "f").write_bytes(b"x" * 100)
        self.run_folder("20261101T000000Z-r", 1, 0.1)
        with mock.patch.object(diskspace, "free_bytes", self.volume):
            report = doctor.storage_report(self.work, self.root, 10, self.home)
        self.assertEqual(100, report["workRoot"]["bytes"])
        self.assertGreater(report["artifactsRoot"]["bytes"], 0)
        self.assertEqual(0, report["buildCache"]["entries"])
        self.assertEqual((10, True), (report["minFreeGiB"], report["belowMinimum"]))
        self.assertIn("freeBytes", report)


class PipelineDiskTests(unittest.TestCase):
    """The pipeline side: BLOCKED before the toolchain, and build output released early."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.home, self.root, self.work = self.base / "home", self.base / "artifacts", self.base / "work"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.world = World(self.home)
        providers.register("android", adapter_class(self.world))
        self.addCleanup(providers.unregister, "android")
        (self.base / "plans").mkdir()
        (self.base / "plans" / "open.plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
        self.world.gate("A", "build").set()
        self.world.gate("A", "launch").set()
        (self.base / "A" / "ProjectSettings").mkdir(parents=True)
        (self.base / "A" / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")

    def settings(self, document):
        (self.home / "host.json").write_text(json.dumps({"workRoot": str(self.work), **document}), encoding="utf-8")

    def run_job(self, stages=None, **options):
        document = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": "c" * 40, "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": stages or [
                        {"stage": "build"}, {"stage": "install"},
                        {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app", "stallSec": 5}, "timeoutSec": 30}]}
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "baseDir": str(self.base / "plans"), "devicePollSec": 0.02,
                   "projectDir": str(self.base / "A")}
        request = jobs.JobRequest(kind="pipeline", project="Sample", sha="c" * 40, label="A", payload=payload,
                                  host={"id": "win", "transport": "local", "selectedBy": "local"}, owner="tester",
                                  **{"stall_sec": 30, "timeout_sec": 60, **options})
        info = jobs.start_job(request, self.root, self.home, foreground=True, work_roots=workroot.WorkRoots(self.work))
        return jobs.JobStore(Path(info["dir"]))

    def test_a_job_survives_the_removal_of_its_schema_folder_while_it_builds(self):
        # The runner executes from a staged copy of the CLI that something may remove under a long job (#18).
        copy = self.base / "schemas-copy"
        shutil.copytree(schema.SCHEMAS_DIRECTORY, copy)
        original = adapter_class(self.world)

        class Removing(original):
            def build(self, project, profile, out):
                shutil.rmtree(copy)
                return super().build(project, profile, out)

        providers.register("android", Removing)
        schema._SHARED.pop(copy, None)
        self.addCleanup(schema._SHARED.pop, copy, None)
        with mock.patch.object(schema, "SCHEMAS_DIRECTORY", copy):
            store = self.run_job()
        job = store.read_job()
        self.assertEqual("PASS", job["status"], job["reason"])
        self.assertTrue(store.result.exists(), "the result was written and validated after the folder was gone")

    def test_the_job_does_not_survive_without_the_preload(self):
        copy = self.base / "schemas-copy2"
        shutil.copytree(schema.SCHEMAS_DIRECTORY, copy)
        original = adapter_class(self.world)

        class Removing(original):
            def build(self, project, profile, out):
                shutil.rmtree(copy)
                return super().build(project, profile, out)

        providers.register("android", Removing)
        schema._SHARED.pop(copy, None)
        self.addCleanup(schema._SHARED.pop, copy, None)
        with mock.patch.object(schema, "SCHEMAS_DIRECTORY", copy), mock.patch.object(jobs, "preload_runtime", lambda: None):
            store = self.run_job()
        self.assertIn("schema file not found", store.read_job()["reason"], "the test would not catch a missing preload otherwise")

    def test_preload_reads_every_schema_and_serves_later_validators_after_the_folder_is_gone(self):
        copy = self.base / "schemas-copy3"
        shutil.copytree(schema.SCHEMAS_DIRECTORY, copy)
        self.addCleanup(schema._SHARED.pop, copy, None)
        schema._SHARED.pop(copy, None)
        count = schema.Validator(copy).preload()
        self.assertGreaterEqual(count, 10)
        shutil.rmtree(copy)
        self.assertEqual("gdv.result/1", schema.Validator(copy).load("result.schema.json")["properties"]["schema"]["const"])

    def test_the_staged_cli_lives_in_the_valtrion_home_not_in_the_work_root(self):
        self.settings({})
        roots = config.load_host_settings(self.home).roots
        self.assertEqual(self.home / "tools", roots.tools)
        self.assertEqual(self.work / "jobs", roots.jobs)
    def events_of(self, store):
        return [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]

    def test_a_build_waits_for_space_that_appears_and_then_builds_without_holding_a_lease_meanwhile(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        calls = []
        low, high = FakeVolume(1), FakeVolume(50)

        def volume(path):  # a neighbour's build ends after a few checks (each check reads the volume twice)
            calls.append(1)
            return low(path) if len(calls) < 9 else high(path)

        with mock.patch.object(diskspace, "free_bytes", volume), mock.patch.object(diskspace, "POLL_SEC", 0.05), \
                mock.patch.object(diskspace, "releasable_bytes", lambda *_: 40 * GIB):
            store = self.run_job(disk_wait_sec=30)
        job = store.read_job()
        self.assertEqual("PASS", job["status"], job["reason"])
        self.assertIn(("A", "build.start"), self.world.timeline)
        events = self.events_of(store)
        phases = [e["data"]["phase"] for e in events if e["type"] == "progress" and "phase" in e.get("data", {})]
        self.assertEqual("waiting-disk", phases[0])
        self.assertIn("waiting-build", phases)
        first_wait = next(i for i, e in enumerate(events) if e["message"].startswith("waiting for disk space"))
        first_lease = next(i for i, e in enumerate(events) if e["type"] == "lease.acquired")
        self.assertLess(first_wait, first_lease, "the project and slot leases are taken only after the space is there")
        data = events[first_wait]["data"]
        self.assertEqual(10 * GIB, data["minFreeBytes"])
        self.assertLess(data["freeBytes"], 10 * GIB)

    def test_space_that_never_appears_ends_blocked_after_the_limit(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        began = time.monotonic()
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(1)), mock.patch.object(diskspace, "POLL_SEC", 0.05), \
                mock.patch.object(diskspace, "releasable_bytes", lambda *_: 40 * GIB):
            store = self.run_job(disk_wait_sec=0.4)
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertGreaterEqual(time.monotonic() - began, 0.4)
        self.assertIn("minimum", job["reason"])
        self.assertIn("no space appeared within the 0.4 s wait", job["reason"])
        self.assertNotIn(("A", "build.start"), self.world.timeline)
        self.assertEqual([], self.world.held())

    def test_space_that_could_never_suffice_ends_blocked_at_once(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        began = time.monotonic()
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(1)):  # no other active job: nothing can bring the volume up
            store = self.run_job(disk_wait_sec=300)
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertLess(time.monotonic() - began, 20)
        self.assertIn("waiting cannot help", job["reason"])
        self.assertFalse(any(e["message"].startswith("waiting for disk space") for e in self.events_of(store)))
        self.assertNotIn(("A", "build.start"), self.world.timeline)

    def test_the_host_setting_enables_the_wait_and_the_default_does_not(self):
        self.settings({"disk": {"minFreeGiB": 10, "waitSec": 0.3}})
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(1)), mock.patch.object(diskspace, "POLL_SEC", 0.05), \
                mock.patch.object(diskspace, "releasable_bytes", lambda *_: 40 * GIB):
            store = self.run_job()
        self.assertIn("no space appeared within the 0.3 s wait", store.read_job()["reason"])
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(1)), mock.patch.object(diskspace, "releasable_bytes", lambda *_: 40 * GIB):
            store = self.run_job(disk_wait_sec=0)  # the job's own 0 wins over the host's wait
        self.assertNotIn("no space appeared", store.read_job()["reason"])
        self.assertNotIn("waiting cannot help", store.read_job()["reason"])

    def test_the_wait_does_not_count_against_the_job_timeout(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        started = {}
        low, high = FakeVolume(1), FakeVolume(50)

        def volume(path):  # low until 2.5 s after the first check, however long the job took to start
            started.setdefault("at", time.monotonic())
            return low(path) if time.monotonic() - started["at"] < 2.5 else high(path)

        with mock.patch.object(diskspace, "free_bytes", volume), mock.patch.object(diskspace, "POLL_SEC", 0.05), \
                mock.patch.object(diskspace, "releasable_bytes", lambda *_: 40 * GIB):
            store = self.run_job(disk_wait_sec=30, timeout_sec=2, tick_sec=0.1)  # the wait is longer than the timeout
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])

    def test_an_idle_incremental_folder_that_could_close_the_gap_is_left_to_the_check_under_the_leases(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        folder = self.home / "incremental" / "Sample" / "queue"
        folder.mkdir(parents=True)
        core.atomic_write_json(folder / "owner.json", {"leaseResource": "unity-project:idle"})
        settings = config.load_host_settings(self.home)
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(1)), mock.patch.object(diskspace, "folder_bytes", lambda path: 20 * GIB), \
                mock.patch.object(diskspace.incremental, "folders", lambda home=None: [folder]), \
                mock.patch.object(diskspace.incremental, "OWNER", "owner.json"):
            outcome = diskspace.wait_for_space(settings, self.home, [], 60, "me", poll_sec=0.01)
        self.assertTrue(outcome.ok, "not BLOCKED by the wait: the build resources are taken and the last check releases the folder")
        self.assertTrue(folder.exists(), "the wait itself removes nothing of the persistent build folders")

    def test_a_bad_disk_wait_option_is_rejected(self):
        from valtrion import cli  # noqa: F401 - commands is imported through cli
        from valtrion import commands
        for bad in ("nan", "inf", "-1", "x"):
            with self.assertRaises(Exception, msg=bad):
                commands._finite_seconds(bad)
        self.assertEqual(30.0, commands._finite_seconds("30"))

    def test_a_cancel_ends_the_wait(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        calls = []

        def cancelled():
            calls.append(1)
            return len(calls) > 2

        with mock.patch.object(diskspace, "free_bytes", FakeVolume(1)), mock.patch.object(diskspace, "releasable_bytes", lambda *_: 40 * GIB):
            outcome = diskspace.wait_for_space(config.load_host_settings(self.home), self.home, [], 60, "me", cancelled=cancelled, poll_sec=0.01)
        self.assertEqual((False, True), (outcome.ok, outcome.cancelled))

    def test_low_space_ends_the_build_blocked_before_any_toolchain_starts(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(1)):
            store = self.run_job()
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("minimum", job["reason"])
        self.assertNotIn(("A", "build.start"), self.world.timeline)
        self.assertEqual([], self.world.held(), "the build resources were released")
        self.assertEqual("BLOCKED", json.loads(store.result.read_text(encoding="utf-8"))["status"])

    def test_enough_space_builds_as_before(self):
        self.settings({"disk": {"minFreeGiB": 10}})
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(100)):
            store = self.run_job()
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])

    def test_no_setting_builds_as_before_even_on_a_full_volume(self):
        with mock.patch.object(diskspace, "free_bytes", FakeVolume(0)):
            store = self.run_job()
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])

    def test_the_build_output_is_released_right_after_publication_and_the_published_build_stays(self):
        observed = {}
        original = adapter_class(self.world)
        work = self.work

        class Observing(original):
            def bind_temporary_registrar(self, register):
                self.register = register

            def build(self, project, profile, out):
                export = out.parent / "xcode-export-test"  # a second big folder the build made beside its output (an Xcode export)
                export.mkdir(parents=True)
                (export / "big.bin").write_bytes(b"x" * 1000)
                self.register(export)
                return super().build(project, profile, out)

            def install(self, device, artifact):
                observed["leftovers"] = [p for p in (work / "jobs").glob("*/xcode-export-test") if p.exists()] + \
                                        [p for p in (work / "jobs").glob("*/build") if p.exists()]
                observed["artifact"] = Path(artifact).exists()
                return super().install(device, artifact)

        providers.register("android", Observing)
        store = self.run_job()
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertEqual([], observed["leftovers"], "the copy in the job work folder was gone before the install")
        self.assertTrue(observed["artifact"], "the published artifact stays")
        build = json.loads(store.result.read_text(encoding="utf-8"))["build"]
        self.assertTrue(Path(build["path"]).exists())
        events = [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e["type"] == "log" and "of build output from the job work folder" in e.get("message", "") for e in events), "reported")

    def test_the_build_an_active_job_waits_to_install_is_never_removed_by_the_cleanup(self):
        from datetime import timedelta
        from valtrion.lease import LeaseManager
        self.settings({"disk": {"minFreeGiB": 10}})
        manager = LeaseManager(self.home)
        device = "device:" + "android:ZY22"
        manager.acquire(device, "alice", "session-9", 600, "series", os.getpid())  # the device is busy: the job waits for it
        payload_job = {}

        def work():
            document = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": "c" * 40, "profile": "Validation"},
                        "device": {"platform": "android"}, "stages": [
                            {"stage": "build"}, {"stage": "install"},
                            {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app", "stallSec": 5}, "timeoutSec": 30}]}
            payload = {"type": "pipeline", "file": "<inline>", "inline": document, "baseDir": str(self.base / "plans"),
                       "devicePollSec": 0.02, "projectDir": str(self.base / "A")}
            request = jobs.JobRequest(kind="pipeline", project="Sample", sha="c" * 40, label="A", payload=payload,
                                      host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=120,
                                      owner="tester", lease_wait_sec=60, tick_sec=0.1)
            payload_job["info"] = jobs.start_job(request, self.root, self.home, foreground=True, work_roots=workroot.WorkRoots(self.work))

        import threading
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        deadline = time.monotonic() + 180  # generous but bounded: late in a long run on a loaded runner the build takes far longer than on a quiet host
        entry = None
        while time.monotonic() < deadline and entry is None:
            time.sleep(0.05)
            entries = buildcache.entries(buildcache.builds_root(self.home))
            waiting = [p for p in self.root.glob("*/*/*/job.json") if core.read_json(p).get("phase") == "waiting-device"]
            entry = entries[0] if entries and waiting else None
        phases = [core.read_json(p).get("phase") for p in self.root.glob("*/*/*/job.json")]
        self.assertIsNotNone(entry, f"the job built, published and waits for the device (no such state within 180 s; builds: {len(buildcache.entries(buildcache.builds_root(self.home)))}, job phases: {phases})")
        old = core.format_utc(core.now() - timedelta(days=10))
        core.atomic_write_json(entry.directory / buildcache.MANIFEST, {**entry.manifest, "lastUsedUtc": old, "publishedUtc": old})
        outcome = diskspace.ensure_free(config.load_host_settings(self.home), self.home, free=FakeVolume(1))
        self.assertTrue(entry.directory.exists(), "an active job's build stays although it is old and the space is low")
        self.assertFalse(outcome.ok)
        manager.release(device, "session-9", os.getpid())
        thread.join(60)
        self.assertEqual("PASS", jobs.JobStore(Path(payload_job["info"]["dir"])).read_job()["status"])
        self.assertIn(entry.key, [e.key for e in buildcache.entries(buildcache.builds_root(self.home))] + [entry.key])
        core.atomic_write_json(entry.directory / buildcache.MANIFEST, {**entry.manifest, "lastUsedUtc": old, "publishedUtc": old})
        diskspace.ensure_free(config.load_host_settings(self.home), self.home, free=FakeVolume(1))
        self.assertFalse(entry.directory.exists(), "once no active job needs it, the expired build goes")

    def test_the_active_build_keys_are_found_from_a_list_of_active_jobs_without_reading_the_index_again(self):
        run = self.base / "run1"
        run.mkdir()
        diskspace.remember_build(run, "k1")
        diskspace.remember_build(run, "k2")  # a pipeline with an install by key and then a build uses two
        diskspace.remember_build(run, "k1")
        self.assertEqual({"keys": ["k1", "k2"]}, json.loads((run / diskspace.BUILD_KEY_FILE).read_text(encoding="utf-8")))
        old = self.base / "run0"
        old.mkdir()
        (old / diskspace.BUILD_KEY_FILE).write_text('{"key": "legacy"}', encoding="utf-8")  # the form 0.8.2 wrote
        jobs_folder = self.home / "jobs"
        jobs_folder.mkdir()
        core.atomic_write_json(jobs_folder / "job-a.json", {"dir": str(run)})
        core.atomic_write_json(jobs_folder / "job-b.json", {"dir": str(old)})
        with mock.patch.object(jobs, "active_job_ids", side_effect=AssertionError("the index was read again")):
            keys = diskspace.active_build_keys(self.home, active=["job-a", "job-b", "job-unknown"])
        self.assertEqual(["k1", "k2", "legacy"], keys)

    def test_the_job_index_loses_entries_whose_run_folder_is_gone(self):
        alive = self.root / "P" / "abcdef12" / "alive"
        alive.mkdir(parents=True)
        index = self.home / "jobs"
        index.mkdir()
        core.atomic_write_json(index / "alive.json", {"dir": str(alive)})
        core.atomic_write_json(index / "gone.json", {"dir": str(self.root / "P" / "abcdef12" / "gone")})
        core.atomic_write_json(index / "running-gone.json", {"dir": str(self.root / "P" / "abcdef12" / "running-gone")})
        core.atomic_write_json(index / "elsewhere.json", {"dir": str(self.base / "other-root" / "gone")})  # another artifacts root
        core.atomic_write_json(index / "nodir.json", {"other": 1})
        (index / "broken.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(jobs, "active_job_ids", lambda home=None: ["running-gone"]):
            self.assertEqual(["gone"], jobs.prune_index(self.root, self.home, dry_run=True))
            self.assertTrue((index / "gone.json").exists())
            self.assertEqual(["gone"], jobs.prune_index(self.root, self.home))
        self.assertEqual(["alive.json", "broken.json", "elsewhere.json", "nodir.json", "running-gone.json"],
                         sorted(p.name for p in index.glob("*.json")))
        # an unavailable artifacts root (unmounted volume) makes every folder look gone: nothing is judged then
        self.assertEqual([], jobs.prune_index(self.base / "not-mounted", self.home))
        self.assertTrue((index / "alive.json").exists())

    def test_the_gc_command_prunes_the_job_index(self):
        from valtrion import cli
        import contextlib
        import io
        index = self.home / "jobs"
        index.mkdir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings({"artifactsRoot": str(self.root)})
        core.atomic_write_json(index / "gone.json", {"dir": str(self.root / "P" / "abcdef12" / "gone")})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["gc"])
        self.assertEqual(0, code)
        self.assertFalse((index / "gone.json").exists())
        self.assertIn("job index entry gone", out.getvalue())

    def test_two_build_stages_release_the_leftovers_of_an_unpublished_first_build(self):
        original = adapter_class(self.world)
        work = self.work
        real_publish = buildcache.publish
        calls = []

        def publish_once_failing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            return real_publish(*args, **kwargs)

        class Leftover(original):
            def bind_temporary_registrar(self, register):
                self.register = register

            def build(self, project, profile, out):
                extra = out.parent / "xcode-export-test"
                extra.mkdir(parents=True, exist_ok=True)
                (extra / "big.bin").write_bytes(b"x" * 100)
                self.register(extra)
                return super().build(project, profile, out)

        providers.register("android", Leftover)
        with mock.patch.object(buildcache, "publish", publish_once_failing):
            store = self.run_job([{"stage": "build"}, {"stage": "build", "args": {"reuse": False}}, {"stage": "install"}])
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertEqual(2, len(calls))
        self.assertEqual([], [p for p in (work / "jobs").glob("*/xcode-export-test") if p.exists()], "released by the pipeline")
        events = [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e["type"] == "log" and "of build output from the job work folder" in e.get("message", "") for e in events))

    def test_a_new_build_removes_what_a_cancelled_job_left_in_the_same_project_tree(self):
        self.settings({"artifactsRoot": str(self.root)})
        project = self.base / "A"
        leftover = project / "Builds" / "iOS"  # an export folder the cancelled build made inside the project
        leftover.mkdir(parents=True)
        (leftover / "Xcode.proj").write_text("x", encoding="utf-8")
        old = self.root / "Sample" / "abcdef12" / "20261101T000000Z-old-abcdef12"
        old.mkdir(parents=True)
        core.atomic_write_json(old / "job.json", {"state": "cancelled"})
        core.atomic_write_json(old / "spec.json", {
            "jobId": old.name, "project": "Sample", "sha8": "abcdef12", "artifactsRoot": str(self.root), "cleanup": True,
            "payload": {"type": "pipeline", "projectDir": str(project)}})
        core.atomic_write_json(old / "temp-paths.json", [str(leftover)])
        store = self.run_job()
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertFalse(leftover.exists())
        self.assertEqual([], core.read_json(old / "temp-paths.json"), "the dead job's registry is emptied")
        events = [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("dead runner" in e.get("message", "") for e in events), "reported in the new job's events")

    def test_a_cancelled_job_of_another_project_tree_is_not_reaped(self):
        self.settings({"artifactsRoot": str(self.root)})
        other = self.base / "OtherCheckout"
        leftover = other / "Builds" / "iOS"
        leftover.mkdir(parents=True)
        old = self.root / "Sample" / "abcdef12" / "20261101T000000Z-old-abcdef12"
        old.mkdir(parents=True)
        core.atomic_write_json(old / "job.json", {"state": "cancelled"})
        core.atomic_write_json(old / "spec.json", {
            "jobId": old.name, "project": "Sample", "sha8": "abcdef12", "artifactsRoot": str(self.root), "cleanup": True,
            "payload": {"type": "pipeline", "projectDir": str(other)}})
        core.atomic_write_json(old / "temp-paths.json", [str(leftover)])
        store = self.run_job()
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertTrue(leftover.exists(), "another checkout of the same project may be in use under its own lease")

    def test_a_cancelled_job_whose_recorded_pid_is_alive_is_not_reaped(self):
        self.settings({"artifactsRoot": str(self.root)})
        project = self.base / "A"
        leftover = project / "Builds" / "iOS"
        leftover.mkdir(parents=True)
        old = self.root / "Sample" / "abcdef12" / "20261101T000000Z-old-abcdef12"
        old.mkdir(parents=True)
        core.atomic_write_json(old / "job.json", {"state": "cancelled", "pid": os.getpid()})  # alive: it may still be working there
        core.atomic_write_json(old / "spec.json", {
            "jobId": old.name, "project": "Sample", "sha8": "abcdef12", "artifactsRoot": str(self.root), "cleanup": True,
            "payload": {"type": "pipeline", "projectDir": str(project)}})
        core.atomic_write_json(old / "temp-paths.json", [str(leftover)])
        store = self.run_job()
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertTrue(leftover.exists())

    def test_a_spec_that_does_not_describe_its_own_folder_removes_nothing(self):
        leftover = self.base / "A" / "Builds" / "iOS"
        leftover.mkdir(parents=True)
        folder = self.root / "Sample" / "abcdef12" / "copied"
        folder.mkdir(parents=True)
        core.atomic_write_json(folder / "temp-paths.json", [str(leftover)])
        spec = {"jobId": "copied", "project": "Sample", "sha8": "abcdef12", "artifactsRoot": str(self.root), "cleanup": True,
                "payload": {"type": "pipeline", "projectDir": str(self.base / "A")}}
        notes = []

        class Events:
            def emit(self, kind, message=None, **kwargs):
                notes.append(message)

        jobs._remove_orphaned_temporaries(spec, "copied", Events(), self.root / "Sample" / "abcdef12" / "somewhere-else")
        self.assertTrue(leftover.exists())
        self.assertTrue(any("does not describe the folder" in (note or "") for note in notes))

    def test_a_retained_or_no_cleanup_job_keeps_its_work_folder(self):
        original = adapter_class(self.world)

        class Leftover(original):
            def bind_temporary_registrar(self, register):
                self.register = register

            def build(self, project, profile, out):
                extra = out.parent / "xcode-export-test"
                extra.mkdir(parents=True)
                self.register(extra)
                return super().build(project, profile, out)

        providers.register("android", Leftover)
        document = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": "c" * 40, "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": [{"stage": "build"}]}
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "baseDir": str(self.base / "plans"),
                   "projectDir": str(self.base / "A")}
        request = jobs.JobRequest(kind="pipeline", project="Sample", sha="c" * 40, label="A", payload=payload,
                                  host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=60,
                                  owner="tester", cleanup=False)
        info = jobs.start_job(request, self.root, self.home, foreground=True, work_roots=workroot.WorkRoots(self.work))
        self.assertEqual("PASS", jobs.JobStore(Path(info["dir"])).read_job()["status"])
        self.assertTrue(list((self.work / "jobs").glob("*/xcode-export-test")), "--no-cleanup keeps the work folder, early release included")

    def test_an_unpublished_build_is_kept_until_the_last_stage_and_released_then(self):
        observed = {}
        original = adapter_class(self.world)
        work = self.work

        class Unpublished(original):
            def bind_temporary_registrar(self, register):
                self.register = register

            def build(self, project, profile, out):
                outcome = super().build(project, profile, out)
                self.register(out)
                return outcome

            def install(self, device, artifact):
                observed["before"] = Path(artifact).exists()
                return super().install(device, artifact)

        providers.register("android", Unpublished)
        stages = [{"stage": "build"}, {"stage": "install"}]
        with mock.patch.object(buildcache, "publish", side_effect=OSError("disk full")):
            store = self.run_job(stages)
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertTrue(observed["before"], "the install still had its artifact")
        self.assertEqual([], [p for p in (work / "jobs").glob("*/build") if p.exists()], "released at the end")
        events = [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e["type"] == "log" and "of build output from the job work folder" in e.get("message", "") for e in events),
                        "released by the pipeline itself, not only by the job cleanup")


if __name__ == "__main__":
    unittest.main()
