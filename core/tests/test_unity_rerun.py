"""`valtrion unity run --rerun-failed`: the failed names of an NUnit result, their escaping and the command line the fake Unity receives."""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from valtrion import cli, unityrerun  # noqa: F401  (cli first: it registers the commands)

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
                                 "--", FAKE, "-batchmode", "-projectPath", str(self.project), *unity_args, "--dump", str(dump))
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


class EscapeTests(unittest.TestCase):
    def test_every_name_matches_only_itself(self):
        names = ['A.B.C(1,"x y")', "A.B.D(1.5)", "A.B.[x]+{2}|?*$^#\\", "A.B.E(a;b)", "A.B.é\n"]
        for name in names:
            pattern = unityrerun.escape_name(name)
            self.assertNotIn(";", pattern)
            self.assertTrue(re.fullmatch(pattern, name), name)
            self.assertIsNone(re.search(pattern, name + "x"))
            self.assertIsNone(re.search(pattern, "x" + name))
        self.assertIsNone(re.search(unityrerun.escape_name("A.B.D(1.5)"), "A.B.D(105)"))

    def test_the_filter_joins_with_semicolons(self):
        self.assertEqual("^A\\.B$;^C$", unityrerun.test_filter(["A.B", "C"]))


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
        self.assertIn('Game\\.Tests\\.Fixture\\.Param\\("a\\x3Bb",1\\.5\\)', value)

    def test_the_equals_form(self):
        self.write(RESULT)
        dump = self.base / "dump"
        code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", "--no-offload", f"--rerun-failed={self.results}",
                                 "--", FAKE, "-projectPath", str(self.project), "--dump", str(dump))
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


if __name__ == "__main__":
    unittest.main()
