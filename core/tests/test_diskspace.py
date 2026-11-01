"""Free-space check before a build: cleanup under pressure, BLOCKED when it is not enough, early release of build output, doctor facts."""

import json
import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from valtrion import artifacts, buildcache, config, core, diskspace, doctor, jobs, pipeline, providers, workroot
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

    def test_config_rejects_bad_disk_settings(self):
        for bad in ({"minFreeGiB": 0}, {"minFreeGiB": "5"}, {"keepDaysUnderPressure": -1}, []):
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

    def run_job(self, stages=None):
        document = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": "c" * 40, "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": stages or [
                        {"stage": "build"}, {"stage": "install"},
                        {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app", "stallSec": 5}, "timeoutSec": 30}]}
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "baseDir": str(self.base / "plans"), "devicePollSec": 0.02,
                   "projectDir": str(self.base / "A")}
        request = jobs.JobRequest(kind="pipeline", project="Sample", sha="c" * 40, label="A", payload=payload,
                                  host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=60,
                                  owner="tester")
        info = jobs.start_job(request, self.root, self.home, foreground=True, work_roots=workroot.WorkRoots(self.work))
        return jobs.JobStore(Path(info["dir"]))

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
