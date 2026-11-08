"""`equivalenceRules` of `checks.json`: a project command that proves a change keeps every value gives the path a cheaper kind.
A failing, missing or timed-out command leaves the path under its normal kinds."""

import copy
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from valtrion import select

from .test_select import CHECKS, CONTRACTS, RepoCase, git, run, write

#: The project command of these tests: argv is `<mode> <base> <path>`. `sorted` proves equivalence when the lines of the file are the same
#: set before and after (a move of fields); `error` exits 3; `sleep` outlives the timeout; `env` proves equivalence only when the
#: worktree flag matches the mode argument's expectation.
SCRIPT = textwrap.dedent('''
    import os, subprocess, sys, time
    mode, base, path = sys.argv[1:4]
    if mode == "error":
        sys.exit(3)
    if mode == "sleep":
        time.sleep(30)
    if mode == "worktree":
        sys.exit(0 if os.environ.get("VALTRION_INCLUDE_WORKTREE") == "1" else 1)
    old = subprocess.run(["git", "show", base + ":" + path], capture_output=True, text=True, check=True).stdout
    new = open(path, encoding="utf-8").read()
    sys.exit(0 if sorted(old.split()) == sorted(new.split()) else 1)
''')

MAP = copy.deepcopy(CHECKS)
MAP["areas"] = [
    {"glob": "src/**", "kind": "code"},
    {"glob": "assets/**", "kind": "asset"},
    {"glob": "docs/**", "kind": "docs"},
    {"glob": "validation/**", "kind": "docs"},
]
MAP["kinds"] = {
    "code": {"L2": ["editmode:Code.Tests@echo {assemblies}"]},
    "asset": {"L4": ["editmode:Asset.Tests@echo {assemblies}"]},
    "layout": {"L0": ["l0"]},
    "docs": {"L0": ["l0"]},
}


class EquivalenceTests(RepoCase):
    checks = MAP

    def setUp(self):
        super().setUp()
        scripts = tempfile.TemporaryDirectory()
        self.addCleanup(scripts.cleanup)
        self.script = Path(scripts.name) / "equal.py"
        self.script.write_text(SCRIPT, encoding="utf-8")
        write(self.root, "assets/a.asset", "alpha: 1\nbeta: 2\n")
        write(self.root, "assets/b.asset", "gamma: 3\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "assets")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "work")
        git(self.root, "checkout", "-q", "-b", "work2")

    def rules(self, mode="sorted", timeout=None, glob="assets/**/*.asset"):
        rule = {"glob": glob, "kind": "layout", "command": [sys.executable, str(self.script), mode]}
        if timeout is not None:
            rule["timeoutSec"] = timeout
        data = copy.deepcopy(MAP)
        data["equivalenceRules"] = [rule]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")

    def select_json(self, *arguments):
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", *arguments)
        self.assertEqual(0, code, err)
        payload = json.loads(out)
        # The test rewrites checks.json itself, which the map classifies as docs: leave that path out of what is compared.
        payload["pathKinds"].pop("validation/device/checks.json", None)
        payload["kinds"] = [kind for kind in payload["kinds"] if kind != "docs"]
        return payload

    def test_a_move_of_values_gets_the_cheaper_kind_and_the_output_names_the_rule(self):
        self.rules()
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")
        self.commit()
        payload = self.select_json()
        self.assertEqual({"assets/a.asset": {"kind": "layout", "rule": 0, "glob": "assets/**/*.asset"}}, payload["equivalentPaths"])
        self.assertEqual({}, payload["equivalenceUnprovenPaths"])
        self.assertEqual({"assets/a.asset": ["layout"]}, payload["pathKinds"])
        self.assertEqual(["layout"], payload["kinds"])
        self.assertEqual([], payload["editorSuites"])

    def test_a_changed_value_keeps_the_normal_kinds_and_is_reported_as_not_proven(self):
        self.rules()
        write(self.root, "assets/a.asset", "alpha: 1\nbeta: 9\n")
        self.commit()
        payload = self.select_json()
        self.assertEqual({}, payload["equivalentPaths"])
        self.assertEqual(["asset"], payload["kinds"])
        self.assertIn("exited with 1", payload["equivalenceUnprovenPaths"]["assets/a.asset"])
        self.assertEqual(["editmode:Asset.Tests"], [item["key"] for item in payload["editorSuites"]])

    def test_a_command_that_fails_cannot_start_or_times_out_keeps_the_normal_kinds(self):
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")
        self.commit()
        for mode, timeout, reason in (("error", None, "exited with 3"), ("sleep", 1, "did not finish in 1 s")):
            self.rules(mode, timeout)
            payload = self.select_json()
            self.assertEqual({}, payload["equivalentPaths"], mode)
            self.assertEqual(["asset"], payload["kinds"], mode)
            self.assertIn(reason, payload["equivalenceUnprovenPaths"]["assets/a.asset"], mode)
        data = json.loads(self.checks_path.read_text(encoding="utf-8"))
        data["equivalenceRules"][0]["command"] = ["valtrion-no-such-program-xyz"]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        payload = self.select_json()
        self.assertEqual({}, payload["equivalentPaths"])
        self.assertEqual(["asset"], payload["kinds"])
        self.assertIn("could not run", payload["equivalenceUnprovenPaths"]["assets/a.asset"])

    def test_a_mixed_diff_selects_the_union_of_the_proven_and_the_real_change(self):
        self.rules()
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")  # moved only
        write(self.root, "assets/b.asset", "gamma: 4\n")  # a value changed
        self.commit()
        payload = self.select_json()
        self.assertEqual(["assets/a.asset"], list(payload["equivalentPaths"]))
        self.assertEqual({"assets/a.asset": ["layout"], "assets/b.asset": ["asset"]}, payload["pathKinds"])
        self.assertEqual(["asset", "layout"], payload["kinds"])
        self.assertEqual(["assets/b.asset"], list(payload["equivalenceUnprovenPaths"]))

    def test_an_unclassified_path_still_fails_closed(self):
        self.rules()
        data = json.loads(self.checks_path.read_text(encoding="utf-8"))
        data["unclassified"] = "fail"
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        write(self.root, "other/x.txt", "changed\n")
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json")
        payload = json.loads(out)
        self.assertEqual(1, code)
        self.assertEqual(["other/x.txt"], payload["unclassifiedPaths"])
        self.assertEqual(["assets/a.asset"], list(payload["equivalentPaths"]))  # classified by the rule, not unclassified

    def test_a_path_the_glob_does_not_match_is_never_run(self):
        self.rules(mode="error", glob="assets/**/*.prefab")
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")
        self.commit()
        payload = self.select_json()
        self.assertEqual({}, payload["equivalentPaths"])
        self.assertEqual({}, payload["equivalenceUnprovenPaths"])
        self.assertEqual(["asset"], payload["kinds"])

    def test_the_command_is_told_whether_the_working_tree_counts(self):
        self.rules("worktree")
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")
        self.commit()
        self.assertEqual({}, self.select_json()["equivalentPaths"])
        write(self.root, "assets/b.asset", "gamma: 3\n  \n")
        self.assertIn("assets/b.asset", self.select_json("--include-worktree")["equivalentPaths"])

    def test_a_path_judged_by_a_content_rule_does_not_run_the_command(self):
        self.rules(mode="error")
        data = json.loads(self.checks_path.read_text(encoding="utf-8"))
        data["contentRules"] = [{"glob": "assets/**", "kind": "docs", "lineComment": ["#"]}]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        write(self.root, "assets/a.asset", "alpha: 1\nbeta: 2\n# a note\n")
        self.commit()
        payload = self.select_json()
        self.assertEqual({"assets/a.asset": "docs"}, payload["cosmeticPaths"])
        self.assertEqual({}, payload["equivalenceUnprovenPaths"])

    def test_the_text_output_lists_the_downgraded_and_the_unproven_paths(self):
        self.rules()
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")
        write(self.root, "assets/b.asset", "gamma: 4\n")
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main")
        self.assertEqual(0, code)
        self.assertIn("EQUIVALENT (1)", out)
        self.assertIn("assets/a.asset -> layout (equivalenceRules[0]", out)
        self.assertIn("NOT PROVEN (1)", out)
        self.assertIn("assets/b.asset:", out)

    def test_a_map_without_equivalence_rules_behaves_as_before(self):
        write(self.root, "assets/a.asset", "beta: 2\nalpha: 1\n")
        self.commit()
        payload = self.select_json()
        self.assertEqual({}, payload["equivalentPaths"])
        self.assertEqual(["asset"], payload["kinds"])


class EquivalenceConfigTests(RepoCase):
    checks = MAP

    def check(self, rule):
        data = copy.deepcopy(MAP)
        data["equivalenceRules"] = [rule]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        return run("check-config", "--checks", str(self.checks_path))

    def test_a_valid_rule_passes(self):
        code, _, err = self.check({"glob": "assets/**", "kind": "layout", "command": ["tool"], "timeoutSec": 5})
        self.assertEqual(0, code, err)

    def test_an_unknown_kind_or_a_bad_glob_is_an_error(self):
        for change in ({"kind": "ghost"}, {"glob": "/abs/**"}, {"glob": "../x/**"}):
            code, _, err = self.check({"glob": "assets/**", "kind": "layout", "command": ["tool"], **change})
            self.assertEqual(1, code, change)
            self.assertIn("equivalenceRules[0]", err)

    def test_the_schema_rejects_an_empty_command_and_a_bad_timeout(self):
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-equivalence-rule-without-command.json"))
        self.assertEqual(1, code)
        for timeout in (0, 3601):
            code, _, _ = self.check({"glob": "assets/**", "kind": "layout", "command": ["tool"], "timeoutSec": timeout})
            self.assertEqual(1, code, timeout)
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-equivalence-rules.json"))
        self.assertEqual(0, code, out)


if __name__ == "__main__":
    unittest.main()
