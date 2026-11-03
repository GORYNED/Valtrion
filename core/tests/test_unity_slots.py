"""The shared Unity capacity of a host: one total for every claimant, held for the whole life of a Unity process."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import constants, core, lease, unityguard
from valtrion.providers import editor as editor_module
from valtrion.providers.editor import EditorAdapter

from .test_platform_editor import make_hub

OWNER = "other@host"


class CapacityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)

    def capacity(self, host_os="windows"):
        with mock.patch.object(core, "local_os", lambda: host_os):
            return lease.unity_editor_capacity(self.home)

    def test_the_default_depends_on_the_host_only(self):
        self.assertEqual(2, self.capacity("windows"))
        self.assertEqual(2, self.capacity("linux"))
        self.assertEqual(1, self.capacity("macos"))

    def test_host_json_overrides_the_default_and_a_bad_value_is_refused(self):
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 3}), encoding="utf-8")
        self.assertEqual(3, self.capacity("macos"))
        for bad in (0, -1, "2", True, 1.5):
            (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": bad}), encoding="utf-8")
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.capacity()
        (self.home / "host.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.capacity()

    def test_a_project_resource_is_a_valid_stable_lease_name_per_folder(self):
        one, other = self.home / "one", self.home / "two"
        one.mkdir()
        other.mkdir()
        name = lease.unity_project_resource(one)
        self.assertEqual(name, lease.unity_project_resource(one))
        self.assertEqual(name, lease.unity_project_resource(one / ".." / "one"))
        self.assertNotEqual(name, lease.unity_project_resource(other))
        self.assertTrue(lease.lease_file_name(name).endswith(".lock"))

    def test_the_project_resource_ignores_case_only_where_the_file_system_does(self):
        folder = self.home / "MyProj"
        folder.mkdir()
        shouted = Path(str(folder).replace("MyProj", "MYPROJ"))
        for host_os in ("windows", "macos"):
            with mock.patch.object(core, "local_os", lambda host_os=host_os: host_os):
                self.assertEqual(lease.unity_project_resource(folder), lease.unity_project_resource(shouted), host_os)
        if os.name == "nt":  # `os.path.normcase` already folds case on Windows, whatever host the code believes it is on
            return
        with mock.patch.object(core, "local_os", lambda: "linux"):
            self.assertNotEqual(lease.unity_project_resource(folder), lease.unity_project_resource(shouted))

    def test_the_project_resource_follows_a_symlink_to_the_folder(self):
        folder = self.home / "real"
        folder.mkdir()
        link = self.home / "link"
        try:
            os.symlink(folder, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available here")
        self.assertEqual(lease.unity_project_resource(folder), lease.unity_project_resource(link))


class ParallelAcquisitionTests(unittest.TestCase):
    CAPACITY = 2

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)

    def test_threads_never_hold_more_slots_than_the_capacity(self):
        active, peak, failures = [0], [0], []
        guard = threading.Lock()

        def worker(index):
            manager = lease.LeaseManager(self.home)
            result = manager.acquire_slot("unity-editor", self.CAPACITY, f"o{index}", f"s{index}", 60, "test", os.getpid(),
                                          wait_sec=60, poll_sec=0.02)
            if not result.acquired:
                failures.append(result.reason)
                return
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with guard:
                active[0] -= 1
            manager.release(result.lease.resource, f"s{index}", os.getpid())

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual([], failures)
        self.assertLessEqual(peak[0], self.CAPACITY)
        self.assertEqual(self.CAPACITY, peak[0], "the capacity is used when there is demand")
        self.assertEqual([], lease.LeaseManager(self.home).list_all(), "every lease was released")

    def test_processes_never_hold_more_slots_than_the_capacity(self):
        code = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "from valtrion import lease\n"
            "home, log, index, capacity = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])\n"
            "manager = lease.LeaseManager(home)\n"
            "result = manager.acquire_slot('unity-editor', capacity, 'o' + index, 's' + index, 60, 'test', os.getpid(), wait_sec=90, poll_sec=0.02)\n"
            "if not result.acquired:\n"
            "    sys.exit(3)\n"
            "with open(log, 'a') as handle:\n"
            "    handle.write('%d 1\\n' % time.time_ns())\n"
            "time.sleep(0.15)\n"
            "with open(log, 'a') as handle:\n"
            "    handle.write('%d 0\\n' % time.time_ns())\n"
            "manager.release(result.lease.resource, 's' + index, os.getpid())\n")
        log = self.home / "events.log"
        package_root = str(Path(lease.__file__).resolve().parent.parent)
        environment = {**os.environ, "PYTHONPATH": package_root}
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.home), str(log), str(index), str(self.CAPACITY)],
                                      env=environment) for index in range(6)]
        codes = [process.wait(timeout=180) for process in processes]
        self.assertEqual([0] * 6, codes)
        events = sorted(tuple(int(part) for part in line.split()) for line in log.read_text().splitlines())
        # An exit is stamped before its release and an entry after its acquire, so sorting by time never overcounts.
        current = peak = 0
        for _, change in events:
            current += 1 if change else -1
            peak = max(peak, current)
        self.assertEqual(12, len(events))
        self.assertLessEqual(peak, self.CAPACITY)


class FakeProcess:
    def __init__(self):
        self.dead = False
        self.pid = 0

    def poll(self):
        return 1 if self.dead else None

    def wait(self, timeout=None):
        return 1 if self.dead else None


class FakeTree:
    def __init__(self, process, killable=True):
        self.process, self.killable, self.kills = process, killable, 0

    def alive(self):
        return not self.process.dead

    def kill(self):
        self.kills += 1
        if self.killable:
            self.process.dead = True

    def close(self):
        pass

    def cpu_seconds(self):
        return 0.0


class EditorSlotTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = make_hub(self.base / "hub")
        self.project = self.base / "repo" / "proj"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (self.project / "validation" / "device").mkdir(parents=True)
        (self.project / "validation" / "device" / "profiles.json").write_text(json.dumps(
            {"schema": "gdv.profiles/1", "profiles": {"Validation": {"editor": {"planAssembly": "My.Plan.Tests"}}}}), encoding="utf-8")
        (self.base / "repo" / "tools" / "validation").mkdir(parents=True)
        (self.base / "repo" / "tools" / "validation" / "run-unity-tests.py").write_text("", encoding="utf-8")
        self.plan = self.base / "plan.json"
        self.plan.write_text("{}", encoding="utf-8")
        # The tests assume two slots whatever the host's default is; the default itself is covered by CapacityTests.
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 2}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home), **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = lease.LeaseManager(self.home)
        self.spawned = []

    def spawn(self, killable=True):
        def fake(argv, cwd, env, log):
            process = FakeProcess()
            tree = FakeTree(process, killable)
            self.spawned.append((process, tree))
            return process, tree
        return fake

    def adapter(self, killable=True, project=None):
        adapter = EditorAdapter(self.env, spawn=self.spawn(killable), home=self.home)
        adapter.use_project(project or self.project)
        self.addCleanup(self.finish, adapter)
        return adapter

    def finish(self, adapter):
        for process, tree in self.spawned:
            process.dead = True
            tree.killable = True
        adapter.terminate(None, "")

    def launch(self, adapter, name="console.log"):
        device = adapter.discover()[0]
        self.assertEqual(constants.PASS, adapter.push(device, self.plan, "gdv/inbox/plan.json").status)
        return device, adapter.launch(device, "editor", [], self.base / name)

    def state(self, resource):
        return self.manager.status(resource).state

    def project_resource(self):
        return lease.unity_project_resource(self.project)

    def test_an_editor_outside_any_lease_stops_the_launch_and_gives_the_leases_back(self):
        editor = unityguard.Proc(4242, 1, "/x/Unity.app/Contents/MacOS/Unity", "/x/Unity.app/Contents/MacOS/Unity -batchmode")

        class Orphan(unityguard.Probe):
            os_name = "macos"

            def processes(self):
                return [editor]

            def free_memory_mib(self):
                return 1.0e6

        with mock.patch.object(unityguard, "probe_factory", Orphan):
            adapter = self.adapter()
            outcome = self.launch(adapter)[1]
        self.assertNotEqual(constants.PASS, outcome.status)
        self.assertIn("outside any lease", outcome.reason or "")
        self.assertEqual("free", self.state(self.project_resource()))
        self.assertEqual([], [p.name for p in self.manager.directory.glob("*.lock")])

    # -- one capacity for every claimant ---------------------------------------------------------------------------------------

    def test_the_editor_asks_for_the_capacity_of_the_host(self):
        asked = []
        original = lease.LeaseManager.acquire_slot

        def record(manager, name, slots, *args, **kwargs):
            asked.append((name, slots))
            return original(manager, name, slots, *args, **kwargs)

        for configured, expected in ((2, 2), (1, 1), (3, 3)):
            (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": configured}), encoding="utf-8")
            asked.clear()
            with mock.patch.object(lease.LeaseManager, "acquire_slot", record):
                adapter = self.adapter()
                self.assertEqual(constants.PASS, self.launch(adapter)[1].status)
                adapter.terminate(None, "")
            self.assertEqual([("unity-editor", expected)], asked, configured)

    def test_an_editor_and_a_build_share_one_total(self):
        """A build that counts the host's slots and an Editor never both hold the last slot, whichever comes first."""
        capacity = lease.unity_editor_capacity(self.home)
        self.assertEqual(2, capacity)
        build = self.manager.acquire_slot("unity-editor", capacity, "ci", "build-1", 600, "build", os.getpid())
        self.assertTrue(build.acquired)
        first = self.adapter()
        self.assertEqual(constants.PASS, self.launch(first)[1].status)
        other_project = self.base / "repo" / "second"
        (other_project / "ProjectSettings").mkdir(parents=True)
        (other_project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        (other_project / "validation" / "device").mkdir(parents=True)
        (other_project / "validation" / "device" / "profiles.json").write_text(
            (self.project / "validation" / "device" / "profiles.json").read_text(encoding="utf-8"), encoding="utf-8")
        third = self.adapter(project=other_project)
        blocked = self.launch(third, "third.log")[1]
        self.assertEqual(constants.BLOCKED, blocked.status)
        self.assertIn("unity-editor", blocked.reason)
        self.assertEqual(1, len(self.spawned), "no Unity process was started without a slot")

    def test_a_host_with_one_slot_keeps_the_editor_out_while_a_build_holds_it(self):
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 1}), encoding="utf-8")
        self.assertTrue(self.manager.acquire_slot("unity-editor", 1, "ci", "build-1", 600, "build", os.getpid()).acquired)
        outcome = self.launch(self.adapter())[1]
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertEqual([], self.spawned)
        self.assertEqual("free", self.state(self.project_resource()), "a refused launch keeps no project lease")

    # -- the project folder ----------------------------------------------------------------------------------------------------

    def test_a_second_editor_on_the_same_project_is_blocked_and_takes_no_slot(self):
        first = self.adapter()
        self.assertEqual(constants.PASS, self.launch(first)[1].status)
        second = self.adapter()
        outcome = self.launch(second, "second.log")[1]
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("unity-project", outcome.reason)
        self.assertEqual("held", self.state("unity-editor:slot1"))
        self.assertEqual("free", self.state("unity-editor:slot2"))
        self.assertEqual(1, len(self.spawned))

    def test_a_refused_slot_gives_the_project_lease_back(self):
        for slot in (1, 2):
            self.assertTrue(self.manager.acquire(f"unity-editor:slot{slot}", OWNER, f"s{slot}", 600, "test", os.getpid()).acquired)
        outcome = self.launch(self.adapter())[1]
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertEqual("free", self.state(self.project_resource()))

    # -- held for the whole life of the processes ------------------------------------------------------------------------------

    def test_the_slot_is_held_from_before_unity_starts_until_its_processes_are_gone(self):
        adapter = self.adapter(killable=False)
        seen = []

        def spawn(argv, cwd, env, log):
            seen.append((self.state("unity-editor:slot1"), self.state(self.project_resource())))
            return self.spawn(killable=False)(argv, cwd, env, log)

        adapter._spawn = spawn  # noqa: SLF001
        self.assertEqual(constants.PASS, self.launch(adapter)[1].status)
        self.assertEqual([("held", "held")], seen, "both leases exist at the moment the runner is started")
        stopped = adapter.terminate(None, "")
        self.assertEqual(constants.FAIL, stopped.status)
        self.assertEqual(("held", "held"), (self.state("unity-editor:slot1"), self.state(self.project_resource())),
                         "processes that survive a kill still occupy the slot")
        self.assertTrue(adapter._alive())  # noqa: SLF001
        blocked = self.launch(adapter)[1]
        self.assertEqual(constants.BLOCKED, blocked.status, "no second launch while the first processes live")
        process, tree = self.spawned[0]
        tree.killable = True
        self.assertEqual(constants.PASS, adapter.terminate(None, "").status)
        self.assertEqual(("free", "free"), (self.state("unity-editor:slot1"), self.state(self.project_resource())))

    def test_a_pipeline_that_stops_without_terminate_releases_through_the_exit_hook(self):
        adapter = self.adapter()
        self.assertEqual(constants.PASS, self.launch(adapter)[1].status)
        adapter._at_exit()  # noqa: SLF001 - what atexit calls
        self.assertTrue(self.spawned[0][0].dead)
        self.assertEqual(("free", "free"), (self.state("unity-editor:slot1"), self.state(self.project_resource())))

    def test_a_second_launch_after_a_dead_runner_keeps_a_single_keeper_and_slot(self):
        adapter = self.adapter()
        self.assertEqual(constants.PASS, self.launch(adapter)[1].status)
        keeper = adapter._keeper  # noqa: SLF001
        self.spawned[0][0].dead = True
        self.assertEqual(constants.PASS, self.launch(adapter, "again.log")[1].status)
        self.assertIsNot(keeper, adapter._keeper)  # noqa: SLF001
        self.assertEqual("held", self.state("unity-editor:slot1"))
        self.assertEqual("free", self.state("unity-editor:slot2"))

    # -- a lease that is lost -------------------------------------------------------------------------------------------------

    def lose_the_slot(self, adapter):
        """What the keeper records when a renewal fails: the slot's lock is gone (removed by hand) and it is listed as lost."""
        resource = adapter._lease_resource  # noqa: SLF001
        self.assertTrue(self.manager.release(resource, adapter._session, os.getpid()))  # noqa: SLF001
        adapter._keeper.lost.append(resource)  # noqa: SLF001
        return resource

    def occupy_both_slots(self):
        for slot in (1, 2):
            self.assertTrue(self.manager.acquire(f"unity-editor:slot{slot}", OWNER, f"x{slot}", 600, "test", os.getpid()).acquired)

    def test_only_the_lost_lease_is_taken_again_and_the_result_says_so(self):
        adapter = self.adapter()
        device, outcome = self.launch(adapter)
        self.assertEqual(constants.PASS, outcome.status)
        project = self.project_resource()
        released = []
        original = lease.LeaseManager.release

        def record(manager, resource, *args):
            released.append(resource)
            return original(manager, resource, *args)

        self.lose_the_slot(adapter)
        with mock.patch.object(lease.LeaseManager, "release", record):
            adapter.pull(device, "gdv/out", self.base / "got")
        self.assertEqual([], released, "the lease that was still valid is never given up (another holder could take it)")
        self.assertFalse(self.spawned[0][0].dead, "the Editor keeps running")
        self.assertEqual(("held", "held"), (self.state("unity-editor:slot1"), self.state(project)))
        self.assertEqual([], adapter._keeper.lost)  # noqa: SLF001
        self.assertTrue(any("taken again" in note for note in adapter._notes))  # noqa: SLF001

    def test_a_lost_slot_that_cannot_be_taken_back_stops_the_editor(self):
        adapter = self.adapter()
        device, outcome = self.launch(adapter)
        self.assertEqual(constants.PASS, outcome.status)
        self.lose_the_slot(adapter)
        self.occupy_both_slots()
        adapter.pull(device, "gdv/out", self.base / "got")
        adapter.pull(device, "gdv/out", self.base / "got")
        self.assertTrue(self.spawned[0][0].dead, "an Editor without a slot is stopped")
        results = list((self.base / "got").glob("*/result.json"))
        self.assertTrue(results)
        document = json.loads(results[0].read_text(encoding="utf-8"))
        self.assertEqual(constants.FAIL, document["status"])
        self.assertIn("lost", document["reason"])
        self.assertEqual("free", self.state(self.project_resource()), "the dead Editor's remaining lease is given back")
        self.assertEqual(constants.PASS, adapter.terminate(device, "editor").status)
        self.assertEqual(OWNER, self.manager.status("unity-editor:slot1").record.owner, "the other holder's leases are untouched")

    def test_a_failed_retake_with_a_surviving_tree_keeps_the_remaining_leases_and_the_exit_hook(self):
        adapter = self.adapter(killable=False)
        device, outcome = self.launch(adapter)
        self.assertEqual(constants.PASS, outcome.status)
        self.lose_the_slot(adapter)
        self.occupy_both_slots()
        with mock.patch.object(editor_module.atexit, "unregister") as unregister:
            adapter.pull(device, "gdv/out", self.base / "got")
        self.assertFalse(self.spawned[0][0].dead)
        self.assertIsNotNone(adapter._keeper, "the keeper keeps renewing what the surviving processes still need")  # noqa: SLF001
        self.assertEqual("held", self.state(self.project_resource()))
        unregister.assert_not_called()
        self.assertEqual(constants.FAIL, adapter.terminate(device, "editor").status)
        self.spawned[0][1].killable = True
        self.assertEqual(constants.PASS, adapter.terminate(device, "editor").status)
        self.assertEqual("free", self.state(self.project_resource()))


if __name__ == "__main__":
    unittest.main()

