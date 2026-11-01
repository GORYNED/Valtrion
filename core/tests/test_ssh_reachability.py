"""Reachability verdicts over SSH: transient failures are retried within a bound, definitive ones are not hidden, and a host that
answered is never reported as unreachable. All with injected fake runners and transports; no real ssh."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import doctor, hostsync, remote, resolver
from valtrion import transport as tr
from valtrion.config import HostEntry
from valtrion.providers import android
from valtrion.resolver import HostCapabilities, HostInfo, HostUnavailable
from valtrion.transport import CommandResult, Transport

from .shortdir import short_temp_dir

REPORT = {"host": {"id": "mac", "os": "macos"}, "core": {"version": "0.0.0"},
          "capabilities": {"unityEditors": [], "buildPlatforms": ["editor"], "iosSigning": False, "adb": False}, "devices": []}


class Scripted(Transport):
    """Answers each call with the next scripted result (the last one repeats) and records the calls."""

    kind = "ssh"

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0
        self.resets = 0

    def run(self, command, timeout_sec, cwd=None):
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def put(self, *_):
        return CommandResult(0)

    def get(self, *_):
        return CommandResult(0)

    def reset_connection(self):
        self.resets += 1


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ClassifyTests(unittest.TestCase):
    def test_the_four_readings(self):
        cases = [
            (CommandResult(0, "ok"), tr.REACHABLE),
            (CommandResult(2, "", "BLOCKED: no project"), tr.REACHABLE),  # the command ran and failed: the host is there
            (CommandResult(127, "", "bash: valtrion: command not found"), tr.TOOL_MISSING),
            (CommandResult(1, "", "'valtrion' is not recognized as an internal or external command"), tr.TOOL_MISSING),
            (CommandResult(255, "", "kex_exchange_identification: read: Connection reset by peer"), tr.TRANSIENT),
            (CommandResult(255, "", "ssh: connect to host x port 22: Connection timed out"), tr.TRANSIENT),
            (CommandResult(None, "", "", True), tr.TRANSIENT),
            (CommandResult(255, "", "me@x: Permission denied (publickey)."), tr.UNREACHABLE),
            (CommandResult(255, "", "Host key verification failed."), tr.UNREACHABLE),
        ]
        for result, state in cases:
            self.assertEqual(state, tr.classify(result).state, result)

    def test_a_transient_failure_that_persists_becomes_unreachable_with_the_evidence(self):
        verdict = tr.final_verdict(CommandResult(255, "", "ssh: connect to host x port 22: No route to host"), attempts=5)
        self.assertEqual(tr.UNREACHABLE, verdict.state)
        self.assertIn("No route to host", verdict.detail)
        self.assertIn("5 attempts", verdict.detail)

    def test_a_timeout_is_a_host_that_did_not_answer_not_an_ssh_error(self):
        verdict = tr.final_verdict(CommandResult(None, "", "", True), attempts=2)
        self.assertEqual(tr.UNREACHABLE, verdict.state)
        self.assertIn("did not answer", verdict.detail)


class RetryTests(unittest.TestCase):
    def retrying(self, inner, **options):
        clock = Clock()
        pauses = []

        def sleep(seconds):
            pauses.append(seconds)
            clock.sleep(seconds)

        return remote.RetryingTransport(inner, sleep=sleep, clock=clock, **options), pauses, clock

    def test_four_connection_resets_in_a_row_are_survived(self):
        # sshd throttling (MaxStartups) or a network flap can refuse several connections in a row before one gets through.
        reset = CommandResult(255, "", "kex_exchange_identification: Connection closed by remote host")
        inner = Scripted(reset, reset, reset, reset, CommandResult(0, "ok"))
        transport, pauses, _ = self.retrying(inner)
        result = transport.run(["x"], 5)
        self.assertEqual("ok", result.stdout)
        self.assertEqual([1.0, 2.0, 4.0, 8.0], pauses)
        self.assertEqual(5, transport.last_attempts)

    def test_an_authentication_refusal_is_not_retried(self):
        inner = Scripted(CommandResult(255, "", "me@x: Permission denied (publickey)."))
        transport, pauses, _ = self.retrying(inner)
        result = transport.run(["x"], 5)
        self.assertEqual(255, result.returncode)
        self.assertEqual(1, inner.calls)
        self.assertEqual([], pauses)

    def test_a_real_outage_is_still_reported_after_a_bounded_number_of_tries(self):
        inner = Scripted(CommandResult(255, "", "ssh: connect to host x port 22: No route to host"))
        transport, pauses, _ = self.retrying(inner)
        result = transport.run(["x"], 5)
        self.assertEqual(255, result.returncode)
        self.assertEqual(remote.RETRY_ATTEMPTS, inner.calls)
        self.assertEqual(remote.RETRY_ATTEMPTS - 1, len(pauses))

    def test_the_time_budget_stops_retrying_even_when_attempts_remain(self):
        class Slow(Scripted):
            def run(self, command, timeout_sec, cwd=None):
                clock.now += 100  # each timed-out call used its whole deadline
                return super().run(command, timeout_sec, cwd)

        inner = Slow(CommandResult(None, "", "", True))
        transport, pauses, clock = self.retrying(inner, budget_sec=240)
        result = transport.run(["x"], 90)
        self.assertTrue(result.timed_out)
        # Calls end at 100, 201 and 304 s; before a 4th try 304 + 4 s pause is past the 240 s budget.
        self.assertEqual(3, inner.calls)
        self.assertEqual([1.0, 2.0], pauses)
        self.assertLessEqual(clock.now, 240 + 100, "bound: the budget plus one call")

    def test_a_refused_login_in_a_file_copy_is_not_retried_whatever_the_exit_code(self):
        class Copy(Scripted):
            def put(self, *_):
                self.calls += 1
                return CommandResult(1, "", "me@x: Permission denied (publickey).")

        copy = Copy(CommandResult(0))
        transport, pauses, _ = self.retrying(copy)
        self.assertEqual(1, transport.put(Path("a"), "b", 5).returncode)
        self.assertEqual(1, copy.calls)
        self.assertEqual([], pauses)

    def test_only_real_control_socket_errors_reset_connection_sharing(self):
        for message, expected in (("mux_client_request_session: read from master failed", True),
                                  ("Control socket connect(/x/cm-1): Connection refused", True),
                                  ("packet_write_wait: Broken pipe", False),
                                  ("ssh: stale file handle on the remote side", False)):
            self.assertEqual(expected, tr.is_connection_state_failure(CommandResult(255, "", message)), message)

    def test_a_remote_command_failure_is_never_retried(self):
        inner = Scripted(CommandResult(1, "", "boom"))
        transport, _, _ = self.retrying(inner)
        self.assertEqual(1, transport.run(["x"], 5).returncode)
        self.assertEqual(1, inner.calls)

    def test_a_broken_control_socket_resets_connection_sharing_before_the_retry(self):
        inner = Scripted(CommandResult(255, "", "mux_client_request_session: read from master failed: Broken pipe"), CommandResult(0, "ok"))
        transport, _, _ = self.retrying(inner)
        self.assertEqual("ok", transport.run(["x"], 5).stdout)
        self.assertEqual(1, inner.resets)

    def test_ssh_transport_stops_using_the_control_socket_after_a_reset(self):
        with short_temp_dir() as folder:
            ssh = tr.SshTransport("me@host", control_dir=Path(folder), multiplex=True)
            self.assertIn("ControlMaster=auto", ssh._options())  # noqa: SLF001
            ssh.reset_connection()
            options = ssh._options()  # noqa: SLF001
            self.assertNotIn("ControlMaster=auto", options)
            self.assertIn("ControlPath=none", options)


class NoiseTests(unittest.TestCase):
    BANNER = "Welcome to host\nLast login: Mon Jan 1 {not json}\n"

    def test_json_after_a_login_banner_and_before_trailing_output_is_read(self):
        text = self.BANNER + json.dumps(REPORT) + "\nlogout noise\n"
        self.assertEqual(REPORT, remote.extract_json_object(text))

    def test_no_json_is_none(self):
        self.assertIsNone(remote.extract_json_object("just text { and more"))
        self.assertIsNone(remote.extract_json_object("[1, 2]"))

    def test_call_json_survives_banner_noise(self):
        target = remote.Remote(Scripted(CommandResult(0, self.BANNER + json.dumps(REPORT))), ("v",))
        self.assertEqual(REPORT, target.call_json(["doctor", "--json"])[1])

    def test_version_after_a_banner_is_found(self):
        self.assertEqual("1.2.3", hostsync.parse_version_output("Welcome\nvaltrion 1.2.3 (contract 1)\nbye\n"))

    def test_a_banner_line_that_looks_like_a_version_is_not_the_version(self):
        text = "valtrion 0.0.1 is scheduled for maintenance\nvaltrion 1.2.3 (contract 1)\n"
        self.assertEqual("1.2.3", hostsync.parse_version_output(text))
        self.assertIsNone(hostsync.parse_version_output("valtrion 0.0.1 notice\n"))

    def test_tool_missing_is_read_for_cmd_exe_too(self):
        self.assertTrue(tr.is_tool_missing(CommandResult(1, "", "'valtrion' is not recognized as an internal or external command")))
        self.assertTrue(tr.is_tool_missing(CommandResult(127, "", "")))
        self.assertFalse(tr.is_tool_missing(CommandResult(1, "", "boom")))
        self.assertFalse(tr.is_tool_missing(CommandResult(0, "", "command not found in a log line")))

    def test_python_probe_ignores_banner_lines_and_keeps_the_real_home(self):
        out = "Welcome back\nvaltrion-home=/home/dev\nvaltrion-py310=True\nprofile says hi\n"
        self.assertEqual(("/home/dev", True), hostsync.parse_python_probe(out))
        self.assertEqual(("/home/dev", True), hostsync.parse_python_probe("motd line\n/home/dev\nTrue\n"))
        self.assertIsNone(hostsync.parse_python_probe("only a banner\n"))


def entry():
    return HostEntry("mac", "ssh", "macos", Path("/a"), ssh_target="me@mac")


class ProbeTests(unittest.TestCase):
    def probe_with(self, transport, home, clock=lambda: 1000.0):
        target = remote.Remote(transport, ("v",))
        probe = remote.make_probe(lambda: REPORT, lambda e: target, {"mac": entry()}, home=home, clock=clock)
        return probe(HostInfo("mac", "ssh", "macos", "me@mac"))

    def test_banner_noise_does_not_make_the_host_unreachable(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.probe_with(Scripted(CommandResult(0, "Last login: now\n" + json.dumps(REPORT))), Path(home))
        self.assertIsInstance(result, HostCapabilities)

    def test_a_missing_command_is_reported_as_missing_not_unreachable(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.probe_with(Scripted(CommandResult(127, "", "bash: v: command not found")), Path(home))
        self.assertIsInstance(result, HostUnavailable)
        self.assertIn("reachable, but the valtrion command is missing", result.reason)
        self.assertIn("hosts sync", result.reason)
        self.assertNotIn("unreachable (", result.reason)

    def test_a_doctor_that_ran_and_failed_is_reachable(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.probe_with(Scripted(CommandResult(1, "", "Traceback\nValueError: boom")), Path(home))
        self.assertIsInstance(result, HostUnavailable)
        self.assertIn("reachable, but", result.reason)
        self.assertIn("ValueError: boom", result.reason)

    def test_a_genuine_outage_is_unreachable_with_evidence_and_attempts(self):
        retrying = remote.RetryingTransport(Scripted(CommandResult(255, "", "ssh: connect to host mac port 22: No route to host")),
                                            sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as home:
            result = self.probe_with(retrying, Path(home))
        self.assertIsInstance(result, HostUnavailable)
        self.assertTrue(result.reason.startswith("unreachable ("), result.reason)
        self.assertIn("No route to host", result.reason)
        self.assertIn(f"{remote.RETRY_ATTEMPTS} attempts", result.reason)

    def test_a_failure_is_not_cached_so_the_next_probe_asks_again(self):
        flaky = Scripted(CommandResult(255, "", "Permission denied (publickey)."), CommandResult(0, json.dumps(REPORT)))
        with tempfile.TemporaryDirectory() as home:
            first = self.probe_with(flaky, Path(home))
            second = self.probe_with(flaky, Path(home))
        self.assertIsInstance(first, HostUnavailable)
        self.assertIsInstance(second, HostCapabilities)

    def test_the_resolver_reports_the_reason_of_a_host_that_is_unavailable(self):
        local = HostInfo("local", "local", "windows")
        mac = HostInfo("mac", "ssh", "macos", "me@mac")
        probes = {"local": HostCapabilities(), "mac": HostUnavailable("reachable, but the valtrion command is missing on the host (x)")}
        result = resolver.HostResolver(local, [mac], lambda host: probes[host.id]).resolve(resolver.Request("ios", True))
        self.assertIn("mac: reachable, but the valtrion command is missing", result.reasons[1])


class HostSyncTests(unittest.TestCase):
    def test_the_evidence_of_an_outage_is_in_the_row(self):
        host = Scripted(CommandResult(255, "", "ssh: connect to host mac port 22: Connection refused"))
        row = hostsync.sync_host(entry(), host, Path("w.whl"), lambda *_: None)
        self.assertEqual(hostsync.UNREACHABLE, row.status)
        self.assertIn("Connection refused", row.reason)

    def test_a_banner_before_the_python_probe_does_not_corrupt_the_home_folder(self):
        class Banner(Scripted):
            def run(self, command, timeout_sec, cwd=None):
                self.calls += 1
                if command[-1] == "--version":
                    return CommandResult(127, "", "not found")
                if command[1:2] == ["-c"] and "expanduser" in command[2]:
                    return CommandResult(0, "*** authorized use only ***\nvaltrion-home=/home/dev\nvaltrion-py310=True\n")
                return CommandResult(0, "")

        recorded = []
        row = hostsync.sync_host(entry(), Banner(CommandResult(0)), Path("w.whl"), lambda *args: recorded.append(args),
                                 version="9.9.9")
        # The install steps run against /home/dev; the version check afterwards finds no version, which is a BLOCKED row about
        # the version, not about a banner line used as a folder.
        self.assertIn("/home/dev/.valtrion/core/9.9.9/bin/valtrion", row.command)
        self.assertNotIn("authorized", row.command)


class DoctorToolSearchTests(unittest.TestCase):
    def test_a_tool_outside_the_bare_ssh_path_is_found_in_the_usual_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / ("adb.exe" if os.name == "nt" else "adb")
            tool.write_text("", encoding="utf-8")
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": ""}):
                found = doctor.which_tool("adb", "macos", [folder])
                facts, blocked = doctor.probe_adb([], "macos", lambda argv: (0, "Android Debug Bridge version 1.0.41"), [folder])
                # The Android adapter resolves adb the same way, so doctor and the adapter cannot disagree about a capable host.
                adapter_adb = android.find_adb({}, extra_dirs=[folder])
            expected = os.path.normcase(str(tool))
            self.assertEqual(expected, os.path.normcase(found))
            self.assertEqual({}, blocked)
            self.assertEqual(expected, os.path.normcase(facts["adb"]))
            self.assertEqual(expected, os.path.normcase(adapter_adb[0]))


if __name__ == "__main__":
    unittest.main()
