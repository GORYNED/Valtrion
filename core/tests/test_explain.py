"""`select --explain`: the rule behind every selected check. The flag only adds information; the selection never changes."""

import copy
import json
import sys
import unittest

from .test_select import CHECKS, RepoCase, git, run, write


class ExplainTests(RepoCase):
    def select_json(self, *arguments, explain=True):
        extra = ["--explain"] if explain else []
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", *extra, *arguments)
        self.assertEqual(0, code, err)
        return json.loads(out)

    def entry(self, payload, check):
        return next(item for item in payload["explain"] if item["check"] == check)

    def write_map(self, **changes):
        data = copy.deepcopy(CHECKS)
        data.update(changes)
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")

    def test_without_the_flag_the_output_is_unchanged(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        plain = self.select_json(explain=False)
        explained = self.select_json()
        self.assertNotIn("explain", plain)
        self.assertEqual(plain, {key: value for key, value in explained.items() if key != "explain"})
        code, text, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main")
        self.assertEqual(0, code)
        self.assertNotIn("Explain", text)
        code, with_flag, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--explain")
        self.assertEqual(0, code)
        self.assertTrue(with_flag.startswith(text), "the selection part of the text is the same, the explanation is appended")

    def test_an_area_rule_names_its_index_glob_kind_and_path_for_every_level(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        payload = self.select_json()
        for check in ("l0", "player:win64", "example.perf.open-playback"):
            because = self.entry(payload, check)["because"]
            self.assertEqual([{"kind": "media", "rule": "areas[0]", "glob": "src/media/**", "paths": ["src/media/a.cs"]}], because, check)
        self.assertEqual("L3", self.entry(payload, "player:win64")["level"])
        self.assertEqual("L4", self.entry(payload, "example.perf.open-playback")["level"])

    def test_a_check_shared_by_two_kinds_lists_both(self):
        write(self.root, "src/shared/s.cs", "changed\n")
        self.commit()
        because = self.entry(self.select_json(), "editmode:Media.Tests")["because"]
        self.assertEqual([("media", "areas[2]"), ("ui", "areas[3]")], [(item["kind"], item["rule"]) for item in because])

    def test_a_named_kind_has_no_path(self):
        code, out, err = run("select", "--checks", str(self.checks_path), "--kind", "docs", "--json", "--explain")
        self.assertEqual(0, code, err)
        because = self.entry(json.loads(out), "l0")["because"]
        self.assertEqual([{"kind": "docs", "rule": "--kind", "paths": []}], because)

    def test_the_widest_set_says_so(self):
        write(self.root, "other/x.txt", "changed\n")
        self.commit()
        because = self.entry(self.select_json(), "player:win64")["because"]
        self.assertEqual([{"kind": "media", "rule": "unclassified", "detail": "widest", "paths": ["other/x.txt"]}], because)

    def test_the_widest_set_and_a_classified_path_both_show_for_one_kind(self):
        write(self.root, "other/x.txt", "changed\n")
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        because = self.entry(self.select_json(), "player:win64")["because"]
        self.assertEqual([("areas[0]", ["src/media/a.cs"]), ("unclassified", ["other/x.txt"])], [(item["rule"], item["paths"]) for item in because])

    def test_a_named_kind_together_with_a_diff_has_both_entries(self):
        write(self.root, "docs/readme.md", "changed\n")
        self.commit()
        because = self.entry(self.select_json("--kind", "ui"), "l0:lint")["because"]
        self.assertEqual([("ui", "--kind", [])], [(item["kind"], item["rule"], item["paths"]) for item in because])
        l0 = self.entry(self.select_json("--kind", "ui"), "l0")["because"]
        self.assertEqual([("docs", "areas[4]", ["docs/readme.md"])], [(item["kind"], item["rule"], item["paths"]) for item in l0])

    def test_an_excluded_path_is_not_named_for_the_area_that_excludes_it(self):
        areas = copy.deepcopy(CHECKS["areas"])
        areas[0] = {"glob": "src/media/**", "kind": "media", "exclude": ["src/media/old.cs"]}
        self.write_map(areas=areas)
        write(self.root, "src/media/old.cs", "changed\n")
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        because = self.entry(self.select_json(), "player:win64")["because"]
        # The excluded path matches no other area, so it is unclassified and the widest set names it; the area names only the other path.
        self.assertEqual([("areas[0]", ["src/media/a.cs"]), ("unclassified", ["src/media/old.cs"])], [(item["rule"], item["paths"]) for item in because])

    def test_a_path_judged_by_a_json_rule_does_not_show_the_area_that_also_matches_it(self):
        manifest = {"dependencies": {"lib.core": "1.0.0"}}
        git(self.root, "checkout", "-q", "main")
        write(self.root, "Packages/manifest.json", json.dumps(manifest))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "manifest")
        git(self.root, "checkout", "-q", "-B", "work")
        areas = copy.deepcopy(CHECKS["areas"]) + [{"glob": "Packages/**", "kind": "ui"}]
        self.write_map(areas=areas, jsonRules=[{"glob": "Packages/manifest.json", "root": "dependencies", "keys": [
            {"match": "lib.core", "kinds": ["docs"]}]}])
        manifest["dependencies"]["lib.core"] = "2.0.0"
        write(self.root, "Packages/manifest.json", json.dumps(manifest))
        self.commit()
        payload = self.select_json()
        self.assertEqual(["jsonRules[0]"], [item["rule"] for item in self.entry(payload, "l0")["because"] if "Packages/manifest.json" in item["paths"]])
        self.assertNotIn("l0:lint", [item["check"] for item in payload["explain"]], "the area kind `ui` must not be selected for a path a json rule judged")

    def test_a_json_key_names_the_rule_the_key_and_the_fallback(self):
        manifest = {"dependencies": {"lib.core": "1.0.0", "lib.other": "1.0.0"}}
        git(self.root, "checkout", "-q", "main")
        write(self.root, "Packages/manifest.json", json.dumps(manifest))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "manifest")
        git(self.root, "checkout", "-q", "-B", "work")
        self.write_map(jsonRules=[{"glob": "Packages/manifest.json", "root": "dependencies", "keys": [
            {"match": "lib.core", "kinds": ["media"]}, {"match": "*", "kind": "docs"}]}])
        manifest["dependencies"]["lib.core"] = "2.0.0"
        manifest["dependencies"]["lib.other"] = "2.0.0"
        write(self.root, "Packages/manifest.json", json.dumps(manifest))
        self.commit()
        because = self.entry(self.select_json(), "player:win64")["because"]
        self.assertEqual([{"kind": "media", "rule": "jsonRules[0]", "glob": "Packages/manifest.json", "key": "lib.core", "match": "lib.core",
                           "fallback": False, "paths": ["Packages/manifest.json"]}], because)
        l0 = self.entry(self.select_json(), "l0")["because"]
        fallback = [item for item in l0 if item.get("key") == "lib.other"]
        self.assertEqual(1, len(fallback))
        self.assertEqual(("docs", "*", True), (fallback[0]["kind"], fallback[0]["match"], fallback[0]["fallback"]))

    def test_a_content_rule_names_its_index(self):
        self.write_map(contentRules=[{"glob": "src/media/**", "kind": "docs", "lineComment": ["//"]}])
        write(self.root, "src/media/a.cs", "x\n// a note\n")
        self.commit()
        payload = self.select_json()
        # The test rewrites checks.json itself, which the map classifies as docs: look at the changed source file only.
        because = [item for item in self.entry(payload, "l0")["because"] if "src/media/a.cs" in item["paths"]]
        self.assertEqual([{"kind": "docs", "rule": "contentRules[0]", "glob": "src/media/**", "paths": ["src/media/a.cs"]}], because)
        self.assertNotIn("player:win64", [item["check"] for item in payload["explain"]])

    def test_an_equivalence_rule_names_its_index(self):
        self.write_map(equivalenceRules=[{"glob": "src/media/**", "kind": "docs", "command": [sys.executable, "-c", "raise SystemExit(0)"]}])
        write(self.root, "src/media/a.cs", "moved\n")
        self.commit()
        because = [item for item in self.entry(self.select_json(), "l0")["because"] if "src/media/a.cs" in item["paths"]]
        self.assertEqual([{"kind": "docs", "rule": "equivalenceRules[0]", "glob": "src/media/**", "paths": ["src/media/a.cs"]}], because)

    def test_the_text_has_one_line_per_check_with_the_rule(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--explain")
        self.assertEqual(0, code)
        self.assertIn("Explain (why each check is selected):", out)
        self.assertIn("  player:win64 [L3] <- kind media: areas[0] glob src/media/** (src/media/a.cs)", out)
        self.assertEqual(len([line for line in out.splitlines() if " <- kind " in line]), len(self.select_json()["explain"]))

    def test_many_paths_are_shortened_in_the_text_but_not_in_the_json(self):
        for name in ("a", "b", "c", "d", "e"):
            write(self.root, f"src/media/{name}.cs", "changed\n")
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--explain")
        self.assertEqual(0, code)
        self.assertIn("+2 more", out)
        self.assertEqual(5, len(self.entry(self.select_json(), "player:win64")["because"][0]["paths"]))

    def test_a_failed_selection_explains_nothing(self):
        self.write_map(unclassified="fail")
        write(self.root, "other/x.txt", "changed\n")
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", "--explain")
        self.assertEqual(1, code)
        self.assertEqual([], json.loads(out)["explain"])
        code, text, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--explain")
        self.assertEqual(1, code)
        self.assertNotIn("Explain", text)

    def test_nothing_selected_has_no_explain_block(self):
        write(self.root, "docs/readme.md", "changed\n")
        self.commit()
        code, text, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--level", "L0", "--explain")
        self.assertEqual(0, code)
        self.assertIn("Explain", text, "L0 still selects the l0 check of the docs kind")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "checkout", "-q", "-B", "work")
        code, text, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--explain")
        self.assertEqual(0, code)
        self.assertIn("Nothing selected.", text)
        self.assertNotIn("Explain", text)


if __name__ == "__main__":
    unittest.main()
