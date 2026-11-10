import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from valtrion import __version__, cli, config, core, hostsync, lease, remote, wheel
from valtrion.transport import CommandResult, Transport

HOME = "/home/dev"


def isolate_host_id(case):
    """The tests name remote hosts such as "mac"; the machine's own hostname must never collide with them."""
    patcher = mock.patch.object(core, "local_host_id", return_value="this-machine")
    patcher.start()
    case.addCleanup(patcher.stop)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class FakeHost(Transport):
    """A scripted SSH host: `installed` is the version its valtrion command reports (None: not installed)."""

    kind = "ssh"

    def __init__(self, installed=None, python_ok=True, down=False, windows=False, version_after=None, pythons=None, pipx=False,
                 prune_output="", busy=False, by_name=True, busy_fails=False, copy_fails=False):
        self.busy_fails, self.copy_fails = busy_fails, copy_fails
        self.installed, self.python_ok, self.down, self.windows = installed, python_ok, down, windows
        self.pipx, self.prune_output, self.busy, self.by_name = pipx, prune_output, busy, by_name
        self.version_after = version_after
        self.pythons = pythons  # {absolute path: runs 3.10+}; any other interpreter is not found
        self.calls, self.puts = [], []
        self.home = "C:\\Users\\dev" if windows else HOME

    def run(self, command, timeout_sec, cwd=None):
        command = list(command)
        self.calls.append(command)
        if self.down:
            return CommandResult(255, "", "ssh: connect failed")
        if command[-1] == "--version" and (command[0].endswith("pipx") or command[1:2] == ["-m"] and command[2:3] == ["pipx"]):
            return CommandResult(0, "1.7.1\n") if self.pipx else CommandResult(127, "", "not found")
        if command[-1] == "--version":
            if command[0] == "valtrion" and not self.by_name:
                return CommandResult(127, "", "valtrion: command not found")
            if self.installed is None:
                return CommandResult(127, "", "not found")
            return CommandResult(0, f"valtrion {self.installed} (contract 1)\n")
        if command[1:2] == ["-c"] and "expanduser" in command[2]:
            if self.pythons is not None:
                if command[0] not in self.pythons:
                    return CommandResult(127, "", "not found")
                return CommandResult(0, f"{self.home}\n{self.pythons[command[0]]}\n")
            return CommandResult(0, f"{self.home}\n{self.python_ok}\n")
        if command[1:3] == ["-m", "pip"] or ("install" in command and "--force" in command):
            self.installed = self.version_after or __version__
        if "environment" in command:
            return CommandResult(0, {"PIPX_LOCAL_VENVS": f"{self.home}/.local/pipx/venvs", "PIPX_BIN_DIR": f"{self.home}/.local/bin"}[command[-1]] + "\n")
        if len(command) > 2 and command[1].endswith("valtrion_host.py"):
            if command[2] == "busy":
                if self.busy_fails:
                    return CommandResult(1, "", "cannot list processes: boom")
                return CommandResult(0, "busy=job j1 is running\n" if self.busy else "")
            if command[2] == "prune":
                return CommandResult(0, self.prune_output)
            if command[2] == "path":
                return CommandResult(0, "added\n")
        return CommandResult(0, "")

    def put(self, local, remote_path, timeout_sec):
        if self.copy_fails and remote_path.endswith("valtrion_host.py"):
            return CommandResult(1, "", "disk full")
        self.puts.append((Path(local).name, remote_path))
        return CommandResult(0, "")

    def get(self, remote_path, local, timeout_sec):
        return CommandResult(0, "")


def entry(host_id="mac", os_name="macos", command=None, python=None):
    return config.HostEntry(host_id, "ssh", os_name, Path("/a"), ssh_target="me@mac", valtrion_command=command, python=python)


class SyncHostTests(unittest.TestCase):
    def setUp(self):
        self.wheel = Path("valtrion-0.7.0-py3-none-any.whl")
        self.recorded = []

    def sync(self, host, e):
        return hostsync.sync_host(e, host, self.wheel, lambda h, c, p: self.recorded.append((h, c, p)))

    def test_fresh_host_gets_a_venv_install_and_the_command_path_is_recorded(self):
        host = FakeHost()
        row = self.sync(host, entry())
        command = f"{HOME}/.valtrion/bin/valtrion"
        self.assertEqual((hostsync.UPDATED, command), (row.status, row.command))
        self.assertEqual([("mac", command, "/opt/homebrew/bin/python3")], self.recorded)
        self.assertEqual((self.wheel.name, f"{HOME}/.valtrion/incoming/{self.wheel.name}"), host.puts[0])
        self.assertTrue(any(c[1:3] == ["-m", "venv"] for c in host.calls))
        pip = [c for c in host.calls if c[1:3] == ["-m", "pip"]]
        self.assertEqual([f"{HOME}/.valtrion/core/{__version__}/bin/python"], [c[0] for c in pip], "pip runs from the new environment")
        self.assertIn("--no-deps", pip[0])
        order = ["expanduser", "makedirs", "venv", "pip"]
        calls = [c for c in host.calls if "pipx" not in " ".join(c) and "valtrion_host" not in " ".join(c)]
        steps = [next(n for n in order if n in " ".join(c)) for c in calls if any(n in " ".join(c) for n in order)]
        self.assertEqual(order, steps)

    def test_host_at_the_same_version_is_left_alone(self):
        host = FakeHost(installed=__version__)
        row = self.sync(host, entry(command="/opt/v/bin/valtrion"))
        self.assertEqual((hostsync.OK, "/opt/v/bin/valtrion"), (row.status, row.command))
        self.assertEqual([], host.puts)
        self.assertEqual([], self.recorded)

    def test_mismatched_version_is_reinstalled(self):
        host = FakeHost(installed="0.6.0")
        row = self.sync(host, entry(command="/old/valtrion"))
        self.assertEqual(hostsync.UPDATED, row.status)
        self.assertEqual(self.wheel.name, host.puts[0][0])

    def test_install_that_does_not_reach_the_version_is_blocked_and_not_recorded(self):
        row = self.sync(FakeHost(version_after="0.6.9"), entry())
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertIn("0.6.9", row.reason)
        self.assertEqual([], self.recorded)

    def test_old_python_is_blocked_with_the_action(self):
        row = self.sync(FakeHost(python_ok=False), entry())
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertIn("3.10", row.reason)

    def test_recorded_command_on_a_host_that_is_down_is_unreachable_without_installing(self):
        host = FakeHost(down=True)
        row = self.sync(host, entry(command="/opt/v/bin/valtrion"))
        self.assertEqual(hostsync.UNREACHABLE, row.status)
        self.assertEqual(1, len(host.calls))

    def test_unreachable_host_is_reported_not_raised(self):
        row = self.sync(FakeHost(down=True), entry())
        self.assertEqual(hostsync.UNREACHABLE, row.status)

    def test_windows_host_uses_scripts_folder_and_backslashes(self):
        host = FakeHost(windows=True)
        row = self.sync(host, entry("win", "windows"))
        self.assertEqual("C:\\Users\\dev\\.valtrion\\bin\\valtrion.cmd", row.command)
        self.assertEqual("python", host.calls[0][0])


class SyncCommandTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.home = Path(self._folder.name) / "home"
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        isolate_host_id(self)
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/artifacts", home=self.home)

    def test_hosts_sync_records_the_command_in_hosts_json(self):
        host = FakeHost()
        with mock.patch.object(remote, "make_transport", return_value=host):
            code, out, _ = run("hosts", "sync")
        self.assertEqual(0, code, out)
        recorded = json.loads((self.home / "hosts.json").read_text(encoding="utf-8"))["hosts"][0]["valtrionCommand"]
        self.assertEqual(f"{HOME}/.valtrion/bin/valtrion", recorded)
        self.assertEqual(recorded, config.find_host("mac", self.home).valtrion_command)

    def test_hosts_sync_exits_blocked_when_a_host_cannot_be_brought_to_the_version(self):
        with mock.patch.object(remote, "make_transport", return_value=FakeHost(down=True)):
            code, out, _ = run("hosts", "--json", "sync")
        self.assertEqual(2, code)
        self.assertEqual("unreachable", json.loads(out)[0]["status"])

    def test_a_host_that_does_not_answer_is_left_out_after_one_short_probe(self):
        host = FakeHost(down=True)
        with mock.patch.object(remote, "make_transport", return_value=host):
            code, out, err = run("hosts", "--json", "sync")
        self.assertEqual(2, code)
        self.assertEqual([["echo", "valtrion"]], host.calls, "nothing but the probe is tried on a host that does not answer")
        row = json.loads(out)[0]
        self.assertEqual("unreachable", row["status"])
        self.assertIn(f"within {hostsync.REACH_TIMEOUT_SEC} s", row["reason"])
        self.assertIn("mac: checking that the host answers", err)
        self.assertIn("mac: unreachable", err)

    def test_a_timed_out_probe_counts_as_not_answering(self):
        class Silent(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                self.calls.append(list(command))
                return CommandResult(-1, "", "", timed_out=True)

        host = Silent()
        with mock.patch.object(remote, "make_transport", return_value=host):
            code, out, _ = run("hosts", "--json", "sync")
        self.assertEqual(2, code)
        self.assertEqual("unreachable", json.loads(out)[0]["status"])
        self.assertEqual(1, len(host.calls))

    def test_progress_goes_to_stderr_and_the_table_stays_on_stdout(self):
        with mock.patch.object(remote, "make_transport", return_value=FakeHost()):
            code, out, err = run("hosts", "sync")
        self.assertEqual(0, code, out)
        self.assertIn("building the wheel", err)
        self.assertIn("mac: syncing...", err)
        self.assertIn("mac: updated", err)
        self.assertNotIn("syncing", out)
        self.assertIn("local core", out)

    def test_unknown_host_name_is_blocked(self):
        code, _, err = run("hosts", "sync", "--only", "nope")
        self.assertEqual(2, code)
        self.assertIn("nope", err)

    def test_doctor_host_without_a_recorded_command_is_blocked_with_the_action(self):
        code, _, err = run("doctor", "--host", "mac")
        self.assertEqual(2, code)
        self.assertIn("valtrion hosts sync", err)

    def doctor_with(self, version):
        config.set_host_command("mac", "/opt/v/bin/valtrion", home=self.home)
        report = {"host": {"id": "mac", "os": "macos"}, "core": {"version": version}, "capabilities": {
            "unityEditors": [], "buildPlatforms": [], "iosSigning": False, "adb": False}, "disk": {"freeGb": 1, "totalGb": 2, "path": "/"},
            "githubRunner": {"state": "none"}, "blocked": {}, "devices": []}

        class Reporting(FakeHost):
            def run(self, command, timeout_sec, cwd=None):
                self.calls.append(list(command))
                return CommandResult(0, json.dumps(report))

        host = Reporting()
        with mock.patch.object(remote, "make_transport", return_value=host):
            return run("doctor", "--host", "mac"), host

    def test_doctor_host_with_a_different_core_version_is_blocked(self):
        (code, _, err), host = self.doctor_with("0.6.0")
        self.assertEqual(2, code)
        self.assertIn("0.6.0", err)
        self.assertIn(__version__, err)
        self.assertEqual("/opt/v/bin/valtrion", host.calls[0][0], "the recorded command is used, not a PATH lookup")

    def test_doctor_host_with_the_same_core_version_passes(self):
        (code, out, _), _ = self.doctor_with(__version__)
        self.assertEqual(0, code)
        self.assertIn("host mac", out)


class LeaseDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.home = Path(self._folder.name) / "home"
        self.home.mkdir()

    def test_default_is_inside_the_home(self):
        self.assertEqual(self.home / "leases", lease.LeaseManager(self.home).directory)

    def test_leases_dir_setting_shares_the_old_directory(self):
        shared = Path(self._folder.name) / "old" / "leases"
        (self.home / "host.json").write_text(json.dumps({"leasesDir": str(shared)}), encoding="utf-8")
        manager = lease.LeaseManager(self.home)
        self.assertEqual(shared, manager.directory)
        manager.acquire("device:ipad", "me", "job-1", 60, "test", os.getpid())
        self.assertTrue(any(shared.iterdir()), "the lease file is written to the shared directory")

    def test_a_broken_host_json_is_an_error_not_a_silent_fallback(self):
        (self.home / "host.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            lease.LeaseManager(self.home)

    def test_relative_leases_dir_is_refused(self):
        (self.home / "host.json").write_text(json.dumps({"leasesDir": "relative/leases"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            lease.LeaseManager(self.home)


class WheelTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.base = Path(self._folder.name)
        self.built = wheel.build_wheel(self.base / "dist")

    def test_wheel_holds_the_package_schemas_compat_and_entry_point(self):
        with zipfile.ZipFile(self.built) as archive:
            names = set(archive.namelist())
            entry_points = archive.read(f"valtrion-{__version__}.dist-info/entry_points.txt").decode()
        self.assertEqual(f"valtrion-{__version__}-py3-none-any.whl", self.built.name)
        for expected in ("valtrion/cli.py", "valtrion/providers/unity/support.py", "valtrion/_schemas/plan.schema.json", "valtrion/_compat.json"):
            self.assertIn(expected, names)
        self.assertIn("valtrion = valtrion.cli:main", entry_points)

    def test_the_unpacked_wheel_runs_and_validates_a_fixture_with_its_own_schemas(self):
        target = self.base / "site"
        with zipfile.ZipFile(self.built) as archive:
            archive.extractall(target)
        fixture = Path(__file__).resolve().parent.parent.parent / "contracts" / "fixtures" / "valid" / "plan.json"
        for args, expected in ((["--version"], f"valtrion {__version__}"), (["validate", str(fixture)], "valid")):
            done = subprocess.run([sys.executable, "-I", "-c", remote.LAUNCHER, str(target), *args], capture_output=True, text=True, check=False)
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertIn(expected, done.stdout)

    def test_building_twice_gives_the_same_bytes(self):
        second = wheel.build_wheel(self.base / "dist2")
        self.assertEqual(self.built.read_bytes(), second.read_bytes())


class PythonSelectionTests(unittest.TestCase):
    BREW, LOCAL = "/opt/homebrew/bin/python3", "/usr/local/bin/python3"

    def setUp(self):
        self.recorded = []
        self.wheel = Path("valtrion-0.7.1-py3-none-any.whl")

    def sync(self, host, e):
        return hostsync.sync_host(e, host, self.wheel, lambda h, c, p: self.recorded.append((h, c, p)))

    def venv_python(self, host):
        return [c[0] for c in host.calls if c[1:3] == ["-m", "venv"]]

    def test_unset_python_probes_the_homebrew_location_before_the_bare_name(self):
        host = FakeHost(pythons={"python3": False, self.BREW: True})
        row = self.sync(host, entry())
        self.assertEqual(hostsync.UPDATED, row.status)
        self.assertEqual([self.BREW], self.venv_python(host))
        self.assertEqual([("mac", row.command, self.BREW)], self.recorded)
        self.assertEqual(self.BREW, host.calls[0][0], "the absolute locations come first")

    def test_an_old_candidate_is_skipped_for_the_next_one(self):
        host = FakeHost(pythons={self.BREW: False, self.LOCAL: True, "python3": False})
        row = self.sync(host, entry())
        self.assertEqual(hostsync.UPDATED, row.status)
        self.assertEqual([self.LOCAL], self.venv_python(host))
        self.assertEqual(self.LOCAL, self.recorded[0][2])

    def test_the_bare_name_is_the_last_resort(self):
        host = FakeHost(pythons={"python3": True})
        self.assertEqual(hostsync.UPDATED, self.sync(host, entry()).status)
        self.assertEqual(["python3"], self.venv_python(host))

    def test_a_recorded_python_is_the_only_one_used(self):
        host = FakeHost(pythons={"/srv/py/bin/python3": True, self.BREW: True})
        row = self.sync(host, entry(python="/srv/py/bin/python3"))
        self.assertEqual(hostsync.UPDATED, row.status)
        self.assertEqual(["/srv/py/bin/python3"], self.venv_python(host))
        self.assertEqual(["/srv/py/bin/python3"], [c[0] for c in host.calls if "expanduser" in " ".join(c)])

    def test_a_recorded_python_that_is_too_old_is_blocked_naming_it(self):
        row = self.sync(FakeHost(pythons={"/srv/py": False}), entry(python="/srv/py"))
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertIn("/srv/py", row.reason)
        self.assertEqual([], self.recorded)

    def test_no_candidate_at_all_is_blocked_with_the_action(self):
        row = self.sync(FakeHost(pythons={}), entry())
        self.assertEqual(hostsync.BLOCKED, row.status)
        self.assertIn("hosts add --python", row.reason)
        self.assertIn("tried", row.reason)

    def test_a_host_that_does_not_answer_stops_the_probe_at_once(self):
        host = FakeHost(down=True)
        self.assertEqual(hostsync.UNREACHABLE, self.sync(host, entry()).status)
        self.assertEqual(1, len(host.calls))


class PythonSettingTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.home = Path(self._folder.name) / "home"
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        isolate_host_id(self)

    def test_hosts_add_python_is_stored_validated_and_listed(self):
        code, _, err = run("hosts", "add", "--id", "mac", "--ssh", "me@mac", "--os", "macos", "--artifacts-root", "/a",
                           "--python", "/opt/homebrew/bin/python3")
        self.assertEqual(0, code, err)
        self.assertEqual("/opt/homebrew/bin/python3", config.find_host("mac", self.home).python)
        _, out, _ = run("hosts", "--json")
        self.assertIn("/opt/homebrew/bin/python3", out)

    def test_an_empty_python_is_rejected_by_the_schema(self):
        from valtrion.schema import validate_document
        document = {"schema": "gdv.hosts/1", "hosts": [{"id": "m", "transport": {"kind": "ssh", "sshTarget": "me@m"}, "os": "macos",
                                                        "artifactsRoot": "/a", "python": ""}]}
        self.assertNotEqual([], validate_document(document)[1])
        document["hosts"][0]["python"] = "/opt/homebrew/bin/python3"
        self.assertEqual([], validate_document(document)[1])

    def test_sync_records_the_python_it_found_next_to_the_command(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/a", home=self.home)
        host = FakeHost(pythons={"python3": False, "/opt/homebrew/bin/python3": True})
        with mock.patch.object(remote, "make_transport", return_value=host):
            code, out, _ = run("hosts", "sync")
        self.assertEqual(0, code, out)
        found = config.find_host("mac", self.home)
        self.assertEqual("/opt/homebrew/bin/python3", found.python)
        self.assertTrue(found.valtrion_command.endswith("/bin/valtrion"))


class HostListingTests(unittest.TestCase):
    def test_a_remote_posix_artifacts_root_is_listed_as_written(self):
        isolate_host_id(self)
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        home = Path(folder.name) / "home"
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/work/artifacts", home=home)
        with mock.patch.dict(os.environ, {core.HOME_ENV: str(home)}):
            _, out, _ = run("hosts", "--json")
        rows = {row["id"]: row for row in json.loads(out)}
        self.assertEqual("/Users/me/work/artifacts", rows["mac"]["artifactsRoot"])
