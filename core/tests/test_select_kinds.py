"""Selection by named kind (`--kind`), named runners and the `;` placeholder, and `exclude` in the area map of `checks.json`."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from valtrion import select

from .test_select import CHECKS, CONTRACTS, CORE_ROOT, RepoCase, run, write

RUNNER_SEMICOLON = "python tools/run-tests.py --assemblies {assemblies;}"
RUNNER_COMMA = "python tools/run-tests.py --assemblies {assemblies}"

MAP = copy.deepcopy(CHECKS)
MAP["runners"] = {"unity-tests": RUNNER_SEMICOLON, "unity-tests-csv": RUNNER_COMMA}
MAP["areas"] = [
    {"glob": "src/media/**", "exclude": ["src/media/gen/**", "src/media/**/*.meta"], "kind": "media"},
    {"glob": "src/media/gen/**", "kind": "generated"},
    {"glob": "src/ui/**", "kind": "ui"},
    {"glob": "docs/**", "kind": "docs"},
    {"glob": "validation/**", "kind": "docs"},
]
MAP["kinds"] = {
    "media": {"L2": ["editmode:Media.Tests@unity-tests"]},
    "ui": {"L2": ["editmode:Ui.Tests@unity-tests"]},
    "generated": {"L0": ["l0"]},
    "docs": {"L0": ["l0"]},
    "scheduled": {"L2": ["editmode:Slow.Tests,Media.Tests@unity-tests"], "L3": ["player:win64"]},
    "release": {"L2": ["editmode:Release.Tests@unity-tests"], "L4": ["device:plans/open-playback"]},
}


def keys(payload, section="editorSuites"):
    return [item["key"] for item in payload[section]]


class NamedKindTests(RepoCase):
    checks = MAP

    def select_json(self, *arguments):
        code, out, err = run("select", "--checks", str(self.checks_path), "--json", *arguments)
        return code, (json.loads(out) if out.strip().startswith("{") else None), err

    def test_a_named_kind_is_selected_without_a_diff(self):
        code, payload, _ = self.select_json("--kind", "scheduled")
        self.assertEqual(0, code)
        self.assertEqual(["scheduled"], payload["selectedKinds"])
        self.assertTrue(payload["kindsNamedExplicitly"])
        self.assertEqual(["scheduled"], payload["kinds"])
        self.assertEqual([], payload["changedPaths"])
        self.assertIsNone(payload["baseRef"])
        self.assertEqual(["editmode:Media.Tests", "editmode:Slow.Tests"], keys(payload))
        self.assertEqual(["player:win64"], keys(payload, "playerTests"))

    def test_the_level_is_cumulative_and_defaults_to_l4(self):
        _, low, _ = self.select_json("--kind", "scheduled", "--level", "L2")
        self.assertEqual([], low["playerTests"])
        _, full, _ = self.select_json("--kind", "release")
        self.assertEqual(["example.perf.open-playback"], keys(full, "devicePlans"))
        _, lower, _ = self.select_json("--kind", "release", "--level", "L3")
        self.assertEqual([], lower["devicePlans"])

    def test_the_option_repeats_and_takes_a_comma_list(self):
        _, payload, _ = self.select_json("--kind", "scheduled", "--kind", "ui,release", "--level", "L2")
        self.assertEqual(["release", "scheduled", "ui"], payload["selectedKinds"])
        self.assertEqual(["editmode:Media.Tests", "editmode:Release.Tests", "editmode:Slow.Tests", "editmode:Ui.Tests"], keys(payload))

    def test_named_kinds_are_added_to_the_kinds_of_a_diff(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        _, payload, _ = self.select_json("--base-ref", "main", "--kind", "scheduled", "--level", "L2")
        self.assertEqual(["media", "scheduled"], payload["kinds"])
        self.assertEqual(["scheduled"], payload["selectedKinds"])  # only what was named
        self.assertEqual(["src/media/a.cs"], payload["changedPaths"])

    def test_without_the_option_nothing_is_named(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        _, payload, _ = self.select_json("--base-ref", "main")
        self.assertEqual([], payload["selectedKinds"])
        self.assertFalse(payload["kindsNamedExplicitly"])

    def test_an_unknown_kind_fails_and_lists_the_kinds(self):
        code, _, err = self.select_json("--kind", "scheduled", "--kind", "nightly")
        self.assertEqual(1, code)
        self.assertIn("nightly", err)
        for name in ("docs", "generated", "media", "release", "scheduled", "ui"):
            self.assertIn(name, err)

    def test_a_run_with_neither_a_diff_nor_a_kind_is_refused_as_before(self):
        code, _, err = run("select", "--checks", str(self.checks_path))
        self.assertEqual(2, code)
        self.assertIn("--base-ref", err)

    def test_the_text_output_names_the_kinds(self):
        code, out, _ = run("select", "--checks", str(self.checks_path), "--kind", "scheduled", "--level", "L2")
        self.assertEqual(0, code)
        self.assertIn("named kinds: scheduled", out)
        self.assertNotIn("nothing to select", out)

    def test_report_takes_the_same_option(self):
        results = self.root.parent / (self.root.name + "-results")
        results.mkdir()
        self.addCleanup(lambda: results.rmdir() if results.exists() and not any(results.iterdir()) else None)
        code, out, _ = run("report", "--checks", str(self.checks_path), "--kind", "scheduled", "--level", "L2", "--results", str(results), "--json")
        payload = json.loads(out)
        self.assertEqual(["scheduled"], payload["selectedKinds"])
        self.assertTrue(payload["kindsNamedExplicitly"])
        self.assertEqual(["editmode:Media.Tests", "editmode:Slow.Tests"], [row["key"] for row in payload["rows"]])
        code, out, _ = run("report", "--checks", str(self.checks_path), "--kind", "scheduled", "--level", "L2", "--results", str(results))
        self.assertIn("named kinds `scheduled`", out)
        self.assertNotIn("No changed paths", out)
        code, _, err = run("report", "--checks", str(self.checks_path), "--kind", "nightly", "--results", str(results))
        self.assertEqual(1, code)


class RunnerTests(RepoCase):
    checks = MAP

    def commands(self, *kinds, level="L2"):
        paths = []
        config = self.config()
        return select.select(config, paths, level, named_kinds=kinds).editor_commands()

    def test_a_name_in_runners_is_replaced_by_its_template(self):
        (command,) = self.commands("scheduled")
        self.assertEqual("python tools/run-tests.py --assemblies Media.Tests;Slow.Tests", command["command"])
        self.assertEqual("unity-tests", command["runnerName"])
        self.assertEqual(RUNNER_SEMICOLON, command["runner"])

    def test_two_kinds_with_one_named_runner_make_exactly_one_command_with_the_united_sorted_list(self):
        commands = self.commands("scheduled", "release", "ui", "media")
        self.assertEqual(1, len(commands))
        self.assertEqual(["Media.Tests", "Release.Tests", "Slow.Tests", "Ui.Tests"], commands[0]["assemblies"])
        self.assertEqual("python tools/run-tests.py --assemblies Media.Tests;Release.Tests;Slow.Tests;Ui.Tests", commands[0]["command"])
        self.assertEqual(["editmode:Media.Tests", "editmode:Release.Tests", "editmode:Slow.Tests", "editmode:Ui.Tests"], commands[0]["suites"])

    def test_a_name_that_is_not_in_runners_is_the_literal_command_as_before(self):
        data = {**copy.deepcopy(MAP), "kinds": {**MAP["kinds"], "docs": {"L2": ["editmode:Docs.Tests@echo {assemblies}"]}}}
        config = select.Config(self.checks_path, data)
        (command,) = select.select(config, [], "L2", named_kinds=["docs"]).editor_commands()
        self.assertEqual("echo Docs.Tests", command["command"])
        self.assertIsNone(command["runnerName"])

    def test_the_comma_placeholder_is_unchanged(self):
        data = {**copy.deepcopy(MAP), "kinds": {**MAP["kinds"], "docs": {"L2": ["editmode:A.Tests,B.Tests@unity-tests-csv"]}}}
        config = select.Config(self.checks_path, data)
        (command,) = select.select(config, [], "L2", named_kinds=["docs"]).editor_commands()
        self.assertEqual("python tools/run-tests.py --assemblies A.Tests,B.Tests", command["command"])

    def test_both_placeholders_in_one_template(self):
        self.assertEqual("a A,B b A;B", select.expand_runner("a {assemblies} b {assemblies;}", ["A", "B"]))
        self.assertEqual("no placeholder", select.expand_runner("no placeholder", ["A"]))

    def test_a_map_without_runners_behaves_as_before(self):
        self.assertEqual({}, select.Config(self.checks_path, copy.deepcopy(CHECKS)).runners)
        self.assertEqual(("x {assemblies}", None), select.Config(self.checks_path, copy.deepcopy(CHECKS)).resolve_runner("x {assemblies}"))
        self.assertEqual((None, None), select.Config(self.checks_path, copy.deepcopy(CHECKS)).resolve_runner(None))

    def test_two_different_runners_for_one_suite_still_conflict_by_name_or_text(self):
        data = {**copy.deepcopy(MAP), "kinds": {**MAP["kinds"], "docs": {"L2": ["editmode:Ui.Tests@unity-tests-csv"]}}}
        config = select.Config(self.checks_path, data)
        with self.assertRaises(select.ConfigError):
            select.select(config, [], "L2", named_kinds=["docs", "ui"])
        same = {**copy.deepcopy(MAP), "kinds": {**MAP["kinds"], "docs": {"L2": [f"editmode:Ui.Tests@{RUNNER_SEMICOLON}"]}}}
        select.select(select.Config(self.checks_path, same), [], "L2", named_kinds=["docs", "ui"])  # a name and its own text agree

    def check_config(self, data):
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        return run("check-config", "--checks", str(self.checks_path))

    def test_check_config_accepts_the_map(self):
        code, out, err = self.check_config(MAP)
        self.assertEqual(0, code, err)

    def test_check_config_names_an_unknown_placeholder(self):
        bad = copy.deepcopy(MAP)
        bad["runners"]["typo"] = "python run.py --assemblies {assembly}"
        bad["runners"]["typo2"] = "python run.py --assemblies {assemblies:;}"
        code, _, err = self.check_config(bad)
        self.assertEqual(1, code)
        self.assertIn("runners.typo:", err)
        self.assertIn("{assembly}", err)
        self.assertIn("runners.typo2", err)
        self.assertIn("{assemblies:;}", err)
        bad = copy.deepcopy(MAP)
        bad["kinds"]["docs"]["L2"] = ["editmode:Docs.Tests@echo {assemblies;;}"]
        code, _, err = self.check_config(bad)
        self.assertEqual(1, code)
        self.assertIn("kinds.docs.L2", err)
        self.assertIn("{assemblies;;}", err)

    def test_a_shell_variable_or_brace_list_in_a_literal_runner_is_not_a_placeholder(self):
        data = copy.deepcopy(MAP)
        data["kinds"]["docs"]["L2"] = ["editmode:Docs.Tests@echo ${HOME} {a,b} {assemblies}"]
        code, _, err = self.check_config(data)
        self.assertEqual(0, code, err)
        # A map that was valid before must stay valid: variables that merely start like the placeholder are the command's own.
        for text in ("run.sh ${assemblyDir} {assemblies}", "run.sh ${assemblies_dir} {assemblies}", "run.sh ${assembly} {assemblies;}"):
            data["kinds"]["docs"]["L2"] = [f"editmode:Docs.Tests@{text}"]
            code, _, err = self.check_config(data)
            self.assertEqual(0, code, f"{text}: {err}")

    def test_check_config_rejects_a_bad_runner_name(self):
        bad = copy.deepcopy(MAP)
        bad["runners"]["bad name"] = "x"
        code, _, err = self.check_config(bad)
        self.assertEqual(1, code)
        self.assertIn("bad name", err)

    def test_an_undefined_bare_name_is_a_warning_when_the_map_has_runners(self):
        data = copy.deepcopy(MAP)
        data["kinds"]["docs"]["L2"] = ["editmode:Docs.Tests@unity-test"]  # a typo of unity-tests
        code, _, err = self.check_config(data)
        self.assertEqual(0, code)
        self.assertIn("warning", err)
        self.assertIn("'unity-test' is not a key of runners", err)

    def test_the_schema_rejects_a_runner_that_is_not_a_string(self):
        for name in ("checks-runner-not-a-string.json", "checks-runner-empty.json", "checks-exclude-not-an-array.json"):
            path = CONTRACTS / "fixtures" / "invalid" / name
            self.assertEqual(1, run("validate", str(path))[0], name)

    def test_the_shipped_fixture_with_the_new_keys_is_valid(self):
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-runners-exclude.json"))
        self.assertEqual(0, code, out)
        config = select.load_config(CONTRACTS / "fixtures" / "valid" / "checks-runners-exclude.json")
        self.assertEqual({"editor-tests", "editor-tests-csv"}, set(config.runners))


class ExcludeTests(RepoCase):
    checks = MAP

    def classify(self, paths):
        return select.classify(self.config(), paths)

    def test_an_excluded_path_does_not_match_the_area(self):
        classified, unclassified = self.classify(["src/media/a.cs", "src/media/deep/c.cs", "src/media/gen/g.cs", "src/media/x.meta", "src/media/deep/y.meta"])
        self.assertEqual({"src/media/a.cs": ["media"], "src/media/deep/c.cs": ["media"], "src/media/gen/g.cs": ["generated"]}, classified)
        self.assertEqual(["src/media/x.meta", "src/media/deep/y.meta"], unclassified)

    def test_an_excluded_path_keeps_the_kind_of_another_area(self):
        classified, _ = self.classify(["src/media/gen/g.cs"])
        self.assertEqual({"src/media/gen/g.cs": ["generated"]}, classified)  # excluded from media, matched by generated

    def test_a_path_excluded_everywhere_is_unclassified_and_widens_the_selection(self):
        write(self.root, "src/media/x.meta", "new\n")
        self.commit()
        selection = self.selection("L2")
        self.assertEqual(["src/media/x.meta"], selection.unclassified)
        self.assertTrue(selection.widest)

    def test_an_area_without_exclude_is_unchanged(self):
        config = select.Config(self.checks_path, copy.deepcopy(CHECKS))
        classified, _ = select.classify(config, ["src/media/gen/g.cs", "src/shared/s.cs"])
        self.assertEqual({"src/media/gen/g.cs": ["media"], "src/shared/s.cs": ["media", "ui"]}, classified)

    def test_check_config_rejects_an_invalid_exclude_glob(self):
        for bad_glob in ("/abs/**", "../outside/**", "a\\b", ""):
            data = copy.deepcopy(MAP)
            data["areas"][0]["exclude"] = [bad_glob]
            self.checks_path.write_text(json.dumps(data), encoding="utf-8")
            code, _, err = run("check-config", "--checks", str(self.checks_path))
            self.assertEqual(1, code, repr(bad_glob))
            self.assertIn("areas[0]", err)

    def test_the_shipped_example_map_classifies_as_before(self):
        config = select.load_config(CORE_ROOT.parent / "examples" / "checks.json")
        classified, unclassified = select.classify(
            config, ["Assets/Project/Runtime/a.cs", "Assets/Project/Playback/b.cs", "docs/x.md", "validation/y.json", "other/z.txt"])
        self.assertEqual({"Assets/Project/Runtime/a.cs": ["runtime"], "Assets/Project/Playback/b.cs": ["playback-perf"],
                          "docs/x.md": ["docs"], "validation/y.json": ["docs"]}, classified)
        self.assertEqual(["other/z.txt"], unclassified)

    def test_the_shipped_fixture_classifies_with_its_exclusions(self):
        config = select.load_config(CONTRACTS / "fixtures" / "valid" / "checks-runners-exclude.json")
        classified, unclassified = select.classify(
            config, ["Assets/Project/Runtime/a.cs", "Assets/Project/Runtime/Tests/t.cs", "Assets/Project/Generated/g.cs"])
        self.assertEqual({"Assets/Project/Runtime/a.cs": ["runtime"], "Assets/Project/Runtime/Tests/t.cs": ["tests"]}, classified)
        self.assertEqual(["Assets/Project/Generated/g.cs"], unclassified)


if __name__ == "__main__":
    unittest.main()
