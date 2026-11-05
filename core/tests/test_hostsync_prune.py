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

    def test_keeps_a_busy_version_under_a_non_ascii_folder_in_the_c_locale(self):
        # Over SSH and under launchd the locale is often C: macOS ps then printed the path vis-encoded (`M-PM^_...`), so the folder of
        # a running process matched nothing and was removed (#81).
        self.root = self.root / "Пользователь"
        self.root.mkdir()
        home = self.make_home(("0.8.4", "0.8.5"))
        holder = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)", str(home / "core" / "0.8.4")])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LC_")}
        environment.update(LC_ALL="C", LANG="C")
        done = subprocess.run([sys.executable, str(self.script), "prune", str(home), "0.8.6"], capture_output=True, text=True,
                              encoding="utf-8", env=environment, check=False)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("busy=0.8.4", done.stdout)
        self.assertIn("removed=0.8.5", done.stdout)
        self.assertTrue((home / "core" / "0.8.4").is_dir())

    def broken_listing(self):
        broken = hostsync.HOST_SCRIPT.replace('["ps", "-axww"', '["ps-does-not-exist", "-axww"').replace(
            'WINDOWS_LISTING = ["powershell", ', 'WINDOWS_LISTING = ["ps-does-not-exist", ')
        self.assertNotEqual(hostsync.HOST_SCRIPT, broken)
        self.script.write_text(broken, encoding="utf-8")

    def test_removes_nothing_when_processes_cannot_be_listed(self):
        home = self.make_home(("0.8.4",))
        self.broken_listing()
        done = self.prune(home)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("unreadable=cannot list processes", done.stdout)
        self.assertNotIn("removed", done.stdout)
        self.assertTrue((home / "core" / "0.8.4").is_dir())

    def test_busy_does_not_count_the_helper_itself(self):
        # The helper names the folder on its own command line; that alone must not make the pipx environment look busy.
        home = self.make_home(())
        venv = self.root / "venvs" / "valtrion"
        venv.mkdir(parents=True)
        done = self.helper("busy", str(home), str(venv))
        self.assertEqual((0, ""), (done.returncode, done.stdout), done.stderr)
        holder = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)", str(venv)])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertIn("busy=a process uses", self.helper("busy", str(home), str(venv)).stdout)

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
        self.broken_listing()
        done = self.helper("busy", str(home), str(venv))
        self.assertNotEqual(0, done.returncode)
        self.assertIn("cannot list processes", done.stderr)

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


CYRILLIC = "/Users/me/Пользователь/.valtrion"
#: `CYRILLIC` as macOS ps prints it in the C locale (vis without backslash).
CYRILLIC_VIS = ("/Users/me/M-PM^_M-PM->M-PM-;M-QM^LM-PM-7M-PM->M-PM-2M-PM-0M-QM^BM-PM-5M-PM-;M-QM^L/.valtrion")


class ListingTests(unittest.TestCase):
    """How the helper reads the process list, against scripted `ps` output."""

    def setUp(self):
        self.ns: dict = {}
        exec(compile(hostsync.HOST_SCRIPT.split("mode, rest = sys.argv")[0], "helper", "exec"), self.ns)
        self.pid = os.getpid()
        self.argv = ["/h/.valtrion/incoming/valtrion_host.py", "prune", CYRILLIC, "0.8.6"]

    def listing(self, stdout, returncode=0, stderr=b"", argv=None):
        done = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with mock.patch.object(self.ns["subprocess"], "run", return_value=done) as run, \
                mock.patch.object(sys, "argv", argv or self.argv):
            table = self.ns["processes"]()
        return table, run

    def own(self, arguments):
        return f"{self.pid} 1 /opt/homebrew/bin/python3 {' '.join(self.argv)}".replace(CYRILLIC, arguments).encode()

    def unreadable(self, stdout, returncode=0, argv=None):
        with self.assertRaises(self.ns["Unreadable"]) as caught:
            self.listing(stdout, returncode, b"ps: boom", argv)
        return str(caught.exception)

    def test_vis_and_escapes_are_decoded(self):
        self.assertIn(self.ns["fold"]("x " + CYRILLIC), self.ns["decoded_forms"]("x " + CYRILLIC_VIS))
        hexed = "/Users/me/\\xd0\\x9f\\xd0\\xbe/x"
        self.assertIn(self.ns["fold"]("/Users/me/По/x"), self.ns["decoded_forms"](hexed))
        self.assertIn(self.ns["fold"]("/Users/me/По/x"), self.ns["decoded_forms"]("/Users/me/\\320\\237\\320\\276/x"))
        self.assertIn(self.ns["fold"](CYRILLIC_VIS), self.ns["encoded_forms"](CYRILLIC), "the folder is also searched for as ps encodes it")

    def test_a_vis_lookalike_that_is_not_utf8_keeps_only_the_text_as_printed(self):
        forms = self.ns["decoded_forms"]("emacs M-x")
        self.assertEqual([self.ns["fold"]("emacs M-x")], forms)

    def test_the_listing_runs_in_a_utf8_locale(self):
        _, run = self.listing(self.own(CYRILLIC))
        if os.name == "nt":
            self.assertEqual(0x08000000, run.call_args.kwargs["creationflags"])
            self.assertIn("UTF8", run.call_args.args[0][-1])
        else:
            self.assertIn(run.call_args.kwargs["env"]["LC_ALL"], ("en_US.UTF-8", "C.UTF-8"))
            self.assertEqual(["ps", "-axww", "-o", "pid=,ppid=,args="], run.call_args.args[0])

    def test_a_vis_encoded_listing_is_read_and_a_vis_encoded_user_is_seen(self):
        folder = CYRILLIC + "/core/0.8.4"
        holder = f"4242 1 /usr/bin/python3 -c sleep {CYRILLIC_VIS}/core/0.8.4".encode()
        table, _ = self.listing(self.own(CYRILLIC_VIS) + b"\n" + holder + b"\n")
        with mock.patch.object(sys, "argv", self.argv):
            self.assertTrue(self.ns["in_use"](table, folder))
            self.assertFalse(self.ns["in_use"](table, CYRILLIC + "/core/0.8.5"))

    def test_an_unreadable_listing_is_refused(self):
        self.assertIn("cannot list processes: ps: boom", self.unreadable(b"", returncode=1))
        self.assertIn("not valid UTF-8", self.unreadable(self.own(CYRILLIC) + b"\n4242 1 /bin/\xff\xfe\n"))
        self.assertIn("empty", self.unreadable(b"\n"))
        self.assertIn("does not show this helper", self.unreadable(b"4242 1 /usr/bin/python3 other\n"))
        # procps in a locale it does not know prints `?` for every non-ASCII byte: nothing of that can be matched.
        mangled = self.own("/Users/me/" + "?" * 24 + "/.valtrion")
        self.assertIn("own argument", self.unreadable(mangled + b"\n"))
        with mock.patch.object(self.ns["subprocess"], "run", side_effect=OSError("no ps")), self.assertRaises(self.ns["Unreadable"]):
            self.ns["processes"]()

    def test_prune_keeps_every_version_and_says_why_when_the_listing_is_unreadable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as base:
            home = Path(base) / "valtrion"
            (home / "core" / "0.8.4").mkdir(parents=True)
            out = io.StringIO()
            done = subprocess.CompletedProcess([], 0, b"4242 1 /usr/bin/python3 other\n", b"")
            with mock.patch.object(self.ns["subprocess"], "run", return_value=done), contextlib.redirect_stdout(out):
                self.ns["prune"](str(home), "0.8.6", [])
            self.assertTrue((home / "core" / "0.8.4").is_dir())
        self.assertIn("unreadable=the process list does not show this helper", out.getvalue())
        self.assertNotIn("removed", out.getvalue())

    def test_the_helper_and_its_wrapper_shell_do_not_count_but_a_program_inside_the_folder_does(self):
        folder = "/h/.valtrion/core/0.8.4"
        argv = ["/h/.valtrion/incoming/valtrion_host.py", "busy", "/h/.valtrion", folder]
        shell = f"77 1 zsh -c '/usr/bin/python3 {' '.join(argv)}'"
        me = f"{self.pid} 77 /usr/bin/python3 {' '.join(argv)}"
        table, _ = self.listing(f"{shell}\n{me}\n".encode(), argv=argv)
        with mock.patch.object(sys, "argv", argv):
            self.assertFalse(self.ns["in_use"](table, folder))
            inside = f"{self.pid} 77 {folder}/bin/python {' '.join(argv)}"
            table, _ = self.listing(f"{shell}\n{inside}\n".encode(), argv=argv)
            self.assertTrue(self.ns["in_use"](table, folder), "the helper's own interpreter inside the folder still counts")

    def test_controller_reports_an_unreadable_listing(self):
        class Host:
            def put(self, local, remote_path, timeout_sec):
                return hostsync.CommandResult(0, "")

            def run(self, command, timeout_sec, cwd=None):
                return hostsync.CommandResult(0, "unreadable=the process list is not valid UTF-8\n")

        from valtrion import config
        entry = config.HostEntry("mac", "ssh", "macos", Path("/a"), ssh_target="me@mac")
        sentence = hostsync.prune_versions(entry, Host(), "/usr/bin/python3", "/Users/me", "0.8.6", ())
        self.assertEqual("old versions were kept: the process list could not be read (the process list is not valid UTF-8)", sentence)


if __name__ == "__main__":
    unittest.main()
