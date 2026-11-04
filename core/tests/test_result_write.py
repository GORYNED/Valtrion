"""`valtrion result write`: results for checks Valtrion does not run itself, and the plan.id of a build-only result."""

import json
import tempfile
import unittest
from pathlib import Path

from valtrion import constants, pipeline
from valtrion.schema import validate_file

from .test_select import RepoCase, git, run, write


class ResultWriteTests(RepoCase):
    def setUp(self):
        super().setUp()
        write(self.root, "src/media/new.cs", "changed\n")
        self.sha = self.commit()
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)
        self.results = Path(self.out.name)

    def write(self, key, *extra, expect=0):
        code, out, err = run("result", "write", key, "--out", str(self.results), "--repo-dir", str(self.root), *extra)
        self.assertEqual(expect, code, err)
        return out.strip()

    def report(self, *extra):
        code, out, _ = run("report", "--base-ref", "main", "--checks", str(self.checks_path), "--results", str(self.results), "--json", *extra)
        return code, json.loads(out)

    def rows(self, payload):
        return {row["key"]: row for row in payload["rows"]}

    def test_written_documents_are_valid_and_carry_the_key(self):
        path = self.write("editmode:Media.Tests", "--status", "PASS", "--count", "passed=12", "--artifact", f"xml={self.checks_path}")
        self.assertEqual(("gdv.result/1", []), validate_file(Path(path)))
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual("editmode:Media.Tests", document["plan"]["id"])
        self.assertEqual(self.sha, document["candidate"]["sha"])
        self.assertFalse(document["candidate"]["dirty"])
        self.assertEqual(("editor", "editor"), (document["target"]["platform"], document["target"]["evidence"]))
        self.assertEqual(12, document["metrics"][0]["samples"][0])
        self.assertEqual("xml", document["artifacts"][0]["kind"])

    def test_report_shows_pass_rows_with_evidence_and_host(self):
        self.write("l0", "--status", "PASS", "--host-id", "win-1")
        self.write("editmode:Media.Tests", "--status", "PASS", "--host-id", "win-1")
        self.write("editmode:Media.More.Tests", "--status", "PASS", "--host-id", "win-1")
        self.write("playmode:Media.Play.Tests", "--status", "FAIL", "--reason", "2 failed", "--host-id", "win-1")
        self.write("player:win64", "--status", "PASS", "--platform", "windows", "--host-id", "win-1")
        code, payload = self.report()
        rows = self.rows(payload)
        self.assertEqual(("PASS", "editor", "win-1"), (rows["editmode:Media.Tests"]["status"], rows["editmode:Media.Tests"]["evidence"], rows["editmode:Media.Tests"]["host"]))
        self.assertEqual("PASS", rows["editmode:Media.More.Tests"]["status"])
        self.assertEqual("PASS", rows["l0"]["status"])
        self.assertEqual("host", rows["player:win64"]["evidence"])
        self.assertEqual(("FAIL", "2 failed"), (rows["playmode:Media.Play.Tests"]["status"], rows["playmode:Media.Play.Tests"]["detail"]))
        self.assertEqual(constants.EXIT_FAIL, code)

    def test_a_dirty_tree_is_not_verified(self):
        write(self.root, "src/media/a.cs", "edited\n")
        self.write("editmode:Media.Tests", "--status", "PASS")
        _, payload = self.report()
        row = self.rows(payload)["editmode:Media.Tests"]
        self.assertEqual("NOT_VERIFIED", row["status"])
        self.assertIn("dirty tree", row["detail"])

    def test_the_output_folder_inside_the_repository_is_not_dirt(self):
        folder = self.root / "results"
        code, _, err = run("result", "write", "l0", "--status", "PASS", "--out", str(folder), "--repo-dir", str(self.root))
        self.assertEqual(0, code, err)
        document = json.loads(next(folder.glob("*.json")).read_text(encoding="utf-8"))
        self.assertFalse(document["candidate"]["dirty"])

    def test_dirty_flag_can_only_make_it_worse(self):
        path = self.write("l0", "--status", "PASS", "--dirty")
        self.assertTrue(json.loads(Path(path).read_text(encoding="utf-8"))["candidate"]["dirty"])

    def test_another_commit_does_not_count(self):
        self.write("l0", "--status", "PASS")
        write(self.root, "src/media/more.cs")
        self.commit()
        _, payload = self.report()
        self.assertEqual("NOT_RUN", self.rows(payload)["l0"]["status"])

    def test_the_same_inputs_give_the_same_file_and_other_assemblies_other_files(self):
        first = self.write("editmode:A", "--status", "PASS")
        again = self.write("editmode:A", "--status", "PASS")
        other = self.write("editmode:B", "--status", "PASS")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(2, len(list(self.results.glob("*.json"))))

    def test_dirty_flag_says_so_and_the_host_is_part_of_the_file_name(self):
        document = json.loads(Path(self.write("l0", "--status", "PASS", "--dirty")).read_text(encoding="utf-8"))
        self.assertIn("marked dirty by --dirty", document["unverified"])
        one = self.write("l0", "--status", "PASS", "--host-id", "h" * 40 + "a")
        two = self.write("l0", "--status", "PASS", "--host-id", "h" * 40 + "b")
        self.assertNotEqual(one, two)

    def test_inputs_that_cannot_be_true_are_refused(self):
        self.write("l0", "--status", "FAIL", expect=1)  # no reason
        self.write("l0", "--status", "PASS", "--reason", "fine", expect=1)
        self.write("player:win64", "--status", "PASS", expect=1)  # platform unknown
        self.write("l0", "--status", "PASS", "--sha", "0" * 40, expect=1)
        self.write("bad key", "--status", "PASS", expect=1)
        with self.assertRaises(SystemExit):  # argparse refuses a device evidence
            self.write("l0", "--status", "PASS", "--evidence", "physical")
        self.write("editmode:A", "--status", "PASS", "--evidence", "host", expect=1)
        self.write("example.perf.open-playback", "--status", "PASS", "--platform", "ios", expect=1)  # a device plan id
        self.write("l0", "--status", "PASS", "--count", "passed=-1", expect=1)
        self.write("l0", "--status", "PASS", "--artifact", "xml=" + str(self.results / "missing.xml"), expect=1)
        self.write("l0", "--status", "PASS", "--started-utc", "2030-01-01T00:00:00Z", "--finished-utc", "2029-01-01T00:00:00Z", expect=1)
        self.assertEqual([], list(self.results.glob("*.json")))


class BuildResultKeyTests(unittest.TestCase):
    def test_a_build_only_pipeline_stands_for_the_player_check(self):
        build = {"stage": "build"}
        base = {"device": {"platform": "windows"}, "stages": [build]}
        self.assertEqual("player:windows", pipeline.build_check_key(base))
        named = {"device": {"platform": "windows"}, "stages": [{"stage": "build", "args": {"checkKey": "player:win64"}}]}
        self.assertEqual("player:win64", pipeline.build_check_key(named))
        other = {"device": {"platform": "windows"}, "stages": [{"stage": "build", "args": {"checkKey": "editmode:A"}}]}
        self.assertEqual("player:windows", pipeline.build_check_key(other))
        self.assertIsNone(pipeline.build_check_key({"device": {"platform": "ios"}, "stages": [build, {"stage": "run", "plan": "p.json"}]}))
        other = {"device": {"platform": "windows"}, "stages": [{"stage": "build", "args": {"checkKey": "editmode:A"}}]}
        self.assertEqual("player:windows", pipeline.build_check_key(other))
        self.assertIsNone(pipeline.build_check_key({"device": {}, "stages": [{"stage": "install"}]}))


if __name__ == "__main__":
    unittest.main()
