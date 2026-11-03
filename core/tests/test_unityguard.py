"""The pre-start checks of `unity run`: parsing what ps, PowerShell/CIM, vm_stat and /proc/meminfo print (fixture outputs, so every
platform's parser runs on every machine), recognising an Editor by its path, the ancestry rule, and the system probe with a fake runner."""

import json
import tempfile
import unittest
from pathlib import Path

from valtrion import config, unityguard
from valtrion.unityguard import Proc

PS = """  PID  PPID COMMAND
    1     0 /sbin/launchd
  501     1 /Applications/Unity Hub.app/Contents/MacOS/Unity Hub
 4242     1 /Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity -batchmode -projectPath /Users/me/My Game
 4300  4242 /Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity -adb2 -batchMode
 4400   900 /opt/unity/6000.6.7f1/Editor/Unity -batchmode
 4500   900 /usr/bin/python3 /tmp/Unity-notes.py
"""

CIM = json.dumps([
    {"ProcessId": 10, "ParentProcessId": 4, "ExecutablePath": None, "CommandLine": None},
    {"ProcessId": 700, "ParentProcessId": 650, "ExecutablePath": "C:\\Program Files\\Unity\\Hub\\Editor\\6000.6.7f1\\Editor\\Unity.exe",
     "CommandLine": "\"C:\\Program Files\\Unity\\Hub\\Editor\\6000.6.7f1\\Editor\\Unity.exe\" -batchmode"},
    {"ProcessId": 800, "ParentProcessId": 650, "ExecutablePath": "C:\\Program Files\\Unity Hub\\Unity Hub.exe", "CommandLine": "Unity Hub.exe"},
    {"ProcessId": 900, "ParentProcessId": 650, "ExecutablePath": "C:\\tools\\NotUnity.exe", "CommandLine": "NotUnity.exe"},
])

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            300000.
Pages inactive:                           20000.
Pages speculative:                         2000.
Pages wired down:                         90000.
"""

MEMINFO = "MemTotal:       16000000 kB\nMemFree:          500000 kB\nMemAvailable:    8192000 kB\nBuffers: 1 kB\nCached: 2 kB\n"


class ParserTests(unittest.TestCase):
    def test_ps_lines_keep_the_whole_command_with_spaces(self):
        procs = {p.pid: p for p in unityguard.parse_ps(PS)}
        self.assertEqual(6, len(procs))
        self.assertEqual((1, "/Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity -batchmode -projectPath /Users/me/My Game"),
                         (procs[4242].ppid, procs[4242].command))

    def test_cim_json_gives_paths_with_spaces_and_survives_null_fields(self):
        procs = {p.pid: p for p in unityguard.parse_cim_processes(CIM)}
        self.assertEqual("C:\\Program Files\\Unity\\Hub\\Editor\\6000.6.7f1\\Editor\\Unity.exe", procs[700].path)
        self.assertEqual(650, procs[700].ppid)
        self.assertEqual("", procs[10].path)

    def test_cim_json_of_a_single_process_a_banner_and_garbage(self):
        one = unityguard.parse_cim_processes('\ufeffWelcome\n{"ProcessId": 5, "ParentProcessId": 1, "ExecutablePath": "C:\\\\a\\\\Unity.exe"}')
        self.assertEqual([5], [p.pid for p in one])
        for text in ("", "no json here", "[1, 2, {\"x\": 1}]", "{broken"):
            self.assertEqual([], unityguard.parse_cim_processes(text), text)

    def test_vm_stat_counts_free_inactive_and_speculative_pages_in_mib(self):
        self.assertAlmostEqual((10000 + 20000 + 2000) * 16384 / 1048576, unityguard.parse_vm_stat(VM_STAT))
        self.assertIsNone(unityguard.parse_vm_stat("nonsense"))

    def test_meminfo_prefers_available_and_falls_back_to_free_buffers_cached(self):
        self.assertAlmostEqual(8000.0, unityguard.parse_meminfo(MEMINFO))
        self.assertAlmostEqual((500000 + 1 + 2) / 1024, unityguard.parse_meminfo("MemFree: 500000 kB\nBuffers: 1 kB\nCached: 2 kB\n"))
        self.assertIsNone(unityguard.parse_meminfo("x"))

    def test_cim_memory_is_kilobytes(self):
        self.assertAlmostEqual(2048.0, unityguard.parse_cim_memory("2097152\r\n"))
        self.assertIsNone(unityguard.parse_cim_memory(""))


class RecognitionTests(unittest.TestCase):
    def test_editors_are_found_by_their_executable_on_every_platform_and_the_hub_and_look_alikes_are_not(self):
        macos = {p.pid: unityguard.is_unity_editor(p, "macos") for p in unityguard.parse_ps(PS)}
        self.assertEqual({4242: True, 4300: True, 4400: True}, {pid: yes for pid, yes in macos.items() if yes})
        windows = {p.pid: unityguard.is_unity_editor(p, "windows") for p in unityguard.parse_cim_processes(CIM)}
        self.assertEqual([700], [pid for pid, yes in windows.items() if yes])

    def test_windows_paths_with_forward_slashes_quotes_and_case(self):
        for path in ("C:/Program Files/Unity/Hub/Editor/6000.6.7f1/Editor/Unity.exe", "d:\\unity\\editor\\UNITY.EXE", "//?/C:/Unity/Editor/Unity.exe"):
            self.assertTrue(unityguard.is_unity_editor(Proc(1, 0, path, path), "windows"), path)  # CIM names the executable
        quoted = '"C:\\Program Files\\Unity\\Editor\\Unity.exe" -batchmode'
        self.assertTrue(unityguard.is_unity_editor(Proc(1, 0, "", quoted), "windows"), quoted)  # no ExecutablePath: the quoted program
        for path in ("C:\\x\\MyUnity.exe", "C:\\x\\Unity.exe.bak", "C:\\Unity Hub\\Unity Hub.exe"):
            self.assertFalse(unityguard.is_unity_editor(Proc(1, 0, path, path), "windows"), path)

    def test_a_program_that_only_has_the_editor_among_its_arguments_is_not_an_editor(self):
        editor = "/Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity"
        for command in (
                f"/usr/bin/python3 -m valtrion unity run --unity {editor} -- -batchmode",  # a queued run
                f"python3 /opt/valtrion/__main__.py unity run --unity {editor}",
                f'sh -c "{editor} -batchmode"',
                f"/bin/sh /opt/run.sh {editor}",
                f"/usr/bin/tail -f {editor}",
                f"/opt/valtrion/bin/valtrion unity run --unity /opt/unity/6000/Editor/Unity -- -x"):
            self.assertFalse(unityguard.is_unity_editor(Proc(9, 1, command, command), "macos"), command)
            self.assertFalse(unityguard.is_unity_editor(Proc(9, 1, command, command), "linux"), command)

    def test_the_editor_itself_is_still_found_with_spaces_in_its_path_and_arguments_of_all_kinds(self):
        for command in ("/Applications/Unity Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity -batchmode -projectPath /x/My Game -quit",
                        "/opt/unity editors/6000/Editor/Unity -batchmode -executeMethod A.B --unity /x/y"):
            self.assertTrue(unityguard.is_unity_editor(Proc(9, 1, command, command), "macos"), command)

    def test_a_windows_process_without_a_path_is_judged_by_its_program_not_its_arguments(self):
        queued = Proc(9, 1, "", '"C:\\tools\\valtrion.exe" unity run --unity C:\\Unity\\Editor\\Unity.exe -- -batchmode')
        self.assertFalse(unityguard.is_unity_editor(queued, "windows"))
        direct = Proc(9, 1, "", '"C:\\Unity\\Editor\\Unity.exe" -batchmode')
        self.assertTrue(unityguard.is_unity_editor(direct, "windows"))

    def test_msys_style_paths_of_a_git_bash_session(self):
        path = "/c/Program Files/Unity/Hub/Editor/6000.6.7f1/Editor/Unity.exe"
        self.assertTrue(unityguard.is_unity_editor(Proc(1, 0, path, path), "windows"))


class AncestryTests(unittest.TestCase):
    def table(self):
        unity = "/Applications/Unity.app/Contents/MacOS/Unity"
        return [Proc(1, 0, "init", "init"), Proc(10, 1, "valtrion", "valtrion unity run"), Proc(11, 10, unity, unity + " -batchmode"),
                Proc(12, 11, unity, unity + " -adb2"), Proc(20, 1, unity, unity), Proc(21, 20, unity, unity + " -adb2")]

    def test_editors_below_a_lease_holder_are_inside_the_rest_are_orphans(self):
        found = unityguard.editors_outside_leases(self.table(), [10], "macos")
        self.assertEqual([20, 21], [p.pid for p in found])

    def test_without_holders_every_editor_is_outside(self):
        self.assertEqual([11, 12, 20, 21], [p.pid for p in unityguard.editors_outside_leases(self.table(), [], "macos")])

    def test_a_parent_loop_in_the_table_does_not_hang(self):
        unity = "/x/Unity.app/Contents/MacOS/Unity"
        loop = [Proc(1, 2, unity, unity), Proc(2, 1, unity, unity)]
        self.assertEqual([1, 2], [p.pid for p in unityguard.editors_outside_leases(loop, [], "macos")])


class SystemProbeTests(unittest.TestCase):
    def probe(self, os_name, outputs, meminfo=None):
        self.calls = []
        self.timeouts = []

        def runner(argv, timeout_sec=30.0):
            self.timeouts.append(timeout_sec)
            self.calls.append(list(argv))
            return outputs.get(argv[0] if os_name != "windows" else "powershell")

        return unityguard.SystemProbe(os_name, runner, meminfo or Path("/nonexistent/meminfo"))

    def test_posix_asks_ps_and_macos_asks_vm_stat(self):
        probe = self.probe("macos", {"ps": PS, "vm_stat": VM_STAT})
        self.assertEqual(6, len(probe.processes()))
        self.assertAlmostEqual(500.0, probe.free_memory_mib())
        self.assertEqual(["ps", "-axo", "pid=,ppid=,command="], self.calls[0])
        self.assertEqual(["vm_stat"], self.calls[1])

    def test_windows_asks_powershell_for_cim_processes_and_memory(self):
        probe = self.probe("windows", {"powershell": CIM})
        self.assertEqual(4, len(probe.processes()))
        command = self.calls[0]
        self.assertIn("-NoProfile", command)
        self.assertIn("Win32_Process", command[-1])
        self.assertIn("ConvertTo-Json", command[-1])
        probe = self.probe("windows", {"powershell": "4194304"})
        self.assertAlmostEqual(4096.0, probe.free_memory_mib())
        self.assertIn("FreePhysicalMemory", self.calls[0][-1])

    def test_linux_reads_proc_meminfo(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            meminfo = Path(folder) / "meminfo"
            meminfo.write_text(MEMINFO, encoding="utf-8")
            self.assertAlmostEqual(8000.0, self.probe("linux", {}, meminfo).free_memory_mib())
        self.assertIsNone(self.probe("linux", {}).free_memory_mib())

    def test_the_timeout_of_a_call_follows_the_probe_setting(self):
        probe = self.probe("macos", {"ps": PS, "vm_stat": VM_STAT})
        probe.timeout_sec = 7.0
        probe.processes()
        probe.free_memory_mib()
        self.assertEqual([7.0, 7.0], self.timeouts)

    def test_a_failing_tool_gives_none_not_an_exception(self):
        probe = self.probe("macos", {})
        self.assertIsNone(probe.processes())
        self.assertIsNone(probe.free_memory_mib())

    def test_run_text_returns_none_for_a_missing_program_and_a_failure(self):
        self.assertIsNone(unityguard.run_text(["/nonexistent/program-xyz"]))
        import sys

        self.assertIsNone(unityguard.run_text([sys.executable, "-c", "raise SystemExit(3)"]))
        self.assertEqual("hi", unityguard.run_text([sys.executable, "-c", "print('hi', end='')"]))


class CheckTests(unittest.TestCase):
    class Fixed(unityguard.Probe):
        os_name = "macos"

        def __init__(self, processes, memory):
            self._p, self._m = processes, memory

        def processes(self):
            return self._p

        def free_memory_mib(self):
            return self._m

    def test_both_problems_are_reported_together(self):
        unity = "/x/Unity.app/Contents/MacOS/Unity"
        probe = self.Fixed([Proc(5, 1, unity, unity)], 100.0)
        reason, notes = unityguard.check(probe, unityguard.GuardSettings(), [])
        self.assertIn("pid 5", reason)
        self.assertIn("only 100 MiB", reason)
        self.assertEqual([], notes)

    def test_nothing_wrong_is_none(self):
        self.assertEqual((None, []), unityguard.check(self.Fixed([], 99999.0), unityguard.GuardSettings(), []))

    def test_many_orphans_are_summarised(self):
        unity = "/x/Unity.app/Contents/MacOS/Unity"
        probe = self.Fixed([Proc(i, 1, unity, unity) for i in range(100, 108)], 99999.0)
        reason, _ = unityguard.check(probe, unityguard.GuardSettings(), [])
        self.assertIn("and 3 more", reason)


class BlockedReasonTests(unittest.TestCase):
    def test_a_setting_that_cannot_be_read_does_not_block_a_build_that_never_asked_for_the_check(self):
        from valtrion import lease

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            home = Path(folder)
            manager = lease.LeaseManager(home)  # made first: the lease directory itself needs a readable host.json
            notes = []
            for text in ("{broken", json.dumps({"unityGuard": {"minFreeMemoryMiB": -1}}), json.dumps({"unityGuard": 5})):
                (home / "host.json").write_text(text, encoding="utf-8")
                self.assertIsNone(unityguard.blocked_reason(manager, home, 0, notes.append), text)
            self.assertEqual(3, len(notes))
            self.assertTrue(all("skipped" in note for note in notes))


class SettingsTests(unittest.TestCase):
    def load(self, document):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            home = Path(folder)
            if document is not None:
                (home / "host.json").write_text(json.dumps(document), encoding="utf-8")
            return unityguard.load_settings(home)

    def test_without_the_setting_the_checks_are_on_for_unity_run_and_off_for_the_build_stage_and_the_provider(self):
        for document in (None, {"other": 1}):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                home = Path(folder)
                if document is not None:
                    (home / "host.json").write_text(json.dumps(document), encoding="utf-8")
                self.assertEqual(unityguard.GuardSettings(True, 2048.0), unityguard.load_settings(home))
                self.assertEqual(unityguard.OFF, unityguard.load_settings(home, default_on=False))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:  # an empty object is the opt-in: the defaults
            (Path(folder) / "host.json").write_text(json.dumps({"unityGuard": {}}), encoding="utf-8")
            self.assertEqual(unityguard.GuardSettings(True, 2048.0), unityguard.load_settings(Path(folder), default_on=False))

    def test_defaults_and_overrides(self):
        self.assertEqual(unityguard.GuardSettings(True, 2048.0), self.load(None))
        self.assertEqual(unityguard.GuardSettings(True, 2048.0), self.load({"other": 1}))
        self.assertEqual(unityguard.GuardSettings(False, 512.0), self.load({"unityGuard": {"orphanEditors": False, "minFreeMemoryMiB": 512}}))

    def test_bad_values_are_config_errors(self):
        for bad in ("x", {"orphanEditors": 1}, {"minFreeMemoryMiB": -5}, {"minFreeMemoryMiB": "8"}, {"minFreeMemoryMiB": float("inf")}, {"nope": True}):
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                self.load({"unityGuard": bad})


if __name__ == "__main__":
    unittest.main()
