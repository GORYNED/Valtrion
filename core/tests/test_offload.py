"""Offloading Editor runs: slot report, settings, host survey, the receiving side, the placement of one run and the result it leaves.
Fakes play the other host; nothing here starts ssh or Unity."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from valtrion import cli, codesync, commands, config, constants, core, jobs, lease, offload, remote  # noqa: F401  (cli first: it registers the commands)
from valtrion.config import HostEntry
from valtrion.schema import Validator
from valtrion.transport import CommandResult

from .test_host import HostTestCase, run

SHA = "a" * 40
SNAP = "b" * 40


class Session:
    """A host as `remote.Remote` shows it: answers `lease slots`, `offload import`, `workroot inbox`, `job tail` and `job cancel`."""

    def __init__(self, base: Path, slots: dict | None = None, imported: dict | None = None):
        self.base = base
        self.slots = slots
        self.imported = imported if imported is not None else {"checkoutId": "queue", "sha": SNAP, "project": "Game", "folder": "/x"}
        self.calls: list[list[str]] = []
        self.tail_answers: list = []
        self.attempts = 1
        self.sent: list[tuple[str, int]] = []
        self.transport = SimpleNamespace(put=self.put)

    def put(self, local, remote_path, timeout_sec):
        self.sent.append((remote_path, Path(local).stat().st_size))
        return CommandResult(0)

    def call(self, args, timeout_sec=60):
        self.calls.append(list(args))
        if args[:2] == ["job", "tail"]:
            answer = self.tail_answers.pop(0) if len(self.tail_answers) > 1 else self.tail_answers[0]
            if answer is None:
                return CommandResult(255, "", "ssh: connection reset")
            return CommandResult(0, json.dumps(answer))
        return CommandResult(0, "{}")

    def call_json(self, args, timeout_sec=60):
        self.calls.append(list(args))
        if args[:2] == ["lease", "slots"]:
            return (CommandResult(0), self.slots) if self.slots is not None else (CommandResult(255, "", "ssh: no route to host"), None)
        if args[:2] == ["workroot", "inbox"]:
            return CommandResult(0), {"inbox": str(self.base / "inbox")}
        if args[:2] == ["offload", "import"]:
            return (CommandResult(0), self.imported) if self.imported else (CommandResult(2, "", "valtrion offload: BLOCKED: every checkout is in use"), None)
        return CommandResult(0), {}


def report(free=1, waiting=0, accept=True, version=None, editors=("2022.3.1f1",), projects=("Game",), kinds=("editor", "unity-run")):
    return {"host": "build-pc", "os": "windows", "version": version or commands.__version__,
            "unityEditor": {"capacity": 2, "free": free, "waiting": waiting, "held": []}, "unityVersions": list(editors),
            "offload": {"accept": accept, "kinds": list(kinds), "maxBundleMiB": 512}, "projects": list(projects)}


def entry(host_id="build-pc", offload_on=True):
    return HostEntry(host_id, "ssh", "windows", Path("C:/art"), ssh_target="dev@build-pc", valtrion_command="valtrion", offload=offload_on)


def tail(state="running", status=None, reason=None, lines=(), directory="/remote/run"):
    return {"jobId": "remote-1", "state": state, "status": status, "reason": reason, "stallSec": 600, "timeoutSec": 3600, "pid": 1,
            "lines": list(lines), "nextOffset": 10, "ageSec": 0.1, "serverUtc": "2026-01-01T00:00:00Z", "dir": directory}


class SlotReportTests(HostTestCase):
    def setUp(self):
        super().setUp()
        self.home.mkdir(parents=True, exist_ok=True)

    def manager(self):
        return lease.LeaseManager(self.home)

    def test_free_held_and_waiting_slots_are_counted(self):
        manager = self.manager()
        manager.try_acquire("unity-editor:slot1", "alice", "s1", 600, "tests", pid=os.getpid())
        manager._enqueue("unity-editor", "bob", "s2", os.getpid(), 600)  # noqa: SLF001 - a live waiter
        slots = manager.slot_report("unity-editor", 2)
        self.assertEqual((2, 1, 1), (slots["capacity"], slots["free"], slots["waiting"]))
        self.assertEqual([1], [held["slot"] for held in slots["held"]])
        self.assertEqual("alice", slots["held"][0]["owner"])

    def test_a_stale_lease_is_a_free_slot(self):
        manager = lease.LeaseManager(self.home, is_alive=lambda pid: False)
        manager.try_acquire("unity-editor:slot1", "alice", "s1", 1, "tests", pid=999999)
        later = lease.LeaseManager(self.home, clock=lambda: core.now() + __import__("datetime").timedelta(seconds=30), is_alive=lambda pid: False)
        self.assertEqual(2, later.slot_report("unity-editor", 2)["free"])

    def test_a_waiter_means_no_slot_for_a_newcomer(self):
        self.assertEqual(0, offload.free_slots(report(free=2, waiting=1)))
        self.assertEqual(2, offload.free_slots(report(free=2, waiting=0)))

    def test_lease_slots_prints_the_report_of_this_host(self):
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 3, "offload": {"accept": True, "kinds": ["editor"]}}), encoding="utf-8")
        code, out, err = run("lease", "slots")
        self.assertEqual(0, code, err)
        document = json.loads(out)
        self.assertEqual(3, document["unityEditor"]["capacity"])
        self.assertEqual({"accept": True, "kinds": ["editor"], "maxBundleMiB": 512}, document["offload"])
        self.assertEqual(commands.__version__, document["version"])


class SettingsTests(HostTestCase):
    def write(self, offload_settings):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.json").write_text(json.dumps({"offload": offload_settings}), encoding="utf-8")

    def test_defaults_accept_nothing(self):
        settings = config.load_offload_settings(self.home)
        self.assertFalse(settings.accept)
        self.assertEqual(config.OFFLOAD_KINDS, settings.kinds)
        self.assertTrue(settings.rerun_local)

    def test_a_full_object_is_read(self):
        self.write({"accept": True, "kinds": ["unity-run"], "maxBundleMiB": 64, "settleSec": 5, "lostSec": 30, "rerunLocal": False})
        settings = config.load_offload_settings(self.home)
        self.assertEqual((True, ("unity-run",), 64, 5.0, 30.0, False),
                         (settings.accept, settings.kinds, settings.max_bundle_mib, settings.settle_sec, settings.lost_sec, settings.rerun_local))

    def test_bad_values_are_refused_with_the_key(self):
        for bad, key in (({"accept": "yes"}, "accept"), ({"kinds": []}, "kinds"), ({"kinds": ["x"]}, "kinds"), ({"maxBundleMiB": 0}, "maxBundleMiB"),
                         ({"settleSec": -1}, "settleSec"), ({"lostSec": "a"}, "lostSec"), ({"other": 1}, "unknown keys"), ({"rerunLocal": 1}, "rerunLocal")):
            self.write(bad)
            with self.assertRaises(config.ConfigError, msg=str(bad)) as caught:
                config.load_offload_settings(self.home)
            self.assertIn(key, str(caught.exception))

    def test_hosts_add_offload_is_recorded_and_valid(self):
        code, _, err = run("hosts", "add", "--id", "build-pc", "--ssh", "dev@build-pc", "--os", "windows", "--artifacts-root", "C:/art", "--offload")
        self.assertEqual(0, code, err)
        document = json.loads((self.home / "hosts.json").read_text(encoding="utf-8"))
        self.assertEqual([], Validator().validate(document, "gdv.hosts/1"))
        self.assertTrue(config.find_host("build-pc", self.home).offload)
        self.assertFalse(any(e.offload for e in config.load_hosts(self.home) if e.kind == "local"))


class SurveyTests(unittest.TestCase):
    def survey(self, answers, unity="2022.3.1f1", names=("Game",), kind="editor"):
        sessions = {host: Session(Path("."), slots) for host, slots in answers.items()}
        entries = [entry(host) for host in answers]
        return offload.survey(entries, lambda e: sessions[e.id], kind, unity, names)

    def test_a_host_that_can_take_the_work_is_a_target(self):
        targets, reasons = self.survey({"build-pc": report()})
        self.assertEqual([("build-pc", "Game")], [(t.entry.id, t.project) for t in targets])
        self.assertEqual([], reasons)

    def test_every_reason_a_host_is_left_out_is_named(self):
        cases = {"accept": report(accept=False), "kind": report(kinds=("unity-run",)), "runs valtrion": report(version="0.0.1"),
                 "Unity 2022": report(editors=("6000.0.1f1",)), "managed project": report(projects=("Other",))}
        for key, answer in cases.items():
            targets, reasons = self.survey({"build-pc": answer})
            self.assertEqual([], targets, key)
            self.assertIn(key, reasons[0], reasons)

    def test_an_unreachable_host_is_a_reason_not_an_error(self):
        targets, reasons = self.survey({"build-pc": None})
        self.assertEqual([], targets)
        self.assertIn("build-pc: unreachable", reasons[0])

    def test_the_second_project_name_is_tried(self):
        targets, _ = self.survey({"build-pc": report(projects=("Repo",))}, names=("Folder", "Repo"))
        self.assertEqual("Repo", targets[0].project)

    def test_an_unknown_unity_version_does_not_exclude_a_host(self):
        targets, _ = self.survey({"build-pc": report()}, unity="unknown")
        self.assertEqual(1, len(targets))

    def test_slot_busy_reasons_are_recognised(self):
        for reason in ("all 2 slots of unity-editor are leased: x", "unity-editor is next for alice (session s), who has waited longer",
                       "unity-project:0123abcd is leased by alice", "a Unity Editor runs outside any lease: pid 4",
                       "only 100 MiB of memory are free, 2048 MiB are needed"):
            self.assertTrue(offload.slot_busy(reason), reason)
        for reason in (None, "", "no Unity editor was found", "the plan is invalid"):
            self.assertFalse(offload.slot_busy(reason), reason)


class AcceptImportTests(HostTestCase):
    def setUp(self):
        super().setUp()
        self.home.mkdir(parents=True, exist_ok=True)
        self.pool = self.base / "pool"
        self.write({"offload": {"accept": True, "maxBundleMiB": 1},
                    "projects": {"Game": {"remote": "https://example.com/game.git", "checkouts": [
                        {"id": "queue", "path": str(self.pool / "queue")}, {"id": "reference", "path": str(self.pool / "reference")}]}}})
        self.bundle = self.base / "x.bundle"
        self.bundle.write_bytes(b"0" * 100)
        patcher = mock.patch.object(codesync, "import_bundle", return_value=None)
        self.import_bundle = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, document):
        (self.home / "host.json").write_text(json.dumps(document), encoding="utf-8")

    def accept(self, bundle="default", kind="editor"):
        return offload.accept_import("Game", SHA, self.bundle if bundle == "default" else bundle, kind, self.home)

    def test_a_host_that_does_not_accept_refuses(self):
        self.write({"projects": {}})
        with self.assertRaisesRegex(offload.OffloadError, "does not accept"):
            self.accept()

    def test_a_kind_that_is_not_accepted_is_refused(self):
        self.write({"offload": {"accept": True, "kinds": ["unity-run"]}})
        with self.assertRaisesRegex(offload.OffloadError, "kind editor"):
            self.accept()

    def test_an_unknown_project_is_refused(self):
        with self.assertRaisesRegex(offload.OffloadError, "no project 'Other'"):
            offload.accept_import("Other", SHA, None, "editor", self.home)

    def test_the_default_checkout_is_used_and_the_bundle_removed_after_the_import(self):
        answer = self.accept()
        self.assertEqual(("queue", SHA, "Game"), (answer["checkoutId"], answer["sha"], answer["project"]))
        self.assertEqual(Path(answer["folder"]), self.pool / "queue")
        self.import_bundle.assert_called_once()
        self.assertFalse(self.bundle.exists())

    def test_a_checkout_in_use_is_skipped(self):
        manager = lease.LeaseManager(self.home)
        resource = lease.unity_project_resource(self.pool / "queue")
        manager.try_acquire(resource, "alice", "s1", 600, "run", pid=os.getpid())
        self.assertEqual("reference", self.accept()["checkoutId"])

    def test_every_checkout_in_use_is_refused(self):
        manager = lease.LeaseManager(self.home)
        for name in ("queue", "reference"):
            manager.try_acquire(lease.unity_project_resource(self.pool / name), "alice", f"s-{name}", 600, "run", pid=os.getpid())
        with self.assertRaisesRegex(offload.OffloadError, "in use"):
            self.accept()

    def test_the_bundle_is_removed_when_no_checkout_is_free(self):
        manager = lease.LeaseManager(self.home)
        for name in ("queue", "reference"):
            manager.try_acquire(lease.unity_project_resource(self.pool / name), "alice", f"s-{name}", 600, "run", pid=os.getpid())
        with self.assertRaises(offload.OffloadError):
            self.accept()
        self.assertFalse(self.bundle.exists())

    def test_the_bundle_is_removed_when_it_is_too_large(self):
        self.bundle.write_bytes(b"0" * (2 * 1024 * 1024))
        with self.assertRaises(offload.OffloadError):
            self.accept()
        self.assertFalse(self.bundle.exists())

    def test_a_bundle_over_the_limit_is_refused_and_not_imported(self):
        self.bundle.write_bytes(b"0" * (2 * 1024 * 1024))
        with self.assertRaisesRegex(offload.OffloadError, "maxBundleMiB"):
            self.accept()
        self.import_bundle.assert_not_called()

    def test_a_failed_import_is_the_reason(self):
        self.import_bundle.return_value = "the bundle needs commits that this host does not have"
        with self.assertRaisesRegex(offload.OffloadError, "needs commits"):
            self.accept()

    def test_without_a_bundle_the_commit_must_be_on_the_remote(self):
        with mock.patch("valtrion.checkout.preflight", return_value="the commit is not on the remote") as preflight:
            with self.assertRaisesRegex(offload.OffloadError, "not on the remote"):
                self.accept(bundle=None)
        preflight.assert_called_once()

    def test_the_command_prints_the_choice_and_refuses_a_bad_sha(self):
        code, out, err = run("offload", "import", "--project", "Game", "--sha", "abc")
        self.assertEqual(2, code)
        self.assertIn("40-character", err)
        with mock.patch("valtrion.checkout.preflight", return_value=None):
            code, out, err = run("offload", "import", "--project", "Game", "--sha", SHA)
        self.assertEqual(0, code, err)
        self.assertEqual("queue", json.loads(out)["checkoutId"])


class Place(unittest.TestCase):
    """One run placed by an `Offloader` with every outside call faked."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.directory = self.base / "job"
        self.directory.mkdir()
        self.events: list[tuple] = []
        self.session = Session(self.base, report())
        self.session.tail_answers = [tail("finished", "PASS", None, [json.dumps({"type": "step.finished", "message": "ran", "step": "1:run", "status": "PASS", "utc": "t"})])]
        self.cancelled = False
        self.clock = [0.0]
        self.forwarded: list = []
        self.snapshots = [codesync.Snapshot(SHA, SNAP, True, 0, None, 3)]
        self.remote_result = {"schema": "gdv.result/1", "status": "PASS", "reason": None, "host": {"id": "build-pc"}}

    def sleep(self, seconds):
        self.clock[0] += seconds

    def emit(self, kind, message=None, step=None, status=None, data=None):
        self.events.append((kind, message, step, status, data))

    def forward(self, entry_, target, prepared, reason):
        self.forwarded.append((entry_.id, target.project, prepared, reason))
        return {"jobId": "remote-1", "dir": "/remote/run", "relDir": "Game/aaaaaaaa/remote-1"}

    def pull(self, transport, remote_dir, local_dir, timeout_sec=600):
        local_dir.mkdir(parents=True, exist_ok=True)
        if self.remote_result is not None:
            (local_dir / "result.json").write_text(json.dumps(self.remote_result), encoding="utf-8")
        return CommandResult(0)

    def offloader(self, **overrides):
        placement = offload.Placement("editor", self.base / "project", ["Game"], "2022.3.1f1", overrides.pop("force", False))
        settings = overrides.pop("settings", config.OffloadSettings(settle_sec=10.0, lost_sec=20.0))
        options = dict(emit=self.emit, cancelled=lambda: self.cancelled, directory=self.directory, forward=self.forward, settings=settings,
                       local_room=lambda: False, entries=[entry()], remote_for=lambda e: self.session, sleep=self.sleep,
                       monotonic=lambda: self.clock[0], snapshot=lambda *a, **k: self.snapshots[0], pull=self.pull,
                       transport_for=lambda e: None)
        options.update(overrides)
        return offload.Offloader(placement, **options)

    def messages(self):
        return [message or "" for _, message, *_ in self.events]

    def test_a_free_local_slot_runs_here(self):
        self.assertIsNone(self.offloader(local_room=lambda: True).run())
        self.assertEqual([], self.session.calls)

    def test_no_marked_host_runs_here(self):
        self.assertIsNone(self.offloader(entries=[]).run())

    def test_a_free_remote_slot_takes_the_run_and_the_result_is_the_remote_result(self):
        outcome = self.offloader().run()
        self.assertEqual((constants.PASS, None, False), (outcome.status, outcome.reason, outcome.rerun))
        (host, project, prepared, reason), = self.forwarded
        self.assertEqual(("build-pc", "Game", "queue", SNAP, SHA, True), (host, project, prepared.checkout_id, prepared.sha, prepared.base_sha, prepared.dirty))
        self.assertIn("busy", reason)
        self.assertEqual(self.remote_result, core.read_json(self.directory / "result.json"))
        self.assertIn("running on host build-pc as job remote-1", " ".join(self.messages()))
        self.assertIn(("lease", "slots"), [tuple(call[:2]) for call in self.session.calls])

    def test_the_remote_events_are_copied_with_their_host_and_job(self):
        self.offloader().run()
        copied = [event for event in self.events if event[2] == "1:run"]
        self.assertEqual(1, len(copied))
        kind, message, step, status, data = copied[0]
        self.assertEqual(("step.finished", "[build-pc] ran", "PASS"), (kind, message, status))
        self.assertEqual({"host": "build-pc", "job": "remote-1", "utc": "t"}, data["remote"])

    def test_a_remote_job_that_ends_fail_is_the_result_of_the_run(self):
        self.session.tail_answers = [tail("finished", "FAIL", "2 tests failed")]
        self.remote_result = {"schema": "gdv.result/1", "status": "FAIL", "reason": "2 tests failed"}
        outcome = self.offloader().run()
        self.assertEqual((constants.FAIL, "2 tests failed"), (outcome.status, outcome.reason))

    def test_a_host_that_turns_out_busy_leaves_the_run_in_the_local_queue(self):
        self.session.tail_answers = [tail("finished", "BLOCKED", "all 2 slots of unity-editor are leased: x")]
        self.remote_result = {"schema": "gdv.result/1", "status": "BLOCKED", "reason": "all 2 slots of unity-editor are leased: x"}
        self.assertIsNone(self.offloader(settings=config.OffloadSettings(settle_sec=0.0)).run())
        self.assertFalse((self.directory / "result.json").exists(), "a lost race leaves no result behind")
        self.assertIn("stays in the local queue", " ".join(self.messages()))

    def test_a_blocked_result_for_another_reason_is_the_result(self):
        self.session.tail_answers = [tail("finished", "BLOCKED", "no Unity editor was found")]
        self.remote_result = {"schema": "gdv.result/1", "status": "BLOCKED", "reason": "no Unity editor was found"}
        self.assertEqual("BLOCKED", self.offloader().run().status)

    def test_a_host_without_a_free_slot_is_not_used_and_the_run_waits_the_settle_time_then_goes_local(self):
        self.session.slots = report(free=0)
        self.assertIsNone(self.offloader().run())
        self.assertEqual([], self.forwarded)
        self.assertGreaterEqual(self.clock[0], 10.0, "it looked again until the settle time was over")

    def test_a_slot_that_frees_up_during_the_settle_time_is_taken(self):
        answers = [report(free=0), report(free=1)]
        session = self.session
        original = session.call_json

        def slots_then_free(args, timeout_sec=60):
            if args[:2] == ["lease", "slots"]:
                session.slots = answers.pop(0) if len(answers) > 1 else answers[0]
            return original(args, timeout_sec)
        session.call_json = slots_then_free
        self.assertEqual("PASS", self.offloader().run().status)

    def test_the_local_queue_wins_when_a_local_slot_frees_up_while_looking(self):
        self.session.slots = report(free=0)
        room = iter([False, False, True])
        self.assertIsNone(self.offloader(local_room=lambda: next(room, True)).run())

    def test_a_snapshot_refused_for_an_lfs_file_runs_here_and_says_why(self):
        def refuse(*a, **k):
            raise codesync.SnapshotError("uncommitted Git LFS file(s) cannot be sent: Assets/a.bin")
        self.assertIsNone(self.offloader(snapshot=refuse).run())
        self.assertIn("cannot be sent", " ".join(self.messages()))

    def test_with_offload_a_snapshot_refused_for_an_lfs_file_is_blocked(self):
        def refuse(*a, **k):
            raise codesync.SnapshotError("uncommitted Git LFS file(s) cannot be sent: Assets/a.bin")
        outcome = self.offloader(snapshot=refuse, force=True).run()
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("uncommitted Git LFS", outcome.reason)

    def test_with_offload_no_host_is_blocked_not_queued(self):
        outcome = self.offloader(force=True, entries=[entry()], remote_for=lambda e: Session(self.base, None)).run()
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("--offload", outcome.reason)

    def test_a_host_that_refuses_the_code_leaves_the_run_local(self):
        self.session.imported = None
        self.assertIsNone(self.offloader(settings=config.OffloadSettings(settle_sec=0.0)).run())
        self.assertEqual([], self.forwarded)

    def test_a_host_that_does_not_start_the_job_leaves_the_run_local(self):
        def refuse(*a):
            raise offload.OffloadError("host build-pc did not start the run")
        self.assertIsNone(self.offloader(forward=refuse, settings=config.OffloadSettings(settle_sec=0.0)).run())

    def test_a_bundle_is_sent_to_the_inbox_of_the_host_before_the_import(self):
        bundle = self.base / "s.bundle"
        bundle.write_bytes(b"abc")
        self.snapshots = [codesync.Snapshot(SHA, SNAP, True, 0, bundle, 1)]
        self.offloader().run()
        self.assertEqual([(str(self.base / "inbox") + "\\" + f"1-{SNAP[:12]}.bundle", 3)], self.session.sent)
        imports = [call for call in self.session.calls if call[:2] == ["offload", "import"]]
        self.assertEqual(1, len(imports))
        self.assertEqual(SNAP, imports[0][imports[0].index("--sha") + 1])
        self.assertIn("--bundle", imports[0])

    def test_a_clean_pushed_commit_sends_no_bundle(self):
        self.snapshots = [codesync.Snapshot(SHA, SHA, False, 0, None, 0)]
        self.offloader().run()
        self.assertEqual([], self.session.sent)
        self.assertNotIn("--bundle", [call for call in self.session.calls if call[:2] == ["offload", "import"]][0])
        self.assertFalse(self.forwarded[0][2].dirty)

    def test_a_lost_host_is_run_again_here_by_default(self):
        self.session.tail_answers = [tail(), None]
        outcome = self.offloader().run()
        self.assertTrue(outcome.rerun)
        self.assertIn(["job", "cancel", "remote-1"], self.session.calls, "the remote job is cancelled on the way out")

    def test_a_lost_host_is_blocked_with_an_unknown_result_when_rerun_is_off(self):
        self.session.tail_answers = [tail(), None]
        outcome = self.offloader(settings=config.OffloadSettings(settle_sec=10.0, lost_sec=20.0, rerun_local=False)).run()
        self.assertEqual((constants.BLOCKED, False), (outcome.status, outcome.rerun))
        self.assertIn("result is unknown", outcome.reason)

    def test_a_cancel_of_the_local_job_cancels_the_remote_one_and_ends_cancelled(self):
        self.session.tail_answers = [tail(), tail("cancelled", None, "cancelled by request")]
        self.remote_result = None
        calls = [0]

        def cancelled():
            calls[0] += 1
            return calls[0] > 1  # not at the start, but once the remote run is being followed
        outcome = self.offloader(cancelled=cancelled).run()
        self.assertTrue(outcome.cancelled)
        self.assertIn(["job", "cancel", "remote-1"], self.session.calls)


class PlaceFailureTests(Place):
    """What happens to the remote run when something goes wrong around it."""

    def test_an_error_after_the_remote_run_started_cancels_it_and_never_starts_a_second_run_unasked(self):
        def broken(*a, **k):
            raise RuntimeError("scp exploded")
        outcome = self.offloader(pull=broken, settings=config.OffloadSettings(settle_sec=10.0, rerun_local=False)).run()
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertFalse(outcome.rerun)
        self.assertIn("result is unknown", outcome.reason)
        self.assertIn(["job", "cancel", "remote-1"], self.session.calls)

    def test_an_error_after_the_start_runs_here_again_only_when_allowed(self):
        def broken(*a, **k):
            raise RuntimeError("scp exploded")
        outcome = self.offloader(pull=broken).run()
        self.assertTrue(outcome.rerun)
        self.assertIn(["job", "cancel", "remote-1"], self.session.calls)

    def test_a_start_that_was_not_answered_may_have_created_a_job_so_the_run_is_not_just_queued(self):
        def unanswered(*a):
            raise offload.StartUncertain("host build-pc did not answer the start of the run")
        outcome = self.offloader(forward=unanswered, settings=config.OffloadSettings(settle_sec=10.0, rerun_local=False)).run()
        self.assertEqual(constants.BLOCKED, outcome.status)
        self.assertIn("result is unknown", outcome.reason)
        self.assertNotIn(["job", "cancel", "remote-1"], self.session.calls, "its id is not known")

    def test_a_result_of_an_earlier_attempt_is_not_taken_for_this_one(self):
        folder = self.directory / "remote"
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps({"status": "FAIL", "reason": "stale"}), encoding="utf-8")
        self.session.tail_answers = [tail("finished", "PASS", None)]
        self.pull = lambda *a, **k: CommandResult(1, "", "scp: timeout")  # noqa: E731
        outcome = self.offloader(pull=lambda *a, **k: CommandResult(1, "", "scp: timeout")).run()
        self.assertEqual((constants.PASS, None), (outcome.status, outcome.reason))
        self.assertFalse((self.directory / "result.json").exists())
        self.assertFalse((self.directory / "remote" / "result.json").exists())

    def test_a_cancel_that_did_not_get_through_is_tried_again(self):
        self.session.tail_answers = [tail(), tail(), tail("cancelled", None, "cancelled by request")]
        cancels = []
        original = self.session.call

        def call(args, timeout_sec=60):
            if args[:2] == ["job", "cancel"]:
                cancels.append(args)
                return CommandResult(255, "", "ssh: connection reset") if len(cancels) == 1 else CommandResult(0, "{}")
            return original(args, timeout_sec)
        self.session.call = call
        self.remote_result = None
        calls = [0]

        def cancelled():
            calls[0] += 1
            return calls[0] > 1
        self.offloader(cancelled=cancelled).run()
        self.assertGreaterEqual(len(cancels), 2, cancels)

    def test_a_host_that_refused_is_not_asked_again_during_the_settle_time(self):
        attempts = []

        def refuse(*a):
            attempts.append(1)
            raise offload.OffloadError("host build-pc does not accept offloaded work")
        self.assertIsNone(self.offloader(forward=refuse).run())
        self.assertEqual(1, len(attempts))

    def test_a_snapshot_refusal_is_not_repeated_for_every_poll(self):
        attempts = []

        def refuse(*a, **k):
            attempts.append(1)
            raise codesync.SnapshotError("uncommitted Git LFS file(s) cannot be sent: Assets/a.bin")
        self.assertIsNone(self.offloader(snapshot=refuse).run())
        self.assertEqual(1, len(attempts))


class JobHookTests(HostTestCase):
    def test_an_error_of_the_offer_itself_leaves_the_run_in_the_local_queue(self):
        runner = SimpleNamespace(spec={"offload": {"kind": "editor", "projectDir": "x", "names": ["Game"]}}, events=SimpleNamespace(emit=mock.Mock()),
                                 store=SimpleNamespace(cancel=Path("nope"), directory=self.base))
        with mock.patch.object(offload.Offloader, "run", side_effect=RuntimeError("boom")):
            self.assertFalse(jobs._Runner.run_elsewhere(runner))  # noqa: SLF001
        self.assertIn("offloading is off", runner.events.emit.call_args.args[1])


class MarksTests(HostTestCase):
    def test_a_run_folder_names_the_host_the_job_follows(self):
        place = Place()
        place.setUp()
        try:
            place.offloader().run()
            marker = core.read_json(place.directory / offload.MARKER)
            self.assertEqual(("build-pc", "remote-1"), (marker["host"], marker["job"]))
        finally:
            place.doCleanups()

    def test_the_dashboard_shows_where_a_job_runs(self):
        from valtrion import dashboard
        request = jobs.JobRequest(kind="pipeline", project="Game", sha=SHA, label="g", payload={"type": "pipeline", "file": "<inline>"},
                                  host={"id": "mac", "transport": "local", "selectedBy": "local"})
        layout, _ = jobs.create_job(request, self.base / "runs", self.home)
        core.atomic_write_json(layout.directory / offload.MARKER, {"host": "build-pc", "job": "remote-1", "note": "sent", "reason": "busy"})
        (row,) = dashboard.job_list(self.home, core.now())
        self.assertEqual({"to": "build-pc", "job": "remote-1", "note": "sent", "reason": "busy"}, row["offloaded"])

    def test_the_report_row_says_it_ran_elsewhere(self):
        from valtrion import report
        from valtrion.select import Check
        from .test_pipeline_resume import _result
        result = _result(host={"id": "build-pc", "transport": "ssh", "selectedBy": "capability", "tools": {"offloadFrom": "mac"}})
        row = report._result_row(Check("perf.open", "device", "android", "L2"), "android", result)  # noqa: SLF001
        self.assertIn("ran on build-pc, sent from mac (offloaded)", row.detail)
        self.assertEqual("build-pc", row.host)


class OffloadedPipelineTests(unittest.TestCase):
    """The receiving host: the pipeline checks out the snapshot commit, names the sender's commit and says the tree was dirty."""

    def test_the_result_names_the_base_commit_the_snapshot_checkout_and_the_transfer(self):
        from .test_checkout import QueuedJobsTests
        case = QueuedJobsTests("test_a_dirty_managed_checkout_is_reset_not_refused")
        case.setUp()
        try:
            document = case.build_only(case.first)
            payload = {"type": "pipeline", "file": "<inline>", "inline": document, "baseDir": str(case.base / "plans"), "devicePollSec": 0.02,
                       "projectDir": str(case.shared), "shaPinned": True, "checkoutId": "queue", "checkoutRoot": str(case.shared),
                       "checkoutRemote": None, "checkoutSha": case.second, "snapshotDirty": True,
                       "offload": {"offloadFrom": "mac", "offloadReason": "every local Editor slot was busy", "offloadSnapshot": "uncommitted changes (2 path(s))",
                                   "offloadBaseSha": case.first}}
            request = jobs.JobRequest(kind="pipeline", project="Sample", sha=case.first, label="queue", payload=payload,
                                      host={"id": "win", "transport": "ssh", "selectedBy": "capability"}, stall_sec=30, timeout_sec=120,
                                      owner="tester", lease_wait_sec=5, tick_sec=0.1)
            info = jobs.start_job(request, case.root, case.home, foreground=True)
            store = jobs.JobStore(Path(info["dir"]))
            self.assertEqual("PASS", store.read_job()["status"], store.read_job()["reason"])
            result = core.read_json(store.result)
            self.assertEqual(case.first, result["candidate"]["sha"], "the result names the sender's commit")
            self.assertTrue(result["candidate"]["dirty"], "the sender's tree was not clean")
            self.assertEqual(case.second, result["checkout"]["sha"], "what was really checked out")
            self.assertEqual(case.second, case.head())
            self.assertEqual({"offloadFrom": "mac", "offloadReason": "every local Editor slot was busy", "offloadSnapshot": "uncommitted changes (2 path(s))",
                              "offloadBaseSha": case.first}, {k: v for k, v in result["host"]["tools"].items() if k.startswith("offload")})
            self.assertEqual("capability", result["host"]["selectedBy"], "selectedBy is not changed")
            self.assertEqual([], Validator().validate(result, "gdv.result/1"))
        finally:
            case.doCleanups()


class EditorPlatformCheckoutTests(unittest.TestCase):
    """The Editor platform runs from the project folder itself: a managed checkout is refused, except for a run that was offloaded
    (the folder is verified again before the run stage)."""

    def run_build(self, offload_info):
        from valtrion import providers
        from .test_checkout import QueuedJobsTests
        case = QueuedJobsTests("test_a_dirty_managed_checkout_is_reset_not_refused")
        case.setUp()
        self.addCleanup(case.doCleanups)
        adapter = type(providers.get("android"))
        with mock.patch.object(adapter, "build_uses_editor", False, create=True):
            payload = {"type": "pipeline", "file": "<inline>", "inline": case.build_only(case.first), "baseDir": str(case.base / "plans"),
                       "devicePollSec": 0.02, "projectDir": str(case.shared), "shaPinned": True, "checkoutId": "queue",
                       "checkoutRoot": str(case.shared), "checkoutRemote": None}
            if offload_info:
                payload["offload"] = {"offloadFrom": "mac"}
            request = jobs.JobRequest(kind="pipeline", project="Sample", sha=case.first, label="queue", payload=payload,
                                      host={"id": "win", "transport": "local", "selectedBy": "local"}, stall_sec=30, timeout_sec=120,
                                      owner="tester", lease_wait_sec=5, tick_sec=0.1)
            info = jobs.start_job(request, case.root, case.home, foreground=True)
        return jobs.JobStore(Path(info["dir"])).read_job()

    def test_a_managed_checkout_is_refused_for_an_ordinary_editor_run(self):
        job = self.run_build(False)
        self.assertEqual("BLOCKED", job["status"])
        self.assertIn("a managed checkout is for platforms", job["reason"])

    def test_an_offloaded_editor_run_may_use_one(self):
        job = self.run_build(True)
        self.assertEqual("PASS", job["status"], job["reason"])


class SendingJobTests(HostTestCase):
    """The sending host: a pipeline for the Editor platform leaves `spec.offload` in its job, other runs do not."""

    def setUp(self):
        super().setUp()
        self.project = self.base / "Game"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1\n", encoding="utf-8")
        import subprocess
        for arguments in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", *arguments], cwd=self.project, check=True)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.project, check=True, capture_output=True, text=True).stdout.strip()
        self.plan = self.base / "open.plan.json"
        core.atomic_write_json(self.plan, {"schema": "gdv.plan/1", "id": "open", "kind": "smoke", "requires": {"platforms": ["editor"]},
                                           "dataPolicy": "devdata", "budgetSeconds": 30, "steps": [{"type": "wait", "timeoutSec": 5}]})
        self.started: list[jobs.JobRequest] = []

        def fake_start(request, *args, **kwargs):
            self.started.append(request)
            return {"jobId": "j1", "dir": str(self.base / "j1"), "relDir": "Game/x/j1", "host": "local"}
        patcher = mock.patch.object(jobs, "start_job", side_effect=fake_start)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Which host a run would be given to depends on what the machine has installed; these tests are about what the job is given.
        patcher = mock.patch.object(commands, "resolve_host", side_effect=self.resolve)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, request, pinned, remote_project=None):
        entries = {entry.id: entry for entry in config.load_hosts()}
        chosen = entries[pinned] if pinned else next(e for e in entries.values() if e.kind == "local")
        kind = "local" if chosen.kind == "local" else "ssh"
        return SimpleNamespace(host=chosen.info(), selected_by="pinned" if pinned else "local", transport=kind, reasons=()), entries

    def run_plan(self, *extra):
        return run("run", str(self.plan), "--repo", "Game", "--sha", self.head, "--app", "editor", "--platform", "editor",
                   "--project-dir", str(self.project), *extra)

    def mark(self):
        config.add_ssh_host("build-pc", "dev@build-pc", "windows", "C:/art", offload=True)

    def test_no_marked_host_no_offload_spec(self):
        code, _, err = self.run_plan()
        self.assertEqual(0, code, err)
        self.assertNotIn("offload", self.started[0].extra)

    def test_a_marked_host_puts_the_offer_into_the_job(self):
        self.mark()
        code, _, err = self.run_plan()
        self.assertEqual(0, code, err)
        spec = self.started[0].extra["offload"]
        self.assertEqual(("editor", ["Game"], "2022.3.1f1", False), (spec["kind"], spec["names"], spec["unityVersion"], spec["force"]))
        self.assertEqual("run", spec["forward"]["command"])
        self.assertEqual(str(self.project), spec["projectDir"])

    def test_a_relative_plan_path_is_made_absolute_for_the_job(self):
        self.mark()
        previous = os.getcwd()
        os.chdir(self.base)
        try:
            code, _, err = run("run", "open.plan.json", "--repo", "Game", "--sha", self.head, "--app", "editor", "--platform", "editor",
                               "--project-dir", str(self.project))
        finally:
            os.chdir(previous)
        self.assertEqual(0, code, err)
        raw = self.started[0].extra["offload"]["forward"]["rawArgv"]
        self.assertEqual(str(self.plan.resolve()), raw[1])

    def test_a_run_that_names_another_commit_than_the_working_copy_is_not_offloaded(self):
        self.mark()
        code, _, err = run("run", str(self.plan), "--repo", "Game", "--sha", "c" * 40, "--app", "editor", "--platform", "editor",
                           "--project-dir", str(self.project))
        self.assertEqual(0, code, err)
        self.assertNotIn("offload", self.started[0].extra, "it is refused locally as before, not tested on another commit")

    def test_offload_forces_and_no_offload_forbids(self):
        self.mark()
        self.run_plan("--offload")
        self.assertTrue(self.started[-1].extra["offload"]["force"])
        self.run_plan("--no-offload")
        self.assertNotIn("offload", self.started[-1].extra)

    def test_a_pinned_host_is_not_offloaded(self):
        self.mark()
        with mock.patch.object(commands, "_start_remote_pipeline", return_value=0):
            code, _, _ = self.run_plan("--host", "build-pc")
        self.assertEqual([], self.started)

    def test_the_forwarded_command_carries_the_checkout_the_commit_and_the_notes(self):
        self.mark()
        sent = []

        class Host:
            kind = "ssh"

            def run(self, command, timeout_sec, cwd=None):
                sent.append(list(command))
                if command[:2] == ["gdv", "workroot"]:
                    return CommandResult(0, json.dumps({"inbox": str(self_.base / "inbox")}))
                return CommandResult(0, json.dumps({"jobId": "remote-1", "dir": "/d", "relDir": "x"}))

            def put(self, local, remote_path, timeout_sec):
                Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
                Path(remote_path).write_bytes(Path(local).read_bytes())
                return CommandResult(0)
        self_ = self
        self.run_plan()
        spec = self.started[0].extra["offload"]
        prepared = offload.Prepared("queue", SNAP, SHA, True, "uncommitted changes (3 path(s)) sent as a bundle on top of aaaaaaaaaaaa")
        with mock.patch.object(commands, "_remote", side_effect=lambda e, a, retry=True: remote.Remote(Host(), ("gdv",), None)):
            started = commands.offload_forward(spec, config.find_host("build-pc"), offload.Target(config.find_host("build-pc"), report(), "Game"),
                                               prepared, "every local Editor slot was busy")
        self.assertEqual("remote-1", started["jobId"])
        command = next(call for call in sent if call[:2] == ["gdv", "run"])
        value = lambda name: command[command.index(name) + 1]  # noqa: E731
        self.assertEqual(("queue", SHA, "0", SNAP, "every local Editor slot was busy"),
                         (value("--checkout"), value("--sha"), value("--lease-wait"), value("--checkout-sha"), value("--offload-reason")))
        self.assertIn("--snapshot-dirty", command)
        self.assertNotIn("--project-dir", command)
        self.assertNotIn(str(self.project), command)
        self.assertEqual(["--local", "--transport", "ssh", "--selected-by", "capability"], command[-5:])


class ReceivingTests(HostTestCase):
    """`run --local --offload-from`: the host takes the run only when it accepts offloaded work."""

    def test_a_host_that_does_not_accept_blocks_the_run(self):
        args = SimpleNamespace(offload_from="mac", checkout_sha=SNAP, snapshot_dirty=True, offload_reason="r", offload_note="n", sha=SHA)
        payload: dict = {}
        self.assertIn("does not accept", commands._offload_payload(args, payload))  # noqa: SLF001
        self.assertEqual({}, payload)

    def test_an_accepting_host_records_what_the_sender_said(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.json").write_text(json.dumps({"offload": {"accept": True}}), encoding="utf-8")
        args = SimpleNamespace(offload_from="mac", checkout_sha=SNAP, snapshot_dirty=True, offload_reason="r", offload_note="n", sha=SHA)
        payload: dict = {}
        self.assertIsNone(commands._offload_payload(args, payload))  # noqa: SLF001
        self.assertEqual((SNAP, True), (payload["checkoutSha"], payload["snapshotDirty"]))
        self.assertEqual({"offloadFrom": "mac", "offloadReason": "r", "offloadSnapshot": "n", "offloadBaseSha": SHA}, payload["offload"])

    def test_an_ordinary_run_is_untouched(self):
        payload: dict = {}
        self.assertIsNone(commands._offload_payload(SimpleNamespace(offload_from=None), payload))  # noqa: SLF001
        self.assertEqual({}, payload)


if __name__ == "__main__":
    unittest.main()
