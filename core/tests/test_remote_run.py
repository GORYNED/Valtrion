"""A plan or pipeline run on an SSH host: the caller sends the files, the host resolves the project, a path rewritten by the local
shell is named as such. A fake transport plays the host; nothing here starts ssh."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from valtrion import cli, commands, config, constants, core, remote, submit  # noqa: F401  (cli first: it registers the commands)
from valtrion.resolver import HostInfo
from valtrion.transport import CommandResult, Transport

from .test_host import HostTestCase, run

PLAN = {"schema": "gdv.plan/1", "id": "open", "kind": "smoke", "requires": {"platforms": ["android"]}, "dataPolicy": "devdata",
        "budgetSeconds": 30, "steps": [{"type": "wait", "timeoutSec": 5}]}
SHA = "a" * 40
STARTED = {"jobId": "job-1", "dir": "/art/job-1", "relDir": "x/job-1", "host": "mac"}


class Host(Transport):
    """The host as seen over SSH: has some folders, makes inbox folders on request, stores what it is sent, and starts jobs."""

    kind = "ssh"

    def __init__(self, folder: Path, directories=()):
        self.folder = folder
        self.directories = set(directories)
        self.calls: list[tuple[list, str | None]] = []
        self.sent: list[tuple[str, bytes]] = []
        self.inboxes: list[Path] = []

    def run(self, command, timeout_sec, cwd=None):
        self.calls.append((list(command), cwd))
        if command[:2] == ["gdv", "workroot"]:
            inbox = self.folder / f"inbox-{len(self.inboxes) + 1}"
            inbox.mkdir(parents=True)
            self.inboxes.append(inbox)
            return CommandResult(0, json.dumps({"inbox": str(inbox)}))
        if command[:2] == ["sh", "-c"]:
            return CommandResult(0, "Welcome to the host\n/etc/motd says hi\nvaltrion-home=/home/me\nlast login: today\n")
        if command[:2] == ["test", "-d"]:
            return CommandResult(0 if command[2] in self.directories else 1)
        return CommandResult(0, json.dumps(STARTED))

    def put(self, local, remote_path, timeout_sec):
        self.sent.append((remote_path, Path(local).read_bytes()))
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, remote_path)
        return CommandResult(0)

    def get(self, *_):
        return CommandResult(0)

    @property
    def started(self):
        return [call for call, _ in self.calls if call[:2] == ["gdv", "run"] or call[:2] == ["gdv", "pipeline"]]


class RemoteRunTests(HostTestCase):
    def setUp(self):
        super().setUp()
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art", projects_root="/Users/me/Projects")
        self.caller = self.base / "only-on-the-caller"
        self.caller.mkdir()
        self.plan = self.caller / "open.plan.json"
        core.atomic_write_json(self.plan, PLAN)
        self.host = Host(self.base / "host", directories={"/Users/me/Projects/MyGame"})
        self.host.folder.mkdir()
        self.entry = config.find_host("mac")
        patches = [
            mock.patch.object(commands, "_remote", side_effect=self.remote_to_host),
            mock.patch.object(commands, "resolve_host", return_value=(
                SimpleNamespace(host=HostInfo("mac", "ssh", "macos", "me@mac"), selected_by="pinned", transport="ssh", reasons=()),
                {"mac": self.entry}))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # No valtrion.json of this repository may name the project of these runs.
        patcher = mock.patch.dict(os.environ, {"GDV_REMOTE_PROJECT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def remote_to_host(self, entry, arguments, retry=True):
        return remote.Remote(self.host, ("gdv",), getattr(arguments, "remote_project", None) or None)

    def run_plan(self, *extra, plan=None):
        return run("run", str(plan or self.plan), "--repo", "Sample", "--sha", SHA, "--app", "com.example.app", "--platform", "android",
                   "--project-dir", str(self.base / "local" / "MyGame"), "--host", "mac", *extra)

    def test_a_plan_that_exists_only_on_the_caller_is_sent_and_the_host_is_told_to_read_the_copy(self):
        code, out, err = self.run_plan()
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        inbox = self.host.inboxes[0]
        self.assertEqual(f"{inbox}/1-open.plan.json", command[2])
        self.assertEqual(PLAN, json.loads((inbox / "1-open.plan.json").read_text(encoding="utf-8")))
        self.assertEqual(hashlib.sha256(self.plan.read_bytes()).hexdigest(), hashlib.sha256((inbox / "1-open.plan.json").read_bytes()).hexdigest(),
                         "the copy is byte for byte the plan, so the result's plan hash is the same on either machine")
        self.assertNotIn(str(self.plan), command)
        self.assertNotIn("--host", command)
        self.assertEqual(["--local", "--transport", "ssh", "--selected-by", "pinned"], command[-5:])
        self.assertEqual("job-1", json.loads(out)["jobId"])

    def test_the_project_is_found_under_the_hosts_projects_root_and_the_callers_project_dir_is_not_forwarded(self):
        code, _, err = self.run_plan()
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        self.assertEqual(["--project-dir", "/Users/me/Projects/MyGame"], command[command.index("--project-dir"):command.index("--project-dir") + 2])
        self.assertEqual(1, command.count("--project-dir"))
        self.assertNotIn(str(self.base / "local" / "MyGame"), command)

    def test_a_project_missing_under_the_projects_root_is_blocked_and_nothing_is_sent_or_started(self):
        self.host.directories.clear()
        code, _, err = self.run_plan()
        self.assertEqual(constants.EXIT_BLOCKED, code)
        for expected in ("BLOCKED", "mac", "'MyGame' (from the Unity project folder name): /Users/me/Projects/MyGame",
                         "'Sample' (from candidate.repo): /Users/me/Projects/Sample", "projectsRoot '/Users/me/Projects'"):
            self.assertIn(expected, err)
        self.assertEqual([], self.host.sent)
        self.assertEqual([], self.host.started)

    def test_an_explicit_remote_project_wins_and_the_projects_root_is_not_consulted(self):
        code, _, err = self.run_plan("--remote-project", "/Users/me/elsewhere")
        self.assertEqual(0, code, err)
        self.assertEqual([], [call for call, _ in self.host.calls if call[0] == "test"])
        (command,) = self.host.started
        self.assertNotIn("/Users/me/Projects/MyGame", command)
        self.assertEqual("/Users/me/elsewhere", self.host.calls[-1][1], "the host runs it in that folder, as before")
        self.assertEqual(1, len(self.host.sent), "the plan is still sent")

    def test_the_remote_project_from_the_environment_wins_too(self):
        with mock.patch.dict(os.environ, {"GDV_REMOTE_PROJECT": "/Users/me/from-env"}):
            code, _, err = self.run_plan()
        self.assertEqual(0, code, err)
        self.assertEqual([], [call for call, _ in self.host.calls if call[0] == "test"])
        (command,) = self.host.started
        self.assertIn(str(self.base / "local" / "MyGame"), command, "the command line is forwarded as given")
        self.assertNotIn("/Users/me/Projects/MyGame", command)

    def test_the_repository_name_is_the_fallback_when_the_first_name_is_not_there(self):
        self.host.directories.clear()
        self.host.directories.add("/Users/me/Projects/Sample")
        code, _, err = self.run_plan()
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        self.assertEqual("/Users/me/Projects/Sample", command[command.index("--project-dir") + 1])

    def test_a_run_that_needs_no_project_is_not_blocked_by_the_projects_root(self):
        self.host.directories.clear()
        code, _, err = run("run", str(self.plan), "--repo", "Sample", "--sha", SHA, "--app", "com.example.app", "--platform", "android",
                           "--artifact", "/builds/app.apk", "--host", "mac")
        self.assertEqual(0, code, err)
        self.assertEqual([], [call for call, _ in self.host.calls if call[0] == "test"])
        (command,) = self.host.started
        self.assertNotIn("--project-dir", command)
        self.assertIn("/builds/app.apk", command, "an artifact stays a path on the host")

    def test_a_projects_root_with_a_tilde_is_expanded_by_the_host(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art", projects_root="~/Projects")
        self.entry = config.find_host("mac")
        commands.resolve_host.return_value[1]["mac"] = self.entry
        self.host.directories.add("/home/me/Projects/MyGame")
        code, _, err = self.run_plan()
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        self.assertEqual("/home/me/Projects/MyGame", command[command.index("--project-dir") + 1])

    def test_an_editor_run_without_a_local_project_still_looks_in_the_projects_root(self):
        self.host.directories.clear()
        self.host.directories.add("/Users/me/Projects/Sample")
        code, _, err = run("run", str(self.plan), "--repo", "Sample", "--sha", SHA, "--app", "editor", "--platform", "editor", "--host", "mac")
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        self.assertEqual("/Users/me/Projects/Sample", command[command.index("--project-dir") + 1])

    def test_the_first_name_is_used_unverified_when_a_windows_host_cannot_be_asked(self):
        entry = config.HostEntry("win", "ssh", "windows", Path("D:/art"), ssh_target="me@win", projects_root="D:\\Work")
        prep = remote.Remote(Host(self.base / "w"), ("gdv",))
        found = submit.find_project(prep, entry, [("First", "the Unity project folder name"), ("Second", "candidate.repo")])
        self.assertEqual("D:\\Work\\First", found)
        self.assertEqual([], prep.transport.calls, "nothing was asked")

    def test_the_inbox_is_honoured_only_for_a_command_forwarded_by_a_caller(self):
        pipeline = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": [{"stage": "run", "plan": "open.plan.json", "args": {"app": "a"}}]}
        core.atomic_write_json(self.caller / "p.json", pipeline)
        core.atomic_write_json(self.caller / "open.plan.json", PLAN)
        seen = []
        real = commands._start_pipeline

        def spy(arguments, pipeline, payload, *rest):
            seen.append(payload.get("inbox"))
            return 0

        with mock.patch.object(commands, "_start_pipeline", spy):
            run("pipeline", str(self.caller / "p.json"), "--inbox", "inbox-0123456789abcdef")
            run("pipeline", str(self.caller / "p.json"), "--inbox", "inbox-0123456789abcdef", "--local", "--transport", "ssh")
        self.assertEqual([None, "inbox-0123456789abcdef"], seen)

    def test_the_command_carries_the_inbox_so_the_host_can_keep_and_remove_it(self):
        self.run_plan()
        (command,) = self.host.started
        self.assertEqual(self.host.inboxes[0].name, command[command.index("--inbox") + 1])

    def test_only_the_plan_word_is_replaced_not_a_protected_path_that_looks_like_it(self):
        raw = ["run", "--protect", "plan.json", "plan.json", "--repo", "R"]
        self.assertEqual(["run", "--protect", "plan.json", "/x/1-plan.json", "--repo", "R"],
                         submit.replace_positional(raw, Path("plan.json"), "/x/1-plan.json"))

    def test_a_malformed_prelaunch_entry_is_blocked_with_a_message(self):
        pipeline = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": [{"stage": "run", "plan": str(self.plan), "args": {"app": "a"}}]}
        for bad in (["x"], [{"remote": "a.json"}], "oops"):
            with self.subTest(bad=bad):
                with self.assertRaises(submit.SubmitError):
                    submit.pipeline_inputs(self.caller / "p.json", {**pipeline, "stages": [{**pipeline["stages"][0], "prelaunchFiles": bad}]},
                                           submit.Inputs(), self.base)

    def test_a_host_without_a_projects_root_gets_the_project_dir_as_given(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art")
        self.entry = config.find_host("mac")
        commands.resolve_host.return_value[1]["mac"] = self.entry
        code, _, err = self.run_plan()
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        self.assertIn(str(self.base / "local" / "MyGame"), command)

    def test_prelaunch_files_are_sent_beside_the_plan_and_named_by_their_copies(self):
        (self.caller / "data").mkdir()
        (self.caller / "data" / "config.json").write_text("{}", encoding="utf-8")
        code, _, err = self.run_plan("--prelaunch-file", "data/config.json=settings/config.json")
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        inbox = self.host.inboxes[0]
        self.assertIn("2-config.json=settings/config.json", command)
        self.assertTrue((inbox / "2-config.json").is_file())

    def test_a_pipeline_file_is_sent_with_its_plans_and_names_them_by_their_copies(self):
        pipeline = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": [{"stage": "run", "plan": "open.plan.json", "args": {"app": "a"}}]}
        file = self.caller / "p1.pipeline.json"
        core.atomic_write_json(file, pipeline)
        code, _, err = run("pipeline", str(file), "--project-dir", str(self.base / "local" / "MyGame"), "--host", "mac")
        self.assertEqual(0, code, err)
        (command,) = self.host.started
        inbox = self.host.inboxes[0]
        sent = json.loads(Path(command[2]).read_text(encoding="utf-8"))
        self.assertEqual(f"{inbox}/", command[2][:len(str(inbox)) + 1])
        self.assertEqual("1-open.plan.json", sent["stages"][0]["plan"])
        self.assertEqual(PLAN, json.loads((inbox / "1-open.plan.json").read_text(encoding="utf-8")))
        self.assertEqual(["--project-dir", "/Users/me/Projects/MyGame"], command[3:5])

    def test_a_file_that_is_not_on_the_caller_is_blocked_before_anything_is_sent(self):
        pipeline = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": [{"stage": "run", "plan": str(self.plan), "args": {"app": "a"},
                                                                  "prelaunchFiles": [{"local": "absent.json", "remote": "a.json"}]}]}
        file = self.caller / "p1.pipeline.json"
        core.atomic_write_json(file, pipeline)
        self.host.directories.add("/Users/me/Projects/Sample")  # the name comes from candidate.repo when the caller names no project
        code, _, err = run("pipeline", str(file), "--host", "mac")
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("absent.json", err)
        self.assertEqual([], self.host.started)


class ProjectNameTests(unittest.TestCase):
    def test_the_name_is_the_final_folder_of_the_unity_project_else_the_last_segment_of_the_repository(self):
        self.assertEqual([("MyGame", "the Unity project folder name"), ("other", "candidate.repo")],
                         submit.project_names(Path("/work/checkouts/MyGame"), "owner/other"))
        self.assertEqual([("MyGame", "the Unity project folder name")], submit.project_names(Path("/work/MyGame"), "MyGame"), "no repeats")
        for repo in ("owner/MyGame", "https://example.com/owner/MyGame.git", "git@example.com:owner/MyGame.git", "MyGame", "owner\\MyGame/"):
            self.assertEqual([("MyGame", "candidate.repo")], submit.project_names(None, repo), repo)

    def test_the_path_below_the_root_uses_the_notation_of_the_hosts_system(self):
        self.assertEqual("/Users/me/Projects/MyGame", submit.join_path("/Users/me/Projects/", "MyGame", "macos"))
        self.assertEqual("D:\\Work\\MyGame", submit.join_path("D:\\Work", "MyGame", "windows"))

    def test_a_windows_host_is_asked_with_its_recorded_python_and_not_at_all_without_one(self):
        calls = []
        answer = [CommandResult(3)]

        class Win(Transport):
            kind = "ssh"

            def run(self, command, timeout_sec, cwd=None):
                calls.append(list(command))
                return answer[0]

            put = get = lambda self, *_: CommandResult(0)

        entry = config.HostEntry("win", "ssh", "windows", Path("D:/art"), ssh_target="me@win", python="C:/Python/python.exe")
        prep = remote.Remote(Win(), ("gdv",))
        self.assertIs(False, submit.is_directory(prep, entry, "D:\\Work\\MyGame"))
        self.assertEqual("C:/Python/python.exe", calls[0][0])
        answer[0] = CommandResult(0)
        self.assertIs(True, submit.is_directory(prep, entry, "D:\\Work\\MyGame"))
        for failed in (CommandResult(1, "", "'C:/Python/python.exe' is not recognized as an internal or external command"),
                       CommandResult(2, "", "refused to send the command"), CommandResult(255, "", "ssh: connect failed")):
            answer[0] = failed
            with self.assertRaises(submit.SubmitError) as caught:
                submit.is_directory(prep, entry, "D:\\Work\\MyGame")
            self.assertIn(failed.stderr, str(caught.exception))
        self.assertIsNone(submit.is_directory(prep, config.HostEntry("win", "ssh", "windows", Path("D:/art"), ssh_target="me@win"), "D:\\Work\\X"))


class RewrittenPathTests(HostTestCase):
    GIT_BASH = "C:/Program Files/Git/Users/me/Projects/MyGame"

    def test_the_prefix_git_bash_puts_before_a_posix_path_is_recognised(self):
        for path in (self.GIT_BASH, "C:\\Program Files\\Git\\Users\\me", "c:/program files/git/Users/me", "C:/Program Files (x86)/Git/Users/me",
                     "C:/msys64/Users/me"):
            self.assertTrue(remote.rewritten_by_local_shell(path, "windows"), path)
        for real in ("C:\\Git\\Game", "D:/git/Unity", "C:/Git", "C:\\msys64\\work", "C:/Program Files/Git/work/Game", "D:/cygwin64/home2/x"):
            self.assertFalse(remote.rewritten_by_local_shell(real, "windows"), real)
        self.assertFalse(remote.rewritten_by_local_shell("D:/Projects/MyGame", "windows"), "a real path on a Windows host")

    def test_a_suspect_path_on_a_windows_host_is_only_a_warning(self):
        config.add_ssh_host("win", "me@win", "windows", "D:/art")
        entry = config.find_host("win")
        entry = config.HostEntry(**{**entry.__dict__, "valtrion_command": "C:/v/valtrion.exe"})
        err = io.StringIO()
        remote._WARNED.clear()  # noqa: SLF001
        with contextlib.redirect_stderr(err):
            target = remote.remote_for(entry, "C:/Program Files/Git/Users/me/x", transport=mock.Mock())
            remote.remote_for(entry, "C:/Program Files/Git/Users/me/x", transport=mock.Mock())
        self.assertIsNotNone(target)
        self.assertIn("looks rewritten by the local shell", err.getvalue())
        self.assertEqual(1, err.getvalue().count("looks rewritten"), "once per process")
        remote.remote_for(entry, "C:/Git/Game", transport=mock.Mock())  # a real folder: silent, accepted

    def test_hosts_add_checks_the_shape_of_the_projects_root(self):
        for root, os_name in (("Projects", "macos"), ("C:/Program Files/Git/Users/me/Projects", "macos"), ("/Users/me", "windows"),
                              ("D:/Program Files/Git/Users/me/x", "windows")):
            with self.subTest(root=root), self.assertRaises(config.ConfigError):
                config.add_ssh_host("h", "me@h", os_name, "/art" if os_name != "windows" else "D:/art", projects_root=root)
        for root, os_name in (("~/Projects", "linux"), ("/Users/me/Projects", "macos"), ("D:\\Work", "windows"), ("C:/Git/Work", "windows")):
            config.add_ssh_host("h", "me@h", os_name, "/art" if os_name != "windows" else "D:/art", projects_root=root)
        self.assertFalse(remote.rewritten_by_local_shell("/Users/me/Projects/MyGame", "macos"))
        self.assertTrue(remote.rewritten_by_local_shell("D:/Projects/MyGame", "macos"), "a drive path cannot exist on a posix host")

    def test_remote_for_says_the_path_was_rewritten_instead_of_trying_it(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art")
        with self.assertRaises(remote.RemotePathError) as caught:
            remote.remote_for(config.find_host("mac"), self.GIT_BASH)
        self.assertIn("looks rewritten by the local shell (MSYS path conversion); set GDV_REMOTE_PROJECT or use a script", str(caught.exception))
        self.assertIn(self.GIT_BASH, str(caught.exception))

    def test_a_command_with_such_a_path_is_blocked_with_that_reason(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art")
        code, _, err = run("doctor", "--host", "mac", "--remote-project", self.GIT_BASH)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("looks rewritten by the local shell", err)
        self.assertNotIn("unreachable", err)

    def test_the_host_probe_reports_the_rewritten_path_as_the_reason_not_as_unreachable(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art")

        def factory(entry):
            return remote.remote_for(entry, self.GIT_BASH)

        probe = remote.make_probe(lambda: {}, factory, {"mac": config.find_host("mac")}, home=self.home)
        answer = probe(HostInfo("mac", "ssh", "macos", "me@mac"))
        self.assertIn("looks rewritten by the local shell", answer.reason)
        self.assertNotIn("unreachable", answer.reason)

    def test_a_run_pinned_to_a_host_with_such_a_path_says_so_in_the_reason_per_host(self):
        config.add_ssh_host("mac", "me@mac", "macos", "/Users/me/art")
        plan = self.base / "open.plan.json"
        core.atomic_write_json(plan, PLAN)
        with mock.patch.object(commands, "local_report", return_value={"host": {"id": "this-machine", "os": "windows"}, "capabilities": {}, "devices": []}):
            code, _, err = run("run", str(plan), "--repo", "Sample", "--sha", SHA, "--app", "a", "--platform", "ios", "--host", "mac",
                               "--remote-project", self.GIT_BASH)
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("looks rewritten by the local shell", err)
        self.assertNotIn("unreachable", err)


class ReceivingSideTests(HostTestCase):
    def test_workroot_inbox_makes_a_new_private_folder_under_the_jobs_folder_each_time(self):
        with mock.patch.dict(os.environ, {"GDV_WORK_ROOT": str(self.base / "work")}):
            first = json.loads(run("workroot", "inbox")[1])["inbox"]
            second = json.loads(run("workroot", "inbox")[1])["inbox"]
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertTrue(Path(path).is_dir())
            self.assertEqual(self.base / "work" / "jobs", Path(path).parent)
            self.assertTrue(Path(path).name.startswith("inbox-"))

    def test_a_sent_run_is_blocked_when_its_project_folder_is_not_on_this_host(self):
        pipeline = {"schema": "gdv.pipeline/1", "id": "p1", "candidate": {"repo": "Sample", "profile": "Validation"},
                    "device": {"platform": "android"}, "stages": [{"stage": "run", "plan": "open.plan.json", "args": {"app": "a"}}]}
        core.atomic_write_json(self.base / "p1.json", pipeline)
        core.atomic_write_json(self.base / "open.plan.json", PLAN)
        code, _, err = run("pipeline", str(self.base / "p1.json"), "--local", "--transport", "ssh", "--project-dir", str(self.base / "nope"))
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertIn("does not exist on host", err)


if __name__ == "__main__":
    unittest.main()
