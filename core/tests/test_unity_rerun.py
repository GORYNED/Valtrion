"""`valtrion unity run --rerun-failed`: the failed names of an NUnit result, their escaping and the command line the fake Unity receives."""

import argparse
import json
import re
import subprocess
import sys
import unittest
from unittest import mock

from valtrion import cli, commands, transport, unityrerun  # noqa: F401  (cli first: it registers the commands)

from .test_host import HostTestCase, run as run_cli
from .test_unity_run import FAKE

RESULT = """<?xml version="1.0" encoding="utf-8"?>
<test-run id="2" result="Failed" total="6" failed="3">
  <test-suite type="Assembly" fullname="Game.Tests" result="Failed">
    <test-suite type="TestFixture" fullname="Game.Tests.Fixture" result="Failed">
      <test-case fullname="Game.Tests.Fixture.Passes" result="Passed" />
      <test-case fullname="Game.Tests.Fixture.Breaks" result="Failed" label="Error" />
      <test-suite type="ParameterizedMethod" fullname="Game.Tests.Fixture.Param" result="Failed">
        <test-case fullname="Game.Tests.Fixture.Param(&quot;a;b&quot;,1.5)" result="Failed" />
        <test-case fullname="Game.Tests.Fixture.Param(&quot;c&quot;,2)" result="Passed" />
        <test-case fullname="Game.Tests.Fixture.Breaks" result="Failed" />
      </test-suite>
    </test-suite>
    <test-suite type="TestFixture" fullname="Game.Tests.OneTime" result="Failed" label="Error">
      <test-case fullname="Game.Tests.OneTime.First" result="Skipped" />
      <test-case fullname="Game.Tests.OneTime.Second" result="Inconclusive" />
    </test-suite>
  </test-suite>
</test-run>
"""
PASSED = '<test-run result="Passed"><test-suite fullname="A" result="Passed"><test-case fullname="A.B" result="Passed"/></test-suite></test-run>'


class RerunTestCase(HostTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.base / "Proj"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.7f1\n", encoding="utf-8")
        self.results = self.base / "results.xml"

    def write(self, text):
        self.results.write_text(text, encoding="utf-8")

    def rerun(self, *unity_args):
        dump = self.base / "dump"
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--no-offload", "--rerun-failed", str(self.results),
                                 "--", FAKE, "-batchmode", "-runTests", "-projectPath", str(self.project), *unity_args, "--dump", str(dump))
        argv = json.loads((dump / "argv.json").read_text()) if (dump / "argv.json").exists() else None
        return code, out, err, argv


class FailedNamesTests(RerunTestCase):
    def test_failed_cases_nested_parameterized_and_setup_failures_without_repeats(self):
        self.write(RESULT)
        names = unityrerun.failed_test_names(self.results)
        self.assertEqual(["Game.Tests.Fixture.Breaks", 'Game.Tests.Fixture.Param("a;b",1.5)', "Game.Tests.OneTime.First", "Game.Tests.OneTime.Second"], names)

    def test_a_failed_suite_without_failed_cases_gives_all_its_cases_and_one_without_cases_is_refused(self):
        self.write('<test-run><test-suite fullname="S" result="Failed"><test-case fullname="S.A" result="Passed"/></test-suite></test-run>')
        self.assertEqual(["S.A"], unityrerun.failed_test_names(self.results))
        self.write('<test-run><test-suite fullname="S" result="Failed"/></test-run>')
        with self.assertRaises(unityrerun.ResultError):
            unityrerun.failed_test_names(self.results)

    def test_unusable_files(self):
        for text in ("not xml", "<other/>", '<!DOCTYPE test-run [<!ENTITY x "y">]><test-run/>'):
            self.write(text)
            with self.assertRaises(unityrerun.ResultError, msg=text):
                unityrerun.failed_test_names(self.results)
        with self.assertRaises(unityrerun.ResultError):
            unityrerun.failed_test_names(self.base / "missing.xml")

    def test_a_file_over_the_cap_is_not_read(self):
        self.write(PASSED)
        old = unityrerun.MAX_RESULT_BYTES
        unityrerun.MAX_RESULT_BYTES = 10
        self.addCleanup(setattr, unityrerun, "MAX_RESULT_BYTES", old)
        with self.assertRaises(unityrerun.ResultError):
            unityrerun.failed_test_names(self.results)


def python_pattern(pattern):
    r"""The pattern as Python's `re` reads it (`\z` is `\Z` before Python 3.14)."""
    return pattern.replace("\\A", "^").replace("\\z", "\\Z")


class EscapeTests(unittest.TestCase):
    NAMES = ['A.B.C(1,"x y")', "A.B.D(1.5)", "A.B.[x]+{2}|?*$^#\\", "A.B.E(a;b)", "A.B.\u00e9\n", "A.B.100%", "A.B.&|<>^!", "A.B.\U0001F600"]

    def test_every_name_matches_only_itself(self):
        for name in self.NAMES[:-1]:  # the last one is a surrogate pair, which Python's `re` cannot match as one character
            pattern = unityrerun.escape_name(name)
            compiled = python_pattern(pattern)
            self.assertTrue(re.fullmatch(compiled, name), name)
            self.assertIsNone(re.search(compiled, name + "x"))
            self.assertIsNone(re.search(compiled, "x" + name))
        self.assertIsNone(re.search(python_pattern(unityrerun.escape_name("A.B.D(1.5)")), "A.B.D(105)"))

    def test_the_value_has_only_characters_that_survive_every_command_line(self):
        value = unityrerun.test_filter(self.NAMES)
        self.assertEqual(len(self.NAMES), len(value.split(";")))
        for char in '^&|<>%"() \t\r\n!\'`$':
            self.assertNotIn(char, value)
        self.assertRegex(value, r"^[A-Za-z0-9_.\\;]+$")

    def test_a_character_beyond_the_bmp_is_a_surrogate_pair(self):
        self.assertEqual("\\A\\uD83D\\uDE00\\z", unityrerun.escape_name("\U0001F600"))

    def test_the_value_passes_list2cmdline_and_quote_cmd_unchanged(self):
        value = unityrerun.test_filter(self.NAMES)
        line = subprocess.list2cmdline(["-testFilter", value])
        self.assertEqual(f"-testFilter {value}", line, "no quotes or backslash doubling were needed")
        quoted = transport.quote_cmd(value)  # must not raise
        self.assertEqual(value, quoted.strip('"'))
        for char in '^&|<>%" ':
            self.assertNotIn(char, quoted.strip('"'))

    def test_the_filter_joins_with_semicolons(self):
        self.assertEqual("\\AA\\.B\\z;\\AC\\z", unityrerun.test_filter(["A.B", "C"]))


class CommandTests(RerunTestCase):
    def test_unity_receives_the_filter_and_the_other_arguments_as_given(self):
        self.write(RESULT)
        code, out, err, argv = self.rerun("-testPlatform", "EditMode", "-assemblyNames", "Game.Tests")
        self.assertEqual(0, code, err)
        self.assertEqual("STATUS PASS", out.strip().splitlines()[-1])
        self.assertIn("running 4 failed test(s)", out)
        self.assertNotIn("--rerun-failed", argv)
        for pair in (["-testPlatform", "EditMode"], ["-assemblyNames", "Game.Tests"]):
            self.assertIn(pair[1], argv)
        self.assertEqual(argv.index("-testFilter") + 1, argv.index(unityrerun.test_filter(unityrerun.failed_test_names(self.results))))
        value = argv[argv.index("-testFilter") + 1]
        self.assertEqual(4, len(value.split(";")))
        self.assertIn('Game\\.Tests\\.Fixture\\.Param\\x28\\x22a\\x3Bb\\x22\\x2C1\\.5\\x29', value)

    def test_the_equals_form(self):
        self.write(RESULT)
        dump = self.base / "dump"
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--no-offload", f"--rerun-failed={self.results}",
                                 "--", FAKE, "-runTests", "-projectPath", str(self.project), "--dump", str(dump))
        self.assertEqual(0, code, err)
        self.assertIn("-testFilter", json.loads((dump / "argv.json").read_text()))

    def test_no_failed_test_runs_nothing_and_is_not_a_pass(self):
        self.write(PASSED)
        code, out, err, argv = self.rerun()
        self.assertEqual(unityrerun.EXIT_NOTHING_TO_RUN, code)
        self.assertIsNone(argv, "Unity must not start")
        self.assertIn("no test was run", out)
        self.assertEqual("STATUS NOTHING_TO_RUN", out.strip().splitlines()[-1])
        self.assertNotIn("PASS", out)

    def test_a_bad_file_is_blocked_and_nothing_runs(self):
        self.write("not xml")
        code, out, err, argv = self.rerun()
        self.assertEqual(2, code)
        self.assertIsNone(argv)
        self.assertEqual("STATUS BLOCKED", out.strip().splitlines()[-1])
        self.results.unlink()
        code, out, err, argv = self.rerun()
        self.assertEqual(2, code)
        self.assertIsNone(argv)

    def test_a_test_filter_of_the_caller_is_refused(self):
        self.write(RESULT)
        for name in ("-testFilter", "-TESTFILTER"):
            code, out, err, argv = self.rerun(name, "A.B")
            self.assertEqual(2, code)
            self.assertIsNone(argv)
            self.assertIn("-testFilter", out)
            self.assertEqual("STATUS BLOCKED", out.strip().splitlines()[-1])

    def test_a_filter_that_is_too_long_is_blocked(self):
        cases = "".join(f'<test-case fullname="Game.Tests.T{n}" result="Failed"/>' for n in range(2000))
        self.write(f"<test-run>{cases}</test-run>")
        code, out, err, argv = self.rerun()
        self.assertEqual(2, code)
        self.assertIsNone(argv)
        self.assertIn("2000 failed tests", out)
        self.assertIn(str(unityrerun.MAX_FILTER_CHARS), out)
        self.assertEqual("STATUS BLOCKED", out.strip().splitlines()[-1])

    def test_unity_arguments_without_run_tests_are_blocked(self):
        self.write(RESULT)
        dump = self.base / "dump"
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--no-offload", "--rerun-failed", str(self.results),
                                 "--", FAKE, "-projectPath", str(self.project), "--dump", str(dump))
        self.assertEqual(2, code)
        self.assertIn("-runTests", out)
        self.assertFalse(dump.exists())

    def test_a_file_that_is_not_utf8_is_refused(self):
        self.results.write_bytes('<?xml version="1.0" encoding="utf-16"?><test-run/>'.encode("utf-16"))
        with self.assertRaises(unityrerun.ResultError):
            unityrerun.failed_test_names(self.results)

    def test_an_abbreviated_option_is_not_taken_for_it(self):
        self.write(RESULT)
        with self.assertRaises(SystemExit):
            run_cli("unity", "run", "--unity", sys.executable, "--rerun", str(self.results), "--", FAKE, "-runTests")

    def test_an_argument_the_host_cannot_quote_ends_blocked_not_in_a_traceback(self):
        class Target:
            def call(self, argv, timeout):
                raise ValueError("argument contains a character that cmd.exe would interpret")

        arguments = argparse.Namespace(command="unity", raw_argv=["unity", "run", "--", "-runTests"])
        with mock.patch.object(commands, "_remote", return_value=Target()):
            code = commands._forward(arguments, mock.Mock(id="win"))
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
