"""Stage selection and resume, the result cache, --keep-going and the repeat guard of `valtrion pipeline` (issue #82)."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path, PureWindowsPath
from unittest import mock

from valtrion import cli, commands, constants, core, jobs, pipeline, repeatguard, resultcache, resume

from tests import test_pipeline as base  # its test class is used through the module, so it is not run again here

SHA, FakeAdapter = base.SHA, base.FakeAdapter

OTHER_SHA = "d" * 40


def clean_git(head=SHA):
    """git as seen by the pipeline: HEAD is `head` and the tree is clean."""
    return lambda folder, arguments: (0, "") if arguments[0] == "status" else (0, head)


class _Harness(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    pipeline_document = base.PipelineTests.pipeline_document
    stages = base.PipelineTests.stages
    result_of = base.PipelineTests.result_of
    _run_pipeline = base.PipelineTests.run_pipeline

    def run_pipeline(self, document, payload_extra=None, git=None, **options):
        FakeAdapter.calls, FakeAdapter.launched, FakeAdapter.pulls = [], False, 0
        with mock.patch.object(pipeline.compare, "run_git", git or clean_git()):
            info, store = self._run_pipeline(document, payload_extra, **options)
        return info, store

    def two_series(self):
        return [{"stage": "build"}, {"stage": "install"},
                {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app", "stallSec": 1}, "timeoutSec": 30},
                {"stage": "run", "plan": "warm.plan.json", "args": {"app": "com.example.app", "stallSec": 1}, "timeoutSec": 30}]

    @contextlib.contextmanager
    def scenarios(self, *names):
        """Each launch takes the next scenario (`pass` or `hang`), then `pass`."""
        queue = list(names)
        original = FakeAdapter.launch

        def launch(adapter, *arguments):
            FakeAdapter.scenario = queue.pop(0) if queue else "pass"
            return original(adapter, *arguments)

        try:
            with mock.patch.object(FakeAdapter, "launch", launch):
                yield
        finally:
            FakeAdapter.scenario = "pass"

    def stage_records(self, result):
        return {item["index"]: item for item in result["pipelineRun"]["stages"]}

    def cache_folder(self):
        return resultcache.cache_root(self.home / "work")


class StageSelectionTests(_Harness):
    def test_a_full_run_records_every_stage_as_ran(self):
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        result = self.result_of(store)
        run = result["pipelineRun"]
        self.assertEqual("p1", run["pipelineId"])
        self.assertNotIn("resumedFrom", run)
        records = self.stage_records(result)
        self.assertEqual([("build", "ran", "PASS"), ("install", "ran", "PASS"), ("run", "ran", "PASS")],
                         [(records[i]["stage"], records[i]["disposition"], records[i]["status"]) for i in (1, 2, 3)])
        self.assertEqual("android:ZY22", records[2]["deviceId"])
        self.assertEqual(("perf.open", result["runId"]), (records[3]["plan"], records[3]["runId"]))
        self.assertTrue(all(len(item["stageSha256"]) == 64 for item in records.values()))

    def test_from_stage_runs_only_the_selected_stage_against_the_earlier_build_and_install(self):
        first, store = self.run_pipeline(self.pipeline_document(self.stages()))
        earlier = self.result_of(store)
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"fromStage": "3:run"}})
        job = store.read_job()
        self.assertEqual("PASS", job["status"], job["reason"])
        self.assertNotIn("build", FakeAdapter.calls)
        self.assertNotIn("install", FakeAdapter.calls)
        self.assertIn("launch", FakeAdapter.calls)
        result = self.result_of(store)
        records = self.stage_records(result)
        self.assertEqual(first["jobId"], result["pipelineRun"]["resumedFrom"])
        for index in (1, 2):
            self.assertEqual(("carried", "PASS", first["jobId"]), (records[index]["disposition"], records[index]["status"], records[index]["fromRun"]))
        self.assertEqual("ran", records[3]["disposition"])
        self.assertEqual(earlier["build"]["key"], result["build"]["key"], "the carried build is the one the result names")
        self.assertTrue(any("carried over from run " + first["jobId"] in item for item in result["unverified"]), result["unverified"])

    def test_a_carried_stage_names_the_run_that_actually_ran_it_through_a_chain_of_resumes(self):
        first, _ = self.run_pipeline(self.pipeline_document(self.stages()))
        second, _ = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}})
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}, "resumeFrom": second["jobId"]})
        records = self.stage_records(self.result_of(store))
        self.assertEqual(first["jobId"], records[1]["fromRun"])
        self.assertEqual(first["jobId"], records[2]["fromRun"])

    def test_a_selected_install_installs_the_earlier_published_build_without_building(self):
        self.run_pipeline(self.pipeline_document(self.stages()))
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["2", "3"]}})
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertNotIn("build", FakeAdapter.calls)
        self.assertIn("install", FakeAdapter.calls)

    def test_only_failed_runs_again_what_did_not_pass_and_then_has_nothing_left(self):
        with self.scenarios("hang"):
            failed, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual("STALL", store.read_job()["status"])
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"onlyFailed": True}})
        job = store.read_job()
        self.assertEqual("PASS", job["status"], job["reason"])
        self.assertEqual(["push:gdv/inbox/plan.json"], [c for c in FakeAdapter.calls if c in ("build", "install", "push:gdv/inbox/plan.json")])
        records = self.stage_records(self.result_of(store))
        self.assertEqual(["carried", "carried", "ran"], [records[i]["disposition"] for i in (1, 2, 3)])
        self.assertEqual(failed["jobId"], self.result_of(store)["pipelineRun"]["resumedFrom"])
        again, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"onlyFailed": True}})
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("nothing to run again", job["reason"])
        self.assertEqual([], FakeAdapter.calls)

    def test_a_carried_stage_that_did_not_pass_decides_the_job(self):
        with self.scenarios("pass", "hang"):
            first, store = self.run_pipeline(self.pipeline_document(self.two_series()))
        self.assertEqual("STALL", store.read_job()["status"])
        _, store = self.run_pipeline(self.pipeline_document(self.two_series()), {"selection": {"stages": ["3"]}})
        job = store.read_job()
        self.assertEqual("STALL", job["status"])
        self.assertIn(f"4:run carried over from run {first['jobId']} is STALL", job["reason"])
        result = self.result_of(store)
        self.assertEqual(("STALL", job["reason"]), (result["status"], result["reason"]))

    def test_a_resume_is_refused_without_an_earlier_run_or_for_another_commit_or_a_dirty_tree(self):
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}})
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn("no earlier run of pipeline p1", store.read_job()["reason"])
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}, "resumeFrom": "nope"})
        self.assertIn("no such job", store.read_job()["reason"])
        first, _ = self.run_pipeline(self.pipeline_document(self.stages()))
        document = self.pipeline_document(self.stages())
        document["candidate"]["sha"] = OTHER_SHA
        _, store = self.run_pipeline(document, {"selection": {"stages": ["3"]}, "resumeFrom": first["jobId"]}, git=clean_git(OTHER_SHA))
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertIn(f"tested commit {SHA[:12]}, not {OTHER_SHA[:12]}", store.read_job()["reason"])
        self.assertEqual([], FakeAdapter.calls, "nothing ran against another commit's install")
        dirty_git = lambda folder, arguments: (0, " M Assets/a.cs") if arguments[0] == "status" else (0, SHA)
        dirty, _ = self.run_pipeline(self.pipeline_document(self.stages()), git=dirty_git)
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}, "resumeFrom": dirty["jobId"]})
        self.assertIn("dirty tree", store.read_job()["reason"])

    def test_a_running_earlier_job_or_one_without_a_stage_record_is_refused(self):
        first, store = self.run_pipeline(self.pipeline_document(self.stages()))
        result = self.result_of(store)
        del result["pipelineRun"]
        core.atomic_write_json(store.result, result)
        _, other = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}, "resumeFrom": first["jobId"]})
        self.assertIn("has no stage record", other.read_job()["reason"])
        job = store.read_job()
        core.atomic_write_json(store.job_json, {**job, "state": "running"})
        _, other = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}, "resumeFrom": first["jobId"]})
        self.assertIn("is still running", other.read_job()["reason"])

    def test_a_carried_stage_that_changed_or_a_failed_prerequisite_is_refused(self):
        first, _ = self.run_pipeline(self.pipeline_document(self.stages()))
        changed = self.stages()
        changed[1]["timeoutSec"] = 99
        _, store = self.run_pipeline(self.pipeline_document(changed), {"selection": {"stages": ["3"]}})
        self.assertIn("stage 2:install changed since the earlier run", store.read_job()["reason"])
        plan = json.loads((self.work / "plans" / "open.plan.json").read_text(encoding="utf-8"))
        (self.work / "plans" / "open.plan.json").write_text(json.dumps({**plan, "budgetSeconds": 31}), encoding="utf-8")
        _, store = self.run_pipeline(self.pipeline_document(self.two_series()), {"selection": {"stages": ["4"]}})
        self.assertIn("differ from this pipeline's", store.read_job()["reason"], "the newest run of p1 had three stages")
        self.run_pipeline(self.pipeline_document(self.two_series()))
        (self.work / "plans" / "open.plan.json").write_text(json.dumps(plan), encoding="utf-8")
        _, store = self.run_pipeline(self.pipeline_document(self.two_series()), {"selection": {"stages": ["4"]}})
        self.assertIn("the plan of stage 3:run changed", store.read_job()["reason"])
        FakeAdapter.build_status = constants.FAIL
        shutil.rmtree(self.home / "builds")  # otherwise the clean build of this commit is reused from the build cache
        failed, _ = self.run_pipeline(self.pipeline_document(self.stages()))
        FakeAdapter.build_status = constants.PASS
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}, "resumeFrom": failed["jobId"]})
        self.assertIn("stage 1:build of the earlier run", store.read_job()["reason"])
        self.assertIn("is FAIL", store.read_job()["reason"])

    def test_a_selected_install_is_refused_when_the_earlier_build_is_gone(self):
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        key = self.result_of(store)["build"]["key"]
        shutil.rmtree(self.home / "builds" / key)
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["2", "3"]}})
        self.assertIn(f"needs the build {key}", store.read_job()["reason"])
        self.assertIn("no longer in the build cache", store.read_job()["reason"])

    def test_a_selected_run_uses_the_device_of_the_carried_install(self):
        self.run_pipeline(self.pipeline_document(self.stages()))
        FakeAdapter.devices = [DeviceOther(), *FakeAdapter.devices]
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"selection": {"stages": ["3"]}})
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertEqual("android:ZY22", self.result_of(store)["target"]["deviceId"])
        document = self.pipeline_document(self.stages())
        document["device"]["id"] = "android:AA00"
        _, store = self.run_pipeline(document, {"selection": {"stages": ["3"]}})
        self.assertIn("installed on android:ZY22, not on the device android:AA00", store.read_job()["reason"])

    def test_selectors(self):
        stages = self.stages()
        self.assertEqual(2, resume.parse_selector("3", stages))
        self.assertEqual(2, resume.parse_selector(" 3:run ", stages))
        for bad in ("0", "4", "x", "3:build", "", "-1", "1.5"):
            with self.assertRaises(resume.ResumeError, msg=bad):
                resume.parse_selector(bad, stages)
        self.assertEqual(["1", "3:run", "2"], resume.split_list(["1,3:run", " 2 ", ","]))
        plan = resume.plan_resume({"fromStage": "1"}, None, self.pipeline_document(stages), SHA, self.work / "plans", "android", self.home)
        self.assertIsNone(plan.prior, "selecting every stage carries nothing and needs no earlier run")
        with self.assertRaises(resume.ResumeError):
            resume.plan_resume(None, "job", self.pipeline_document(stages), SHA, self.work / "plans", "android", self.home)


def DeviceOther():
    from valtrion.adapter import DeviceInfo
    return DeviceInfo("android:AA00", "android", "phone-mid", "physical", "moto e", "Android 15", ("touch",))


class KeepGoingTests(_Harness):
    def test_keep_going_runs_every_independent_stage_and_ends_fail(self):
        with self.scenarios("hang", "pass"):
            _, store = self.run_pipeline(self.pipeline_document(self.two_series()), {"keepGoing": True})
        job = store.read_job()
        self.assertEqual("FAIL", job["status"])
        self.assertIn("1 of 4 stages did not pass", job["reason"])
        self.assertIn("3:run STALL", job["reason"])
        self.assertEqual(2, FakeAdapter.calls.count("launch"))
        result = self.result_of(store)
        self.assertEqual(("FAIL", True), (result["status"], result["pipelineRun"]["keepGoing"]))
        records = self.stage_records(result)
        self.assertEqual([("ran", "STALL"), ("ran", "PASS")], [(records[i]["disposition"], records[i]["status"]) for i in (3, 4)])
        summary = json.loads((store.directory / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(["STALL", "PASS"], [entry["status"] for entry in summary])

    def test_without_keep_going_the_first_failure_stops_and_the_rest_did_not_run(self):
        with self.scenarios("hang", "pass"):
            _, store = self.run_pipeline(self.pipeline_document(self.two_series()))
        self.assertEqual("STALL", store.read_job()["status"])
        self.assertEqual(1, FakeAdapter.calls.count("launch"))
        records = self.stage_records(self.result_of(store))
        self.assertEqual("not-run", records[4]["disposition"])
        self.assertNotIn("status", records[4])

    def test_keep_going_with_every_stage_passing_is_a_pass(self):
        _, store = self.run_pipeline(self.pipeline_document(self.two_series()), {"keepGoing": True})
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])

    def test_a_failed_build_still_stops_a_keep_going_pipeline(self):
        FakeAdapter.build_status = constants.FAIL
        _, store = self.run_pipeline(self.pipeline_document(self.two_series()), {"keepGoing": True})
        job = store.read_job()
        self.assertEqual("FAIL", job["status"])
        self.assertIn("1:build FAIL", job["reason"])
        self.assertNotIn("install", FakeAdapter.calls)


class ResultCachePipelineTests(_Harness):
    def stored(self):
        folder = self.cache_folder()
        return sorted(folder.glob("*.json")) if folder.is_dir() else []

    def test_a_pass_is_stored_by_default_but_reused_only_when_asked(self):
        first, store = self.run_pipeline(self.pipeline_document(self.stages()))
        produced = self.result_of(store)
        self.assertEqual(1, len(self.stored()))
        first, store = self.run_pipeline(self.pipeline_document(self.stages()))
        produced = self.result_of(store)
        self.assertIn("launch", FakeAdapter.calls, "without --reuse-results the plan runs again")
        self.assertEqual(1, len(self.stored()), "the same key: the newer pass replaces the older one")
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"resultCache": "reuse"})
        job = store.read_job()
        self.assertEqual("PASS", job["status"], job["reason"])
        self.assertEqual([], [c for c in FakeAdapter.calls if c in ("build", "install", "launch")], "every run stage was answered")
        result = self.result_of(store)
        self.assertEqual(produced["runId"], result["reusedFrom"]["runId"])
        self.assertEqual(first["jobId"], result["reusedFrom"]["jobId"])
        self.assertTrue(any(f"reused from run {produced['runId']}" in item for item in result["unverified"]))
        records = self.stage_records(result)
        self.assertEqual(["not-run", "not-run", "reused"], [records[i]["disposition"] for i in (1, 2, 3)])
        self.assertEqual(produced["runId"], records[3]["fromRun"])

    def test_host_json_can_turn_reuse_on_and_no_cache_turns_it_off_again(self):
        self.run_pipeline(self.pipeline_document(self.stages()))
        self.home.mkdir(parents=True, exist_ok=True)
        core.atomic_write_json(self.home / "host.json", {"resultCache": {"reuse": True}})
        _, store = self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertNotIn("launch", FakeAdapter.calls)
        self.assertIn("reusedFrom", self.result_of(store))
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"resultCache": "off"})
        self.assertIn("launch", FakeAdapter.calls)
        self.assertNotIn("reusedFrom", self.result_of(store))

    def test_no_cache_stores_nothing(self):
        self.run_pipeline(self.pipeline_document(self.stages()), {"resultCache": "off"})
        self.assertEqual([], self.stored())

    def test_a_result_that_did_not_pass_is_never_stored_or_reused(self):
        with self.scenarios("hang"):
            self.run_pipeline(self.pipeline_document(self.stages()))
        self.assertEqual([], self.stored())
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"resultCache": "reuse"})
        self.assertIn("launch", FakeAdapter.calls)

    def test_a_dirty_tree_neither_stores_nor_reuses(self):
        dirty_git = lambda folder, arguments: (0, "?? new.txt") if arguments[0] == "status" else (0, SHA)
        self.run_pipeline(self.pipeline_document(self.stages()), git=dirty_git)
        self.assertEqual([], self.stored())
        self.run_pipeline(self.pipeline_document(self.stages()))
        self.run_pipeline(self.pipeline_document(self.stages()), {"resultCache": "reuse"}, git=dirty_git)
        self.assertIn("launch", FakeAdapter.calls)

    def test_only_the_stages_that_hit_are_reused_and_the_others_run(self):
        self.run_pipeline(self.pipeline_document(self.stages()))
        _, store = self.run_pipeline(self.pipeline_document(self.two_series()), {"resultCache": "reuse"})
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertEqual(1, FakeAdapter.calls.count("launch"), "the second series had no cached result")
        records = self.stage_records(self.result_of(store))
        self.assertEqual(["ran", "ran", "reused", "ran"], [records[i]["disposition"] for i in (1, 2, 3, 4)])

    def test_a_tampered_entry_is_ignored_and_the_plan_runs(self):
        self.run_pipeline(self.pipeline_document(self.stages()))
        (entry,) = self.stored()
        document = json.loads(entry.read_text(encoding="utf-8"))
        document["result"]["metrics"][0]["samples"] = [1, 1, 1, 1]
        entry.write_text(json.dumps(document), encoding="utf-8")
        _, store = self.run_pipeline(self.pipeline_document(self.stages()), {"resultCache": "reuse"})
        self.assertIn("launch", FakeAdapter.calls)
        self.assertNotIn("reusedFrom", self.result_of(store))
        events = store.events.read_text(encoding="utf-8")
        self.assertIn("does not match its recorded hash", events)

    def test_a_changed_stage_or_plan_is_another_key(self):
        self.run_pipeline(self.pipeline_document(self.stages()))
        self.run_pipeline(self.pipeline_document(self.stages(launchArgs=["-x"])), {"resultCache": "reuse"})
        self.assertIn("launch", FakeAdapter.calls, "other launch arguments are another stage")
        self.assertEqual(2, len(self.stored()))


def _result(status=constants.PASS, sha=SHA, dirty=False, **changes):
    document = {
        "schema": "gdv.result/1", "runId": "job-3-1", "status": status, "reason": None if status == constants.PASS else f"it ended {status}",
        "candidate": {"repo": "Sample", "sha": sha, "profile": "Validation", "unity": "6000.6.7f1", "dirty": dirty},
        "target": {"platform": "android", "deviceId": "android:ZY22", "deviceClass": "phone-mid", "osMajor": 15, "evidence": "physical"},
        "host": {"id": "win", "transport": "local", "selectedBy": "local"}, "plan": {"id": "perf.open", "sha256": "a" * 64},
        "startedUtc": "2026-11-04T10:00:00Z", "finishedUtc": "2026-11-04T10:01:00Z", "layers": {"scenario": status},
        "steps": [], "metrics": [], "artifacts": [], "unverified": []}
    document.update(changes)
    return document


def _key(**changes):
    values = {"repo": "Sample", "platform": "android", "sha": SHA, "plan_sha256": "a" * 64, "device_class": "phone-mid",
              "profile": "Validation", "stage_sha256": "b" * 64, "core_version": "0.13.1"}
    values.update(changes)
    return resultcache.Key(**values)


class ResultCacheUnitTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "work" / resultcache.FOLDER

    def test_a_stored_pass_is_found_and_counts_as_a_use(self):
        path = resultcache.store(self.root, _key(), _result(), "job")
        self.assertEqual(f"{_key().digest}.json", path.name)
        os.utime(path, (1, 1))
        hit, why = resultcache.lookup(self.root, _key())
        self.assertEqual(("reused", "job-3-1", "job"), (why, hit.result["runId"], hit.job_id))
        self.assertGreater(path.stat().st_mtime, 1)
        os.utime(path, (1, 1))
        resultcache.lookup(self.root, _key(), touch=False)
        self.assertEqual(1, path.stat().st_mtime)

    def test_every_status_but_pass_is_never_stored(self):
        for status in (constants.FAIL, constants.BLOCKED, constants.STALL, constants.TIMEOUT, constants.NEEDS_PERMISSION):
            self.assertIsNone(resultcache.store(self.root, _key(), _result(status), "job"), status)
            self.assertEqual("no cached result", resultcache.lookup(self.root, _key())[1])

    def test_a_dirty_or_commitless_or_mismatching_or_invalid_result_is_not_stored(self):
        self.assertIsNone(resultcache.store(self.root, _key(), _result(dirty=True), "job"))
        self.assertIsNone(resultcache.store(self.root, _key(sha="0" * 40), _result(sha="0" * 40), "job"))
        self.assertIsNone(resultcache.store(self.root, _key(device_class="tablet"), _result(), "job"))
        self.assertIsNone(resultcache.store(self.root, _key(), {**_result(), "metrics": "x"}, "job"))
        self.assertIsNone(resultcache.store(self.root, _key(), _result(metrics=[{"name": "m", "unit": "ms", "samples": [1.5] * 900_000,
                                                                                 "stats": {"n": 1, "median": 1, "p90": 1, "max": 1}}]), "job"))

    def test_a_comparison_is_not_kept(self):
        path = resultcache.store(self.root, _key(), _result(comparison={"baseline": "b", "verdict": "PASS", "details": ["x"]}), "job")
        self.assertNotIn("comparison", json.loads(path.read_text(encoding="utf-8"))["result"])

    def test_every_key_component_changes_the_key(self):
        base = _key()
        variants = [_key(repo="Other"), _key(platform="ios"), _key(sha=OTHER_SHA), _key(plan_sha256="c" * 64), _key(device_class="tablet"),
                    _key(profile="Release"), _key(stage_sha256="d" * 64), _key(core_version="0.14.0")]
        digests = {base.digest, *(variant.digest for variant in variants)}
        self.assertEqual(9, len(digests))
        self.assertEqual({"repo", "platform", "sha", "planSha256", "deviceClass", "profile", "coreVersion", "stageSha256"}, set(base.components()))

    def test_another_core_version_is_a_miss(self):
        resultcache.store(self.root, _key(), _result(), "job")
        self.assertIsNone(resultcache.lookup(self.root, _key(core_version="9.9.9"))[0])

    def tamper(self, change):
        path = resultcache.store(self.root, _key(), _result(), "job")
        entry = json.loads(path.read_text(encoding="utf-8"))
        change(entry, path)
        return resultcache.lookup(self.root, _key())

    def test_corrupted_foreign_or_rehashed_entries_are_ignored(self):
        def edit_result(entry, path):
            entry["result"]["unverified"] = ["edited"]
            path.write_text(json.dumps(entry), encoding="utf-8")

        def rehash_as_fail(entry, path):
            entry["result"].update(status="FAIL", reason="x", layers={"scenario": "FAIL"})
            entry["resultSha256"] = core.sha256_bytes(resultcache._canonical(entry["result"]))
            path.write_text(json.dumps(entry), encoding="utf-8")

        def rehash_other_commit(entry, path):
            entry["result"]["candidate"]["sha"] = OTHER_SHA
            entry["resultSha256"] = core.sha256_bytes(resultcache._canonical(entry["result"]))
            path.write_text(json.dumps(entry), encoding="utf-8")

        def rehash_invalid(entry, path):
            entry["result"]["metrics"] = [{"name": "bad name!"}]
            entry["resultSha256"] = core.sha256_bytes(resultcache._canonical(entry["result"]))
            path.write_text(json.dumps(entry), encoding="utf-8")

        def other_key(entry, path):
            entry["components"]["deviceClass"] = "tablet"
            path.write_text(json.dumps(entry), encoding="utf-8")

        cases = {
            "truncated": (lambda entry, path: path.write_bytes(path.read_bytes()[:40]), "unreadable"),
            "not an entry": (lambda entry, path: path.write_text("[]", encoding="utf-8"), "not a result cache entry"),
            "edited result": (edit_result, "does not match its recorded hash"),
            "rehashed failure": (rehash_as_fail, "only PASS is reused"),
            "rehashed other commit": (rehash_other_commit, "has sha"),
            "rehashed invalid result": (rehash_invalid, "not a valid gdv.result/1"),
            "foreign key": (other_key, "stored for another key"),
            "no job": (lambda entry, path: path.write_text(json.dumps({**entry, "jobId": ""}), encoding="utf-8"), "which job"),
        }
        for name, (change, expected) in cases.items():
            shutil.rmtree(self.root, ignore_errors=True)
            hit, why = self.tamper(change)
            self.assertIsNone(hit, name)
            self.assertIn(expected, why, name)

    def test_an_entry_copied_under_another_key_is_ignored(self):
        path = resultcache.store(self.root, _key(), _result(), "job")
        other = _key(device_class="tablet")
        shutil.copyfile(path, self.root / f"{other.digest}.json")
        self.assertIn("another key", resultcache.lookup(self.root, other)[1])

    def test_an_oversized_entry_is_not_read(self):
        path = resultcache.store(self.root, _key(), _result(), "job")
        with open(path, "ab") as handle:
            handle.write(b" " * (resultcache.MAX_ENTRY_BYTES + 1))
        self.assertIn("larger than", resultcache.lookup(self.root, _key())[1])

    def test_a_link_is_never_followed(self):
        target = resultcache.store(self.root, _key(), _result(), "job")
        moved = target.with_name("elsewhere.json")
        os.replace(target, moved)
        try:
            os.symlink(moved, target)
        except (OSError, NotImplementedError):
            self.skipTest("this host cannot create symbolic links")
        self.assertIn("link", resultcache.lookup(self.root, _key())[1])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_an_entry_others_can_write_is_ignored_and_entries_are_private(self):
        path = resultcache.store(self.root, _key(), _result(), "job")
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)
        os.chmod(path, 0o666)
        self.assertIn("writable by other users", resultcache.lookup(self.root, _key())[1])

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "geteuid"), "POSIX ownership")
    def test_an_entry_of_another_user_is_ignored(self):
        path = resultcache.store(self.root, _key(), _result(), "job")
        with mock.patch.object(os, "geteuid", lambda: path.stat().st_uid + 1):
            self.assertIn("another user", resultcache.lookup(self.root, _key())[1])

    def test_gc_removes_old_and_surplus_entries_and_stray_temporary_files(self):
        paths = [resultcache.store(self.root, _key(stage_sha256=f"{n:064x}"), _result(), "job") for n in range(4)]
        for age, path in zip((40, 3, 2, 1), paths):
            stamp = 1_000_000 - age * 86400
            os.utime(path, (stamp, stamp))
        stray = self.root / ".incoming-abc.tmp"
        stray.write_bytes(b"x")
        os.utime(stray, (1, 1))
        removed = resultcache.gc(self.root, keep_days=14, max_entries=2, now=lambda: 1_000_000)
        self.assertEqual({paths[0].name, paths[1].name, stray.name}, set(removed))
        self.assertEqual({paths[2].name, paths[3].name}, {p.name for p in self.root.glob("*.json")})
        with self.assertRaises(ValueError):
            resultcache.gc(self.root, keep_days=-1)

    def test_keys_are_hex_and_never_name_a_path_outside_the_cache(self):
        self.assertTrue(resultcache._valid_digest(_key().digest))
        for bad in ("..\\..\\x", "../x", "A" * 64, "a" * 63, "C:\\x"):
            self.assertFalse(resultcache._valid_digest(bad))

    def test_a_windows_style_relative_prelaunch_path_is_hashed_from_the_pipeline_folder(self):
        base = self.root.parent
        base.mkdir(parents=True, exist_ok=True)
        (base / "cfg").mkdir()
        (base / "cfg" / "camera.json").write_text("{}", encoding="utf-8")
        stage = {"stage": "run", "plan": "p.json", "prelaunchFiles": [{"local": str(PureWindowsPath("cfg", "camera.json")) if os.name == "nt"
                                                                       else "cfg/camera.json", "remote": "config/camera.json"}]}
        first = resultcache.stage_fingerprint(stage, base)
        (base / "cfg" / "camera.json").write_text('{"x": 1}', encoding="utf-8")
        self.assertNotEqual(first, resultcache.stage_fingerprint(stage, base), "the content of a pre-launch file is part of the stage")
        self.assertEqual(resultcache.stage_fingerprint(stage, base), resultcache.stage_fingerprint({**stage, "plan": "C:\\elsewhere\\p.json",
                                                                                                    "cleanup": ["out"]}, base))


class _CliMixin:
    def write_pipeline(self, document):
        path = self.work / "plans" / "p1.pipeline.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def cli(self, *argv, capture=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(pipeline.compare, "run_git", clean_git()):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def captured_payloads(self):
        payloads = []

        def start(request, *arguments, **options):
            payloads.append(request.payload)
            return {"jobId": f"job-{len(payloads)}", "dir": str(self.root / f"job-{len(payloads)}")}

        return payloads, mock.patch.object(jobs, "start_job", start)


class CliTests(_CliMixin, _Harness):
    def test_the_new_options_reach_the_job_payload(self):
        path = self.write_pipeline(self.pipeline_document(self.stages()))
        payloads, patch = self.captured_payloads()
        with patch:
            code, _, err = self.cli("pipeline", str(path), "--local", "--project-dir", str(self.work / "project"), "--stages", "1,3:run",
                                    "--stages", "2", "--resume-from", "J-1", "--keep-going", "--reuse-results")
        self.assertEqual(0, code, err)
        self.assertEqual({"stages": ["1", "3:run", "2"]}, payloads[0]["selection"])
        self.assertEqual(("J-1", True, "reuse"), (payloads[0]["resumeFrom"], payloads[0]["keepGoing"], payloads[0]["resultCache"]))
        with patch:
            # Selecting every stage carries nothing, so no earlier run is needed for these to start.
            self.cli("pipeline", str(path), "--local", "--project-dir", str(self.work / "project"), "--from-stage", "1", "--no-cache")
            self.cli("pipeline", str(path), "--local", "--project-dir", str(self.work / "project"))
        self.assertEqual(({"fromStage": "1"}, "off"), (payloads[1]["selection"], payloads[1]["resultCache"]))
        for name in ("selection", "resumeFrom", "keepGoing", "resultCache"):
            self.assertNotIn(name, payloads[2], "without the options the payload is as before")
        payload = {}
        self.assertIsNone(commands._selection_payload(mock.Mock(stages=None, from_stage=None, only_failed=True, resume_from=None,
                                                                keep_going=False, reuse_results=False, no_cache=False),
                                                      self.pipeline_document(self.stages()), payload))
        self.assertEqual({"selection": {"onlyFailed": True}}, payload)
        code, _, err = self.cli("pipeline", str(path), "--local", "--project-dir", str(self.work / "project"), "--only-failed")
        self.assertIn("no earlier run", err, "the earlier run is looked for before a job is created")

    def test_conflicting_or_bad_options_are_refused_before_a_job_exists(self):
        path = self.write_pipeline(self.pipeline_document(self.stages()))
        payloads, patch = self.captured_payloads()
        project = ["--local", "--project-dir", str(self.work / "project")]
        with patch:
            for extra in (["--stages", "1", "--from-stage", "2"], ["--stages", "1", "--only-failed"], ["--reuse-results", "--no-cache"]):
                with self.assertRaises(SystemExit, msg=extra):
                    self.cli("pipeline", str(path), *project, *extra)
            for extra, message in ((["--resume-from", "J-1"], "needs --stages"), (["--stages", "9"], "is not one of 1..3"),
                                   (["--from-stage", "2:run"], "is a install stage"), (["--stages", "3", "--resume-from", "../x"], "not a job id"),
                                   (["--stages", "3"], "no earlier run of pipeline p1")):
                code, _, err = self.cli("pipeline", str(path), *project, *extra)
                self.assertEqual(constants.EXIT_BLOCKED, code, extra)
                self.assertIn(message, err, extra)
        self.assertEqual([], payloads)

    def test_cache_status_prints_the_key_and_state_of_every_run_stage_and_starts_nothing(self):
        document = self.pipeline_document(self.stages())
        document["device"]["class"] = "phone-mid"
        path = self.write_pipeline(document)
        payloads, patch = self.captured_payloads()
        project = ["--local", "--project-dir", str(self.work / "project")]
        with patch:
            code, out, err = self.cli("pipeline", str(path), *project, "--cache-status")
        self.assertEqual(0, code, err)
        rows = json.loads(out)["stages"]
        self.assertEqual([("3:run", "miss", "no cached result")], [(r["stage"], r["state"], r["reason"]) for r in rows])
        self.assertEqual([], payloads)
        self.run_pipeline(document)
        with patch:
            code, out, _ = self.cli("pipeline", str(path), *project, "--cache-status")
        (row,) = json.loads(out)["stages"]
        self.assertEqual("hit", row["state"])
        self.assertEqual(SHA, row["components"]["sha"])
        del document["device"]["class"]
        path = self.write_pipeline(document)
        with patch:
            code, out, _ = self.cli("pipeline", str(path), *project, "--cache-status")
        self.assertEqual("unknown", json.loads(out)["stages"][0]["state"])


class RepeatGuardTests(_CliMixin, _Harness):
    def guard(self, window):
        self.home.mkdir(parents=True, exist_ok=True)
        core.atomic_write_json(self.home / "host.json", {"repeatGuard": {"windowSec": window}})

    def start(self, path, *extra):
        return self.cli("pipeline", str(path), "--local", "--project-dir", str(self.work / "project"), *extra)

    def test_an_identical_second_start_is_refused_naming_the_first_and_allow_repeat_overrides(self):
        self.guard(600)
        path = self.write_pipeline(self.pipeline_document(self.stages()))
        payloads, patch = self.captured_payloads()
        with patch:
            self.assertEqual(0, self.start(path)[0])
            code, _, err = self.start(path)
            self.assertEqual(constants.EXIT_BLOCKED, code)
            self.assertIn("job job-1", err)
            self.assertIn("--allow-repeat", err)
            self.assertEqual(0, self.start(path, "--allow-repeat")[0])
            code, _, err = self.start(path)
            self.assertIn("job job-2", err, "the newest start is the one named")
        self.assertEqual(2, len(payloads))

    def test_a_changed_input_is_a_new_start(self):
        self.guard(600)
        path = self.write_pipeline(self.pipeline_document(self.stages()))
        payloads, patch = self.captured_payloads()
        with patch:
            self.start(path)
            self.assertEqual(0, self.start(path, "--keep-going")[0], "another option")
            plan = self.work / "plans" / "open.plan.json"
            plan.write_text(plan.read_text(encoding="utf-8") + " ", encoding="utf-8")
            self.assertEqual(0, self.start(path)[0], "the plan changed")
            dirty = lambda folder, arguments: (0, "?? Assets/new.cs") if arguments[0] == "status" else (0, SHA)
            (self.work / "project" / "Assets").mkdir()
            (self.work / "project" / "Assets" / "new.cs").write_text("a", encoding="utf-8")
            with mock.patch.object(pipeline.compare, "run_git", dirty), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["pipeline", str(path), "--local", "--project-dir", str(self.work / "project")])
            self.assertEqual(0, code, "an uncommitted change is an input")
        self.assertEqual(4, len(payloads))

    def test_the_guard_is_off_by_default_and_ends_with_its_window(self):
        path = self.write_pipeline(self.pipeline_document(self.stages()))
        payloads, patch = self.captured_payloads()
        with patch:
            self.start(path)
            self.assertEqual(0, self.start(path)[0])
        self.assertFalse(repeatguard.guard_root(self.home).exists())
        key = "k" * 8
        repeatguard.record(key, "job-x", self.home)
        later = lambda: core.now() + timedelta(seconds=601)
        self.assertIsNotNone(repeatguard.check(key, 600, self.home))
        self.assertIsNone(repeatguard.check(key, 600, self.home, now=later))
        self.assertIsNone(repeatguard.check(key, 0, self.home))
        self.assertEqual([], repeatguard.prune(600, self.home))
        self.assertEqual([key], repeatguard.prune(600, self.home, now=lambda: core.now() + timedelta(days=2)))

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_project_folder_spelled_in_another_case_is_the_same_input_on_windows(self):
        document = self.pipeline_document(self.stages())
        upper = Path(str(self.work / "project").upper())
        first = repeatguard.fingerprint(document, {}, self.work / "plans", self.work / "project", clean_git())
        self.assertEqual(first, repeatguard.fingerprint(document, {}, self.work / "plans", upper, clean_git()))

    def test_bad_host_settings_are_refused(self):
        from valtrion import config
        for value in ({"windowSec": -1}, {"windowSec": True}, {"other": 1}, 5):
            core.atomic_write_json(self.home / "host.json", {"repeatGuard": value})
            with self.assertRaises(config.ConfigError, msg=value):
                config.load_host_settings(self.home)
        for value in ({"reuse": "yes"}, {"keepDays": -1}, {"maxEntries": 1.5}, {"other": 1}, []):
            core.atomic_write_json(self.home / "host.json", {"resultCache": value})
            with self.assertRaises(config.ConfigError, msg=value):
                config.load_host_settings(self.home)
        core.atomic_write_json(self.home / "host.json", {"resultCache": {"reuse": True, "keepDays": 3, "maxEntries": 10},
                                                         "repeatGuard": {"windowSec": 900}})
        settings = config.load_host_settings(self.home)
        self.assertEqual((True, 3.0, 10, 900.0), (settings.result_cache.reuse, settings.result_cache.keep_days,
                                                  settings.result_cache.max_entries, settings.repeat_guard_sec))


class ReportTests(unittest.TestCase):
    def test_a_reused_result_says_which_run_it_came_from(self):
        from valtrion import report
        from valtrion.select import Check
        result = _result(reusedFrom={"runId": "job-a-3-1", "key": "e" * 64})
        row = report._result_row(Check("perf.open", "device", "android", "L2"), "android", result)
        self.assertIn("reused from run job-a-3-1", row.detail)


if __name__ == "__main__":
    unittest.main()
