import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import constants, core, jobs, lease, providers
from valtrion.providers import editor as editor_module
from valtrion.providers.editor import EditorAdapter, normalize_nunit
from valtrion.schema import Validator

RUNNER = r'''
import argparse, json, os, sys, time
parser = argparse.ArgumentParser()
parser.add_argument("--editor"); parser.add_argument("--output"); parser.add_argument("--assemblies"); parser.add_argument("--timeout")
args = parser.parse_args()
mode = os.environ.get("GDV_TEST_MODE", "pass")
os.makedirs(args.output, exist_ok=True)
out = os.path.join(os.environ["GDV_OUT_ROOT"], "run-1")
def xml(passed):
    body = '<test-case fullname="A.Passes" result="Passed" duration="0.5"/>'
    if not passed:
        body += '<test-case fullname="A.Breaks" result="Failed" duration="0.1"><failure><message>expected 1</message></failure></test-case>'
    total = 1 if passed else 2
    with open(os.path.join(args.output, "PlayMode.xml"), "w") as handle:
        handle.write('<test-run total="%d" passed="1" failed="%d" skipped="0" inconclusive="0" result="%s">%s</test-run>' % (total, total - 1, "Passed" if passed else "Failed", body))
def harness(status):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "heartbeat.json"), "w") as handle:
        json.dump({"seq": 1}, handle)
    with open(os.path.join(out, "result.json"), "w") as handle:
        json.dump({"status": status, "reason": None if status == "PASS" else "plan failed", "steps": [], "metrics": [], "unverified": ["Golf flow"]}, handle)
if mode == "pass":
    harness("PASS"); xml(True)
elif mode == "harness-pass-xml-fail":
    harness("PASS"); xml(False); sys.exit(1)
elif mode == "no-harness":
    xml(True)
elif mode == "xml-fail":
    xml(False); sys.exit(1)
elif mode == "crash":
    print("boom"); sys.exit(1)
elif mode == "blocked":
    print("GDV-RESULT BLOCKED no PlayMode module"); sys.exit(2)
elif mode == "harness-pass-xml-fail-clean":
    harness("PASS"); xml(False)
elif mode == "harness-pass-crash":
    harness("PASS"); sys.exit(1)
elif mode == "harness-pass-status-fail":
    harness("PASS"); xml(True); print("unapproved skip"); print("STATUS FAIL"); sys.exit(1)
elif mode == "harness-pass-hang":
    harness("PASS"); xml(True); time.sleep(300)
elif mode == "tool-blocked":
    print("Commit candidate changes before collecting exact-revision evidence"); print("STATUS BLOCKED"); sys.exit(2)
elif mode == "slow":
    for index in range(40):
        print("importing %d" % index, flush=True)
        time.sleep(0.2)
    harness("PASS"); xml(True)
elif mode == "hang":
    time.sleep(300)
'''


def make_hub(root: Path) -> dict:
    """A fake Unity Hub with one editor, laid out the way the running OS installs it."""
    host_os = core.local_os()
    if host_os == "macos":
        binary = root / "6000.6.7f1" / "Unity.app" / "Contents" / "MacOS" / "Unity"
    else:
        binary = root / "6000.6.7f1" / "Editor" / ("Unity.exe" if host_os == "windows" else "Unity")
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    return {"GDV_UNITY_HUB_PATHS": str(root)}


class NUnitTests(unittest.TestCase):
    def write(self, text):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "PlayMode.xml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_passed_cases_become_steps_and_a_pass(self):
        path = self.write('<test-run><test-case fullname="Ns.Suite.Opens(1)" result="Passed" duration="1.25"/></test-run>')
        status, reason, steps, counts = normalize_nunit(path)
        self.assertEqual((constants.PASS, None), (status, reason))
        self.assertEqual(1, counts["Passed"])
        self.assertEqual({"type", "status", "startMs", "durationMs"}, set(steps[0]))
        self.assertEqual(1250, steps[0]["durationMs"])

    def test_failures_empty_and_unreadable_results_never_pass(self):
        failed = normalize_nunit(self.write(
            '<test-run><test-case fullname="A.B" result="Passed"/><test-case fullname="A.C" result="Failed"><failure><message>nope</message>'
            '</failure></test-case></test-run>'))
        self.assertEqual(constants.FAIL, failed[0])
        self.assertIn("A.C: nope", failed[1])
        self.assertEqual(constants.FAIL, normalize_nunit(self.write("<test-run/>"))[0])
        self.assertEqual(constants.FAIL, normalize_nunit(self.write("<test-run><test-case fullname='A' result='Skipped'/></test-run>"))[0])
        self.assertEqual(constants.FAIL, normalize_nunit(self.write("not xml"))[0])


class EditorAdapterTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.home = self.base / "home"
        self.env = make_hub(self.base / "hub")
        self.project = self.base / "repo" / "proj"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.project / "validation" / "device").mkdir(parents=True)
        (self.base / "repo" / "tools" / "validation").mkdir(parents=True)
        (self.base / "repo" / "tools" / "validation" / "run-unity-tests.py").write_text(RUNNER, encoding="utf-8")
        self.set_profile({"planAssembly": "My.Plan.Tests"})
        self.plan = self.base / "plan.json"
        self.plan.write_text("{}", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home), "GDV_TEST_MODE": "pass", **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapters = []

    def set_profile(self, editor):
        (self.project / "validation" / "device" / "profiles.json").write_text(json.dumps({"schema": "gdv.profiles/1", "profiles": {"Validation": {"editor": editor}}}), encoding="utf-8")

    def adapter(self):
        adapter = EditorAdapter(self.env, home=self.home)
        adapter.use_project(self.project)
        self.adapters.append(adapter)
        self.addCleanup(adapter.terminate, None, "")
        return adapter

    def start(self, mode, app="editor"):
        os.environ["GDV_TEST_MODE"] = mode
        adapter = self.adapter()
        device = adapter.discover()[0]
        self.assertEqual(constants.PASS, adapter.push(device, self.plan, "gdv/inbox/plan.json").status)
        outcome = adapter.launch(device, app, [], self.base / f"console-{mode}.log")
        return adapter, device, outcome

    def finish(self, adapter, device, name, wait=30.0):
        deadline = time.monotonic() + wait
        folder = self.base / name
        seen_heartbeat = []
        while True:
            adapter.pull(device, "gdv/out", folder)
            results = list(folder.glob("*/result.json"))
            beats = list(folder.glob("*/heartbeat.json"))
            if beats:
                seen_heartbeat.append(json.loads(beats[0].read_text(encoding="utf-8")).get("seq"))
            if results or time.monotonic() > deadline:
                return (json.loads(results[0].read_text(encoding="utf-8")) if results else None), seen_heartbeat
            time.sleep(0.1)

    # -- discovery -------------------------------------------------------------------------------------------------------------

    def test_discovery_reports_one_editor_target_with_editor_evidence(self):
        device = self.adapter().discover()[0]
        self.assertEqual(("editor", "editor", False, "unity-editor"), (device.platform, device.evidence, device.exclusive, device.device_class))
        self.assertIn("Unity 6000.6.7f1", device.os_version)
        self.assertEqual("editor", device.id.split(":")[0])

    def test_no_editor_means_no_target_and_a_blocked_probe(self):
        adapter = EditorAdapter({"GDV_UNITY_HUB_PATHS": str(self.base / "nothing")}, home=self.home)
        self.assertEqual([], adapter.discover())
        self.assertIn("unity", adapter.probe().blocked)

    def test_the_pipeline_adds_the_editor_startup_allowance_to_the_plan_budget(self):
        from valtrion import pipeline
        self.assertEqual(900.0, pipeline.GuardedAdapter(self.adapter()).startup_allowance_sec)
        self.assertEqual(0.0, pipeline.GuardedAdapter(mock.Mock(spec=["platform"], platform="x")).startup_allowance_sec)

    def test_build_is_a_confirmation_and_names_a_missing_editor(self):
        self.assertEqual(constants.PASS, self.adapter().build(self.project, "Validation", self.base / "out").status)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 1.2.3\n", encoding="utf-8")
        outcome = self.adapter().build(self.project, "Validation", self.base / "out")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("1.2.3 is not installed", outcome.reason)

    # -- launch preconditions --------------------------------------------------------------------------------------------------

    def test_launch_blocks_without_assembly_runner_or_a_pushed_plan(self):
        self.set_profile({})
        adapter, device, outcome = self.start("pass")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("planAssembly", outcome.reason)
        self.set_profile({"planAssembly": "A", "runner": "missing.py"})
        self.assertIn("no Unity test runner", self.start("pass")[2].reason)
        self.set_profile({"planAssembly": "A"})
        bare = self.adapter()
        outcome = bare.launch(bare.discover()[0], "editor", [], self.base / "c.log")
        self.assertIn("plan was not pushed", outcome.reason)

    def test_the_app_names_the_test_assembly_when_it_is_not_the_word_editor(self):
        self.set_profile({})
        adapter, device, outcome = self.start("pass", app="Other.Tests")
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        self.assertEqual("Other.Tests", outcome.details["assembly"])
        self.finish(adapter, device, "got")

    def test_a_busy_editor_pool_blocks_naming_the_holders(self):
        manager = lease.LeaseManager(self.home)
        for slot in (1, 2):
            self.assertTrue(manager.acquire(f"unity-editor:slot{slot}", "other@host", f"s{slot}", 600, "test", os.getpid()).acquired)
        adapter, device, outcome = self.start("pass")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("other@host", outcome.reason)

    # -- the run ---------------------------------------------------------------------------------------------------------------

    def test_pass_holds_a_slot_while_running_and_releases_it_on_terminate(self):
        adapter, device, outcome = self.start("slow")
        self.assertEqual(constants.PASS, outcome.status, outcome.reason)
        self.assertEqual("held", lease.LeaseManager(self.home).status("unity-editor:slot1").state)
        result, beats = self.finish(adapter, device, "got")
        self.assertEqual(constants.PASS, result["status"])
        self.assertTrue(beats and max(beats) >= 1, "progress is visible from the runner's output before the harness starts")
        self.assertIn(editor_module.EDITOR_NOTE, result["unverified"])
        self.assertIn("Golf flow", result["unverified"])
        self.assertEqual(constants.PASS, adapter.terminate(device, "editor").status)
        self.assertEqual("free", lease.LeaseManager(self.home).status("unity-editor:slot1").state)

    def test_harness_pass_with_failing_nunit_is_a_failure(self):
        adapter, device, _ = self.start("harness-pass-xml-fail")
        result, _ = self.finish(adapter, device, "got")
        self.assertEqual(constants.FAIL, result["status"])
        self.assertIn("did not finish cleanly", result["reason"], "the runner also exited 1")

    def test_harness_pass_with_failing_nunit_and_a_clean_exit_is_still_a_failure(self):
        adapter, device, _ = self.start("harness-pass-xml-fail-clean")
        result, _ = self.finish(adapter, device, "got")
        self.assertEqual(constants.FAIL, result["status"])
        self.assertIn("NUnit", result["reason"])
        self.assertIn("A.Breaks", result["reason"])

    def test_passing_nunit_without_a_plan_result_is_not_a_pass(self):
        adapter, device, _ = self.start("no-harness")
        result, _ = self.finish(adapter, device, "got")
        self.assertEqual(constants.FAIL, result["status"])
        self.assertIn("no plan result", result["reason"])
        self.assertEqual("test:A.Passes", result["steps"][0]["type"])

    def test_failing_nunit_without_a_harness_result_is_normalized(self):
        adapter, device, _ = self.start("xml-fail")
        result, _ = self.finish(adapter, device, "got")
        self.assertEqual(constants.FAIL, result["status"])
        self.assertEqual({"PASS", "FAIL"}, {step["status"] for step in result["steps"]})

    def test_runner_crash_and_marker_become_results_instead_of_a_stall(self):
        def result_of(mode):
            adapter, device, _ = self.start(mode)
            document = self.finish(adapter, device, mode)[0]
            adapter.terminate(device, "editor")
            return document

        crash = result_of("crash")
        self.assertEqual(constants.FAIL, crash["status"])
        self.assertIn("code 1", crash["reason"])
        blocked = result_of("blocked")
        self.assertEqual(constants.BLOCKED, blocked["status"])
        self.assertIn("PlayMode module", blocked["reason"])
        tool = result_of("tool-blocked")
        self.assertEqual(constants.BLOCKED, tool["status"], "the runner's STATUS line is read, not just its exit code")
        self.assertIn("Commit candidate changes", tool["reason"])

    def test_a_harness_pass_needs_a_clean_runner_exit(self):
        for mode, needle in (("harness-pass-crash", "code 1"), ("harness-pass-status-fail", "unapproved skip")):
            adapter, device, _ = self.start(mode)
            result, _ = self.finish(adapter, device, mode)
            adapter.terminate(device, "editor")
            self.assertEqual(constants.FAIL, result["status"], mode)
            self.assertIn("did not finish cleanly", result["reason"])
            self.assertIn(needle, result["reason"])

    def test_a_runner_that_never_exits_after_the_plan_is_stopped_and_the_run_fails(self):
        with mock.patch.object(editor_module, "WITHHOLD_RESULT_SEC", 0.5):
            adapter, device, _ = self.start("harness-pass-hang")
            result, _ = self.finish(adapter, device, "got", wait=60)
            adapter.terminate(device, "editor")
        self.assertEqual(constants.FAIL, result["status"])
        self.assertIn("did not exit", result["reason"])

    def test_launch_failures_leave_no_lease_and_no_staging_folder(self):
        manager = lease.LeaseManager(self.home)
        for slot in (1, 2):
            manager.acquire(f"unity-editor:slot{slot}", "other@host", f"s{slot}", 600, "test", os.getpid())
        adapter, device, outcome = self.start("pass")
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIsNone(adapter._stage)  # noqa: SLF001
        for slot in (1, 2):
            manager.release(f"unity-editor:slot{slot}", f"s{slot}", os.getpid())

        def broken_spawn(*arguments):
            raise RuntimeError("spawn exploded")

        broken = EditorAdapter(self.env, spawn=broken_spawn, home=self.home)
        broken.use_project(self.project)
        self.addCleanup(broken.terminate, None, "")
        device = broken.discover()[0]
        broken.push(device, self.plan, "gdv/inbox/plan.json")
        outcome = broken.launch(device, "editor", [], self.base / "b.log")
        self.assertEqual(constants.FAIL, outcome.status)
        self.assertIn("spawn exploded", outcome.reason)
        self.assertEqual("free", lease.LeaseManager(self.home).status("unity-editor:slot1").state)
        self.assertIsNone(broken._stage)  # noqa: SLF001

    def test_the_editor_build_stage_takes_no_slot_of_its_own(self):
        from valtrion import pipeline
        self.assertFalse(pipeline.GuardedAdapter(self.adapter()).build_uses_editor)
        self.assertTrue(pipeline.GuardedAdapter(mock.Mock(spec=["platform"], platform="x")).build_uses_editor)

    def test_terminate_stops_a_hung_runner_and_screenshot_is_honestly_unavailable(self):
        adapter, device, _ = self.start("hang")
        self.assertTrue(adapter._alive())  # noqa: SLF001
        self.assertEqual(constants.PASS, adapter.terminate(device, "editor").status)
        self.assertFalse(adapter._alive())  # noqa: SLF001
        self.assertEqual("free", lease.LeaseManager(self.home).status("unity-editor:slot1").state)
        self.assertEqual(constants.BLOCKED, adapter.screenshot(device, self.base / "s.png").status)

    def test_paths_outside_the_data_folder_are_refused(self):
        adapter = self.adapter()
        device = adapter.discover()[0]
        self.assertEqual(constants.BLOCKED, adapter.push(device, self.plan, "../x").status)
        self.assertEqual(constants.BLOCKED, adapter.pull(device, "/abs", self.base / "z").status)


class EditorPipelineTests(unittest.TestCase):
    """The pipeline drives the adapter end to end: no build stage, a run stage, a result with editor evidence."""

    def test_pipeline_run_yields_a_valid_editor_result_that_cannot_be_a_device_baseline(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        home, root = base / "home", base / "artifacts"
        env = make_hub(base / "hub")
        project = base / "repo" / "proj"
        (project / "ProjectSettings").mkdir(parents=True)
        (project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (project / "validation" / "device").mkdir(parents=True)
        (project / "validation" / "device" / "profiles.json").write_text(json.dumps({"schema": "gdv.profiles/1", "profiles": {"Validation": {"editor": {"planAssembly": "A"}}}}), encoding="utf-8")
        (base / "repo" / "tools" / "validation").mkdir(parents=True)
        (base / "repo" / "tools" / "validation" / "run-unity-tests.py").write_text(RUNNER, encoding="utf-8")
        plan = {"schema": "gdv.plan/1", "id": "smoke", "kind": "smoke", "requires": {"platforms": ["editor"]}, "dataPolicy": "isolated",
                "budgetSeconds": 120, "steps": [{"type": "wait", "timeoutSec": 5}]}
        (base / "smoke.plan.json").write_text(json.dumps(plan), encoding="utf-8")
        sha = "d" * 40
        pipeline = {"schema": "gdv.pipeline/1", "id": "e", "candidate": {"repo": "Sample", "sha": sha, "profile": "Validation"},
                    "device": {"platform": "editor"}, "stages": [{"stage": "run", "plan": "smoke.plan.json", "args": {"app": "editor", "stallSec": 20},
                                                                  "timeoutSec": 60}]}
        providers.register("editor", lambda: EditorAdapter(env, home=home))
        self.addCleanup(providers.unregister, "editor")
        with mock.patch.dict(os.environ, {core.HOME_ENV: str(home), "GDV_TEST_MODE": "pass"}):
            request = jobs.JobRequest(
                kind="pipeline", project="Sample", sha=sha, label="e", payload={
                    "type": "pipeline", "file": "<inline>", "inline": pipeline, "projectDir": str(project), "baseDir": str(base),
                    "devicePollSec": 0.1},
                host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=120, owner="tester")
            info = jobs.start_job(request, root, home, foreground=True)
        store = jobs.JobStore(Path(info["dir"]))
        job = store.read_job()
        self.assertEqual(("finished", "PASS"), (job["state"], job["status"]), job["reason"])
        result = json.loads(store.result.read_text(encoding="utf-8"))
        self.assertEqual([], Validator().validate(result, "gdv.result/1"))
        self.assertEqual(("editor", "editor"), (result["target"]["platform"], result["target"]["evidence"]))
        self.assertIn(editor_module.EDITOR_NOTE, result["unverified"])


if __name__ == "__main__":
    unittest.main()
