import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import compare, constants, core, jobs, lease, pipeline, providers
from valtrion.adapter import Capabilities, DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from valtrion.schema import Validator

SHA = "c" * 40


class FakeAdapter(PlatformAdapter):
    platform = "android"
    scenario = "pass"
    build_status = constants.PASS
    devices: list[DeviceInfo] = []
    calls: list[str] = []
    pulls = 0
    launched = False
    stale = False
    samples = [900, 100, 101, 99]
    samples_by_launch: list[list[int]] = []  # consumed one per launch, for a pipeline with several run stages
    current_samples = samples

    def probe(self):
        return Capabilities("android", ("build",))

    def discover(self):
        return list(FakeAdapter.devices)

    def build(self, project, profile, out):
        FakeAdapter.calls.append("build")
        out.mkdir(parents=True, exist_ok=True)
        if FakeAdapter.build_status != constants.PASS:
            return StepOutcome(FakeAdapter.build_status, "gradle failed", details={"layer.export": "PASS", "layer.nativeBuild": "FAIL"})
        apk = out / "app.apk"
        apk.write_bytes(b"apk")
        return StepOutcome(constants.PASS, artifacts=(apk,), details={"layer.export": "PASS", "layer.nativeBuild": "PASS"})

    def install(self, device, artifact):
        FakeAdapter.calls.append("install")
        return StepOutcome(constants.PASS)

    def push(self, device, local, remote):
        FakeAdapter.calls.append(f"push:{remote}")
        return StepOutcome(constants.PASS)

    def pull(self, device, remote, local):
        FakeAdapter.calls.append("pull")
        if FakeAdapter.stale:  # an earlier run's finished output is still on the device
            old = local / "old-run"
            old.mkdir(parents=True, exist_ok=True)
            core.atomic_write_json(old / "heartbeat.json", {"seq": 99})
            core.atomic_write_json(old / "result.json", {"status": "PASS", "reason": None, "steps": [], "metrics": [
                {"name": "openMs", "unit": "ms", "samples": [1, 1, 1, 1]}], "unverified": []})
        if not FakeAdapter.launched:
            return StepOutcome(constants.PASS)
        FakeAdapter.pulls += 1
        run = local / "run-1"
        run.mkdir(parents=True, exist_ok=True)
        if FakeAdapter.scenario == "hang":
            core.atomic_write_json(run / "heartbeat.json", {"seq": 3})
        elif FakeAdapter.scenario == "pass":
            core.atomic_write_json(run / "heartbeat.json", {"seq": FakeAdapter.pulls})
            if FakeAdapter.pulls >= 2:
                core.atomic_write_json(run / "result.json", {
                    "status": "PASS", "reason": None,
                    "steps": [{"type": "wait", "status": "PASS", "startMs": 0, "durationMs": 10}],
                    "metrics": [{"name": "openMs", "unit": "ms", "samples": list(FakeAdapter.current_samples)}], "unverified": ["Sample flow"]})
        return StepOutcome(constants.PASS)

    def launch(self, device, app, args, log):
        FakeAdapter.calls.append("launch")
        FakeAdapter.launched = True
        FakeAdapter.pulls = 0
        FakeAdapter.current_samples = FakeAdapter.samples_by_launch.pop(0) if FakeAdapter.samples_by_launch else FakeAdapter.samples
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("started\n", encoding="utf-8")
        return StepOutcome(constants.PASS)

    def terminate(self, device, app):
        FakeAdapter.calls.append("terminate")
        FakeAdapter.launched = False  # the application is gone: a later run stage launches it again
        return StepOutcome(constants.PASS)

    def cleanup_run_data(self, device):
        FakeAdapter.calls.append("cleanup")
        return StepOutcome(constants.PASS)

    def retain_run_data(self, device, status, reason):
        FakeAdapter.calls.append(f"retain:{status}")
        return StepOutcome(constants.PASS, details={"retainedAt": "/kept/data.retained-1"})

    def screenshot(self, device, out):
        FakeAdapter.calls.append("screenshot")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"png")
        return StepOutcome(constants.PASS)

    def health(self, device):
        return DeviceHealth(True, locked=False, thermal_state="nominal")


PLAN = {
    "schema": "gdv.plan/1", "id": "perf.open", "kind": "performance", "requires": {"platforms": ["android"], "capabilities": ["touch"]},
    "dataPolicy": "devdata", "budgetSeconds": 30, "warmup": 1,
    "steps": [{"type": "wait", "timeoutSec": 5}],
    "metrics": {"openMs": {"unit": "ms", "direction": "lower", "regressionPct": 10, "minSamples": 3}}}


#: A second series: another plan, and a metric where more is better.
PLAN_WARM = {**PLAN, "id": "perf.warm", "metrics": {"openMs": {"unit": "ms", "direction": "higher", "regressionPct": 10, "minSamples": 3}}}


def phone(evidence="physical"):
    return DeviceInfo("android:ZY22", "android", "phone-mid", evidence, "moto g", "Android 15", ("touch",))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        base = Path(self._folder.name)
        self.home, self.root, self.work = base / "home", base / "artifacts", base / "work"
        (self.work / "project" / "ProjectSettings").mkdir(parents=True)
        (self.work / "project" / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.work / "plans").mkdir()
        (self.work / "plans" / "open.plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeAdapter.scenario, FakeAdapter.build_status, FakeAdapter.calls, FakeAdapter.pulls = "pass", constants.PASS, [], 0
        FakeAdapter.launched = FakeAdapter.stale = False
        FakeAdapter.samples_by_launch = []
        FakeAdapter.devices = [phone()]
        (self.work / "plans" / "warm.plan.json").write_text(json.dumps(PLAN_WARM), encoding="utf-8")
        providers.register("android", FakeAdapter)
        self.addCleanup(providers.unregister, "android")

    def pipeline_document(self, stages, **extra):
        return {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": SHA, "profile": "Validation"},
                "device": {"platform": "android"}, "stages": stages, **extra}

    def run_pipeline(self, document, payload_extra=None, host=None, **request_options):
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "projectDir": str(self.work / "project"),
                   "baseDir": str(self.work / "plans"), "devicePollSec": 0.05, **(payload_extra or {})}
        options = dict(stall_sec=30, timeout_sec=120, owner="tester")
        options.update(request_options)
        request = jobs.JobRequest(
            kind="pipeline", project="Sample", sha=SHA, label="p1", payload=payload,
            host=host or {"id": "win", "transport": "local", "selectedBy": "local"}, **options)
        info = jobs.start_job(request, self.root, self.home, foreground=True)
        return info, jobs.JobStore(Path(info["dir"]))

    def stages(self, **run_args):
        return [{"stage": "build"}, {"stage": "install"},
                {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app", "stallSec": 1, **run_args}, "timeoutSec": 30}]

    def result_of(self, store):
        document = json.loads(store.result.read_text(encoding="utf-8"))
        self.assertEqual([], Validator().validate(document, "gdv.result/1"))
        return document

    def test_full_pass_builds_installs_runs_collects_and_cleans_up(self):
        info, store = self.run_pipeline(self.pipeline_document(self.stages()))
        job = store.read_job()
        self.assertEqual(("finished", "PASS"), (job["state"], job["status"]), job["reason"])
        result = self.result_of(store)
        self.assertEqual("PASS", result["status"])
        self.assertEqual({"id": "win", "transport": "local", "selectedBy": "local"}, result["host"])
        self.assertEqual(("android", "phone-mid", 15, "physical"),
                         (result["target"]["platform"], result["target"]["deviceClass"], result["target"]["osMajor"], result["target"]["evidence"]))
        self.assertEqual("6000.6.7f1", result["candidate"]["unity"])
        self.assertEqual({"export", "nativeBuild", "install", "launch", "scenario"}, set(result["layers"]))
        self.assertTrue(all(value == "PASS" for value in result["layers"].values()))
        metric = result["metrics"][0]
        self.assertEqual([900, 100, 101, 99], metric["samples"])
        self.assertEqual(4, metric["stats"]["n"])
        self.assertIn("Sample flow", result["unverified"])
        self.assertTrue(any(a["path"] == "console.log" or a["path"].endswith("console.log") for a in result["artifacts"]))
        self.assertEqual(["build", "install", "push:gdv/inbox/plan.json", "pull", "launch"], FakeAdapter.calls[:5],
                         "the device output is listed before the launch so earlier runs are recognised")
        self.assertIn("terminate", FakeAdapter.calls)
        self.assertNotIn("screenshot", FakeAdapter.calls, "no failure screenshot after a pass")
        self.assertFalse((store.directory / "build").exists(), "the temporary build is removed after a pass")
        self.assertEqual("free", lease.LeaseManager(self.home).status("device:android:ZY22").state)
        self.assertEqual("free", lease.LeaseManager(self.home).status("unity-editor:slot1").state)

    def test_a_run_sent_over_ssh_records_the_plan_hash_and_the_project_folder_it_tested(self):
        info, store = self.run_pipeline(self.pipeline_document(self.stages()), host={"id": "mac", "transport": "ssh", "selectedBy": "pinned"})
        result = self.result_of(store)
        plan_bytes = (self.work / "plans" / "open.plan.json").read_bytes()
        self.assertEqual({"id": PLAN["id"], "sha256": hashlib.sha256(plan_bytes).hexdigest()}, result["plan"])
        self.assertEqual("ssh", result["host"]["transport"])
        self.assertEqual(str(self.work / "project"), result["host"]["tools"]["projectDir"])
        self.assertFalse([item for item in result["unverified"] if "project folder" in item], "it is not reported as a gap")

    def test_a_local_run_has_no_project_folder_in_the_host(self):
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertNotIn("tools", self.result_of(store)["host"])

    def test_compare_against_a_baseline_is_recorded_in_the_result(self):
        baselines = self.work / "baselines"
        seeded = self.run_pipeline(self.pipeline_document(self.stages()))[1]
        first = self.result_of(seeded)
        first["candidate"]["dirty"] = False
        document = compare.build_baseline(first, PLAN, "2026-10-29T12:00:00.000Z")
        core.atomic_write_json(compare.baseline_path(baselines, "perf.open", "phone-mid"), document)
        FakeAdapter.pulls, FakeAdapter.launched = 0, False
        _, store = self.run_pipeline(self.pipeline_document(self.stages(), compare=str(baselines)))
        result = self.result_of(store)
        self.assertEqual("PASS", result["comparison"]["verdict"], result["comparison"])
        self.assertEqual("PASS", store.read_job()["status"])

    def test_missing_baseline_makes_the_job_blocked_not_pass(self):
        _, store = self.run_pipeline(self.pipeline_document(self.stages(), compare=str(self.work / "nowhere")))
        result = self.result_of(store)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("NOT_COMPARABLE", result["comparison"]["verdict"])
        self.assertEqual("BLOCKED", store.read_job()["status"])

    # -- several series in one pipeline ------------------------------------------------------------------------------------

    def series_stages(self):
        run = {"app": "com.example.app", "stallSec": 1}
        return [{"stage": "build"}, {"stage": "install"},
                {"stage": "run", "plan": "open.plan.json", "args": run, "timeoutSec": 30},
                {"stage": "run", "plan": "warm.plan.json", "args": run, "timeoutSec": 30}]

    def seed_baseline(self, baselines, plan_file, plan_document, samples, name="phone-mid.json", **key):
        """A baseline for what the fake pipeline produces (device class phone-mid, OS 15, profile Validation)."""
        sha = core.sha256_bytes((self.work / "plans" / plan_file).read_bytes())
        run = {"runId": "seed", "plan": {"id": plan_document["id"], "sha256": sha},
               "target": {"deviceClass": "phone-mid", "osMajor": 15, "evidence": "physical"},
               "candidate": {"profile": "Validation", "unity": "6000.6.7f1", "repo": "Sample", "sha": SHA},
               "metrics": [{"name": "openMs", "samples": samples}]}
        document = compare.build_baseline(run, plan_document, "2026-10-29T12:00:00.000Z")
        document["key"].update(key)
        core.atomic_write_json(baselines / core.safe_segment(plan_document["id"]) / name, document)

    def summary_of(self, store):
        return json.loads((store.directory / "summary.json").read_text(encoding="utf-8"))

    def test_every_series_is_compared_with_its_own_plan_and_baseline(self):
        baselines = self.work / "baselines"
        self.seed_baseline(baselines, "open.plan.json", PLAN, [999, 100, 101, 99])
        self.seed_baseline(baselines, "warm.plan.json", PLAN_WARM, [999, 50, 50, 50])  # the second series improved on it
        _, store = self.run_pipeline(self.pipeline_document(self.series_stages(), compare=str(baselines)))
        summary = self.summary_of(store)
        self.assertEqual(["perf.open", "perf.warm"], [entry["plan"] for entry in summary])
        self.assertEqual(["PASS", "IMPROVED"], [entry["comparison"]["verdict"] for entry in summary], summary)
        self.assertTrue(summary[1]["comparison"]["baseline"].endswith("phone-mid.json"))
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])

    def test_a_regression_in_an_earlier_series_is_not_hidden_by_a_passing_last_series(self):
        baselines = self.work / "baselines"
        self.seed_baseline(baselines, "open.plan.json", PLAN, [999, 50, 50, 50])  # the first series is twice as slow as its baseline
        self.seed_baseline(baselines, "warm.plan.json", PLAN_WARM, [999, 100, 101, 99])
        _, store = self.run_pipeline(self.pipeline_document(self.series_stages(), compare=str(baselines)))
        job = store.read_job()
        self.assertEqual("FAIL", job["status"])
        self.assertIn("perf.open", job["reason"])
        result = self.result_of(store)
        self.assertEqual(("perf.open", "REGRESSION"), (result["plan"]["id"], result["comparison"]["verdict"]),
                         "the job result is the worst series")
        self.assertEqual(["REGRESSION", "PASS"], [entry["comparison"]["verdict"] for entry in self.summary_of(store)])

    def test_a_series_without_a_baseline_is_not_comparable_and_the_job_says_which(self):
        baselines = self.work / "baselines"
        self.seed_baseline(baselines, "open.plan.json", PLAN, [999, 100, 101, 99])
        _, store = self.run_pipeline(self.pipeline_document(self.series_stages(), compare=str(baselines)))
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("perf.warm", job["reason"])
        self.assertEqual(["PASS", "NOT_COMPARABLE"], [entry["comparison"]["verdict"] for entry in self.summary_of(store)])
        result = self.result_of(store)
        self.assertEqual("perf.warm", result["plan"]["id"])
        self.assertTrue(any(str(baselines) in line for line in result["comparison"]["details"]), result["comparison"])

    def test_the_worst_verdict_of_all_series_decides_the_job(self):
        baselines = self.work / "baselines"
        self.seed_baseline(baselines, "open.plan.json", PLAN, [999, 50, 50, 50])  # REGRESSION
        # no baseline for the second series: NOT_COMPARABLE, which is less severe than the regression
        _, store = self.run_pipeline(self.pipeline_document(self.series_stages(), compare=str(baselines)))
        self.assertEqual("FAIL", store.read_job()["status"])
        self.assertIn("perf.warm", " ".join(self.result_of(store)["comparison"]["details"]), "the other series is still named")

    def test_the_baseline_is_found_by_its_key_not_by_a_file_name(self):
        baselines = self.work / "baselines"
        self.seed_baseline(baselines, "open.plan.json", PLAN, [999, 50, 50, 50], name="phone-mid.json", osMajor=14)
        self.seed_baseline(baselines, "open.plan.json", PLAN, [999, 100, 101, 99], name="another-series.json")
        _, store = self.run_pipeline(self.pipeline_document(self.stages(), compare=str(baselines)))
        result = self.result_of(store)
        self.assertEqual("PASS", result["comparison"]["verdict"], result["comparison"])
        self.assertTrue(result["comparison"]["baseline"].endswith("another-series.json"))

    def test_a_relative_baselines_folder_is_found_from_the_pipeline_or_the_project_whatever_the_working_directory(self):
        for root in (self.work / "plans", self.work / "project"):
            with self.subTest(root=root.name):
                self.seed_baseline(root / "baselines", "open.plan.json", PLAN, [999, 100, 101, 99])
                _, store = self.run_pipeline(self.pipeline_document(self.stages(), compare="baselines"))
                self.assertEqual("PASS", self.result_of(store)["comparison"]["verdict"])
                shutil.rmtree(root / "baselines")

    def test_the_warmup_of_every_repeat_is_left_out_of_the_merged_samples(self):
        def run(samples):
            return {"status": "PASS", "reason": None, "unverified": [],
                    "metrics": [{"name": "openMs", "unit": "ms", "samples": samples, "stats": compare.compute_stats(samples)}]}
        merged = pipeline._merge_runs([run([900, 100, 101, 99]), run([910, 102, 100, 98])], PLAN, "a" * 64)
        kept = compare.drop_warmup(merged["metrics"][0]["samples"], PLAN["warmup"])
        self.assertEqual([100, 101, 99, 102, 100, 98], kept, "only the first repeat keeps its warmup sample, and the plan drops it")

    def test_an_earlier_regression_is_named_when_a_later_series_ends_without_a_pass(self):
        baselines = self.work / "baselines"
        self.seed_baseline(baselines, "open.plan.json", PLAN, [999, 50, 50, 50])  # REGRESSION
        stages = self.series_stages()
        stages[3]["args"] = {}  # the second series names no application: BLOCKED
        _, store = self.run_pipeline(self.pipeline_document(stages, compare=str(baselines)))
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("perf.open", job["reason"])
        self.assertIn("regressed", job["reason"])

    def test_a_repeat_with_fewer_samples_than_the_warmup_is_left_out_of_the_join(self):
        def run(samples):
            return {"status": "PASS", "reason": None, "unverified": [],
                    "metrics": [{"name": "openMs", "unit": "ms", "samples": samples, "stats": compare.compute_stats(samples)}]}
        merged = pipeline._merge_runs([run([]), run([900, 100, 101, 99]), run([910, 102, 100, 98])], PLAN, "a" * 64)
        metric = merged["metrics"][0]
        self.assertEqual([100, 101, 99, 102, 100, 98], compare.drop_warmup(metric["samples"], PLAN["warmup"]))
        self.assertTrue(any("fewer samples" in line for line in merged["unverified"]))

    def test_a_failed_build_is_never_installed(self):
        FakeAdapter.build_status = constants.FAIL
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        job = store.read_job()
        self.assertEqual("FAIL", job["status"])
        self.assertIn("build failed", job["reason"])
        self.assertNotIn("install", FakeAdapter.calls)
        self.assertNotIn("launch", FakeAdapter.calls)
        result = self.result_of(store)
        self.assertEqual("FAIL", result["layers"]["nativeBuild"])
        self.assertNotIn("install", result["layers"])

    def test_a_hung_heartbeat_is_a_stall_and_the_failure_is_diagnosed(self):
        FakeAdapter.scenario = "hang"
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        job = store.read_job()
        self.assertEqual("STALL", job["status"])
        self.assertIn("heartbeat did not advance", job["reason"])
        self.assertEqual(constants.EXIT_STALL, constants.exit_code_for_status(job["status"]))
        self.assertIn("screenshot", FakeAdapter.calls)
        self.assertIn("terminate", FakeAdapter.calls)
        self.assertEqual("STALL", self.result_of(store)["status"])
        self.assertTrue(any(a["kind"] == "screenshot" for a in self.result_of(store)["artifacts"]))

    def test_a_passing_run_deletes_its_data_and_a_failed_run_keeps_it_as_diagnostics(self):
        self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertIn("cleanup", FakeAdapter.calls)
        self.assertFalse([c for c in FakeAdapter.calls if c.startswith("retain")])
        FakeAdapter.scenario, FakeAdapter.calls, FakeAdapter.pulls, FakeAdapter.launched = "hang", [], 0, False
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertIn("retain:STALL", FakeAdapter.calls)
        self.assertNotIn("cleanup", FakeAdapter.calls, "a failed run's data is not deleted")
        result = self.result_of(store)
        self.assertTrue(any("kept as diagnostics at /kept/data.retained-1" in note for note in result["unverified"]), result["unverified"])

    def test_a_run_that_ended_before_the_launch_keeps_what_it_left_instead_of_deleting_it(self):
        with mock.patch.object(FakeAdapter, "health", lambda self, device: DeviceHealth(False, notes=("unplugged",))):
            self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertIn("retain:BLOCKED", FakeAdapter.calls)
        self.assertNotIn("cleanup", FakeAdapter.calls)

    def test_a_leased_device_blocks_the_pipeline_naming_the_holder(self):
        lease.LeaseManager(self.home).acquire("device:android:ZY22", "alice", "session-9", 600, "series", os.getpid())
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("alice", job["reason"])
        self.assertNotIn("launch", FakeAdapter.calls)
        self.assertEqual("session-9", lease.LeaseManager(self.home).status("device:android:ZY22").record.session)

    def prelaunch_stages(self, files):
        stages = self.stages()
        stages[2]["prelaunchFiles"] = files
        return stages

    def test_prelaunch_files_are_pushed_before_the_launch_and_recorded_in_the_result(self):
        (self.work / "plans" / "cameras.json").write_text('{"fps": 120}', encoding="utf-8")
        files = [{"local": "cameras.json", "remote": "Video/cameras.json"}, {"local": str(self.work / "plans" / "cameras.json"), "remote": "diag.txt"}]
        _, store = self.run_pipeline(self.pipeline_document(self.prelaunch_stages(files)))
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        calls = FakeAdapter.calls
        self.assertLess(calls.index("push:Video/cameras.json"), calls.index("launch"))
        self.assertLess(calls.index("push:diag.txt"), calls.index("launch"))
        self.assertLess(calls.index("push:gdv/inbox/plan.json"), calls.index("launch"))
        paths = [a["path"] for a in self.result_of(store)["artifacts"]]
        self.assertTrue(any(p.endswith("prelaunch/Video/cameras.json") for p in paths), paths)
        self.assertTrue(any(p.endswith("prelaunch/diag.txt") for p in paths), paths)

    def test_a_bad_prelaunch_file_blocks_the_run_before_the_application_starts(self):
        (self.work / "plans" / "cameras.json").write_text("{}", encoding="utf-8")
        for bad in ({"local": "cameras.json", "remote": "../escape.json"}, {"local": "cameras.json", "remote": "/abs.json"},
                    {"local": "cameras.json", "remote": "C:x.json"}, {"local": "cameras.json", "remote": "a//b.json"},
                    {"local": "missing.json", "remote": "x.json"}):
            FakeAdapter.calls, FakeAdapter.launched = [], False
            _, store = self.run_pipeline(self.pipeline_document(self.prelaunch_stages([bad])))
            self.assertEqual("BLOCKED", store.read_job()["status"], bad)
            self.assertNotIn("launch", FakeAdapter.calls, bad)

    def test_prelaunch_targets_that_are_unsafe_or_collide_block_before_any_stage_runs(self):
        (self.work / "plans" / "c.json").write_text("{}", encoding="utf-8")
        cases = [[{"local": "c.json", "remote": remote}] for remote in (
            "CON", "dir/nul.txt", "COM1.json", "trailing.", "trailing /x", "gdv/inbox/plan.json", "gdv", "\\\\server\\x", "a/./b", "a/",
            "GDV/inbox/plan.json", "Gdv/out/x", "NUL .txt", "CONIN$", "COM¹", "lpt³.txt", "x/COM²")]
        cases.append([{"local": "c.json", "remote": "a"}, {"local": "c.json", "remote": "a/b"}])
        cases.append([{"local": "c.json", "remote": "A.json"}, {"local": "c.json", "remote": "a.json"}])
        cases.append([{"local": "c.json", "remote": "a/b"}, {"local": "c.json", "remote": "a/b"}])
        for files in cases:
            FakeAdapter.calls = []
            _, store = self.run_pipeline(self.pipeline_document(self.prelaunch_stages(files)))
            self.assertEqual("BLOCKED", store.read_job()["status"], files)
            self.assertEqual([], FakeAdapter.calls, f"nothing ran (not even the build) for {files}")

    def test_a_missing_prelaunch_file_costs_no_build(self):
        _, store = self.run_pipeline(self.pipeline_document(self.prelaunch_stages([{"local": "nope.json", "remote": "x.json"}])))
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("does not exist", store.read_job()["reason"])
        self.assertEqual([], FakeAdapter.calls)

    def test_a_failed_prelaunch_push_stops_the_run_with_its_status(self):
        (self.work / "plans" / "cameras.json").write_text("{}", encoding="utf-8")
        original = FakeAdapter.push

        def push(self, device, local, remote):
            if remote == "Video/cameras.json":
                return StepOutcome(constants.TIMEOUT, "copy timed out")
            return original(self, device, local, remote)

        with mock.patch.object(FakeAdapter, "push", push):
            _, store = self.run_pipeline(self.pipeline_document(self.prelaunch_stages([{"local": "cameras.json", "remote": "Video/cameras.json"}])))
        self.assertEqual("TIMEOUT", store.read_job()["status"])
        self.assertIn("prelaunch file Video/cameras.json", store.read_job()["reason"])
        self.assertNotIn("launch", FakeAdapter.calls)

    def test_a_busy_device_blocks_before_the_long_build_not_after_it(self):
        lease.LeaseManager(self.home).acquire("device:android:ZY22", "alice", "session-9", 600, "series", os.getpid())
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("alice", store.read_job()["reason"])
        self.assertEqual([], FakeAdapter.calls, "no build was started for a request that cannot get the device")
        self.assertEqual("free", lease.LeaseManager(self.home).status("unity-editor:slot1").state)

    def test_the_run_binds_the_application_and_the_host_decides_the_editor_slots(self):
        bound = []

        class Bound(FakeAdapter):
            def bind_application(self, app):
                bound.append(app)

        providers.register("android", Bound)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.json").write_text('{"unityEditorSlots": 1}', encoding="utf-8")
        manager = lease.LeaseManager(self.home)
        manager.acquire("unity-editor:slot1", "bob", "s", 600, "build", os.getpid())
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("BLOCKED", store.read_job()["status"], "the host has one slot and it is taken")
        self.assertIn("bob", store.read_job()["reason"])
        manager.release("unity-editor:slot1", "s", os.getpid())
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertEqual(["com.example.app"], bound)

    def test_a_folder_registered_by_the_build_is_removed_even_when_the_build_times_out(self):
        folder = self.work / "project" / "Builds" / "iOS"
        seen = {}

        class Hanging(FakeAdapter):
            def bind_temporary_registrar(self, register):
                seen["register"] = register

            def bind_build_budget(self, seconds):
                seen["budget"] = seconds

            def build(self, project, profile, out):
                folder.mkdir(parents=True)
                seen["register"](folder)  # registered at once, not when the build returns
                import time
                time.sleep(3)
                return StepOutcome(constants.PASS)

        providers.register("android", Hanging)
        stages = self.stages()
        stages[0]["timeoutSec"] = 1
        _, store = self.run_pipeline(self.pipeline_document(stages))
        self.assertEqual("TIMEOUT", store.read_job()["status"])
        self.assertEqual(1, seen["budget"])
        self.assertFalse(folder.exists())

    def test_folders_a_build_names_temporary_are_removed_after_the_result(self):
        extra = self.work / "project" / "Builds" / "iOS"

        class Exporting(FakeAdapter):
            def build(self, project, profile, out):
                extra.mkdir(parents=True, exist_ok=True)
                (extra / "Unity-iPhone.xcodeproj").write_text("x", encoding="utf-8")
                outcome = super().build(project, profile, out)
                return StepOutcome(outcome.status, outcome.reason, outcome.artifacts, {**outcome.details, "temp.export": str(extra)})

        providers.register("android", Exporting)
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertFalse(extra.exists(), "the export folder the build created is removed with the build output")
        self.assertTrue((self.work / "project" / "ProjectSettings").exists(), "the project itself is never touched")

    def test_an_emulator_is_rejected_for_a_performance_plan(self):
        FakeAdapter.devices = [phone("emulator")]
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("no attached device satisfies", store.read_job()["reason"])

    def test_no_adapter_is_blocked_with_the_platform_named(self):
        providers.unregister("android")
        with mock.patch.object(providers, "load_known"):  # the real android adapter would be imported (and registered) otherwise
            _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("no adapter for platform android", store.read_job()["reason"])

    def test_the_budget_of_the_plan_is_a_timeout(self):
        FakeAdapter.scenario = "advancing-forever"
        original = FakeAdapter.pull

        def pull(self, device, remote, local):
            if not FakeAdapter.launched:
                return StepOutcome(constants.PASS)
            FakeAdapter.pulls += 1
            run = local / "run-1"
            run.mkdir(parents=True, exist_ok=True)
            core.atomic_write_json(run / "heartbeat.json", {"seq": FakeAdapter.pulls})
            return StepOutcome(constants.PASS)

        with mock.patch.object(FakeAdapter, "pull", pull):
            stages = self.stages()
            stages[2]["timeoutSec"] = 1
            _, store = self.run_pipeline(self.pipeline_document(stages))
        self.assertEqual("TIMEOUT", store.read_job()["status"])
        self.assertEqual(constants.EXIT_STALL, constants.exit_code_for_status("TIMEOUT"))
        del original

    def test_an_earlier_runs_result_on_the_device_is_never_taken_as_this_runs(self):
        FakeAdapter.stale = True
        FakeAdapter.scenario = "hang"
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("STALL", store.read_job()["status"], "the previous run's PASS must not end this run")
        FakeAdapter.stale, FakeAdapter.scenario, FakeAdapter.launched, FakeAdapter.pulls = True, "pass", False, 0
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        result = self.result_of(store)
        self.assertEqual("PASS", result["status"])
        self.assertEqual([900, 100, 101, 99], result["metrics"][0]["samples"], "the samples are this run's, not the earlier run's")

    def test_artifacts_list_only_immutable_evidence_and_nothing_that_cleanup_removes(self):
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        result = self.result_of(store)
        paths = [a["path"] for a in result["artifacts"]]
        self.assertTrue(paths)
        for name in ("job.json", "events.jsonl", "spec.json"):
            self.assertNotIn(name, paths)
        self.assertFalse(any(p.startswith(("build/", "device-poll/", "device-before/")) for p in paths))
        for artifact in result["artifacts"]:
            self.assertEqual(artifact["sha256"], core.sha256_file(store.directory / artifact["path"]), artifact["path"])

    def test_a_checkout_that_is_not_the_candidate_commit_is_marked_dirty(self):
        git = lambda folder, arguments: (0, "") if arguments[0] == "status" else (0, "f" * 40)
        with mock.patch.object(pipeline.compare, "run_git", git):
            _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        result = self.result_of(store)
        self.assertTrue(result["candidate"]["dirty"])
        self.assertTrue(any("not the candidate" in item for item in result["unverified"]))

    def head_git(self, head):
        return lambda folder, arguments: (0, "") if arguments[0] == "status" else ((0, head) if head else (128, "fatal: not a git repository"))

    def test_an_omitted_or_zero_candidate_sha_is_the_checkout_head_and_is_recorded(self):
        head = "ab" * 20
        for sha in (None, "0" * 40):
            document = self.pipeline_document(self.stages())
            if sha is None:
                del document["candidate"]["sha"]
            else:
                document["candidate"]["sha"] = sha
            FakeAdapter.calls, FakeAdapter.launched, FakeAdapter.pulls = [], False, 0
            with mock.patch.object(pipeline.compare, "run_git", self.head_git(head)):
                _, store = self.run_pipeline(document)
            result = self.result_of(store)
            self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
            self.assertEqual((head, False), (result["candidate"]["sha"], result["candidate"]["dirty"]), sha)

    def test_an_unreadable_checkout_commit_leaves_no_sha_and_marks_the_result_dirty(self):
        document = self.pipeline_document(self.stages())
        del document["candidate"]["sha"]
        with mock.patch.object(pipeline.compare, "run_git", self.head_git(None)):
            _, store = self.run_pipeline(document)
        result = self.result_of(store)
        self.assertEqual(("0" * 40, True), (result["candidate"]["sha"], result["candidate"]["dirty"]))
        self.assertTrue(any("no commit" in item for item in result["unverified"]))

    def test_an_unreadable_working_tree_state_is_an_explicit_note_and_the_commit_is_kept(self):
        head = "ab" * 20
        document = self.pipeline_document(self.stages())
        del document["candidate"]["sha"]
        git = lambda folder, arguments: (128, "fatal: Unable to create index.lock") if arguments[0] == "status" else (0, head)
        with mock.patch.object(pipeline.compare, "run_git", git):
            _, store = self.run_pipeline(document)
        result = self.result_of(store)
        self.assertEqual((head, True), (result["candidate"]["sha"], result["candidate"]["dirty"]))
        self.assertTrue(any("working-tree state could not be read" in item and "index.lock" in item for item in result["unverified"]),
                        result["unverified"])

    def test_a_result_that_ends_before_the_run_still_says_why_it_has_no_commit_or_is_dirty(self):
        FakeAdapter.build_status = constants.FAIL
        document = self.pipeline_document(self.stages())
        del document["candidate"]["sha"]
        with mock.patch.object(pipeline.compare, "run_git", self.head_git(None)):
            _, store = self.run_pipeline(document)
        result = self.result_of(store)
        self.assertEqual(("0" * 40, True), (result["candidate"]["sha"], result["candidate"]["dirty"]))
        self.assertTrue(any("no commit" in item for item in result["unverified"]), result["unverified"])
        self.assertTrue(any("not a git repository" in item for item in result["unverified"]), result["unverified"])

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_project_in_a_repository_subfolder_keeps_the_head_and_is_clean_despite_the_runs_own_files_and_outside_edits(self):
        repo = Path(self._folder.name).resolve() / "repo"
        project = repo / "unity"
        (project / "ProjectSettings").mkdir(parents=True)
        (project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "readme.txt").write_text("a\n", encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        for arguments in (["init", "--quiet"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                                                                  "commit", "--quiet", "-m", "first"]):
            subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, env=environment)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
                              env=environment).stdout.strip()
        (repo / "docs" / "readme.txt").write_text("edited outside the project\n", encoding="utf-8")
        self.root = project / "artifacts"  # the run folder is created before the state is read, inside the project
        document = self.pipeline_document(self.stages())
        del document["candidate"]["sha"]
        _, store = self.run_pipeline(document, {"projectDir": str(project)})
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        result = self.result_of(store)
        self.assertEqual((head, False), (result["candidate"]["sha"], result["candidate"]["dirty"]), result["unverified"])

    def test_a_sha_named_on_the_command_line_must_be_the_checked_out_commit(self):
        head = "cd" * 20
        document = self.pipeline_document(self.stages())
        document["candidate"]["sha"] = head
        with mock.patch.object(pipeline.compare, "run_git", self.head_git(head)):
            _, store = self.run_pipeline(document, {"shaPinned": True})
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        FakeAdapter.calls, FakeAdapter.launched, FakeAdapter.pulls = [], False, 0
        document["candidate"]["sha"] = "ef" * 20
        with mock.patch.object(pipeline.compare, "run_git", self.head_git(head)):
            _, store = self.run_pipeline(document, {"shaPinned": True})
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("must name the commit that is checked out", job["reason"])
        self.assertEqual([], FakeAdapter.calls, "nothing ran for the wrong commit")

    def test_a_hung_adapter_call_cannot_hang_the_run(self):
        original = FakeAdapter.push

        def hanging_push(self, device, local, remote):
            import time
            time.sleep(30)

        with mock.patch.object(FakeAdapter, "push", hanging_push):
            _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"opTimeoutSec": 0.5})
        job = store.read_job()
        self.assertEqual("TIMEOUT", job["status"])
        self.assertIn("push did not return", job["reason"])
        del original

    def test_a_silent_long_pipeline_keeps_emitting_events_and_the_timeout_is_enforced(self):
        FakeAdapter.scenario = "hang"
        stages = self.stages(stallSec=600)
        stages[2]["timeoutSec"] = 600
        payload_request = dict(timeout_sec=2, tick_sec=0.3)
        _, store = self.run_pipeline(self.pipeline_document(stages), **payload_request)
        events = [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines()]
        self.assertIn("progress", [e["type"] for e in events if e["type"] != "step.started"][:50])
        self.assertTrue(sum(1 for e in events if e["type"] == "progress" and e.get("message") == "pipeline running") >= 2)
        self.assertEqual("TIMEOUT", store.read_job()["status"])
        self.assertEqual("TIMEOUT", json.loads(store.result.read_text(encoding="utf-8"))["status"], "the result agrees with the job")
        self.assertIn("timeout.detected", [e["type"] for e in events])

    def test_a_timeout_before_any_stage_produced_a_result_still_leaves_a_result(self):
        original = FakeAdapter.build

        def slow_build(self, project, profile, out):
            import time
            time.sleep(2.5)
            return original(self, project, profile, out)

        with mock.patch.object(FakeAdapter, "build", slow_build):
            _, store = self.run_pipeline(self.pipeline_document(self.stages()), timeout_sec=1, tick_sec=0.2)
        self.assertEqual("TIMEOUT", store.read_job()["status"])
        result = self.result_of(store)
        self.assertEqual("TIMEOUT", result["status"])
        self.assertNotIn("launch", FakeAdapter.calls)

    def test_select_device_honours_class_capabilities_and_id(self):
        devices = [phone(), DeviceInfo("android:2", "android", "phone-low", "physical", capabilities=("touch", "ble"))]
        plan = {"kind": "functional", "requires": {"capabilities": ["ble"]}}
        self.assertEqual("android:2", pipeline.select_device(devices, {}, plan)[0].id)
        self.assertIsNone(pipeline.select_device(devices, {"class": "tablet"}, plan)[0])
        self.assertEqual("android:ZY22", pipeline.select_device(devices, {"id": "android:ZY22"}, {"kind": "smoke"})[0].id)

    def test_select_device_tells_an_offline_device_from_one_that_does_not_exist(self):
        tablet = DeviceInfo("ios:TABLET", "ios", "tablet", "physical", capabilities=("touch", "camera.fast"))
        plan = {"kind": "functional", "requires": {"capabilities": ["camera.fast"]}}
        found, why = pipeline.select_device([], {"class": "tablet"}, plan, known=[tablet])
        self.assertIsNone(found)
        self.assertTrue(why.startswith("device offline: ios:TABLET"), why)
        found, why = pipeline.select_device([], {"class": "tablet"}, {"requires": {"capabilities": ["camera.faster"]}}, known=[tablet])
        self.assertIsNone(found)
        self.assertIn("no attached device satisfies: tablet, camera.faster", why)
        self.assertIn("no device with these settings is known", why)
        found, _ = pipeline.select_device([tablet], {"class": "tablet"}, plan, known=[tablet])
        self.assertEqual("ios:TABLET", found.id, "a registry entry only explains a failure; the attached device is selected")
        weak = DeviceInfo("ios:TABLET", "ios", "tablet", "physical", capabilities=("touch",))
        _, why = pipeline.select_device([weak], {"class": "tablet"}, plan, known=[tablet])
        self.assertNotIn("offline", why, "an attached device is never reported offline")

    def ui_adapter(self, replies, requests=None, push_fails=False):
        """An application that asks the host `requests` one per poll (default: `touch.tap` as `ui-1`) and ends after the last one."""
        asked = requests or [{"id": "ui-1", "action": "touch.tap", "args": {"target": "footer.3"}}]

        class Asking(FakeAdapter):
            def push(self, device, local, remote):
                if remote.startswith("gdv/inbox/reply-"):
                    replies[remote] = json.loads(Path(local).read_text(encoding="utf-8"))
                    if push_fails:
                        FakeAdapter.calls.append(f"push:{remote}")
                        return StepOutcome(constants.FAIL, "the inbox is not writable")
                return super().push(device, local, remote)

            def pull(self, device, remote, local):
                if not FakeAdapter.launched:
                    return super().pull(device, remote, local)
                FakeAdapter.calls.append("pull")
                FakeAdapter.pulls += 1
                run = local / "run-1"
                run.mkdir(parents=True, exist_ok=True)
                if FakeAdapter.pulls <= len(asked):
                    core.atomic_write_json(run / "heartbeat.json", {"seq": FakeAdapter.pulls, "state": "waiting-host",
                                                                    "request": asked[FakeAdapter.pulls - 1]})
                else:
                    core.atomic_write_json(run / "heartbeat.json", {"seq": FakeAdapter.pulls})
                    core.atomic_write_json(run / "result.json", {"status": "PASS", "reason": None, "steps": [], "metrics": [
                        {"name": "openMs", "unit": "ms", "samples": list(FakeAdapter.current_samples)}], "unverified": []})
                return StepOutcome(constants.PASS)
        providers.register("android", Asking)

    def ui_stages(self, command):
        stages = self.stages()
        stages[2]["externalUi"] = {"command": command, "timeoutSec": 20}
        return stages

    def test_an_external_ui_request_is_answered_by_the_stage_command_and_the_reply_reaches_the_inbox(self):
        replies = {}
        self.ui_adapter(replies)
        code = "import os,json;print(json.dumps({'message': 'tapped '+os.environ['GDV_UI_ARGS']+' on '+os.environ['GDV_UI_DEVICE']}))"
        stages = self.ui_stages([sys.executable, "-c", code])
        _, store = self.run_pipeline(self.pipeline_document(stages))
        self.assertEqual({"gdv/inbox/reply-ui-1.json"}, set(replies), "one request is answered once")
        self.assertEqual("PASS", replies["gdv/inbox/reply-ui-1.json"]["status"])
        self.assertIn("ZY22", replies["gdv/inbox/reply-ui-1.json"]["message"], "the platform's own id, not the prefixed one")
        self.assertLess(FakeAdapter.calls.index("launch"), FakeAdapter.calls.index("push:gdv/inbox/reply-ui-1.json"))
        paths = [a["path"] for a in self.result_of(store)["artifacts"]]
        self.assertTrue(any(p.endswith("external-ui/reply-ui-1.json") for p in paths), paths)

    def test_a_failing_stage_command_is_a_fail_reply_not_a_stuck_run(self):
        replies = {}
        self.ui_adapter(replies)
        _, store = self.run_pipeline(self.pipeline_document(self.ui_stages([sys.executable, "-c", "import sys;sys.exit(4)"])))
        self.assertEqual("FAIL", replies["gdv/inbox/reply-ui-1.json"]["status"])

    def test_a_reply_that_cannot_be_pushed_ends_the_run_with_a_clear_reason(self):
        replies = {}
        self.ui_adapter(replies, push_fails=True)
        _, store = self.run_pipeline(self.pipeline_document(self.ui_stages([sys.executable, "-c", "pass"])))
        result = self.result_of(store)
        self.assertEqual("FAIL", result["status"])
        self.assertIn("ui-1", result["reason"])
        self.assertIn("could not be pushed", result["reason"])
        self.assertIn("the inbox is not writable", result["reason"])

    def test_a_repeated_request_id_runs_the_command_once_and_several_requests_run_it_each(self):
        replies = {}
        counter = Path(self.work) / "calls.txt"
        requests = [{"id": rid, "action": "touch.tap", "args": {"target": "t"}} for rid in ("ui-1", "ui-1", "ui-2", "ui-2", "ui-3")]
        self.ui_adapter(replies, requests)
        code = "import sys;open(sys.argv[1], 'a').write(sys.argv[2] + chr(10))"
        _, store = self.run_pipeline(self.pipeline_document(self.ui_stages([sys.executable, "-c", code, str(counter), "{request}"])))
        self.assertEqual(["ui-1", "ui-2", "ui-3"], counter.read_text(encoding="utf-8").split())
        self.assertEqual({f"gdv/inbox/reply-ui-{n}.json" for n in (1, 2, 3)}, set(replies))
        self.assertEqual("PASS", self.result_of(store)["status"])

    def test_an_unsafe_request_is_a_blocked_reply_and_the_run_goes_on(self):
        replies = {}
        self.ui_adapter(replies, [{"id": "ui-1", "action": "a\u0000b", "args": {}}, {"id": "ui-2", "action": "-x", "args": {}}])
        _, store = self.run_pipeline(self.pipeline_document(self.ui_stages([sys.executable, "-c", "pass", "{action}"])))
        self.assertEqual("BLOCKED", replies["gdv/inbox/reply-ui-1.json"]["status"])
        self.assertEqual("BLOCKED", replies["gdv/inbox/reply-ui-2.json"]["status"])
        self.assertEqual("PASS", self.result_of(store)["status"], "the plan decides what a blocked reply means; the host run completes")

    def test_a_command_that_runs_longer_than_the_stall_window_does_not_stall_the_run(self):
        replies = {}
        self.ui_adapter(replies)
        _, store = self.run_pipeline(self.pipeline_document(self.ui_stages([sys.executable, "-c", "import time;time.sleep(2.5)"])))
        self.assertEqual("PASS", replies["gdv/inbox/reply-ui-1.json"]["status"])
        self.assertEqual("PASS", self.result_of(store)["status"])  # stallSec is 1 in `stages()`

    def test_a_stage_without_a_host_command_leaves_the_request_alone(self):
        replies = {}
        self.ui_adapter(replies)
        self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual({}, replies, "without externalUi the host writes no reply; an outside host may still answer")


if __name__ == "__main__":
    unittest.main()
