"""`valtrion report`: the results in a folder as rows (passed, failed, files, how to run the failed ones again)."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from valtrion import cli, constants, core, report

from .test_pipeline_resume import _result

OTHER_SHA = "d" * 40
STATS = {"n": 1, "median": 1, "p90": 1, "max": 1, "stdev": 0}


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def put(self, name, **changes):
        result = _result(**changes)
        result["runId"] = changes.get("runId", name)
        result.setdefault("plan", {"id": "perf.open", "sha256": "a" * 64})
        core.atomic_write_json(self.folder / f"{name}.result.json", result)
        return result

    def rows(self, *extra):
        code, out, _ = run("report", "--results", str(self.folder), "--json", *extra)
        payload = json.loads(out)
        return code, payload, {(row["name"], row["platform"], row["host"]): row for row in payload["rows"]}

    def test_the_newest_result_of_a_name_platform_and_host_counts(self):
        self.put("old", status=constants.FAIL, finishedUtc="2026-11-04T09:00:00Z")
        self.put("new", finishedUtc="2026-11-04T11:00:00Z")
        code, payload, rows = self.rows()
        self.assertEqual(constants.EXIT_PASS, code)
        self.assertEqual(1, len(rows))
        self.assertEqual("new", next(iter(rows.values()))["runId"])
        self.assertEqual(1, payload["counts"]["PASS"])

    def test_a_failure_gives_the_exit_code_and_lists_files_and_how_to_run_it_again(self):
        self.put("bad", status=constants.FAIL, plan={"id": "editmode.Game.Tests", "sha256": "a" * 64},
                 artifacts=[{"kind": "xml", "path": "Builds/editmode.xml", "sha256": "d" * 64}],
                 metrics=[{"name": "passed", "unit": "count", "samples": [9], "stats": STATS}, {"name": "failed", "unit": "count", "samples": [1], "stats": STATS},
                          {"name": "skipped", "unit": "count", "samples": [2], "stats": STATS}])
        code, _, rows = self.rows()
        row = rows[("editmode.Game.Tests", "android", "win")]
        self.assertEqual(constants.EXIT_FAIL, code)
        self.assertEqual({"passed": 9, "failed": 1, "skipped": 2}, row["counts"])
        self.assertEqual([{"kind": "xml", "path": "Builds/editmode.xml"}], row["artifacts"])
        self.assertEqual(["valtrion unity run --rerun-failed Builds/editmode.xml -- <the same Unity arguments>"], row["rerun"])
        code, out, _ = run("report", "--results", str(self.folder))
        self.assertEqual(constants.EXIT_FAIL, code)
        self.assertIn("| `editmode.Game.Tests` | android | FAIL | 9 passed, 1 failed, 2 skipped |", out)
        self.assertIn("Files of runs that did not pass:", out)
        self.assertIn("valtrion unity run --rerun-failed Builds/editmode.xml", out)

    def test_a_failed_pipeline_points_to_only_failed(self):
        self.put("pipe", status=constants.FAIL, pipelineRun={"pipelineId": "nightly", "stages": []})
        _, _, rows = self.rows()
        (row,) = rows.values()
        self.assertEqual(["valtrion pipeline FILE --only-failed --resume-from pipe   (pipeline nightly)"], row["rerun"])

    def test_a_passing_run_has_nothing_to_run_again(self):
        self.put("ok", artifacts=[{"kind": "xml", "path": "a.xml", "sha256": "d" * 64}])
        _, _, rows = self.rows()
        (row,) = rows.values()
        self.assertEqual([], row["rerun"])

    def test_sha_keeps_the_results_of_one_commit_and_a_dirty_run_says_so(self):
        self.put("one", dirty=True)
        self.put("two", sha=OTHER_SHA, plan={"id": "other", "sha256": "a" * 64})
        _, _, rows = self.rows("--sha", OTHER_SHA)
        self.assertEqual({"other"}, {key[0] for key in rows})
        _, _, rows = self.rows()
        self.assertIn("dirty tree", rows[("perf.open", "android", "win")]["detail"])
        self.assertEqual("PASS", rows[("perf.open", "android", "win")]["status"])

    def test_a_folder_without_results_or_with_a_broken_one_is_not_a_clean_report(self):
        code, _, _ = self.rows()
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.put("ok")
        (self.folder / "broken.json").write_text('{"schema": "gdv.result/1"}', encoding="utf-8")
        code, payload, _ = self.rows()
        self.assertEqual(constants.EXIT_BLOCKED, code)
        self.assertEqual(1, len(payload["ignoredResults"]))
        code, _, err = run("report", "--results", str(self.folder / "missing"))
        self.assertEqual(constants.EXIT_BLOCKED, code, err)

    def test_the_pull_request_block_has_a_heading(self):
        self.put("ok")
        _, out, _ = run("report", "--results", str(self.folder), "--pr")
        self.assertTrue(out.startswith("## Run results"), out)

    def test_a_reused_or_offloaded_result_is_said_in_the_detail(self):
        self.assertIn("reused from run job-a-3-1", report._row(_result(reusedFrom={"runId": "job-a-3-1", "key": "e" * 64})).detail)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
