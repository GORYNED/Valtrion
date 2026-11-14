"""A command handed to the interactive session of the logged-in user (Windows): when it is needed, the task, the output and exit code that
come back, the fallbacks. A fake Task Scheduler plays the session; nothing is started."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, commands, config, core, usersession  # noqa: F401  (cli first)

from .test_host import HostTestCase, run as run_cli


class FakeScheduler:
    """`schtasks` as the code sees it. On `/Run` the 'session' starts the batch file: it writes the marker, the output and the exit code."""

    def __init__(self, create=(0, "SUCCESS"), run=(0, "SUCCESS"), starts=True, output="line one\nSTATUS PASS\n", exit_code="0", exists=None):
        self.create, self.run, self.starts, self.output, self.exit_code = create, run, starts, output, exit_code
        self.exists = create[0] == 0 if exists is None else exists  # whether /Query finds the task
        self.calls: list[list[str]] = []
        self.batch: Path | None = None
        self.script = ""

    def __call__(self, arguments):
        self.calls.append(list(arguments))
        if arguments[0] == "/Create":
            self.batch = Path(arguments[arguments.index("/TR") + 1].strip('"'))
            self.script = self.batch.read_text(encoding="utf-8")
            return self.create
        if arguments[0] == "/Query":
            return (0, "Ready") if self.exists else (1, "ERROR: The system cannot find the file specified.")
        if arguments[0] == "/Run":
            if self.run[0] == 0 and self.starts:
                folder = self.batch.parent
                (folder / "started.txt").write_text("started", encoding="ascii")
                (folder / "out.txt").write_text(self.output, encoding="utf-8")
                if self.exit_code is not None:
                    (folder / "exit.txt").write_text(self.exit_code + "\r\n", encoding="ascii")
            return self.run
        return 0, "SUCCESS"

    def verbs(self):
        return [call[0] for call in self.calls]


class NeededTests(unittest.TestCase):
    def test_only_a_windows_process_without_a_desktop_that_was_not_handed_over_needs_it(self):
        with mock.patch.object(core, "local_os", return_value="windows"):
            self.assertTrue(usersession.needed(lambda: False, {}))
            self.assertFalse(usersession.needed(lambda: True, {}))
            self.assertFalse(usersession.needed(lambda: None, {}))  # not known: not guessed
            self.assertFalse(usersession.needed(lambda: False, {usersession.ENVIRONMENT_VARIABLE: "1"}))
        with mock.patch.object(core, "local_os", return_value="macos"):
            self.assertFalse(usersession.needed(lambda: False, {}))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.now = 0.0

    def run_it(self, scheduler, cancelled=lambda: False):
        def sleep(seconds):
            self.now += seconds

        return usersession.run(["valtrion.exe", "unity", "run", "--", "-projectPath", "C:/My Game/100%"], self.out, self.err, cancelled,
                               scheduler, sleep, lambda: self.now, self.home, user="PC\\dev")

    def test_the_command_runs_as_an_interactive_elevated_task_and_its_output_and_exit_code_come_back(self):
        scheduler = FakeScheduler(output="line one\nSTATUS FAIL\n", exit_code="1")
        self.assertEqual(1, self.run_it(scheduler))
        self.assertEqual("line one\nSTATUS FAIL\n", self.out.getvalue())
        create = scheduler.calls[0]
        for expected in ("/IT", "/RU", "PC\\dev", "/F"):
            self.assertIn(expected, create)
        self.assertEqual("HIGHEST", create[create.index("/RL") + 1])
        self.assertEqual(["/Create", "/Run", "/Delete"], scheduler.verbs())  # the task is deleted at the end
        self.assertEqual([], [p for p in (self.home / "session").iterdir()])  # and so is the folder

    def test_the_batch_file_marks_the_start_sets_the_guard_variable_and_quotes_the_arguments(self):
        scheduler = FakeScheduler()
        self.run_it(scheduler)
        self.assertIn(f"set {usersession.ENVIRONMENT_VARIABLE}=1", scheduler.script)
        self.assertIn('valtrion.exe unity run -- -projectPath "C:/My Game/100%%"', scheduler.script)  # a percent sign is not expanded
        self.assertIn("started.txt", scheduler.script)
        self.assertIn("%errorlevel%", scheduler.script)

    def test_a_task_that_cannot_be_created_or_run_falls_back_to_this_session(self):
        for scheduler in (FakeScheduler(create=(1, "ERROR: no logged on user")), FakeScheduler(run=(1, "ERROR: cannot run"))):
            self.err.truncate(0)
            self.assertIsNone(self.run_it(scheduler))
            self.assertIn("the command is run in this session", self.err.getvalue())
        self.assertIn("/Delete", scheduler.verbs())  # a created task does not stay

    def test_a_warning_of_the_scheduler_about_the_time_does_not_stop_a_task_that_was_created(self):
        scheduler = FakeScheduler(create=(1, "WARNING: Task may not run because /ST is earlier than current time."), exists=True)
        self.assertEqual(0, self.run_it(scheduler))

    def test_a_task_that_never_starts_falls_back_after_the_wait(self):
        scheduler = FakeScheduler(starts=False)
        self.assertIsNone(self.run_it(scheduler))
        self.assertGreaterEqual(self.now, usersession.START_WAIT_SEC)
        self.assertIn("/End", scheduler.verbs())
        self.assertIn("nobody is logged in", self.err.getvalue())

    def test_a_caller_that_is_gone_stops_the_task(self):
        scheduler = FakeScheduler(exit_code=None)  # the command does not end by itself
        self.assertEqual(1, self.run_it(scheduler, cancelled=lambda: True))
        self.assertIn("/End", scheduler.verbs())
        self.assertIn("/Delete", scheduler.verbs())


class CommandTests(HostTestCase):
    """What `unity run` does with the session: handed over, not handed over, switched off."""

    def unity(self, handed, *options, needed=True):
        with mock.patch.object(usersession, "needed", return_value=needed), mock.patch.object(usersession, "run", return_value=handed) as run, \
                mock.patch("valtrion.unityrun.run", return_value=0) as local:
            code, out, err = run_cli("unity", "run", "--lease-wait", "0", "--no-offload", *options, "--", "-batchmode", "-projectPath", str(self.base))
        return code, run, local

    def test_the_result_of_the_session_is_the_result_of_the_command(self):
        code, run, local = self.unity(3)
        self.assertEqual(3, code)
        local.assert_not_called()
        argv = run.call_args.args[0]
        self.assertEqual("--", argv[argv.index("--")])
        self.assertNotIn("--stop-on-stdin-eof", argv)

    def test_without_a_session_the_command_runs_here(self):
        code, run, local = self.unity(None)
        run.assert_called_once()
        local.assert_called_once()
        self.assertEqual(0, code)

    def test_a_command_that_needs_no_session_does_not_ask_for_one(self):
        _, run, local = self.unity(5, needed=False)
        run.assert_not_called()
        local.assert_called_once()

    def test_host_json_can_switch_it_off(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.json").write_text('{"interactiveSession": false}', encoding="utf-8")
        _, run, local = self.unity(5)
        run.assert_not_called()
        local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
