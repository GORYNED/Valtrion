"""The host-side prune and idle checks, run for real against a temporary valtrion home."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import hostsync


class PruneTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.root = Path(self._folder.name)
        self.script = self.root / "helper.py"
        self.script.write_text(hostsync.HOST_SCRIPT, encoding="utf-8")

    def helper(self, *args):
        return subprocess.run([sys.executable, str(self.script), *args], capture_output=True, text=True, check=False)

    def make_home(self, versions=("0.8.4", "0.8.5", "0.8.6", "0.8.7", "backup", "0.9.0rc1")):
        home = self.root / "valtrion"
        home.mkdir(exist_ok=True)
        for name in versions:
            (home / "core" / name / "bin").mkdir(parents=True)
        return home

    def prune(self, home, below="0.8.6", *protect):
        return self.helper("prune", str(home), below, *protect)

    def register_job(self, home, state, pid):
        run_dir = self.root / "run1"
        run_dir.mkdir(exist_ok=True)
        (run_dir / "job.json").write_text(json.dumps({"state": state, "pid": pid}), encoding="utf-8")
        (home / "jobs").mkdir(exist_ok=True)
        (home / "jobs" / "j1.json").write_text(json.dumps({"dir": str(run_dir)}), encoding="utf-8")

    def test_removes_only_plain_versions_below_the_caller(self):
        home = self.make_home()
        done = self.prune(home)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual({"0.8.4", "0.8.5"}, {line.split("=")[1] for line in done.stdout.split() if line.startswith("removed=")})
        self.assertEqual({"0.8.6", "0.8.7", "backup", "0.9.0rc1"}, {p.name for p in (home / "core").iterdir()},
                         "the caller's version, newer versions and folders that are not versions stay")

    def test_keeps_a_protected_command_and_the_target_of_the_stable_link(self):
        home = self.make_home()
        (home / "bin").mkdir()
        target = home / "core" / "0.8.5" / "bin" / "valtrion"
        if os.name == "nt":
            (home / "bin" / "valtrion.cmd").write_text('@echo off\r\n"%s" %%*\r\n' % target, encoding="utf-8")
        else:
            os.symlink(target, home / "bin" / "valtrion")
        done = self.prune(home, "0.8.6", str(home / "core" / "0.8.4" / "bin" / "valtrion"))
        self.assertNotIn("removed", done.stdout)
        self.assertTrue((home / "core" / "0.8.4").is_dir() and (home / "core" / "0.8.5").is_dir())

    @unittest.skipIf(os.name == "nt", "a symlink needs a privilege on Windows")
    def test_never_follows_or_removes_a_symlink(self):
        home = self.make_home(("0.8.4",))
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        os.symlink(outside, home / "core" / "0.8.3")
        self.prune(home)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue(os.path.islink(home / "core" / "0.8.3"))

    def test_skips_everything_while_a_job_runs_found_through_the_registry(self):
        home = self.make_home()
        self.register_job(home, "running", os.getpid())
        done = self.prune(home)
        self.assertIn("skipped=job j1 is running", done.stdout)
        self.assertTrue((home / "core" / "0.8.4").is_dir())

    def test_a_dead_runner_does_not_hold_the_prune_back(self):
        home = self.make_home(("0.8.4",))
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.register_job(home, "running", gone.pid)
        self.assertIn("removed=0.8.4", self.prune(home).stdout)

    def test_skips_everything_while_a_lease_holder_lives(self):
        home = self.make_home()
        (home / "leases").mkdir()
        (home / "leases" / "unity-editor.json").write_text(json.dumps({"resource": "unity-editor", "pid": os.getpid()}), encoding="utf-8")
        self.assertIn("skipped=lease unity-editor is held", self.prune(home).stdout)
        self.assertTrue((home / "core" / "0.8.4").is_dir())

    def test_keeps_a_version_a_process_has_on_its_command_line(self):
        home = self.make_home(("0.8.4", "0.8.5"))
        holder = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)", str(home / "core" / "0.8.4")])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        done = self.prune(home)
        self.assertIn("busy=0.8.4", done.stdout)
        self.assertIn("removed=0.8.5", done.stdout)

    def test_removes_nothing_when_processes_cannot_be_listed(self):
        home = self.make_home(("0.8.4",))
        broken = hostsync.HOST_SCRIPT.replace('["ps", "-axww", "-o", "args="]', '["ps-does-not-exist"]').replace(
            '["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"]', '["ps-does-not-exist"]')
        self.script.write_text(broken, encoding="utf-8")
        done = self.prune(home)
        self.assertNotEqual(0, done.returncode)
        self.assertTrue((home / "core" / "0.8.4").is_dir())

    def test_busy_reports_activity_through_the_registry(self):
        home = self.make_home(())
        folder = str(self.root / "venvs" / "valtrion")
        self.assertEqual("", self.helper("busy", str(home), folder).stdout)
        self.register_job(home, "running", os.getpid())
        self.assertIn("busy=job j1 is running", self.helper("busy", str(home), folder).stdout)

    def test_busy_fails_when_processes_cannot_be_listed(self):
        home = self.make_home(())
        venv = self.root / "venvs" / "valtrion"
        venv.mkdir(parents=True)
        broken = hostsync.HOST_SCRIPT.replace('["ps", "-axww", "-o", "args="]', '["ps-does-not-exist"]').replace(
            '["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"]', '["ps-does-not-exist"]')
        self.script.write_text(broken, encoding="utf-8")
        self.assertNotEqual(0, self.helper("busy", str(home), str(venv)).returncode)

    def test_a_failing_rmtree_is_reported_and_the_loop_goes_on(self):
        home = self.make_home(("0.8.3", "0.8.4"))
        namespace: dict = {}
        exec(compile(hostsync.HOST_SCRIPT.split("mode, rest = sys.argv")[0], "helper", "exec"), namespace)
        real = namespace["shutil"].rmtree

        def flaky(path, *args, **kwargs):
            if str(path).endswith("0.8.3"):
                raise PermissionError("locked")
            real(path, *args, **kwargs)

        out = io.StringIO()
        with mock.patch.object(namespace["shutil"], "rmtree", flaky), contextlib.redirect_stdout(out):
            namespace["prune"](str(home), "0.8.6", [])
        self.assertIn("failed=0.8.3", out.getvalue())
        self.assertIn("removed=0.8.4", out.getvalue())

    @unittest.skipIf(os.name == "nt", "a symlink needs a privilege on Windows")
    def test_relink_repoints_an_existing_link_and_creates_none(self):
        link = self.root / "bin" / "valtrion"
        self.assertEqual("", self.helper("relink", str(link), "/new").stdout)
        self.assertFalse(os.path.lexists(link))
        (self.root / "bin").mkdir()
        os.symlink("/old", link)
        self.helper("relink", str(link), "/new")
        self.assertEqual("/new", os.readlink(link))


if __name__ == "__main__":
    unittest.main()
