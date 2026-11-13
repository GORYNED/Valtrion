"""`valtrion unity run` offloaded to another host: the argument rewrite, the fallbacks, the files copied back, the receiving side.
A fake transport plays the host; no ssh and no Unity start."""

import io
import json
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock

from valtrion import __version__, cli, codesync, commands, config, constants, remote, unityoffload, unityrun  # noqa: F401  (cli first)
from valtrion.transport import CommandResult, Transport
from valtrion.unityoffload import PROJECT_TOKEN, NotOffloadable, UnityOffload, plan

from .test_host import HostTestCase, run as run_cli
from .test_unity_run import FAKE, FakeProbe, UnityRunTestCase

SHA = "a" * 40
BUSY = "all 2 slots of unity-editor are leased (held by someone)"


class FakeHost(Transport):
    """The offload host as seen over SSH: tells its slots, takes the code, makes an output folder and answers `unity run`."""

    kind = "ssh"

    def __init__(self, free=2, waiting=0, answer=None, files=None):
        self.free = free
        self.waiting = waiting
        self.answer = answer if answer is not None else CommandResult(0, "STATUS PASS\n")
        self.files = dict(files or {})
        self.calls: list[list[str]] = []
        self.runs: list[list[str]] = []

    def run(self, command, timeout_sec, cwd=None):
        arguments = list(command[1:])
        self.calls.append(arguments)
        if arguments[:2] == ["lease", "slots"]:
            report = {"host": "mac", "version": __version__, "unityEditor": {"capacity": 2, "free": self.free, "waiting": self.waiting, "held": []},
                      "unityVersions": ["6000.0.1f1"], "offload": {"accept": True, "kinds": ["editor", "unity-run"], "maxBundleMiB": 512},
                      "projects": ["MyGame"]}
            return CommandResult(0, json.dumps(report))
        if arguments[:2] == ["offload", "import"]:
            return CommandResult(0, json.dumps({"checkoutId": "c1", "sha": SHA, "project": "MyGame", "folder": "/work/c1"}))
        if arguments[:2] == ["workroot", "inbox"]:
            return CommandResult(0, json.dumps({"inbox": "/work/inbox-1"}))
        if arguments[:2] == ["unity", "run"]:
            self.runs.append(arguments)
            return self.answer
        return CommandResult(127, "", "unexpected call")

    def put(self, *_):
        return CommandResult(0)

    def get(self, remote_path, local, timeout_sec):
        if remote_path not in self.files:
            return CommandResult(1, "", "No such file or directory")
        Path(local).write_bytes(self.files[remote_path])
        return CommandResult(0)


def snapshot_of_head(project_dir, git, staging, max_bytes):
    return codesync.Snapshot(SHA, SHA, False, 0, None, 0)


class PlanTests(unittest.TestCase):
    def render(self, *arguments, out="/work/inbox-1", os_name="macos"):
        return plan(arguments).render(out, os_name)

    def test_the_arguments_that_are_understood_go_on_and_the_project_is_added(self):
        given = ["-batchmode", "-nographics", "-quit", "-runTests", "-testPlatform", "EditMode", "-testFilter", "A.B;C", "-assemblyNames", "X;Y",
                 "-executeMethod", "Ns.Build.Run", "-buildTarget", "Standalone", "-logFile", "-"]
        arguments, copies = self.render(*given)
        self.assertEqual([*given, "-projectPath", PROJECT_TOKEN], arguments)
        self.assertEqual([], copies)

    def test_the_project_path_is_replaced_by_the_token_wherever_it_stands(self):
        arguments, _ = self.render("-batchmode", "-projectPath", "C:/Work/MyGame", "-quit")
        self.assertEqual(["-batchmode", "-projectPath", PROJECT_TOKEN, "-quit"], arguments)

    def test_output_files_go_into_the_output_folder_and_come_back_to_their_own_paths(self):
        arguments, copies = self.render("-logFile", "logs/Editor.log", "-testResults", "out/results.xml", "-editorTestsResultFile", "out/r2.xml")
        self.assertEqual(["-logFile", "/work/inbox-1/1-Editor.log", "-testResults", "/work/inbox-1/2-results.xml", "-editorTestsResultFile",
                          "/work/inbox-1/3-r2.xml", "-projectPath", PROJECT_TOKEN], arguments)
        self.assertEqual([("/work/inbox-1/1-Editor.log", Path("logs/Editor.log").absolute()), ("/work/inbox-1/2-results.xml", Path("out/results.xml").absolute()),
                          ("/work/inbox-1/3-r2.xml", Path("out/r2.xml").absolute())], copies)

    def test_a_windows_host_gets_a_windows_path(self):
        arguments, _ = self.render("-logFile", "Editor.log", out="C:\\work\\inbox-1", os_name="windows")
        self.assertEqual("C:\\work\\inbox-1\\1-Editor.log", arguments[1])

    def test_what_cannot_be_sent_is_refused_with_a_reason(self):
        refused = [
            ("-buildTarget", "Android"), ("-activeBuildProfile", "Assets/Settings/Android.asset"), ("-customMethodArg", "/abs/path"),
            ("-customMethodArg", "C:\\Work\\file"), ("-customMethodArg", "C:/Work/file"), ("-customMethodArg", "\\\\server\\share"),
            ("-customMethodArg", "../up"), ("-customMethodArg", "a/../b"), ("-customMethodArg", "~/file"), ("-flag=/abs/path",),
        ]
        for case in refused:
            with self.assertRaises(NotOffloadable, msg=case):
                plan(["-batchmode", *case])

    def test_an_option_without_its_value_is_refused(self):
        for name in ("-projectPath", "-logFile", "-executeMethod", "-testResults"):
            with self.assertRaises(NotOffloadable, msg=name):
                plan(["-batchmode", name])

    def test_names_that_look_odd_are_not_taken_for_paths(self):
        arguments, _ = self.render("-testFilter", "/regex/..", "-executeMethod", "Ns.Cls.Method", "-customArg", "Ns:Value")
        self.assertIn("/regex/..", arguments)


class PlacementTests(HostTestCase):
    def setUp(self):
        super().setUp()
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art", offload=True)
        self.project = self.base / "MyGame"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.1f1\n", encoding="utf-8")
        self.host = FakeHost()
        self.sessions: list[tuple[bool, bool]] = []
        self.now = 0.0
        self.err = io.StringIO()

    def factory(self, entry, retry=True, lifeline=False):
        self.sessions.append((retry, lifeline))
        return remote.Remote(self.host, ("valtrion",), None)

    def sleep(self, seconds):
        self.now += seconds

    def offload(self, unity_args, *, local_room=False, force=False, settings=None, snapshot=snapshot_of_head, unity=None, extra=()):
        raw = ["unity", "run", "--project-dir", str(self.project), "--lease-wait", "30", "--timeout-sec", "100", *extra, "--", *unity_args]
        return UnityOffload(unity_args, raw, self.project, entries=[config.find_host("mac")], settings=settings or config.OffloadSettings(settle_sec=0),
                            remote_for=self.factory, local_room=lambda: local_room, timeout_sec=1000, force=force,
                            git=lambda folder, arguments: (1, ""), snapshot=snapshot, sleep=self.sleep, monotonic=lambda: self.now,
                            local_host="build-pc", unity=unity, err=self.err)

    def run_offload(self, unity_args, **options):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = self.offload(unity_args, **options).run()
        return code, out.getvalue()

    def test_a_run_goes_to_the_host_when_the_local_slots_are_busy_and_its_answer_is_relayed(self):
        self.host.answer = CommandResult(1, "tests failed\nSTATUS FAIL\n", "console text\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.offload(["-batchmode", "-projectPath", str(self.project), "-runTests", "-testPlatform", "EditMode"]).run()
        self.assertEqual(1, code)
        self.assertEqual("tests failed\nSTATUS FAIL\n", out.getvalue())
        self.assertIn("console text", err.getvalue())
        self.assertIn("offload: ran on host mac (commit fetched by the host from the project's remote)", self.err.getvalue())
        (command,) = self.host.runs
        self.assertEqual(["unity", "run", "--timeout-sec", "100", "--lease-wait", "0", "--stop-on-stdin-eof", "--offload-project", "MyGame",
                          "--offload-checkout", "c1", "--offload-sha", SHA, "--offload-from", "build-pc", "--offload-out", "/work/inbox-1", "--",
                          "-batchmode", "-projectPath", PROJECT_TOKEN, "-runTests", "-testPlatform", "EditMode"], command)
        self.assertIn((False, True), self.sessions)  # started without retries and with the lifeline

    def test_the_local_project_path_never_reaches_the_host(self):
        self.run_offload(["-batchmode"], extra=("--hidden-desktop",))
        (command,) = self.host.runs
        self.assertNotIn("--project-dir", command)
        self.assertNotIn(str(self.project), " ".join(command))
        self.assertIn("--hidden-desktop", command)

    def test_output_files_are_copied_back_also_when_the_run_failed_and_a_missing_one_is_a_note(self):
        log, results = self.base / "logs" / "Editor.log", self.base / "out" / "results.xml"
        self.host.answer = CommandResult(1, "STATUS FAIL\n")
        self.host.files = {"/work/inbox-1/1-Editor.log": b"the log"}
        code, _ = self.run_offload(["-batchmode", "-logFile", str(log), "-testResults", str(results)])
        self.assertEqual(1, code)
        self.assertEqual(b"the log", log.read_bytes())
        self.assertFalse(results.exists())
        self.assertIn("results.xml was not copied back", self.err.getvalue())
        (command,) = self.host.runs
        self.assertEqual("/work/inbox-1/1-Editor.log", command[command.index("-logFile") + 1])
        self.assertEqual("/work/inbox-1/2-results.xml", command[command.index("-testResults") + 1])

    def test_nothing_is_done_while_the_local_slots_have_room(self):
        code, out = self.run_offload(["-batchmode"], local_room=True)
        self.assertIsNone(code)
        self.assertEqual([], self.host.calls)
        self.assertEqual("", self.err.getvalue())

    def test_a_host_that_turned_out_to_be_busy_sends_the_run_back_to_the_local_queue(self):
        self.host.answer = CommandResult(2, f"{BUSY}\nSTATUS BLOCKED\n")
        code, out = self.run_offload(["-batchmode"])
        self.assertIsNone(code)
        self.assertEqual("", out)  # the host's BLOCKED is not the result of the run
        self.assertIn("offload: host mac was busy, running here", self.err.getvalue())

    def test_a_project_lease_that_is_taken_counts_as_busy_too(self):
        self.host.answer = CommandResult(2, "unity-project:0123abcd is leased by someone\nSTATUS BLOCKED\n")
        code, _ = self.run_offload(["-batchmode"])
        self.assertIsNone(code)
        self.assertIn("was busy", self.err.getvalue())

    def test_with_offload_a_busy_host_is_waited_for_and_then_the_run_is_blocked(self):
        self.host.answer = CommandResult(2, f"{BUSY}\nSTATUS BLOCKED\n")
        code, _ = self.run_offload(["-batchmode"], force=True)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertGreater(len(self.host.runs), 1)
        self.assertIn("--offload: no host has a free Editor slot", self.err.getvalue())

    def test_a_host_without_a_free_slot_is_not_asked_to_run(self):
        self.host.free = 0
        code, _ = self.run_offload(["-batchmode"])
        self.assertIsNone(code)
        self.assertEqual([], self.host.runs)
        self.assertIn("offload: not used: no host has a free Editor slot", self.err.getvalue())

    def test_a_refusal_of_the_host_before_unity_started_goes_back_to_the_local_queue_or_blocks_with_offload(self):
        self.host.answer = CommandResult(2, "offload refused: host.json declares no project 'MyGame'\nSTATUS BLOCKED\n")
        code, out = self.run_offload(["-batchmode"])
        self.assertIsNone(code)
        self.assertEqual("", out)
        self.assertIn("offload: not used: mac: host.json declares no project", self.err.getvalue())
        self.err.truncate(0)
        code, _ = self.run_offload(["-batchmode"], force=True)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("--offload: mac: host.json declares no project", self.err.getvalue())

    def test_a_host_that_cannot_be_reached_when_the_run_starts_is_a_refusal_not_a_lost_run(self):
        self.host.answer = CommandResult(remote.SSH_FAILURE, "", "ssh: connect to host mac port 22: Connection refused")
        code, _ = self.run_offload(["-batchmode"])
        self.assertIsNone(code)
        self.assertIn("is unreachable", self.err.getvalue())
        self.assertNotIn("lost", self.err.getvalue())

    def test_a_connection_lost_while_the_run_goes_on_runs_it_again_here_when_allowed(self):
        self.host.answer = CommandResult(remote.SSH_FAILURE, "partial\n", "Connection reset by peer")
        code, out = self.run_offload(["-batchmode"])
        self.assertIsNone(code)
        self.assertEqual("", out)
        self.assertIn("was lost while the run was in progress", self.err.getvalue())
        self.assertIn("run again here", self.err.getvalue())

    def test_a_connection_lost_while_the_run_goes_on_is_blocked_when_a_second_run_is_not_allowed(self):
        self.host.answer = CommandResult(None, "", "", timed_out=True)
        code, _ = self.run_offload(["-batchmode"], settings=config.OffloadSettings(settle_sec=0, rerun_local=False))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("result is unknown", self.err.getvalue())
        self.assertIn("lease list --host mac", self.err.getvalue())

    def test_code_that_cannot_be_sent_runs_here_and_with_offload_ends_blocked(self):
        def refusing(project_dir, git, staging, max_bytes):
            raise codesync.SnapshotError("Git LFS file(s) changed but not committed: Assets/a.png")

        code, _ = self.run_offload(["-batchmode"], snapshot=refusing)
        self.assertIsNone(code)
        self.assertIn("offload: not used: Git LFS file(s) changed but not committed", self.err.getvalue())
        self.err.truncate(0)
        code, _ = self.run_offload(["-batchmode"], snapshot=refusing, force=True)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("BLOCKED: Git LFS file(s) changed but not committed", self.err.getvalue())
        self.assertNotIn("not used", self.err.getvalue())
        self.assertEqual([], self.host.runs)

    def test_an_argument_that_is_not_understood_runs_here_with_one_line_and_blocks_with_offload(self):
        code, _ = self.run_offload(["-batchmode", "-buildTarget", "Android"])
        self.assertIsNone(code)
        self.assertEqual(["offload: not used: -buildTarget Android would switch the platform of the host's checkout (only -buildTarget Standalone is sent)"],
                         self.err.getvalue().splitlines())
        self.assertEqual([], self.host.calls)
        code, _ = self.run_offload(["-batchmode", "-buildTarget", "Android"], force=True)
        self.assertEqual(constants.EXIT_BLOCKED, code)

    def test_an_editor_of_this_machine_cannot_be_used_on_the_host(self):
        code, _ = self.run_offload(["-batchmode"], unity="/Applications/Unity")
        self.assertIsNone(code)
        self.assertIn("--unity", self.err.getvalue())


class CommandTests(HostTestCase):
    """What `unity run` does with the offload options; the placement itself is faked."""

    def setUp(self):
        super().setUp()
        self.project = self.base / "MyGame"
        self.project.mkdir()

    def unity(self, *options, placed=None, marked=True):
        if marked:
            config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art", offload=True)
        with mock.patch.object(unityrun, "run", return_value=0) as local, mock.patch.object(unityoffload, "UnityOffload") as sender:
            sender.return_value.run.return_value = placed
            code, out, err = run_cli("unity", "run", "--lease-wait", "0", *options, "--", "-batchmode", "-projectPath", str(self.project))
        return code, local, sender, err

    def test_without_a_marked_host_the_run_is_the_local_one(self):
        code, local, sender, _ = self.unity(marked=False)
        self.assertEqual(0, code)
        sender.assert_not_called()
        local.assert_called_once()
        self.assertIsNone(local.call_args.kwargs["prepare"])

    def test_no_offload_keeps_the_run_here(self):
        _, local, sender, _ = self.unity("--no-offload")
        sender.assert_not_called()
        local.assert_called_once()

    def test_a_marked_host_is_tried_and_the_local_run_follows_when_it_did_not_take_the_run(self):
        code, local, sender, _ = self.unity()
        self.assertEqual(0, code)
        sender.assert_called_once()
        self.assertEqual(["mac"], [entry.id for entry in sender.call_args.kwargs["entries"]])
        self.assertFalse(sender.call_args.kwargs["force"])
        local.assert_called_once()

    def test_the_answer_of_the_host_ends_the_command_and_nothing_runs_here(self):
        code, local, sender, _ = self.unity("--offload", placed=7)
        self.assertEqual(7, code)
        self.assertTrue(sender.call_args.kwargs["force"])
        local.assert_not_called()

    def test_the_run_that_a_caller_watches_is_never_offloaded_again(self):
        _, local, sender, _ = self.unity("--stop-on-stdin-eof")
        sender.assert_not_called()
        local.assert_called_once()

    def test_offload_needs_a_marked_host_and_does_not_go_with_host_or_no_offload(self):
        code, local, sender, err = self.unity("--offload", marked=False)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("no host in hosts.json is marked for offloading", err)
        local.assert_not_called()
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art", offload=True)
        code, _, err = run_cli("unity", "run", "--offload", "--host", "mac", "--", "-batchmode")
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("cannot be combined with --host", err)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["unity", "run", "--offload", "--no-offload", "--", "-batchmode"])


class ReceiverTests(HostTestCase):
    def setUp(self):
        super().setUp()
        self.home.mkdir(parents=True, exist_ok=True)
        self.checkout = self.base / "co"
        self.out = self.base / "work" / "inbox-1"
        self.out.mkdir(parents=True)
        self.write_host({"accept": True, "kinds": ["unity-run"]})

    def write_host(self, offload):
        document = {"projects": {"MyGame": {"checkouts": [{"id": "c1", "path": str(self.checkout)}], "unityPath": "Game", "remote": "https://example.com/g.git"}}}
        if offload is not None:
            document["offload"] = offload
        (self.home / "host.json").write_text(json.dumps(document), encoding="utf-8")

    def receive(self, *arguments, ensure=None, **options):
        values = {"project": "MyGame", "checkout_id": "c1", "sha": SHA, "out_dir": str(self.out), **options}
        return unityoffload.receive(list(arguments) or ["-batchmode", "-projectPath", PROJECT_TOKEN], values["project"], values["checkout_id"],
                                    values["sha"], values["out_dir"], ensure=ensure or mock.Mock())

    def test_a_host_that_does_not_accept_the_kind_refuses(self):
        for offload in (None, {"accept": False}, {"accept": True, "kinds": ["editor"]}):
            self.write_host(offload)
            reason = self.receive()
            self.assertIsInstance(reason, str, offload)
            self.assertTrue(reason.startswith(unityoffload.REFUSED), reason)
            self.assertIn("does not accept offloaded work", reason)

    def test_an_unknown_project_or_checkout_or_commit_is_refused(self):
        self.assertIn("declares no project", self.receive(project="Other"))
        self.assertIn("no checkout", self.receive(checkout_id="c9"))
        self.assertIn("40-character", self.receive(sha="abc"))

    def test_the_token_becomes_the_unity_project_of_the_checkout(self):
        received = self.receive("-batchmode", "-projectPath", PROJECT_TOKEN, "-logFile", str(self.out / "1-Editor.log"))
        self.assertEqual(self.checkout / "Game", received.project_dir)
        self.assertEqual(["-batchmode", "-projectPath", str(self.checkout / "Game"), "-logFile", str(self.out / "1-Editor.log")], received.unity_args)

    def test_an_output_file_outside_the_output_folder_is_refused(self):
        reason = self.receive("-projectPath", PROJECT_TOKEN, "-logFile", str(self.base / "elsewhere.log"))
        self.assertIsInstance(reason, str)
        self.assertIn("must be inside", reason)

    def test_the_prepare_hook_resets_the_checkout_to_the_commit(self):
        ensure = mock.Mock(return_value=mock.Mock(ok=True, reason=None))
        received = self.receive(ensure=ensure)
        ensure.assert_not_called()  # nothing happens until the run holds the project lease
        self.assertIsNone(received.prepare())
        args, kwargs = ensure.call_args
        self.assertEqual((self.checkout, SHA), args[:2])
        self.assertEqual(("Game", "https://example.com/g.git"), (kwargs["unity_path"], kwargs["remote"]))

    def test_a_commit_that_cannot_be_checked_out_is_a_refusal_with_the_reason(self):
        ensure = mock.Mock(return_value=mock.Mock(ok=False, reason="it is not on the remote"))
        reason = self.receive(ensure=ensure).prepare()
        self.assertTrue(reason.startswith(unityoffload.REFUSED))
        self.assertIn("it is not on the remote", reason)

    def test_the_command_refuses_with_a_status_the_sender_can_read(self):
        self.write_host({"accept": False})
        with mock.patch.object(unityrun, "run") as local:
            code, out, _ = run_cli("unity", "run", "--lease-wait", "0", "--offload-from", "build-pc", "--offload-project", "MyGame", "--offload-checkout", "c1",
                                   "--offload-sha", SHA, "--offload-out", str(self.out), "--", "-batchmode", "-projectPath", PROJECT_TOKEN)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        lines = out.splitlines()
        self.assertEqual("STATUS BLOCKED", lines[-1])
        self.assertTrue(lines[-2].startswith(unityoffload.REFUSED))
        local.assert_not_called()

    def test_the_command_runs_unity_on_the_checkout_with_the_hook_and_never_offloads_again(self):
        with mock.patch.object(unityrun, "run", return_value=0) as local, mock.patch.object(unityoffload, "UnityOffload") as sender:
            code, _, _ = run_cli("unity", "run", "--lease-wait", "0", "--offload-from", "build-pc", "--offload-project", "MyGame", "--offload-checkout", "c1",
                                 "--offload-sha", SHA, "--offload-out", str(self.out), "--", "-batchmode", "-projectPath", PROJECT_TOKEN)
        self.assertEqual(0, code)
        sender.assert_not_called()
        args, kwargs = local.call_args
        self.assertEqual(["-batchmode", "-projectPath", str(self.checkout / "Game")], args[0])
        self.assertEqual(0, args[3])  # no wait for the leases: a busy host answers at once
        self.assertTrue(callable(kwargs["prepare"]))


class PrepareHookTests(UnityRunTestCase):
    """`unityrun.run(prepare=...)`: called with the project lease held, before Unity starts; its reason ends the run `BLOCKED`."""

    def test_the_hook_runs_under_the_project_lease_before_unity(self):
        from valtrion import lease

        seen = []
        dump = self.base / "dump"

        def hook():
            seen.append(self.manager.status(lease.unity_project_resource(self.project)).state)
            seen.append(dump.exists())
            return None

        code, lines, _ = self.unity("--dump", str(dump), prepare=hook)
        self.assertEqual(["held", False], seen)
        self.assertEqual("STATUS PASS", lines[-1])
        self.assertTrue(dump.exists())

    def test_the_project_and_its_editor_are_looked_for_after_the_hook_has_made_the_checkout(self):
        """An offloaded run starts on a host whose checkout has not been reset to the commit yet: the folder (and its Unity version)
        may not exist until the hook has run."""
        from valtrion.providers.unity import support

        project = self.base / "NotYet"
        seen = []

        def hook():
            (project / "ProjectSettings").mkdir(parents=True)
            (project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1" + chr(10), encoding="utf-8")
            return None

        def find(version, os_name, environ=None):
            seen.append((version, project.is_dir()))
            return {"path": sys.executable}, None

        with mock.patch.object(support, "find_editor", side_effect=find):
            out, err = io.StringIO(), io.StringIO()
            code = unityrun.run([FAKE, "-batchmode", "-projectPath", str(project)], home=self.home, out=out, err=err, poll_sec=0.1,
                                cancel_poll_sec=0.1, lease_wait_sec=0, probe=FakeProbe(), guard_poll_sec=0.1, prepare=hook)
        self.assertEqual([("6000.6.7f1", True)], seen)
        self.assertEqual("STATUS PASS", out.getvalue().splitlines()[-1], err.getvalue())

    def test_a_folder_that_the_hook_did_not_make_blocks_the_run(self):
        out = io.StringIO()
        code = unityrun.run([FAKE, "-batchmode", "-projectPath", str(self.base / "Missing")], home=self.home, out=out, err=io.StringIO(),
                            poll_sec=0.1, cancel_poll_sec=0.1, lease_wait_sec=0, probe=FakeProbe(), guard_poll_sec=0.1, prepare=lambda: None)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("does not exist", out.getvalue())

    def test_a_reason_from_the_hook_blocks_the_run_and_unity_does_not_start(self):
        dump = self.base / "dump"
        code, lines, _ = self.unity("--dump", str(dump), prepare=lambda: "offload refused: the commit cannot be checked out")
        self.assertEqual((constants.EXIT_BLOCKED, "STATUS BLOCKED"), (code, lines[-1]))
        self.assertEqual("offload refused: the commit cannot be checked out", lines[-2])
        self.assertFalse(dump.exists())
        self.assertNoLeases()


if __name__ == "__main__":
    unittest.main()
