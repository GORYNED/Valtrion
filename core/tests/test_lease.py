import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from valtrion import constants, core, lease
from valtrion.schema import Validator


class FakeTime:
    def __init__(self):
        self.now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
        self.mono = 0.0

    def clock(self):
        return self.now

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.mono += seconds
        self.now += timedelta(seconds=seconds)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.time = FakeTime()
        self.alive: set[int] = {111, 222}
        self.manager = self.make_manager()

    def make_manager(self):
        return lease.LeaseManager(
            Path(self._folder.name), "test-host", self.time.clock, lambda pid: pid in self.alive, self.time.sleep, self.time.monotonic)

    def acquire(self, resource="device:ipad", session="s1", pid=111, ttl=60, owner="alice", **options):
        return self.manager.acquire(resource, owner, session, ttl, "test", pid, **options)

    def test_a_lease_file_validates_against_the_contract(self):
        result = self.acquire()
        self.assertTrue(result.acquired)
        document = json.loads(self.manager.path_for("device:ipad").read_text(encoding="utf-8"))
        self.assertEqual([], Validator().validate(document, "gdv.lease/1"))
        self.assertNotIn(":", self.manager.path_for("device:ipad").name)

    def test_a_live_lease_is_never_taken_even_after_the_ttl_expires(self):
        self.acquire(session="s1", pid=111, ttl=60)
        self.time.advance(3600)  # TTL long expired, but the holder process is alive
        other = self.acquire(session="s2", pid=222, owner="bob")
        self.assertFalse(other.acquired)
        self.assertEqual(constants.BLOCKED, other.status)
        self.assertIn("alice", other.reason)
        self.assertIn("s1", other.reason)

    def test_a_dead_holder_within_the_ttl_keeps_the_lease(self):
        self.acquire(session="s1", pid=111, ttl=60)
        self.alive.discard(111)
        self.time.advance(30)
        self.assertFalse(self.acquire(session="s2", pid=222, owner="bob").acquired)

    def test_reclaim_only_after_the_ttl_when_the_holder_was_killed(self):
        self.acquire(session="s1", pid=111, ttl=60)
        self.alive.discard(111)  # kill -9 of the holder
        self.time.advance(59)
        self.assertFalse(self.acquire(session="s2", pid=222, owner="bob").acquired, "reclaimed before the TTL expired")
        self.time.advance(2)
        taken = self.acquire(session="s2", pid=222, owner="bob")
        self.assertTrue(taken.acquired)
        self.assertEqual("s1", taken.reclaimed.session)
        self.assertEqual("bob", self.manager.status("device:ipad").record.owner)

    def test_renewal_keeps_a_lease_alive_across_the_ttl(self):
        self.acquire(session="s1", pid=111, ttl=60)
        for _ in range(5):
            self.time.advance(40)
            self.assertTrue(self.manager.renew("device:ipad", "s1", 111))
        self.alive.discard(111)
        self.time.advance(30)  # 30 s since the last renewal: the TTL has not expired
        self.assertFalse(self.acquire(session="s2", pid=222).acquired)

    def test_renew_and_release_refuse_a_foreign_holder(self):
        self.acquire(session="s1", pid=111)
        self.assertFalse(self.manager.renew("device:ipad", "s2", 222))
        self.assertFalse(self.manager.release("device:ipad", "s2", 222))
        self.assertEqual("held", self.manager.status("device:ipad").state)
        self.assertTrue(self.manager.release("device:ipad", "s1", 111))
        self.assertEqual("free", self.manager.status("device:ipad").state)
        self.assertTrue(self.manager.release("device:ipad", "s1", 111), "releasing twice is idempotent")

    def test_acquire_is_idempotent_for_the_same_holder(self):
        self.acquire(session="s1", pid=111)
        again = self.acquire(session="s1", pid=111)
        self.assertTrue(again.acquired)

    def test_bounded_wait_is_blocked_naming_the_holder(self):
        self.acquire(session="s1", pid=111)
        started = self.time.mono
        waited = self.acquire(session="s2", pid=222, owner="bob", wait_sec=10, poll_sec=2)
        self.assertFalse(waited.acquired)
        self.assertAlmostEqual(10, self.time.mono - started, delta=2)
        self.assertEqual("s1", waited.holder.session)
        self.assertIn("alice", waited.reason)

    def test_a_waiter_gets_the_lease_when_it_is_released_in_time(self):
        self.acquire(session="s1", pid=111)
        original_sleep = self.time.sleep

        def sleep_and_release(seconds):
            original_sleep(seconds)
            self.manager.release("device:ipad", "s1", 111)

        self.manager._sleep = sleep_and_release
        self.assertTrue(self.acquire(session="s2", pid=222, owner="bob", wait_sec=10, poll_sec=2).acquired)

    def test_counted_resource_uses_one_lease_per_slot(self):
        first = self.manager.acquire_slot("unity-editor", 2, "a", "s1", 60, "x", 111)
        second = self.manager.acquire_slot("unity-editor", 2, "b", "s2", 60, "x", 222)
        self.assertEqual({"unity-editor:slot1", "unity-editor:slot2"}, {first.lease.resource, second.lease.resource})
        third = self.manager.acquire_slot("unity-editor", 2, "c", "s3", 60, "x", 333)
        self.assertFalse(third.acquired)
        self.assertIn("all 2 slots", third.reason)
        self.assertIn("a", third.reason)
        again = self.manager.acquire_slot("unity-editor", 2, "a", "s1", 60, "x", 111)
        self.assertEqual(first.lease.resource, again.lease.resource, "a holder keeps its own slot")
        self.manager.release(first.lease.resource, "s1", 111)
        self.assertTrue(self.manager.acquire_slot("unity-editor", 2, "c", "s3", 60, "x", 333).acquired)

    def test_a_holder_keeps_its_own_slot_when_a_lower_one_is_freed(self):
        self.manager.acquire_slot("unity-editor", 2, "a", "s1", 60, "x", 111)  # slot1
        second = self.manager.acquire_slot("unity-editor", 2, "b", "s2", 60, "x", 222)  # slot2
        self.manager.release("unity-editor:slot1", "s1", 111)
        again = self.manager.acquire_slot("unity-editor", 2, "b", "s2", 60, "x", 222)
        self.assertEqual(second.lease.resource, again.lease.resource)
        self.assertEqual("free", self.manager.status("unity-editor:slot1").state, "a retry must not take a second slot")

    def test_on_wait_is_called_after_the_first_failed_attempt_then_every_thirty_seconds(self):
        self.acquire(session="s1", pid=111, owner="alice")
        calls = []
        result = self.acquire(session="s2", pid=222, owner="bob", wait_sec=100, poll_sec=10, on_wait=lambda reason, waited: calls.append((reason, waited)))
        self.assertFalse(result.acquired)
        self.assertEqual([0, 30, 60, 90], [waited for _, waited in calls])
        self.assertIn("alice", calls[0][0])

    def test_on_wait_is_not_called_without_waiting_or_when_the_lease_is_free(self):
        calls = []
        self.assertTrue(self.acquire(wait_sec=60, on_wait=lambda *args: calls.append(args)).acquired)
        self.assertFalse(self.acquire(session="s2", pid=222, wait_sec=0, on_wait=lambda *args: calls.append(args)).acquired)
        self.assertEqual([], calls)

    def test_a_failing_on_wait_never_breaks_the_acquisition(self):
        self.acquire(session="s1", pid=111, owner="alice")
        calls = []

        def broken(reason, waited):
            calls.append(waited)
            raise OSError("stderr is closed")

        def release_then_sleep(seconds):
            self.time.sleep(seconds)
            self.manager.release("device:ipad", "s1", 111)

        self.manager._sleep = release_then_sleep
        self.assertTrue(self.acquire(session="s2", pid=222, owner="bob", wait_sec=10, poll_sec=2, on_wait=broken).acquired)
        self.assertEqual([0], calls)

    def test_on_wait_for_a_counted_resource_reports_the_slots(self):
        self.manager.acquire_slot("unity-editor", 1, "a", "s1", 60, "x", 111)
        calls = []
        result = self.manager.acquire_slot("unity-editor", 1, "b", "s2", 60, "x", 222, wait_sec=70, poll_sec=10,
                                           on_wait=lambda reason, waited: calls.append((reason, waited)))
        self.assertFalse(result.acquired)
        self.assertEqual([0, 30, 60], [waited for _, waited in calls])
        self.assertIn("all 1 slots of unity-editor", calls[0][0])

    def test_resource_names_cannot_escape_the_lease_folder_or_collide(self):
        for name in ("../../x", "a\\b", "a/b", "", ".hidden", "a%3Ab", "x" * 300):
            with self.subTest(name), self.assertRaises(ValueError):
                lease.lease_file_name(name)
        self.assertEqual("device%3Aios%3A1.lock", lease.lease_file_name("device:ios:1"))

    def test_a_failing_renewal_is_recorded_as_lost_and_does_not_end_the_keeper(self):
        manager = lease.LeaseManager(Path(self._folder.name), "h")
        manager.acquire("device:flaky", "me", "job", 60, "x", os.getpid())
        keeper = lease.LeaseKeeper(manager, "job", os.getpid(), 0.1, "me")
        keeper.add("device:flaky")
        calls = []

        def broken(*args, **kwargs):
            calls.append(1)
            raise PermissionError("sharing violation")

        manager.renew = broken
        keeper.start()
        time.sleep(0.6)
        keeper.stop()
        self.assertEqual(["device:flaky"], keeper.lost)
        self.assertGreater(len(calls), 1, "the thread kept running after the first failure")

    def test_a_torn_lock_file_is_held_until_the_grace_passes(self):
        path = self.manager.path_for("device:x")
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")
        # Real file times against the injected clock: the file is brand new, so it is still being written.
        manager = lease.LeaseManager(Path(self._folder.name), "h", core.now, lambda _: False, lambda s: None, time.monotonic)
        self.assertFalse(manager.try_acquire("device:x", "a", "s", 60, "x", 5).acquired)
        old = time.time() - lease.UNREADABLE_GRACE_SEC - 5
        os.utime(path, (old, old))
        self.assertTrue(manager.try_acquire("device:x", "a", "s", 60, "x", 5).acquired)

    def test_two_reclaimers_never_both_win(self):
        self.acquire(session="s1", pid=111, ttl=10)
        self.alive.discard(111)
        self.time.advance(20)
        winners = []
        for index, pid in enumerate((222, 333)):
            self.alive.add(pid)
            result = self.acquire(session=f"r{index}", pid=pid)
            if result.acquired:
                winners.append(result.lease.session)
        self.assertEqual(["r0"], winners)

    def test_keeper_renews_in_the_background(self):
        real = lease.LeaseManager(Path(self._folder.name), "h")
        outcome = real.acquire("device:real", "me", "job-1", 2, "x", os.getpid())
        keeper = lease.LeaseKeeper(real, "job-1", os.getpid(), 0.2, "me")
        keeper.add("device:real")
        keeper.start()
        before = real.status("device:real").record.renewed_utc
        time.sleep(0.8)
        after = real.status("device:real").record.renewed_utc
        self.assertGreater(core.parse_utc(after), core.parse_utc(before))
        self.assertEqual([], keeper.release_all())
        keeper.stop()
        self.assertEqual("free", real.status("device:real").state)

    def test_release_waits_for_a_pending_renewal_before_removing_the_lease(self):
        manager = lease.LeaseManager(Path(self._folder.name), "h")
        resource = "device:pending"
        self.assertTrue(manager.acquire(resource, "me", "job", 60, "x", os.getpid()).acquired)
        keeper = lease.LeaseKeeper(manager, "job", os.getpid(), 0.01, "me")
        keeper.add(resource)
        pending = threading.Event()
        publish = threading.Event()
        release_decided = threading.Event()
        original_write = core.atomic_write_json
        original_release = manager.release
        errors, released = [], []

        class ObservedLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    if threading.current_thread() is releaser:
                        release_decided.set()  # Release is waiting for the in-flight renewal.
                    self.lock.acquire()

            def __exit__(self, *args):
                self.lock.release()

        def paused_write(*args, **kwargs):
            pending.set()  # renew() has read the holder, but has not published its replacement.
            if not publish.wait(5):
                raise TimeoutError("test did not permit renewal publication")
            return original_write(*args, **kwargs)

        def observed_release(*args, **kwargs):
            try:
                return original_release(*args, **kwargs)
            finally:
                release_decided.set()  # The old implementation removes the file before publication.

        def release():
            try:
                released.extend(keeper.release_all())
            except Exception as error:
                errors.append(error)

        releaser = threading.Thread(target=release, name="test-lease-release")
        keeper._lock = ObservedLock()
        with mock.patch.object(core, "atomic_write_json", side_effect=paused_write), \
                mock.patch.object(manager, "release", side_effect=observed_release):
            keeper.start()
            try:
                self.assertTrue(pending.wait(5), "renewal reached its publication barrier")
                releaser.start()
                self.assertTrue(release_decided.wait(5), "release either waits for renewal or removes its file")
            finally:
                publish.set()
                if releaser.ident is not None:
                    releaser.join(5)
                keeper.stop()
        self.assertFalse(releaser.is_alive(), "release completed")
        self.assertEqual([], errors)
        self.assertEqual([], released)
        self.assertEqual([], keeper.held())
        self.assertEqual([], keeper.lost)
        self.assertEqual("free", manager.status(resource).state)

    def test_real_killed_holder_blocks_then_is_reclaimed_after_the_ttl(self):
        manager = lease.LeaseManager(Path(self._folder.name), "h")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        manager.acquire("device:killed", "me", "dead-session", 3, "x", child.pid)
        self.assertFalse(manager.try_acquire("device:killed", "other", "s2", 3, "x", os.getpid()).acquired, "holder alive")
        child.kill()
        child.wait()
        self.assertFalse(manager.try_acquire("device:killed", "other", "s2", 3, "x", os.getpid()).acquired, "TTL not expired")
        time.sleep(3.3)
        taken = manager.try_acquire("device:killed", "other", "s2", 3, "x", os.getpid())
        self.assertTrue(taken.acquired)
        self.assertEqual("dead-session", taken.reclaimed.session)


class SlotPreconditionTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.time = FakeTime()
        self.manager = lease.LeaseManager(
            Path(self._folder.name), "test-host", self.time.clock, lambda pid: pid in {111, 222}, self.time.sleep, self.time.monotonic)

    def slot(self, session="s1", pid=111, owner="alice", **options):
        return self.manager.acquire_slot("unity-editor", 2, owner, session, 60, "test", pid, **options)

    def free_slots(self):
        return [p.name for p in self.manager.directory.glob("unity-editor*.lock")]

    def test_a_refusal_keeps_the_slots_free_and_names_the_reason(self):
        asked = []
        result = self.slot(precondition=lambda: asked.append(1) or "an Editor runs by hand")
        self.assertFalse(result.acquired)
        self.assertEqual("an Editor runs by hand", result.reason)
        self.assertTrue(result.gated)
        self.assertEqual([], self.free_slots())
        self.assertEqual(1, len(asked))

    def test_the_slot_is_taken_once_the_precondition_passes_and_it_is_asked_again_with_the_slot_held(self):
        answers = iter(["busy", "busy", None, None])
        seen_slots = []

        def precondition():
            seen_slots.append(self.free_slots())
            return next(answers)

        result = self.slot(wait_sec=30, poll_sec=2, precondition=precondition)
        self.assertTrue(result.acquired)
        self.assertEqual([[], [], [], ["unity-editor%3Aslot1.lock"]], seen_slots)  # two refusals, a pass, the check with the slot held
        self.assertEqual(["unity-editor%3Aslot1.lock"], self.free_slots())

    def test_a_refusal_after_the_slot_was_taken_gives_it_back(self):
        answers = iter([None, "an Editor appeared", None, None])
        result = self.slot(wait_sec=30, poll_sec=2, precondition=lambda: next(answers))
        self.assertTrue(result.acquired)
        self.assertEqual(["unity-editor%3Aslot1.lock"], self.free_slots())  # taken again after the one that was given back

    def test_a_waiter_held_back_by_its_precondition_does_not_keep_the_others_waiting(self):
        gated = self.slot(session="gated", pid=111, wait_sec=5, poll_sec=1, precondition=lambda: "an Editor runs by hand")
        self.assertFalse(gated.acquired)
        other = self.slot(session="other", pid=222, owner="bob")
        self.assertTrue(other.acquired, other.reason)  # no ticket of the gated waiter stands in front
        self.assertEqual([], list((self.manager.directory / "queue" / "unity-editor").glob("*.ticket"))
                         if (self.manager.directory / "queue" / "unity-editor").is_dir() else [])

    def test_a_holder_that_has_a_slot_is_not_asked_again(self):
        self.assertTrue(self.slot().acquired)
        self.assertTrue(self.slot(precondition=lambda: self.fail("asked although the slot is held")).acquired)

    def test_a_full_resource_still_queues_in_order(self):
        self.assertTrue(self.slot(session="a", pid=111).acquired)
        self.assertTrue(self.slot(session="b", pid=111).acquired)
        waited = self.slot(session="c", pid=222, owner="bob", wait_sec=3, poll_sec=1, precondition=lambda: None)
        self.assertFalse(waited.acquired)
        self.assertFalse(waited.gated)
        self.assertIn("all 2 slots", waited.reason)


if __name__ == "__main__":
    unittest.main()
