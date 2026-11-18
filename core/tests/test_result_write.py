"""`valtrion result write`: the result of a run the project made itself, and the plan.id of a build-only result."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from valtrion import cli, constants, pipeline
from valtrion.schema import validate_file


def git(root: Path, *arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    done = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *arguments],
                          capture_output=True, text=True, check=True, env=environment)
    return done.stdout.strip()


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ResultWriteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve() / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "a.txt").write_text("x\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.sha = git(self.root, "rev-parse", "HEAD")
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        self.results = Path(out.name)
        self.note = self.results / "note.xml"
        self.note.write_text("<x/>", encoding="utf-8")

    def write(self, name, *extra, expect=0):
        code, out, err = run("result", "write", name, "--out", str(self.results / "out"), "--repo-dir", str(self.root), *extra)
        self.assertEqual(expect, code, err)
        return out.strip()

    def test_written_documents_are_valid_and_carry_the_name(self):
        path = self.write("editmode.Game.Tests", "--status", "PASS", "--count", "passed=12", "--artifact", f"xml={self.note}")
        self.assertEqual(("gdv.result/1", []), validate_file(Path(path)))
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual("editmode.Game.Tests", document["plan"]["id"])
        self.assertEqual(self.sha, document["candidate"]["sha"])
        self.assertFalse(document["candidate"]["dirty"])
        self.assertEqual(("editor", "editor"), (document["target"]["platform"], document["target"]["evidence"]))
        self.assertEqual(12, document["metrics"][0]["samples"][0])
        self.assertEqual("xml", document["artifacts"][0]["kind"])

    def test_any_name_and_platform_are_free_and_evidence_follows_the_platform(self):
        document = json.loads(Path(self.write("my-script:smoke", "--status", "PASS", "--platform", "windows")).read_text(encoding="utf-8"))
        self.assertEqual(("windows", "host"), (document["target"]["platform"], document["target"]["evidence"]))

    def test_a_dirty_tree_is_recorded(self):
        (self.root / "a.txt").write_text("edited\n", encoding="utf-8")
        document = json.loads(Path(self.write("tests", "--status", "PASS")).read_text(encoding="utf-8"))
        self.assertTrue(document["candidate"]["dirty"])

    def test_the_output_folder_inside_the_repository_is_not_dirt(self):
        folder = self.root / "results"
        code, _, err = run("result", "write", "tests", "--status", "PASS", "--out", str(folder), "--repo-dir", str(self.root))
        self.assertEqual(0, code, err)
        document = json.loads(next(folder.glob("*.json")).read_text(encoding="utf-8"))
        self.assertFalse(document["candidate"]["dirty"])

    def test_dirty_flag_can_only_make_it_worse(self):
        path = self.write("tests", "--status", "PASS", "--dirty")
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertTrue(document["candidate"]["dirty"])
        self.assertIn("marked dirty by --dirty", document["unverified"])

    def test_the_same_inputs_give_the_same_file_and_other_names_other_files(self):
        first = self.write("a", "--status", "PASS")
        again = self.write("a", "--status", "PASS")
        other = self.write("b", "--status", "PASS")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(2, len(list((self.results / "out").glob("*.json"))))

    def test_the_host_is_part_of_the_file_name(self):
        one = self.write("tests", "--status", "PASS", "--host-id", "h" * 40 + "a")
        two = self.write("tests", "--status", "PASS", "--host-id", "h" * 40 + "b")
        self.assertNotEqual(one, two)

    def test_inputs_that_cannot_be_true_are_refused(self):
        self.write("tests", "--status", "FAIL", expect=1)  # no reason
        self.write("tests", "--status", "PASS", "--reason", "fine", expect=1)
        self.write("tests", "--status", "PASS", "--sha", "0" * 40, expect=1)
        self.write("bad name", "--status", "PASS", expect=1)
        with self.assertRaises(SystemExit):  # argparse refuses a device evidence
            self.write("tests", "--status", "PASS", "--evidence", "physical")
        self.write("tests", "--status", "PASS", "--count", "passed=-1", expect=1)
        self.write("tests", "--status", "PASS", "--artifact", "xml=" + str(self.results / "missing.xml"), expect=1)
        self.write("tests", "--status", "PASS", "--started-utc", "2030-01-01T00:00:00Z", "--finished-utc", "2029-01-01T00:00:00Z", expect=1)
        self.assertEqual([], list((self.results / "out").glob("*.json")))

    def test_a_folder_that_is_not_a_repository_is_blocked(self):
        code, _, err = run("result", "write", "tests", "--status", "PASS", "--out", str(self.results / "out"), "--repo-dir", str(self.results))
        self.assertEqual(constants.EXIT_BLOCKED, code, err)


class BuildResultIdTests(unittest.TestCase):
    def test_a_build_only_pipeline_is_named_build_and_the_platform(self):
        build = {"stage": "build"}
        self.assertEqual("build:windows", pipeline.build_result_id({"device": {"platform": "windows"}, "stages": [build]}))
        self.assertIsNone(pipeline.build_result_id({"device": {"platform": "ios"}, "stages": [build, {"stage": "run", "plan": "p.json"}]}))
        self.assertIsNone(pipeline.build_result_id({"device": {}, "stages": [build]}))
        self.assertIsNone(pipeline.build_result_id({"device": {"platform": "windows"}, "stages": [{"stage": "install"}]}))


if __name__ == "__main__":
    unittest.main()
