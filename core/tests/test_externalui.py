import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from valtrion import constants, externalui

REQUEST = {"id": "run1-ui-0-2", "action": "touch.tap", "args": {"target": "footer.3", "count": 2, "nested": {"a": 1}}}


def python(code):
    return [sys.executable, "-c", code]


class ExpandTests(unittest.TestCase):
    def test_placeholders_are_replaced_inside_one_element_and_never_split(self):
        values = {"device": "U 1", "app": "com.example.app", "action": "touch.tap", "request": "r1"}
        argv = externalui.expand(["tool", "{device}", "--to={app}:{arg:target}", "x{arg:count}"], values, REQUEST["args"])
        self.assertEqual(["tool", "U 1", "--to=com.example.app:footer.3", "x2"], argv)

    def test_a_missing_or_structured_argument_is_an_error_not_an_empty_string(self):
        for name in ("absent", "nested"):
            with self.assertRaises(KeyError):
                externalui.expand(["tool", f"{{arg:{name}}}"], {}, REQUEST["args"])

    def test_request_ids_are_file_name_safe(self):
        self.assertEqual("run1-ui-0-2", externalui.safe_request_id("run1-ui-0-2"))
        for bad in ("../x", "a/b", "", None, 5, "a b"):
            self.assertIsNone(externalui.safe_request_id(bad))


class AnswerTests(unittest.TestCase):
    def answer(self, command, request=REQUEST, **spec):
        return externalui.answer({"command": command, **spec}, request, "DEV", "com.example.app", None)

    def test_exit_zero_is_pass_and_the_environment_carries_the_request(self):
        code = "import os,json;print(json.dumps({'message': os.environ['GDV_UI_DEVICE']+' '+os.environ['GDV_UI_ACTION']+' '+" \
               "os.environ['GDV_UI_ARGS']}))"
        reply = self.answer(python(code))
        self.assertEqual(constants.PASS, reply.status)
        self.assertEqual("DEV touch.tap " + json.dumps(REQUEST["args"], sort_keys=True), reply.message)

    def test_a_non_zero_exit_is_a_failure_and_never_pass(self):
        reply = self.answer(python("import sys;print('no tap');sys.exit(1)"))
        self.assertEqual(constants.FAIL, reply.status)
        self.assertEqual("no tap", reply.message)
        reply = self.answer(python("import sys;print('{\"status\": \"PASS\", \"message\": \"lie\"}');sys.exit(1)"))
        self.assertEqual(constants.FAIL, reply.status)

    def test_the_command_can_make_a_failure_more_precise_or_a_pass_stricter(self):
        reply = self.answer(python("import sys;print('{\"status\": \"NEEDS_PERMISSION\", \"message\": \"unlock\"}');sys.exit(3)"))
        self.assertEqual((constants.NEEDS_PERMISSION, "unlock"), (reply.status, reply.message))
        reply = self.answer(python("print('{\"status\": \"BLOCKED\", \"message\": \"no runner\"}')"))
        self.assertEqual(constants.BLOCKED, reply.status)

    def test_a_command_that_hangs_ends_as_timeout(self):
        reply = self.answer(python("import time;time.sleep(30)"), timeoutSec=1)
        self.assertEqual(constants.TIMEOUT, reply.status)

    def test_a_missing_program_or_argument_is_blocked_or_failed_not_raised(self):
        self.assertEqual(constants.BLOCKED, self.answer(["x", "{arg:absent}"]).status)
        self.assertNotEqual(constants.PASS, self.answer(["valtrion-no-such-program-xyz"]).status)
        self.assertEqual(constants.BLOCKED, self.answer([]).status)

class AppBackgroundActionTests(unittest.TestCase):
    """The host side of the `app.background` plan step: the request reaches the project's command as data, and its answer is the reply."""

    def test_the_seconds_reach_the_command_as_an_argument_and_the_environment(self):
        script = (
            "import json, os, sys\n"
            "args = json.loads(os.environ['GDV_UI_ARGS'])\n"
            "assert os.environ['GDV_UI_ACTION'] == 'app.background'\n"
            "assert sys.argv[1] == '--seconds=2.5', sys.argv\n"
            "assert args == {'seconds': 2.5}, args\n"
            "print(json.dumps({'status': 'PASS', 'message': 'back after ' + sys.argv[1]}))\n")
        request = {"id": "run-ui-0-1", "action": "app.background", "args": {"seconds": 2.5}}
        spec = {"command": [sys.executable, "-c", script, "--seconds={arg:seconds}"], "timeoutSec": 30}
        reply = externalui.answer(spec, request, "device-1", "app", None)
        self.assertEqual((constants.PASS, "back after --seconds=2.5"), (reply.status, reply.message))

    def test_a_command_that_cannot_bring_the_app_back_ends_the_step_as_failed(self):
        request = {"id": "run-ui-0-1", "action": "app.background", "args": {"seconds": 5}}
        spec = {"command": [sys.executable, "-c", "import sys; print('the app did not return'); sys.exit(3)"], "timeoutSec": 30}
        reply = externalui.answer(spec, request, "device-1", "app", None)
        self.assertEqual(constants.FAIL, reply.status)

    def test_a_missing_seconds_argument_is_blocked_and_runs_nothing(self):
        request = {"id": "run-ui-0-1", "action": "app.background", "args": {}}
        spec = {"command": [sys.executable, "-c", "raise SystemExit(1)", "--seconds={arg:seconds}"], "timeoutSec": 30}
        reply = externalui.answer(spec, request, "device-1", "app", None)
        self.assertEqual(constants.BLOCKED, reply.status)


class UntrustedValueTests(unittest.TestCase):
    """`action`, `args` and the id come from the app: they never choose the program, an option or a path, and never abort the run."""

    def answer(self, command, request, **kwargs):
        return externalui.answer({"command": command}, request, "DEV", "com.example.app", None, **kwargs)

    def marker_command(self, folder, *extra):
        marker = Path(folder) / "ran"
        return [sys.executable, "-c", "import sys,pathlib;pathlib.Path(sys.argv[1]).write_text('x')", str(marker), *extra], marker

    def test_a_nul_byte_in_the_action_an_argument_or_the_id_is_blocked_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            command, marker = self.marker_command(folder, "{action}", "{arg:x}", "{request}")
            for request in ({"id": "r1", "action": "a\u0000b", "args": {"x": "1"}},
                            {"id": "r1", "action": "a", "args": {"x": "a\u0000b"}},
                            {"id": "r\u00001", "action": "a", "args": {"x": "1"}}):
                reply = self.answer(command, request)
                self.assertEqual(constants.BLOCKED, reply.status, request)
                self.assertIn("NUL", reply.message)
                self.assertFalse(marker.exists())
            # also when the command takes none of them: the environment carries them too
            plain, marker = self.marker_command(folder)
            self.assertEqual(constants.BLOCKED, self.answer(plain, {"id": "r1", "action": "a\u0000b"}).status)
            self.assertFalse(marker.exists())

    def test_overlong_values_are_blocked(self):
        long = "a" * (externalui.VALUE_LIMIT + 1)
        self.assertEqual(constants.BLOCKED, self.answer(["x", "{arg:x}"], {"id": "r1", "action": "a", "args": {"x": long}}).status)
        self.assertEqual(constants.BLOCKED, self.answer(["x"], {"id": "r1", "action": long}).status)
        many = {f"k{i}": "v" * 100 for i in range(externalui.ARGS_LIMIT // 100)}
        self.assertEqual(constants.BLOCKED, self.answer(["x"], {"id": "r1", "action": "a", "args": many}).status)

    def test_a_value_the_environment_cannot_carry_is_blocked_not_raised(self):
        reply = self.answer(python("pass"), REQUEST, environ={"BAD": "a\u0000b"})
        self.assertEqual(constants.BLOCKED, reply.status)

    def test_a_placeholder_in_the_program_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            _, marker = self.marker_command(folder)
            for program in ("{action}", "{arg:x}", "{device}", "{app}", "{request}", "tools/{arg:x}.py"):
                reply = self.answer([program, "go"], {"id": "r1", "action": str(marker), "args": {"x": str(marker)}})
                self.assertEqual(constants.BLOCKED, reply.status, program)
                self.assertIn("program", reply.message)
            self.assertFalse(marker.exists())

    def test_a_value_that_is_a_whole_element_cannot_start_with_a_dash(self):
        request = {"id": "r1", "action": "-rf", "args": {"x": "--eval=boom"}}
        for command in (["tool", "{action}"], ["tool", "{arg:x}"]):
            self.assertEqual(constants.BLOCKED, self.answer(command, request).status)
        self.assertEqual(["tool", "--flag=--eval=boom", "-k", "x-rf"],
                         externalui.expand(["tool", "--flag={arg:x}", "-k", "x{action}"], {"action": "-rf"}, request["args"]),
                         "a dash inside an element, or in an option the project wrote, is data")

    def test_a_value_in_a_path_cannot_leave_it(self):
        for bad in ("../x", "a/b", "a\\b", "..", "x..y"):
            with self.assertRaises(externalui.UnsafeValue, msg=bad):
                externalui.expand(["tool", "out/{arg:name}.png"], {}, {"name": bad})
            with self.assertRaises(externalui.UnsafeValue, msg=bad):
                externalui.expand(["tool", "out\\{arg:name}"], {}, {"name": bad})
        self.assertEqual(["tool", "out/shot-1.png"], externalui.expand(["tool", "out/{arg:name}.png"], {}, {"name": "shot-1"}))
        # an element without a path separator may hold anything the app sent, except what the other rules refuse
        self.assertEqual(["tool", "a/b"], externalui.expand(["tool", "{arg:name}"], {}, {"name": "a/b"}))

    def test_the_host_values_may_have_path_characters(self):
        self.assertEqual(["tool", "/dev/x/y"], externalui.expand(["tool", "/dev/{device}/y"], {"device": "x"}, {}))
        self.assertEqual(["tool", "C:\\dev"], externalui.expand(["tool", "{device}"], {"device": "C:\\dev"}, {}))


class RunControlTests(unittest.TestCase):
    def answer(self, command, **kwargs):
        spec = {"command": command, "timeoutSec": kwargs.pop("timeoutSec", 30)}
        return externalui.answer(spec, REQUEST, "DEV", "com.example.app", None, **kwargs)

    def test_only_the_tail_of_a_huge_output_is_read_and_its_last_line_answers(self):
        code = "import sys;sys.stdout.write('x' * 1000 + chr(10)) ;[sys.stdout.write('y' * 1023 + chr(10)) for _ in range(4000)];" \
               "print('{\"status\": \"BLOCKED\", \"message\": \"tail\"}')"
        reply = self.answer(python(code))
        self.assertEqual((constants.BLOCKED, "tail"), (reply.status, reply.message))

    def test_a_named_stall_is_a_timeout(self):
        reply = self.answer(python("import sys;print('{\"status\": \"STALL\", \"message\": \"stuck\"}');sys.exit(1)"))
        self.assertEqual((constants.TIMEOUT, "stuck"), (reply.status, reply.message))
        reply = self.answer(python("print('{\"status\": \"STALL\", \"message\": \"stuck\"}')"))
        self.assertEqual(constants.TIMEOUT, reply.status)
        self.assertEqual(constants.FAIL, self.answer(python("import sys;print('{\"status\": \"nonsense\"}');sys.exit(2)")).status)

    def test_the_commands_timeout_is_capped_by_what_is_left_of_the_stage(self):
        began = time.monotonic()
        reply = self.answer(python("import time;time.sleep(30)"), timeoutSec=60, remaining_sec=1.5)
        self.assertEqual(constants.TIMEOUT, reply.status)
        self.assertLess(time.monotonic() - began, 20)
        self.assertEqual(constants.BLOCKED, self.answer(python("pass"), remaining_sec=0).status)

    def test_a_cancel_request_stops_the_command(self):
        with tempfile.TemporaryDirectory() as folder:
            cancel = Path(folder) / "cancel.requested"
            cancel.write_text("x", encoding="utf-8")
            began = time.monotonic()
            reply = self.answer(python("import time;time.sleep(30)"), cancel_file=cancel)
            self.assertEqual(constants.BLOCKED, reply.status)
            self.assertIn("cancel", reply.message)
            self.assertLess(time.monotonic() - began, 20)

    def test_the_statuses_a_command_may_name_are_known_contract_statuses(self):
        self.assertTrue(set(externalui.COMMAND_STATUSES) <= set(constants.STATUSES))
        self.assertNotIn(constants.STALL, externalui.COMMAND_STATUSES)


if __name__ == "__main__":
    unittest.main()
