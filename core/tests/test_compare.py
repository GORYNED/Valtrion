import copy
import json
import tempfile
import unittest
from pathlib import Path

from valtrion import compare, constants, core
from valtrion.schema import Validator

FIXTURES = Path(__file__).resolve().parent.parent.parent / "contracts" / "fixtures" / "valid"
SHA = "7c46db7" + "0" * 33


def result(samples, **overrides):
    document = json.loads((FIXTURES / "result.json").read_text(encoding="utf-8"))
    document["status"], document["reason"] = "PASS", None
    document["layers"] = {"scenario": "PASS"}
    document["target"].update({"deviceClass": "ipad-a14", "osMajor": 27, "evidence": "physical"})
    document["candidate"].update({"profile": "Internal", "unity": "6000.6.7f1", "dirty": False, "sha": SHA})
    document["plan"] = {"id": "perf.open", "sha256": "a" * 64}
    document["metrics"] = [{"name": "openMs", "unit": "ms", "samples": list(samples), "stats": compare.compute_stats(samples)}]
    document.update(overrides)
    return document


def plan(**metric):
    spec = {"unit": "ms", "direction": "lower", "regressionPct": 10, "minSamples": 3}
    spec.update(metric)
    return {"schema": "gdv.plan/1", "id": "perf.open", "kind": "performance", "warmup": 1, "metrics": {"openMs": spec}}


def baseline(samples, **key):
    base = result(samples)
    document = compare.build_baseline(base, plan(), "2026-10-29T12:00:00.000Z")
    document["key"].update(key)
    return document


BASE = [999, 100, 101, 100, 99]  # the first sample is warmup and is dropped


class StatsTests(unittest.TestCase):
    def test_stats(self):
        stats = compare.compute_stats([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual((10, 5.5, 9, 10), (stats["n"], stats["median"], stats["p90"], stats["max"]))
        self.assertAlmostEqual(3.0276, stats["stdev"], places=3)
        self.assertEqual(0, compare.compute_stats([])["n"])
        self.assertEqual(0, compare.compute_stats([4])["stdev"])

    def test_generated_documents_satisfy_the_contract(self):
        validator = Validator()
        self.assertEqual([], validator.validate(result(BASE), "gdv.result/1"))
        self.assertEqual([], validator.validate(baseline(BASE), "gdv.baseline/1"))


class VerdictMatrixTests(unittest.TestCase):
    def verdict(self, samples, base=None, spec=None, **result_overrides):
        return compare.compare(result(samples, **result_overrides), spec or plan(), baseline(BASE) if base is None else base)

    def test_pass_within_the_threshold(self):
        self.assertEqual("PASS", self.verdict([5, 103, 102, 104]).verdict)

    def test_regression_beyond_the_threshold(self):
        verdict = self.verdict([5, 120, 121, 119])
        self.assertEqual("REGRESSION", verdict.verdict)
        self.assertEqual(constants.EXIT_FAIL, verdict.exit_code)
        self.assertIn("worse", verdict.details[0])

    def test_improvement_beyond_the_threshold(self):
        self.assertEqual("IMPROVED", self.verdict([5, 80, 81, 79]).verdict)

    def test_direction_higher_is_better(self):
        higher = plan(direction="higher")
        base = baseline(BASE)
        base["metrics"]["openMs"]["direction"] = "higher"
        self.assertEqual("REGRESSION", compare.compare(result([5, 80, 81, 79]), higher, base).verdict)
        self.assertEqual("IMPROVED", compare.compare(result([5, 120, 121, 119]), higher, base).verdict)

    def test_budget_breach_is_a_regression_even_when_the_baseline_is_just_as_bad(self):
        spec = plan(budget=110)
        verdict = compare.compare(result([5, 120, 121, 119]), spec, baseline([5, 120, 121, 119]))
        self.assertEqual("REGRESSION", verdict.verdict)
        self.assertIn("budget", verdict.details[0])

    def test_noisy_when_the_baseline_spread_exceeds_the_threshold(self):
        noisy_base = baseline([0, 60, 100, 140, 100, 90])
        verdict = compare.compare(result([5, 100, 101, 99]), plan(), noisy_base)
        self.assertEqual("NOISY", verdict.verdict)
        self.assertIn("increase repeat", verdict.details[0])
        self.assertEqual(constants.EXIT_BLOCKED, verdict.exit_code)

    def test_noisy_when_the_run_itself_is_too_spread(self):
        self.assertEqual("NOISY", self.verdict([5, 60, 140, 100, 90]).verdict)

    def test_too_few_samples_after_warmup_asks_for_more_repeats(self):
        verdict = self.verdict([5, 100, 101])
        self.assertEqual("NOISY", verdict.verdict)
        self.assertIn("2 sample(s) after warmup", verdict.details[0])

    def test_not_comparable_when_any_key_field_differs(self):
        for field, value in (("deviceClass", "ipad-m1"), ("osMajor", 26), ("buildProfile", "Release"), ("unityVersion", "6000.7.0f1"),
                             ("planSha256", "b" * 64)):
            with self.subTest(field):
                verdict = compare.compare(result([5, 100, 100, 100]), plan(), baseline(BASE, **{field: value}))
                self.assertEqual("NOT_COMPARABLE", verdict.verdict)
                self.assertIn(field, verdict.details[0])
                self.assertEqual(constants.EXIT_BLOCKED, verdict.exit_code)

    def test_not_comparable_across_evidence_classes_and_for_editor_runs(self):
        emulator = result([5, 100, 100, 100])
        emulator["target"]["evidence"] = "emulator"
        self.assertEqual("NOT_COMPARABLE", compare.compare(emulator, plan(), baseline(BASE)).verdict)
        editor = result([5, 100, 100, 100])
        editor["target"]["evidence"] = "editor"
        self.assertEqual("NOT_COMPARABLE", compare.compare(editor, plan(), baseline(BASE)).verdict)

    def test_a_budget_breach_is_a_regression_even_without_a_comparable_baseline(self):
        spec = plan(budget=110)
        breach = result([5, 120, 121, 119])
        self.assertEqual("REGRESSION", compare.compare(breach, spec, None).verdict)
        self.assertEqual("REGRESSION", compare.compare(breach, spec, baseline(BASE, osMajor=26)).verdict)
        self.assertEqual("NOT_COMPARABLE", compare.compare(result([5, 100, 100, 100]), spec, None).verdict)

    def test_not_comparable_without_a_baseline_or_for_a_failed_run(self):
        self.assertEqual("NOT_COMPARABLE", compare.compare(result([5, 100, 100, 100]), plan(), None).verdict)
        failed = result([5, 100, 100, 100], status="FAIL", reason="boom")
        self.assertEqual("NOT_COMPARABLE", compare.compare(failed, plan(), baseline(BASE)).verdict)

    def test_a_metric_missing_from_the_result_or_the_baseline_is_not_comparable(self):
        missing = result([5, 100, 100, 100])
        missing["metrics"] = []
        self.assertEqual("NOT_COMPARABLE", compare.compare(missing, plan(), baseline(BASE)).verdict)
        base = baseline(BASE)
        base["metrics"] = {}
        self.assertEqual("NOT_COMPARABLE", compare.compare(result([5, 100, 100, 100]), plan(), base).verdict)

    def test_precedence_regression_beats_not_comparable_and_noisy(self):
        spec = plan()
        spec["metrics"]["other"] = {"unit": "ms", "direction": "lower", "regressionPct": 10, "minSamples": 3}
        document = result([5, 130, 131, 129])
        verdict = compare.compare(document, spec, baseline(BASE))  # openMs regressed, "other" is missing from the result
        self.assertEqual("REGRESSION", verdict.verdict)
        self.assertTrue(any("other" in line for line in verdict.details))

    def test_no_threshold_is_informational_pass(self):
        spec = plan()
        del spec["metrics"]["openMs"]["regressionPct"]
        self.assertEqual("PASS", compare.compare(result([5, 500, 500, 500]), spec, baseline(BASE)).verdict)


def baseline_of(plan_id, samples, **key):
    """A baseline for another plan id."""
    base = result(samples)
    base["plan"] = {"id": plan_id, "sha256": "a" * 64}
    document = compare.build_baseline(base, plan(), "2026-10-29T12:00:00.000Z")
    document["key"].update(key)
    return document


class BaselineLookupTests(unittest.TestCase):
    """A baseline is found by the comparability key of the result, not by a file name derived from two of its fields."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.baselines = Path(self._folder.name) / "baselines"

    def write(self, name, document, plan_id="perf.open"):
        core.atomic_write_json(self.baselines / core.safe_segment(plan_id) / name, document)

    def check(self, document, folders=None):
        return compare.compare_against_folders(document, plan(), folders or [self.baselines])

    def test_the_baseline_with_the_key_of_the_result_is_found_among_several_files(self):
        self.write("ipad-a14.json", baseline([999, 50, 50, 50, 50], osMajor=26))  # the name a key-blind lookup would take
        self.write("second-series.json", baseline(BASE))
        comparison, shown = self.check(result([5, 101, 100, 102]))
        self.assertEqual("PASS", comparison.verdict, comparison.details)
        self.assertTrue(shown.endswith("second-series.json"), shown)

    def test_a_baseline_with_another_evidence_class_is_not_taken_for_the_key(self):
        emulator = baseline(BASE)
        emulator["source"]["evidence"] = "emulator"
        self.write("a.json", emulator)
        comparison, _ = self.check(result([5, 101, 100, 102]))
        self.assertEqual("NOT_COMPARABLE", comparison.verdict)
        self.assertTrue(any("evidence" in line for line in comparison.details), comparison.details)

    def test_the_newest_of_several_baselines_with_the_same_key_wins(self):
        old, new = baseline([999, 50, 50, 50, 50]), baseline(BASE)
        old["createdUtc"], new["createdUtc"] = "2026-01-01T00:00:00.000Z", "2026-06-01T00:00:00.000Z"
        self.write("z-old.json", old)
        self.write("a-new.json", new)
        comparison, shown = self.check(result([5, 101, 100, 102]))
        self.assertEqual("PASS", comparison.verdict, comparison.details)
        self.assertTrue(shown.endswith("a-new.json"), shown)

    def test_a_baseline_of_another_plan_in_a_folder_the_ids_share_is_ignored(self):
        # `perf:open` and `perf-open` are two identifiers and one folder name.
        self.write("ipad-a14.json", baseline_of("perf-open", [999, 50, 50, 50, 50]), plan_id="perf-open")
        document = result([5, 101, 100, 102])
        document["plan"]["id"] = "perf:open"
        comparison, _ = self.check(document)
        self.assertEqual("NOT_COMPARABLE", comparison.verdict)
        self.assertTrue(any("no baseline" in line for line in comparison.details), comparison.details)

    def test_a_baseline_that_names_another_plan_is_never_compared(self):
        foreign = baseline(BASE)
        foreign["planId"] = "perf.other"
        verdict = compare.compare(result([5, 101, 100, 102]), plan(), foreign)
        self.assertEqual("NOT_COMPARABLE", verdict.verdict)
        self.assertIn("planId", verdict.details[0])

    def test_a_missing_baseline_names_every_folder_that_was_searched(self):
        other = Path(self._folder.name) / "elsewhere"
        comparison, shown = self.check(result([5, 100, 100, 100]), [self.baselines, other])
        self.assertEqual("NOT_COMPARABLE", comparison.verdict)
        text = " ".join(comparison.details)
        self.assertIn(str(self.baselines), text)
        self.assertIn(str(other), text)
        self.assertTrue(shown)

    def test_baselines_that_exist_only_under_other_keys_say_so_and_are_not_comparable(self):
        self.write("a.json", baseline(BASE, osMajor=26))
        self.write("b.json", baseline(BASE, buildProfile="Release"))
        comparison, _ = self.check(result([5, 100, 100, 100]))
        self.assertEqual("NOT_COMPARABLE", comparison.verdict)
        text = " ".join(comparison.details)
        self.assertIn("2 baseline(s)", text)
        self.assertIn("none has the key of the result", text)

    def test_an_unreadable_baseline_file_is_reported_and_never_read_as_no_baseline(self):
        folder = self.baselines / "perf.open"
        folder.mkdir(parents=True)
        (folder / "broken.json").write_text("{not json", encoding="utf-8")
        (folder / "invalid.json").write_text('{"schema": "gdv.baseline/1"}', encoding="utf-8")
        comparison, _ = self.check(result([5, 100, 100, 100]))
        self.assertEqual("NOT_COMPARABLE", comparison.verdict)
        text = " ".join(comparison.details)
        self.assertIn("broken.json", text)
        self.assertIn("invalid.json", text)

    def test_an_unreadable_newer_baseline_fails_closed_instead_of_letting_an_older_one_decide(self):
        self.write("old.json", baseline(BASE))  # valid and of the same key: it would give a PASS
        (self.baselines / "perf.open" / "newest.json").write_text("{not json", encoding="utf-8")
        comparison, _ = self.check(result([5, 101, 100, 102]))
        self.assertEqual("NOT_COMPARABLE", comparison.verdict)
        self.assertIn("newest.json", comparison.details[0], "the file is the first line, which the job reason quotes")
        budget, _ = compare.compare_against_folders(result([5, 120, 121, 119]), plan(budget=110), [self.baselines])
        self.assertEqual("REGRESSION", budget.verdict, "a budget breach stays a regression")

    def test_equal_creation_times_pick_the_same_baseline_every_time(self):
        first, second = baseline(BASE), baseline(BASE)
        first["createdUtc"] = second["createdUtc"] = "2026-06-01T00:00:00.000Z"
        second["source"]["runId"] = "other-run"
        self.write("a.json", first)
        self.write("b.json", second)
        _, shown = self.check(result([5, 101, 100, 102]))
        self.assertTrue(shown.endswith("b.json"), "the last file name wins a tie")

    def test_different_baselines_for_the_key_in_two_folders_are_called_out(self):
        one, two = Path(self._folder.name) / "one", Path(self._folder.name) / "two"
        old, new = baseline(BASE), baseline(BASE)
        old["createdUtc"], new["createdUtc"] = "2026-01-01T00:00:00.000Z", "2026-06-01T00:00:00.000Z"
        core.atomic_write_json(one / "perf.open" / "x.json", old)
        core.atomic_write_json(two / "perf.open" / "x.json", new)
        comparison, _ = self.check(result([5, 101, 100, 102]), [one, two])
        self.assertEqual("PASS", comparison.verdict)
        self.assertTrue(any("different baselines" in line for line in comparison.details), comparison.details)

    def test_several_baseline_folders_are_searched_as_one(self):
        first, second = Path(self._folder.name) / "one", Path(self._folder.name) / "two"
        core.atomic_write_json(second / "perf.open" / "x.json", baseline(BASE))
        comparison, shown = self.check(result([5, 101, 100, 102]), [first, second])
        self.assertEqual("PASS", comparison.verdict, comparison.details)
        self.assertIn("two", shown)

    def test_a_budget_breach_stays_a_regression_when_no_baseline_matches(self):
        spec = plan(budget=110)
        comparison, _ = compare.compare_against_folders(result([5, 120, 121, 119]), spec, [self.baselines])
        self.assertEqual("REGRESSION", comparison.verdict)


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.baselines = Path(self._folder.name) / "baselines"
        self.state = {"status": "", "head": SHA, "ancestor": True}

    def git(self, folder, arguments):
        if arguments[0] == "status":
            return 0, self.state["status"]
        if arguments[0] == "rev-parse":
            return 0, self.state["head"]
        if arguments[0] == "merge-base":
            return (0 if self.state["ancestor"] else 1), ""
        raise AssertionError(arguments)

    def promote(self, document=None):
        return compare.promote(document or result(BASE), plan(), self.baselines, Path(self._folder.name), self.git)

    def test_promotes_a_passing_clean_run_on_main_and_the_file_validates(self):
        path = self.promote()
        self.assertEqual(self.baselines / "perf.open" / "ipad-a14.json", path)
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([], Validator().validate(written, "gdv.baseline/1"))
        self.assertEqual(4, written["metrics"]["openMs"]["stats"]["n"], "the warmup sample is not part of the baseline")

    def test_refuses_each_rule_violation(self):
        cases = {
            "failed run": (lambda: result(BASE, status="FAIL", reason="x"), {}),
            "editor evidence": (lambda: self.with_evidence("editor"), {}),
            "dirty run": (lambda: self.with_dirty(), {}),
            "dirty tree now": (lambda: result(BASE), {"status": " M file.cs"}),
            "head moved": (lambda: result(BASE), {"head": "f" * 40}),
            "not on main": (lambda: result(BASE), {"ancestor": False}),
        }
        for name, (build, state) in cases.items():
            with self.subTest(name):
                self.state.update({"status": "", "head": SHA, "ancestor": True})
                self.state.update(state)
                with self.assertRaises(compare.PromotionRefused):
                    self.promote(build())
        self.assertFalse(self.baselines.exists())

    def test_a_dirty_tree_refusal_names_the_changed_paths_and_caps_the_list(self):
        self.state["status"] = " M a.cs\n?? b.txt"
        with self.assertRaises(compare.PromotionRefused) as caught:
            self.promote()
        message = str(caught.exception)
        self.assertIn("2 changed path(s)", message)
        self.assertIn("M a.cs", message)
        self.assertIn("?? b.txt", message)
        self.assertNotIn("more", message)
        self.state["status"] = "\n".join(f" M file{index:02}.cs" for index in range(25))
        with self.assertRaises(compare.PromotionRefused) as caught:
            self.promote()
        message = str(caught.exception)
        self.assertIn("25 changed path(s)", message)
        self.assertIn("file19.cs", message)
        self.assertNotIn("file20.cs", message)
        self.assertIn("and 5 more", message)
        self.assertFalse(self.baselines.exists())

    def test_a_run_that_recorded_no_commit_is_refused_even_when_head_is_all_zeros(self):
        document = result(BASE)
        document["candidate"]["sha"] = "0" * 40
        self.state["head"] = "0" * 40
        with self.assertRaises(compare.PromotionRefused) as caught:
            self.promote(document)
        self.assertIn("no commit", str(caught.exception))
        self.assertFalse(self.baselines.exists())

    def with_evidence(self, evidence):
        document = result(BASE)
        document["target"]["evidence"] = evidence
        return document

    def with_dirty(self):
        document = result(BASE)
        document["candidate"]["dirty"] = True
        return document

    def test_a_busy_local_runner_blocks_the_promotion_and_an_idle_one_does_not(self):
        with self.assertRaises(compare.PromotionRefused) as caught:
            compare.promote(result(BASE), plan(), self.baselines, Path(self._folder.name), self.git, runner_state=lambda: "busy")
        self.assertIn("runner is busy", str(caught.exception))
        self.assertFalse(self.baselines.exists())
        for state in ("idle", "absent", "unknown"):
            compare.promote(result(BASE), plan(), self.baselines, Path(self._folder.name), self.git, runner_state=lambda s=state: s)

    def test_runs_of_one_plan_on_different_keys_are_promoted_side_by_side(self):
        first = self.promote(self.keyed(osMajor=27))
        second = self.promote(self.keyed(osMajor=26))
        third = self.promote(self.keyed(profile="Release"))
        self.assertEqual(3, len({first, second, third}), "no promotion overwrites another key's baseline")
        self.assertEqual(self.baselines / "perf.open" / "ipad-a14.json", first, "the first keeps the documented file name")
        for path, os_major, profile in ((first, 27, "Internal"), (second, 26, "Internal"), (third, 27, "Release")):
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((os_major, profile), (written["key"]["osMajor"], written["key"]["buildProfile"]))
            comparison, shown = compare.compare_against_folders(self.keyed(os_major, profile=profile), plan(), [self.baselines])
            self.assertEqual("PASS", comparison.verdict, comparison.details)
            self.assertEqual(str(path), shown)

    def test_promoting_the_same_key_again_replaces_its_file_even_when_the_plan_changed(self):
        first = self.promote()
        changed = result(BASE)
        changed["plan"]["sha256"] = "b" * 64
        again = self.promote(changed)
        self.assertEqual(first, again)
        self.assertEqual(1, len(list((self.baselines / "perf.open").glob("*.json"))))
        self.assertEqual("b" * 64, json.loads(again.read_text(encoding="utf-8"))["key"]["planSha256"])

    def test_plan_ids_and_device_classes_that_share_a_file_name_do_not_overwrite_each_other(self):
        paths = []
        for plan_id, device_class in (("perf:open", "ipad:a14"), ("perf-open", "ipad:a14"), ("perf-open", "ipad-a14")):
            document = result(BASE)
            document["plan"]["id"] = plan_id
            document["target"]["deviceClass"] = device_class
            paths.append(self.promote(document))
        self.assertEqual(3, len(set(paths)), paths)
        for path, (plan_id, device_class) in zip(paths, (("perf:open", "ipad:a14"), ("perf-open", "ipad:a14"), ("perf-open", "ipad-a14"))):
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((plan_id, device_class), (written["planId"], written["key"]["deviceClass"]))
            document = result(BASE)
            document["plan"]["id"] = plan_id
            document["target"]["deviceClass"] = device_class
            comparison, shown = compare.compare_against_folders(document, plan(), [self.baselines])
            self.assertEqual(("PASS", str(path)), (comparison.verdict, shown), comparison.details)

    def keyed(self, osMajor=27, profile="Internal"):
        document = result(BASE)
        document["target"]["osMajor"] = osMajor
        document["candidate"]["profile"] = profile
        return document

    def test_refuses_when_the_plan_declares_no_metrics_or_samples_are_missing(self):
        with self.assertRaises(compare.PromotionRefused):
            compare.promote(result(BASE), {"id": "x", "metrics": {}}, self.baselines, Path(self._folder.name), self.git)
        with self.assertRaises(compare.PromotionRefused):
            self.promote(result([1, 2]))


if __name__ == "__main__":
    unittest.main()
