"""The dashboard: the snapshot it builds from what Valtrion keeps on disk, and the local server that shows it."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from valtrion import core, dashboard, dashboardpage, dashboardserver, lease, unityguard


class FakeProbe(unityguard.Probe):
    os_name = "windows"

    def __init__(self, processes=None, free=4096.0) -> None:
        self._processes = processes
        self._free = free
        self.calls = 0

    def processes(self):
        self.calls += 1
        return self._processes

    def free_memory_mib(self):
        return self._free


EDITOR = r"C:\Program Files\Unity\Hub\Editor\6000.6.8f1\Editor\Unity.exe"


def editor(pid: int, arguments: str) -> unityguard.Proc:
    return unityguard.Proc(pid, 1, EDITOR, f'"{EDITOR}" {arguments}')


class HomeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="valtrion-dash-"))
        self.addCleanup(lambda: shutil.rmtree(self.home, ignore_errors=True))

    def add_job(self, name: str, state: str = "finished", status: str = "PASS", events: list[dict] | None = None) -> Path:
        directory = self.home / "artifacts" / "Game" / "abc12345" / name
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(json.dumps({
            "jobId": name, "kind": "pipeline", "state": state, "status": status, "reason": "all fine", "createdUtc": "2026-11-05T10:00:00.000Z",
            "updatedUtc": "2026-11-05T10:05:00.000Z", "host": "h", "pid": 999999,
            "watchdog": {"stallSec": 600, "timeoutSec": 7200, "lastProgressUtc": "2026-11-05T10:04:00.000Z"}}), encoding="utf-8")
        (directory / "events.jsonl").write_text("\n".join(json.dumps(event) for event in (events or [])), encoding="utf-8")
        (self.home / "jobs").mkdir(exist_ok=True)
        (self.home / "jobs" / f"{name}.json").write_text(json.dumps({"dir": str(directory)}), encoding="utf-8")
        return directory


class SnapshotTests(HomeCase):
    def test_an_empty_home_gives_an_empty_picture(self) -> None:
        snapshot = dashboard.Collector(self.home, FakeProbe([])).snapshot()
        self.assertEqual(dashboard.SCHEMA, snapshot["schema"])
        self.assertEqual([], snapshot["leases"])
        self.assertEqual([], snapshot["jobs"])
        self.assertEqual({"readable": True, "editors": []}, snapshot["unity"])

    def test_a_held_lease_is_listed_with_its_holder(self) -> None:
        manager = lease.LeaseManager(self.home)
        manager.acquire("unity-editor:slot1", "me@host", "unity-run-1", 300, "EditMode tests", os.getpid())
        entries = dashboard.leases(self.home, core.now())
        self.assertEqual(["unity-editor:slot1"], [entry["resource"] for entry in entries])
        self.assertEqual("held", entries[0]["state"])
        self.assertEqual("EditMode tests", entries[0]["purpose"])
        self.assertEqual("me@host", entries[0]["owner"])

    def test_steps_and_the_phase_come_from_the_events(self) -> None:
        events = [
            {"type": "step.started", "step": "1:build"}, {"type": "step.finished", "step": "1:build", "status": "PASS"},
            {"type": "step.started", "step": "2:run"}, {"type": "progress", "message": "phase: on-device", "data": {"phase": "on-device"}}]
        self.add_job("job-1", "running", "RUNNING", events)
        job = dashboard.job_list(self.home, core.now())[0]
        self.assertEqual([{"step": "1:build", "status": "PASS"}, {"step": "2:run", "status": "running"}], job["steps"])
        self.assertEqual("2:run", job["currentStep"])
        self.assertEqual("on-device", job["phase"])
        self.assertEqual("Game", job["project"])
        self.assertFalse(job["active"], "its runner process does not exist")

    def test_a_damaged_job_is_skipped_not_fatal(self) -> None:
        self.add_job("good")
        (self.home / "jobs" / "bad.json").write_text("{not json", encoding="utf-8")
        (self.home / "jobs" / "gone.json").write_text(json.dumps({"dir": str(self.home / "nowhere")}), encoding="utf-8")
        self.assertEqual(["good"], [job["id"] for job in dashboard.job_list(self.home, core.now())])

    def test_unity_editors_are_listed_and_workers_are_marked(self) -> None:
        probe = FakeProbe([
            editor(10, '-batchmode -projectPath "C:\\work\\Game" -runTests -testPlatform EditMode'),
            editor(11, '"-adb2" "-batchMode" "-name" "AssetImportWorkerHW0" "-projectPath" "C:/work/Game"'),
            unityguard.Proc(12, 1, r"C:\Program Files\Unity Hub\resources\unity.exe", "unity.exe serve")])
        result = dashboard.unity_editors(probe)
        self.assertEqual([10, 11], [item["pid"] for item in result["editors"]])
        first, worker = result["editors"]
        self.assertEqual(r"C:\work\Game", first["project"])
        self.assertTrue(first["tests"] and first["batch"] and not first["worker"])
        self.assertTrue(worker["worker"])

    def test_an_unreadable_process_list_is_said_so(self) -> None:
        self.assertEqual({"readable": False, "editors": []}, dashboard.unity_editors(FakeProbe(None)))

    def test_the_process_list_is_not_taken_at_every_snapshot(self) -> None:
        probe = FakeProbe([])
        ticks = iter(range(0, 1000))
        collector = dashboard.Collector(self.home, probe, process_ttl_sec=6.0, clock=lambda: float(next(ticks)))
        collector.snapshot()
        collector.snapshot()
        self.assertEqual(1, probe.calls)
        for _ in range(7):
            next(ticks)
        collector.snapshot()
        self.assertEqual(2, probe.calls)


class DeviceTests(HomeCase):
    def test_devices_show_who_uses_them(self) -> None:
        (self.home / "registry.json").write_text(json.dumps({"devices": [
            {"id": "ios:AAA", "platform": "ios", "model": "iPad (10th generation)", "os": "iPadOS 27.2", "evidence": "physical", "online": True},
            {"id": "ios:BBB", "platform": "ios", "class": "iphone", "evidence": "emulator", "online": False}, {"no": "id"}]}), encoding="utf-8")
        lease.LeaseManager(self.home).acquire("device:ios:AAA", "me@mac", "pipeline-1", 300, "sensoredge.record-playback", os.getpid())
        result = dashboard.devices(self.home, dashboard.leases(self.home, core.now()))
        self.assertEqual(["ios:AAA", "ios:BBB"], [device["id"] for device in result])
        self.assertTrue(result[0]["busy"])
        self.assertEqual("sensoredge.record-playback", result[0]["purpose"])
        self.assertFalse(result[1]["busy"])
        self.assertFalse(result[1]["online"])

    def test_no_registry_is_no_devices(self) -> None:
        self.assertEqual([], dashboard.devices(self.home, []))


class SlotTests(HomeCase):
    def test_every_slot_is_listed_free_or_held(self) -> None:
        manager = lease.LeaseManager(self.home)
        manager.acquire("unity-editor:slot2", "me@host", "unity-run-7", 300, "EditMode tests", os.getpid())
        manager.acquire("unity-project:abc", "me@host", "unity-run-7", 300, "EditMode tests", os.getpid(), worktree=r"C:\work\Game")
        result = dashboard.slots(self.home, dashboard.leases(self.home, core.now()))
        self.assertEqual(2, result["capacity"])
        self.assertEqual(["free", "held"], [slot["state"] for slot in result["unityEditor"]])
        self.assertEqual(r"C:\work\Game", result["unityEditor"][1]["project"])

    def test_the_host_setting_changes_how_many_slots_there_are(self) -> None:
        (self.home / "host.json").write_text(json.dumps({"unityEditorSlots": 3}), encoding="utf-8")
        self.assertEqual(3, len(dashboard.slots(self.home, [])["unityEditor"]))

    def test_a_device_without_the_flag_is_exclusive_like_the_registry_says(self) -> None:
        (self.home / "registry.json").write_text(json.dumps({"devices": [
            {"id": "ios:A", "model": "iPad", "exclusive": True}, {"id": "editor:mac", "model": "Unity Editor", "exclusive": False}]}), encoding="utf-8")
        flags = {device["id"]: device["exclusive"] for device in dashboard.devices(self.home, [])}
        self.assertEqual({"ios:A": True, "editor:mac": False}, flags)


class MemoryTests(unittest.TestCase):
    def test_windows_output_is_read(self) -> None:
        text = json.dumps({"total": 33554432, "free": 4194304, "procs": [
            {"id": 10, "name": "Unity", "ws": 4 * 2 ** 30}, {"id": 11, "name": "chrome", "ws": 2 ** 30}, {"id": 12, "name": "chrome", "ws": 2 ** 29}]})
        overview = dashboard.parse_windows_memory("banner\n" + text)
        self.assertEqual(32768, overview["totalMiB"])
        self.assertEqual(4096, overview["freeMiB"])
        self.assertEqual([4096, 1024, 512], [round(item["mib"]) for item in overview["processes"]])

    def test_ps_output_is_read_and_a_path_is_cut_to_the_program(self) -> None:
        text = "  1  2048 /sbin/launchd\n  50 1048576 /Applications/Unity/Hub/Editor/6000.6.8f1/Unity.app/Contents/MacOS/Unity\n"
        overview = dashboard.parse_ps_memory(text, 16384.0, 8000.0)
        self.assertEqual(["launchd", "Unity"], [item["name"] for item in overview["processes"]])
        self.assertEqual(1024, round(overview["processes"][1]["mib"]))

    def test_unreadable_memory_output_gives_nothing(self) -> None:
        self.assertIsNone(dashboard.parse_windows_memory("not json"))
        self.assertIsNone(dashboard.parse_windows_memory(None))
        self.assertIsNone(dashboard.parse_ps_memory("", 1.0, 1.0))
        self.assertIsNone(dashboard.parse_ps_memory("1 2 x", None, 1.0))

    def test_programs_are_grouped_and_unity_is_named_by_project(self) -> None:
        processes = [{"pid": 1, "name": "chrome.exe", "mib": 300.0}, {"pid": 2, "name": "chrome.exe", "mib": 200.0},
                     {"pid": 3, "name": "Unity", "mib": 4000.0}, {"pid": 4, "name": "Unity", "mib": 900.0}, {"pid": 5, "name": "tiny", "mib": 1.0}]
        groups = dashboard.memory_groups(processes, {3: "Unity · Game", 4: "Unity · Game"}, limit=2)
        self.assertEqual(["Unity · Game", "chrome"], [group["label"] for group in groups[:2]])
        self.assertEqual(4900, groups[0]["mib"])
        self.assertTrue(groups[0]["unity"])
        self.assertEqual(500, groups[1]["mib"])
        self.assertEqual(2, groups[1]["count"])
        self.assertTrue(groups[2]["other"], "what does not fit is summed up, not dropped")

    def test_the_machine_block_carries_the_breakdown_when_the_probe_has_one(self) -> None:
        class Probe(FakeProbe):
            def memory_overview(self):
                return {"totalMiB": 16000.0, "freeMiB": 4000.0, "processes": [{"pid": 9, "name": "Unity", "mib": 3000.0}]}

        editors = {"readable": True, "editors": [{"pid": 9, "project": "C:\\work\\Game", "worker": False}]}
        result = dashboard.machine(Path(tempfile.gettempdir()), Probe([]), editors)
        self.assertEqual(12000, result["memory"]["usedMiB"])
        self.assertEqual("Unity · Game", result["memory"]["groups"][0]["label"])

    def test_a_probe_that_cannot_say_leaves_the_block_empty(self) -> None:
        self.assertIsNone(dashboard.machine(Path(tempfile.gettempdir()), FakeProbe([]))["memory"])


class Entry:
    def __init__(self, host_id: str, os_name: str = "macos") -> None:
        self.id, self.os, self.kind = host_id, os_name, "ssh"


def answer(stdout: str = "", stderr: str = "", code: int | None = 0, timed_out: bool = False):
    from valtrion.transport import CommandResult

    return CommandResult(code, stdout, stderr, timed_out)


class RemoteTests(HomeCase):
    def snapshot_text(self) -> str:
        return "Last login: today\n" + json.dumps(dashboard.Collector(self.home, FakeProbe([])).snapshot())

    def test_a_host_that_answers_gives_its_snapshot_around_a_login_banner(self) -> None:
        result = dashboard.fetch_remote(Entry("mac"), call=lambda args: answer(self.snapshot_text()))
        self.assertTrue(result["ok"])
        self.assertEqual(dashboard.SCHEMA, result["snapshot"]["schema"])

    def test_the_command_asked_is_the_snapshot(self) -> None:
        asked = []
        dashboard.fetch_remote(Entry("mac"), call=lambda args: asked.append(list(args)) or answer(self.snapshot_text()))
        self.assertEqual([["dashboard", "--snapshot"]], asked)

    def test_an_old_valtrion_is_called_legacy_and_told_to_sync(self) -> None:
        result = dashboard.fetch_remote(Entry("mac"), call=lambda args: answer(stderr="valtrion: error: argument command: invalid choice: 'dashboard'", code=2))
        self.assertTrue(result["ok"])
        self.assertTrue(result["legacy"])
        self.assertIn("hosts sync", result["note"])

    def test_a_dead_connection_is_an_error_with_the_last_line(self) -> None:
        result = dashboard.fetch_remote(Entry("mac"), call=lambda args: answer(stderr="ssh: connect to host x port 22\nConnection refused", code=255))
        self.assertFalse(result["ok"])
        self.assertEqual("Connection refused", result["error"])

    def test_a_timeout_is_an_error(self) -> None:
        result = dashboard.fetch_remote(Entry("mac"), call=lambda args: answer(code=None, timed_out=True))
        self.assertFalse(result["ok"])
        self.assertIn("no answer", result["error"])

    def test_an_answer_that_is_not_a_snapshot_is_an_error(self) -> None:
        result = dashboard.fetch_remote(Entry("mac"), call=lambda args: answer('{"hello": 1}'))
        self.assertFalse(result["ok"])


class FleetTests(HomeCase):
    def fleet(self, fetch, hosts=None, clock=None, refresh_sec=8.0, background=False):
        return dashboard.Fleet(self.home, dashboard.Collector(self.home, FakeProbe([])), hosts or (lambda: [Entry("mac")]), fetch, refresh_sec,
                               clock or (lambda: 0.0), background)

    def test_this_machine_comes_first_and_the_other_hosts_follow(self) -> None:
        fleet = self.fleet(lambda entry: {"id": entry.id, "os": entry.os, "ok": True, "snapshot": {"schema": dashboard.SCHEMA}})
        document = fleet.snapshot()
        self.assertEqual(dashboard.SCHEMA, document["schema"])
        self.assertEqual(["mac"], [remote["id"] for remote in document["remotes"]])
        self.assertTrue(document["remotes"][0]["ok"])

    def test_a_host_is_asked_again_only_after_the_refresh_time(self) -> None:
        calls = []
        now = [0.0]
        fleet = self.fleet(lambda entry: calls.append(entry.id) or {"id": entry.id, "os": entry.os, "ok": True}, clock=lambda: now[0])
        fleet.snapshot()
        now[0] = 3.0
        fleet.snapshot()
        self.assertEqual(1, len(calls))
        now[0] = 9.0
        document = fleet.snapshot()
        self.assertEqual(2, len(calls))
        self.assertEqual(0.0, document["remotes"][0]["ageSec"])

    def test_a_host_that_fails_does_not_break_the_page(self) -> None:
        def broken(entry):
            raise RuntimeError("boom")

        document = self.fleet(broken).snapshot()
        self.assertFalse(document["remotes"][0]["ok"])
        self.assertIn("boom", document["remotes"][0]["error"])
        self.assertEqual(dashboard.SCHEMA, document["schema"])

    def test_an_unreadable_hosts_file_leaves_this_machine_intact(self) -> None:
        def unreadable():
            raise ValueError("hosts.json is damaged")

        document = self.fleet(lambda entry: {}, hosts=unreadable).snapshot()
        self.assertEqual([], document["remotes"])
        self.assertIn("damaged", document["remotesError"])

    def test_a_slow_host_shows_as_loading_and_the_page_does_not_wait(self) -> None:
        import threading

        release = threading.Event()
        fleet = self.fleet(lambda entry: release.wait(10) and {"id": entry.id, "os": entry.os, "ok": True}, background=True)
        try:
            self.assertTrue(fleet.snapshot()["remotes"][0]["loading"])
        finally:
            release.set()


class BackgroundTests(HomeCase):
    def test_an_old_process_list_is_shown_while_a_new_one_is_read(self) -> None:
        import threading
        import time

        release = threading.Event()

        class SlowProbe(FakeProbe):
            def processes(self):
                self.calls += 1
                if self.calls > 1:
                    release.wait(10)
                return []

        probe = SlowProbe([])
        ticks = iter(range(0, 1000, 10))
        collector = dashboard.Collector(self.home, probe, process_ttl_sec=5.0, clock=lambda: float(next(ticks)), background=True)
        collector.snapshot()  # nothing cached yet: this one waits
        started = time.monotonic()
        collector.snapshot()  # out of date: answers at once from the cache
        self.assertLess(time.monotonic() - started, 5.0)
        release.set()
        for _ in range(100):
            if probe.calls >= 2:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(probe.calls, 2)


class ServerTests(HomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.server, self.state = dashboardserver.start(self.home, port=47900, collector=dashboard.Collector(self.home, FakeProbe([])))
        self.stopped = False
        self.addCleanup(self.shutdown)
        self.port = self.state["port"]

    def shutdown(self) -> None:
        if not self.stopped:
            self.stopped = True
            dashboardserver.stop(self.server, self.home)

    def request(self, method: str, path: str, headers: dict | None = None, host: str | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        merged = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        connection.request(method, path, headers=merged)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_the_state_file_names_the_port_and_the_token(self) -> None:
        state = dashboardserver.read_state(self.home)
        self.assertEqual(self.port, state["port"])
        self.assertEqual(48, len(state["token"]))

    def test_nothing_is_served_without_the_token(self) -> None:
        for path in ("/", "/api/snapshot", "/favicon.png"):
            response, _ = self.request("GET", path)
            self.assertEqual(403, response.status, path)

    def test_the_link_with_the_token_sets_a_cookie_and_the_page_follows(self) -> None:
        response, _ = self.request("GET", f"/?t={self.state['token']}")
        self.assertEqual(303, response.status)
        self.assertEqual("/", response.getheader("Location"))
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        page, body = self.request("GET", "/", {"Cookie": cookie.split(";")[0]})
        self.assertEqual(200, page.status)
        self.assertIn(b"<title>Valtrion</title>", body)

    def test_a_wrong_token_is_refused(self) -> None:
        response, _ = self.request("GET", "/?t=" + "0" * 48)
        self.assertEqual(403, response.status)

    def test_a_foreign_host_name_is_refused_even_with_the_token(self) -> None:
        response, _ = self.request("GET", f"/?t={self.state['token']}", host="evil.example:80")
        self.assertEqual(403, response.status)

    def test_the_snapshot_is_json(self) -> None:
        response, body = self.request("GET", "/api/snapshot", {"X-Valtrion-Token": self.state["token"]})
        self.assertEqual(200, response.status)
        self.assertEqual(dashboard.SCHEMA, json.loads(body)["schema"])
        self.assertEqual("no-store", response.getheader("Cache-Control"))

    def test_it_is_read_only(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            response, _ = self.request(method, "/api/snapshot", {"X-Valtrion-Token": self.state["token"]})
            self.assertEqual(405, response.status, method)

    def test_health_needs_no_token_and_says_nothing_else(self) -> None:
        response, body = self.request("GET", "/api/health")
        self.assertEqual(200, response.status)
        self.assertEqual({"ok", "pid"}, set(json.loads(body)))

    def test_the_server_stops_and_removes_its_state(self) -> None:
        self.shutdown()
        self.assertIsNone(dashboardserver.read_state(self.home))
        with self.assertRaises(OSError):
            self.request("GET", "/api/health")


class PageTests(unittest.TestCase):
    def test_the_page_is_self_contained(self) -> None:
        html = dashboardpage.page()
        self.assertIn("<svg", html)
        self.assertNotRegex(html, r'(?:src|href|url\()\s*=?\s*["\']?https?://')
        self.assertNotIn("stylesheet", html)
        self.assertNotIn("innerHTML", html, "machine values must be put on the page as text")


if __name__ == "__main__":
    unittest.main()
