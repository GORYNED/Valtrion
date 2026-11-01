"""Stage-scoped leases, builds overlapping a device run, and the build cache, with fake adapters and the real lease files."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import buildcache, constants, core, jobs, lease, pipeline, providers
from valtrion.adapter import Capabilities, DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from valtrion.lease import LeaseManager
from valtrion.schema import Validator

PLAN = {
    "schema": "gdv.plan/1", "id": "perf.open", "kind": "performance", "requires": {"platforms": ["android"], "capabilities": ["touch"]},
    "dataPolicy": "devdata", "budgetSeconds": 30, "warmup": 1, "steps": [{"type": "wait", "timeoutSec": 5}],
    "metrics": {"openMs": {"unit": "ms", "direction": "lower", "regressionPct": 10, "minSamples": 3}}}
PHONE = "android:ZY22"
WAIT = 30  # seconds a test waits for another thread before it gives up and fails


def phone(identifier=PHONE):
    return DeviceInfo(identifier, "android", "phone-mid", "physical", "moto g", "Android 15", ("touch",))


class World:
    """What the fake adapters of all jobs of one test share: a timeline, gates and counters."""

    def __init__(self, home):
        self.home = home
        self.lock = threading.Lock()
        self.timeline = []
        self.gates = {}
        self.entered = {}
        self.builds = []
        self.installs = []
        self.toolchain = {}
        self.devices = [phone()]
        self.violations = []

    def mark(self, label, what):
        with self.lock:
            self.timeline.append((label, what))
            self.entered.setdefault((label, what), threading.Event()).set()

    def entered_event(self, label, what):
        with self.lock:
            return self.entered.setdefault((label, what), threading.Event())

    def gate(self, label, what):
        with self.lock:
            return self.gates.setdefault((label, what), threading.Event())

    def held(self):
        return sorted(item.resource for item in LeaseManager(self.home).list_all() if item.state == "held")


def adapter_class(world):
    class Fake(PlatformAdapter):
        platform = "android"

        def __init__(self):
            self.label = "?"
            self.launched = False

        def use_project(self, project):
            self.label = Path(project).name

        def probe(self):
            return Capabilities("android", ("build",))

        def discover(self):
            return list(world.devices)

        def toolchain(self):
            return dict(world.toolchain)

        def build(self, project, profile, out):
            job = out.parent.name  # the run folder is named after the job
            mine = [item.resource for item in LeaseManager(world.home).list_all()
                    if item.record is not None and item.record.session == job and item.state == "held"]
            if any(resource.startswith("device:") for resource in mine):
                world.violations.append(f"{self.label} builds while it holds {mine}")
            world.mark(self.label, "build.start")
            world.gate(self.label, "build").wait(WAIT)
            out.mkdir(parents=True, exist_ok=True)
            apk = out / "app.apk"
            apk.write_bytes(f"apk {self.label}".encode())
            with world.lock:
                world.builds.append(self.label)
            world.mark(self.label, "build.end")
            return StepOutcome(constants.PASS, artifacts=(apk,), details={"layer.export": "PASS", "layer.nativeBuild": "PASS"})

        def install(self, device, artifact):
            with world.lock:
                world.installs.append((self.label, Path(artifact)))
            world.mark(self.label, "install")
            return StepOutcome(constants.PASS)

        def push(self, device, local, remote):
            return StepOutcome(constants.PASS)

        def pull(self, device, remote, local):
            if not self.launched:
                return StepOutcome(constants.PASS)
            run = local / "run-1"
            run.mkdir(parents=True, exist_ok=True)
            core.atomic_write_json(run / "heartbeat.json", {"seq": 5})
            core.atomic_write_json(run / "result.json", {
                "status": "PASS", "reason": None, "steps": [{"type": "wait", "status": "PASS", "startMs": 0, "durationMs": 10}],
                "metrics": [{"name": "openMs", "unit": "ms", "samples": [100, 101, 99, 100]}], "unverified": []})
            return StepOutcome(constants.PASS)

        def launch(self, device, app, args, log):
            world.mark(self.label, "launch")
            world.gate(self.label, "launch").wait(WAIT)
            self.launched = True
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("started\n", encoding="utf-8")
            return StepOutcome(constants.PASS)

        def terminate(self, device, app):
            self.launched = False
            return StepOutcome(constants.PASS)

        def screenshot(self, device, out):
            return StepOutcome(constants.PASS)

        def health(self, device):
            return DeviceHealth(True, locked=False, thermal_state="nominal")

    return Fake


class ScopedLeaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.base, self.home, self.root = base, base / "home", base / "artifacts"
        self.home.mkdir()
        (self.home / "host.json").write_text('{"unityEditorSlots": 1}', encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.world = World(self.home)
        providers.register("android", adapter_class(self.world))
        self.addCleanup(providers.unregister, "android")
        (base / "plans").mkdir()
        (base / "plans" / "open.plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
        self.threads = []
        self.outcomes = {}
        self.addCleanup(self.open_all_gates)

    def open_all_gates(self):
        for label in "ABCDEFGH":
            for what in ("build", "launch"):
                self.world.gate(label, what).set()
        for thread in self.threads:
            thread.join(WAIT)

    def project(self, label):
        path = self.base / "projects" / label
        (path / "ProjectSettings").mkdir(parents=True, exist_ok=True)
        (path / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        return path

    def document(self, stages=None, sha="c" * 40, device=None, profile="Validation"):
        return {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "sha": sha, "profile": profile},
                "device": device or {"platform": "android"}, "stages": stages or self.full_stages()}

    @staticmethod
    def full_stages():
        return [{"stage": "build"}, {"stage": "install"},
                {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app", "stallSec": 5}, "timeoutSec": 30}]

    def run_job(self, label, document, lease_wait=0.0, project=True):
        payload = {"type": "pipeline", "file": "<inline>", "inline": document, "baseDir": str(self.base / "plans"), "devicePollSec": 0.02,
                   **({"projectDir": str(self.project(label))} if project else {})}
        request = jobs.JobRequest(
            kind="pipeline", project="Sample", sha=document["candidate"]["sha"], label=label, payload=payload,
            host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=120, owner="tester",
            lease_wait_sec=lease_wait, tick_sec=0.1)
        info = jobs.start_job(request, self.root, self.home, foreground=True)
        return info, jobs.JobStore(Path(info["dir"]))

    def start(self, label, document, lease_wait=60.0):
        def work():
            self.outcomes[label] = self.run_job(label, document, lease_wait)

        thread = threading.Thread(target=work, name=f"job-{label}", daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def job_files(self):
        found = {}
        for path in sorted(self.root.glob("*/*/*/job.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            found[path.parent.name] = document
        return found

    def wait_for(self, condition, what):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}; timeline {self.world.timeline}")

    def events_of(self, store):
        return [json.loads(line) for line in store.events.read_text(encoding="utf-8").splitlines() if line.strip()]

    # -- the overlap ------------------------------------------------------------------------------------------------------------

    def test_the_next_job_builds_while_the_previous_one_is_on_the_device(self):
        self.world.gate("A", "build").set()
        self.world.gate("B", "build").set()
        self.world.gate("B", "launch").set()
        first = self.start("A", self.document(sha="a" * 40))
        self.world.entered_event("A", "launch").wait(WAIT)
        self.assertTrue(self.world.entered_event("A", "launch").is_set(), "A reached the device")
        self.assertEqual(["device:" + PHONE], self.world.held(), "A holds only the device: the project and the editor slot are released")
        second = self.start("B", self.document(sha="b" * 40))
        self.assertTrue(self.world.entered_event("B", "build.end").wait(WAIT), "B built while A was still on the device")
        self.assertFalse(self.world.entered_event("B", "install").is_set(), "B cannot install before it has the device")
        waiting = self.wait_for(lambda: [j for j in self.job_files().values() if j.get("phase") == "waiting-device"], "B waiting for the device")
        self.assertEqual(1, len(waiting))
        holders = {item.resource: item.record.session for item in LeaseManager(self.home).list_all() if item.record}
        b_session = waiting[0]["jobId"]
        self.assertEqual([PHONE], [name[len("device:"):] for name, session in holders.items() if session != b_session],
                         "only A's lease is held; B holds nothing while it waits")
        self.assertFalse([name for name, session in holders.items() if session == b_session], holders)
        self.world.gate("A", "launch").set()
        for thread in (first, second):
            thread.join(WAIT)
        for label in "AB":
            _, store = self.outcomes[label]
            job = store.read_job()
            self.assertEqual(("finished", "PASS"), (job["state"], job["status"]), (label, job["reason"]))
            self.assertNotIn("phase", job)
        self.assertEqual([], self.world.violations)
        order = [entry for entry in self.world.timeline if entry[1] in ("build.end", "launch", "install")]
        self.assertLess(order.index(("B", "build.end")), order.index(("B", "install")))
        self.assertLess(order.index(("A", "launch")), order.index(("B", "build.end")), "B's build finished during A's device run")
        a_events, b_events = (self.events_of(self.outcomes[label][1]) for label in "AB")
        a_released = next(e for e in a_events if e["type"] == "lease.released" and e["message"] == "device:" + PHONE)
        b_acquired = next(e for e in b_events if e["type"] == "lease.acquired" and e["message"] == "device:" + PHONE)
        self.assertLessEqual(a_released["utc"], b_acquired["utc"], "the device lease line never overlaps")
        a_slot_released = next(e for e in a_events if e["type"] == "lease.released" and e["message"].startswith("unity-editor"))
        a_device_acquired = next(e for e in a_events if e["type"] == "lease.acquired" and e["message"] == "device:" + PHONE)
        self.assertLess(a_slot_released["seq"], a_device_acquired["seq"], "the editor slot is released before the device is taken")
        phases = [e["data"]["phase"] for e in b_events if e["type"] == "progress" and "phase" in (e.get("data") or {})]
        self.assertEqual(["waiting-build", "building", None, "waiting-device", "on-device", None], phases)

    def test_no_deadlock_with_interleaved_jobs_on_two_devices_and_two_slots(self):
        (self.home / "host.json").write_text('{"unityEditorSlots": 2}', encoding="utf-8")
        self.world.devices = [phone("android:ONE"), phone("android:TWO")]
        for label in "ABCD":
            self.world.gate(label, "build").set()
            self.world.gate(label, "launch").set()
        for position, label in enumerate("ABCD"):
            device = {"platform": "android", "id": "android:ONE" if position % 2 == 0 else "android:TWO"}
            self.start(label, self.document(sha=f"{position + 1}" * 40, device=device), lease_wait=90)
        for thread in self.threads:
            thread.join(60)
            self.assertFalse(thread.is_alive(), "a job is stuck: deadlock or starvation")
        for label in "ABCD":
            job = self.outcomes[label][1].read_job()
            self.assertEqual("PASS", job["status"], (label, job["reason"]))
        self.assertEqual([], self.world.violations)
        self.assertEqual([], self.world.held(), "every lease is released")

    def test_a_device_that_cannot_be_served_is_blocked_without_a_build_even_when_the_job_waits(self):
        for device in ({"platform": "android", "id": "android:MISSING"}, {"platform": "android", "class": "tablet-large"}):
            _, store = self.run_job("A", self.document(device=device), lease_wait=60)
            job = store.read_job()
            self.assertEqual("BLOCKED", job["status"], device)
            self.assertIn("no attached device satisfies", job["reason"])
        self.assertEqual([], self.world.builds)
        self.assertNotIn(("A", "build.start"), self.world.timeline)
        self.assertEqual([], self.world.held())

    def test_an_offline_device_is_blocked_before_the_build(self):
        self.world.devices = []
        _, store = self.run_job("A", self.document(), lease_wait=60)
        self.assertEqual("BLOCKED", store.read_job()["status"])
        self.assertEqual([], [entry for entry in self.world.timeline if entry[1].startswith("build")])

    def test_a_busy_device_with_no_lease_wait_is_blocked_before_the_build(self):
        LeaseManager(self.home).acquire("device:" + PHONE, "alice", "session-9", 600, "series", os.getpid())
        _, store = self.run_job("A", self.document(), lease_wait=0)
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("alice", job["reason"])
        self.assertEqual([], self.world.timeline)

    def test_a_busy_device_with_a_lease_wait_does_not_hold_the_editor_while_it_waits(self):
        manager = LeaseManager(self.home)
        manager.acquire("device:" + PHONE, "alice", "session-9", 600, "series", os.getpid())
        self.world.gate("A", "build").set()
        self.world.gate("A", "launch").set()
        thread = self.start("A", self.document(), lease_wait=60)
        self.wait_for(lambda: [j for j in self.job_files().values() if j.get("phase") == "waiting-device"], "the job waiting for the device")
        self.assertEqual(["device:" + PHONE], self.world.held(), "the build resources were released before the wait")
        manager.release("device:" + PHONE, "session-9", os.getpid())
        thread.join(WAIT)
        self.assertEqual("PASS", self.outcomes["A"][1].read_job()["status"], self.outcomes["A"][1].read_job()["reason"])

    def test_a_failed_build_releases_the_build_resources_and_never_takes_the_device(self):
        failing = adapter_class(self.world)

        class Failing(failing):
            def build(self, project, profile, out):
                return StepOutcome(constants.FAIL, "gradle failed")

        providers.register("android", Failing)
        _, store = self.run_job("A", self.document(), lease_wait=5)
        self.assertEqual("FAIL", store.read_job()["status"])
        self.assertEqual([], self.world.held())
        self.assertFalse([e for e in self.events_of(store) if e["type"] == "lease.acquired" and e["message"].startswith("device:")])

    # -- the build artifact and the cache ----------------------------------------------------------------------------------------

    def open_gates(self, *labels):
        for label in labels:
            self.world.gate(label, "build").set()
            self.world.gate(label, "launch").set()

    def clean_git(self, head="ab" * 20, dirty=False):
        def git(folder, arguments):
            if arguments[0] == "status":
                return (0, " M Assets/Player.cs\0") if dirty else (0, "")
            return 0, head
        return mock.patch.object(pipeline.compare, "run_git", git)

    def result_of(self, store):
        document = json.loads(store.result.read_text(encoding="utf-8"))
        self.assertEqual([], Validator().validate(document, "gdv.result/1"))
        return document

    def test_the_build_artifact_is_published_outside_the_work_folder_with_its_provenance(self):
        self.open_gates("A")
        with self.clean_git():
            _, store = self.run_job("A", self.document(sha="ab" * 20))
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        build = self.result_of(store)["build"]
        path = Path(build["path"])
        self.assertTrue(path.is_file())
        self.assertTrue(buildcache.builds_root(self.home) in path.parents, "kept under the host's builds folder, not the job's folders")
        self.assertEqual((core.sha256_file(path), path.stat().st_size, "ab" * 20, False, "Validation", False),
                         (build["sha256"], build["sizeBytes"], build["candidateSha"], build["dirty"], build["profile"], build["reused"]))
        self.assertEqual("6000.6.7f1", build["toolchain"]["unity"])
        events = [e for e in self.events_of(store) if e["type"] == "progress" and "build" in (e.get("data") or {})]
        self.assertEqual(build["path"], events[0]["data"]["build"]["path"])
        self.assertEqual([(("A", path))], self.world.installs, "install takes the published artifact")

    def test_a_clean_commit_is_built_once_and_the_second_pipeline_reuses_it(self):
        self.open_gates("A", "B")
        with self.clean_git():
            _, first = self.run_job("A", self.document(sha="ab" * 20))
            _, second = self.run_job("B", self.document(sha="ab" * 20))
        self.assertEqual(["A"], self.world.builds, "the second pipeline did not build")
        a, b = self.result_of(first)["build"], self.result_of(second)["build"]
        self.assertEqual((False, True, a["path"], a["sha256"]), (a["reused"], b["reused"], b["path"], b["sha256"]))
        self.assertEqual("PASS", second.read_job()["status"], second.read_job()["reason"])
        self.assertEqual(a["path"], str(self.world.installs[-1][1]), "the reused build is the one installed")
        self.assertEqual("free", LeaseManager(self.home).status("unity-editor:slot1").state)
        self.assertTrue(any("reused from the cache" in line for line in self.result_of(second)["unverified"]))

    def test_a_dirty_tree_is_never_reused(self):
        self.open_gates("A", "B")
        with self.clean_git(dirty=True):
            _, first = self.run_job("A", self.document(sha="ab" * 20))
            _, second = self.run_job("B", self.document(sha="ab" * 20))
        self.assertEqual(["A", "B"], self.world.builds)
        a, b = self.result_of(first)["build"], self.result_of(second)["build"]
        self.assertEqual((True, True, False, False), (a["dirty"], b["dirty"], a["reused"], b["reused"]))
        self.assertNotEqual(a["path"], b["path"])

    def test_a_changed_toolchain_profile_or_option_builds_again(self):
        self.open_gates("A", "B", "C", "D")
        with self.clean_git():
            self.run_job("A", self.document(sha="ab" * 20))
            self.world.toolchain = {"xcode": "Xcode 99.1"}
            self.run_job("B", self.document(sha="ab" * 20))
            self.run_job("C", self.document(sha="ab" * 20, profile="Other"))
            stages = self.full_stages()
            stages[0]["args"] = {"reuse": False}
            self.run_job("D", self.document(stages=stages, sha="ab" * 20))
            self.assertEqual(["A", "B", "C", "D"], self.world.builds)
            self.run_job("A", self.document(sha="ab" * 20))
        self.assertEqual(["A", "B", "C", "D"], self.world.builds, "the unchanged request is a cache hit again (toolchain B is current)")

    def test_a_project_profile_change_is_a_new_key(self):
        self.open_gates("A", "B")
        with self.clean_git():
            self.run_job("A", self.document(sha="ab" * 20))
            config = self.base / "projects" / "A" / "validation" / "device"
            config.mkdir(parents=True)
            (config / "profiles.json").write_text(json.dumps({"profiles": {"Validation": {"android": {"executeMethod": "X.Y"}}}}), encoding="utf-8")
            self.run_job("A", self.document(sha="ab" * 20))
            (config / "profiles.json").write_text(json.dumps({"profiles": {"Validation": {"android": {"executeMethod": "X.Z"}}}}), encoding="utf-8")
            self.run_job("A", self.document(sha="ab" * 20))
        self.assertEqual(["A", "A", "A"], self.world.builds)

    def test_build_only_pipeline_publishes_and_install_by_key_uses_it_without_building(self):
        self.open_gates("A", "B")
        with self.clean_git():
            _, built = self.run_job("A", self.document(stages=[{"stage": "build"}], sha="ab" * 20))
            job = built.read_job()
            self.assertEqual(("finished", "PASS"), (job["state"], job["status"]), job["reason"])
            build = self.result_of(built)["build"]
            self.assertEqual([], self.world.held())
            self.assertNotIn("install", [what for _, what in self.world.timeline])
            stages = [{"stage": "install", "args": {"artifactKey": build["key"]}},
                      {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app", "stallSec": 5}, "timeoutSec": 30}]
            _, ran = self.run_job("B", self.document(stages=stages, sha="ab" * 20))
        self.assertEqual("PASS", ran.read_job()["status"], ran.read_job()["reason"])
        self.assertEqual(["A"], self.world.builds)
        self.assertEqual(build["path"], str(self.world.installs[-1][1]))
        self.assertEqual(build["sha256"], self.result_of(ran)["build"]["sha256"])

    def test_the_build_command_runs_a_build_only_pipeline_and_run_installs_by_key(self):
        import contextlib
        import io
        from valtrion import cli
        self.open_gates("A")
        project = self.project("A")

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(list(argv))
            return code, out.getvalue()

        with self.clean_git():
            code, out = run("build", "--repo", "Sample", "--platform", "android", "--profile", "Validation", "--project-dir", str(project),
                            "--local", "--foreground")
            self.assertEqual(0, code, out)
            info = json.loads(out)
            store = jobs.JobStore(Path(info["dir"]))
            build = self.result_of(store)["build"]
            self.assertEqual(["A"], self.world.builds)
            self.assertEqual("PASS", store.read_job()["status"])
            code, out = run("run", str(self.base / "plans" / "open.plan.json"), "--repo", "Sample", "--sha", "ab" * 20, "--app", "com.example.app",
                            "--platform", "android", "--artifact-key", build["key"], "--project-dir", str(project), "--local", "--foreground")
            self.assertEqual(0, code, out)
        self.assertEqual(["A"], self.world.builds, "the run installed the published build")
        self.assertEqual(build["path"], str(self.world.installs[-1][1]))

    def test_install_by_an_unknown_key_is_blocked(self):
        self.open_gates("A")
        stages = [{"stage": "install", "args": {"artifactKey": "0" * 32}},
                  {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app"}, "timeoutSec": 30}]
        _, store = self.run_job("A", self.document(stages=stages), project=False)
        job = store.read_job()
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("no published build", job["reason"])
        self.assertEqual([], self.world.held())

    def test_a_pipeline_without_a_build_stage_keeps_working_and_has_no_build_entry(self):
        self.open_gates("A")
        stages = [{"stage": "install", "args": {"artifact": str(self.base / "app.apk")}},
                  {"stage": "run", "plan": "open.plan.json", "args": {"app": "com.example.app"}, "timeoutSec": 30}]
        (self.base / "app.apk").write_bytes(b"apk")
        _, store = self.run_job("A", self.document(stages=stages), project=False)
        self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
        self.assertNotIn("build", self.result_of(store))


class BuildCacheTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / "builds"
        self.number = 0

    def publish(self, key, content=b"apk", cacheable=True, dirty=False):
        self.number += 1
        source = self.base / f"out{self.number}" / "app.apk"
        source.parent.mkdir()
        source.write_bytes(content)
        return buildcache.publish(self.root, key, source, project="P", platform="android", sha="a" * 40, profile="Prof", dirty=dirty,
                                  toolchain={"unity": "6"}, config_hash="none", job_id=f"job-{self.number}", cacheable=cacheable)

    def test_publish_moves_the_artifact_and_describes_it(self):
        entry = self.publish("k1")
        self.assertEqual(b"apk", entry.path.read_bytes())
        self.assertFalse((self.base / "out1" / "app.apk").exists())
        self.assertEqual(("file", 3), (entry.manifest["kind"], entry.manifest["sizeBytes"]))
        self.assertEqual(entry.manifest["sha256"], entry.describe()["sha256"])

    def test_a_folder_artifact_is_hashed_over_its_files_and_changes_with_them(self):
        one, two = self.base / "one", self.base / "two"
        for folder, text in ((one, "x"), (two, "y")):
            (folder / "Payload").mkdir(parents=True)
            (folder / "Payload" / "a.bin").write_text(text, encoding="utf-8")
        self.assertNotEqual(buildcache.tree_digest(one)[0], buildcache.tree_digest(two)[0])
        self.assertEqual("directory", buildcache.tree_digest(one)[2])

    def test_the_second_publisher_of_a_key_gets_the_first_build(self):
        first = self.publish("k1", b"first")
        second = self.publish("k1", b"second")
        self.assertEqual(first.path, second.path)
        self.assertEqual(b"first", second.path.read_bytes())

    def test_lookup_ignores_dirty_entries_and_artifacts_that_changed(self):
        self.publish("clean")
        self.publish("dirty-job", cacheable=False, dirty=True)
        self.assertIsNotNone(buildcache.lookup(self.root, "clean"))
        self.assertIsNone(buildcache.lookup(self.root, "dirty-job"))
        self.assertIsNotNone(buildcache.find(self.root, "dirty-job"), "a dirty build can still be installed by its key")
        buildcache.find(self.root, "clean").path.write_bytes(b"truncated!!")
        self.assertIsNone(buildcache.lookup(self.root, "clean"))
        self.assertIsNone(buildcache.find(self.root, "../escape"))

    def test_gc_removes_unused_builds_dirty_ones_sooner_and_surplus_but_not_protected_or_recent(self):
        from datetime import timedelta
        old, older, dirty, recent, extra = (self.publish(key, cacheable=key != "dirty") for key in ("old", "older", "dirty", "recent", "extra"))
        now = core.now()

        def age(entry, days):
            manifest = {**entry.manifest, "lastUsedUtc": core.format_utc(now - timedelta(days=days))}
            core.atomic_write_json(entry.directory / buildcache.MANIFEST, manifest)

        age(old, 20)
        age(older, 30)
        age(dirty, 3)
        age(extra, 5)
        report = buildcache.gc(self.root, keep_days=14, dirty_keep_days=2, max_entries=20, protect=["older"], clock=lambda: now)
        names = sorted(item.split(" ")[0] for item in report.removed)
        self.assertEqual(["dirty", "old"], names)
        self.assertEqual(["extra", "older", "recent"], sorted(e.key for e in buildcache.entries(self.root)))
        report = buildcache.gc(self.root, max_entries=2, protect=["older"], clock=lambda: now)
        self.assertEqual(["extra"], [item.split(" ")[0] for item in report.removed], "the least recently used above the limit; recent is guarded")
        dry = buildcache.gc(self.root, keep_days=0, dry_run=True, clock=lambda: now + timedelta(days=1))
        self.assertTrue(dry.removed)
        self.assertEqual(2, len(buildcache.entries(self.root)), "a dry run removes nothing")

    def test_the_builds_command_lists_shows_and_collects(self):
        import contextlib
        import io
        from valtrion import cli
        with mock.patch.dict(os.environ, {core.HOME_ENV: str(self.base / "home")}):
            self.root = buildcache.builds_root()
            entry = self.publish("k1")

            def run(*argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = cli.main(list(argv))
                return code, out.getvalue()

            code, out = run("builds", "list")
            self.assertEqual((0, ["k1"]), (code, [item["key"] for item in json.loads(out)]))
            code, out = run("builds", "show", "k1")
            self.assertEqual((0, str(entry.path)), (code, json.loads(out)["path"]))
            self.assertEqual(2, run("builds", "show", "nope")[0])
            code, out = run("builds", "gc", "--dry-run", "--keep-days", "0")
            self.assertEqual(0, code)
            self.assertEqual(1, len(buildcache.entries(self.root)), "the entry was used a moment ago, so it is guarded")

    def test_lease_keeper_release_one_gives_back_only_that_lease(self):
        manager = LeaseManager(self.base / "home")
        for name in ("device:x", "unity-project:y"):
            self.assertTrue(manager.acquire(name, "me", "job", 60, "t", os.getpid()).acquired)
        keeper = lease.LeaseKeeper(manager, "job", os.getpid(), 10, "me")
        keeper.add("device:x")
        keeper.add("device:x")
        keeper.add("unity-project:y")
        self.assertEqual(["device:x", "unity-project:y"], keeper.held())
        self.assertTrue(keeper.release_one("device:x"))
        self.assertIsNone(keeper.release_one("device:x"), "already released")
        self.assertEqual(("free", "held"), (manager.status("device:x").state, manager.status("unity-project:y").state))
        self.assertEqual(["unity-project:y"], keeper.held())


if __name__ == "__main__":
    unittest.main()
