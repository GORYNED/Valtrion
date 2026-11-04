import json
import sys
import unittest

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
                externalui.expand([f"{{arg:{name}}}"], {}, REQUEST["args"])

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


if __name__ == "__main__":
    unittest.main()
