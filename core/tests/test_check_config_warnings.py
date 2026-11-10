"""`check-config` warnings about map patterns that pass every check yet select more than a change calls for. They are warnings: exit code 0."""

import copy
import json
import unittest

from valtrion import select

from .test_select import CHECKS, CONTRACTS, RepoCase, run

AREAS = len(CHECKS["areas"])  # index of the first area a test appends
KEY_RULE = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kind": "docs"}]}
LOCK_RULE = {"glob": "Packages/packages-lock.json", "root": "dependencies", "keys": [{"match": "*", "kind": "docs"}]}


class ConfigWarningTests(RepoCase):
    def warnings(self, **changes):
        data = copy.deepcopy(CHECKS)
        for key, value in changes.items():
            if key == "extra_areas":
                data["areas"] = data["areas"] + value
            elif key == "extra_kinds":
                data["kinds"].update(value)
            else:
                data[key] = value
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, warnings = select.validate_config(select.load_config(self.checks_path))
        self.assertEqual([], errors)
        return [message for message in warnings if not message.startswith("fullSetReasons")]  # tested in test_select_modes

    def matching(self, warnings, text):
        return [item for item in warnings if text in item]

    def test_a_map_without_json_rules_has_none_of_the_new_warnings(self):
        self.assertEqual([], self.warnings())

    def test_a_lock_file_that_only_an_area_classifies_is_named_with_the_area_and_its_heavy_checks(self):
        warnings = self.warnings(jsonRules=[KEY_RULE], extra_areas=[{"glob": "Packages/**", "kind": "media"}])
        found = self.matching(warnings, "Packages/packages-lock.json")
        self.assertEqual(1, len(found), warnings)
        self.assertIn("jsonRules[0]", found[0])
        self.assertIn(f"areas[{AREAS}]", found[0])
        self.assertIn("L3 player:win64", found[0])
        self.assertIn("L4 device:plans/open-playback", found[0])

    def test_the_other_direction_is_found_too(self):
        warnings = self.warnings(jsonRules=[LOCK_RULE], extra_areas=[{"glob": "Packages/**", "kind": "docs"}])
        self.assertEqual(1, len(self.matching(warnings, "Packages/manifest.json is not")), warnings)

    def test_both_files_classified_by_key_give_no_warning(self):
        warnings = self.warnings(jsonRules=[KEY_RULE, LOCK_RULE], extra_areas=[{"glob": "Packages/**", "kind": "media"}])
        self.assertEqual([], self.matching(warnings, "is not:"), warnings)

    def test_a_lock_file_that_nothing_matches_says_so(self):
        warnings = self.warnings(jsonRules=[KEY_RULE])
        found = self.matching(warnings, "Packages/packages-lock.json")
        self.assertEqual(1, len(found), warnings)
        self.assertIn("no jsonRules entry and no area matches it", found[0])

    def test_an_excluded_lock_file_is_not_called_classified_by_that_area(self):
        areas = [{"glob": "Packages/**", "kind": "media", "exclude": ["Packages/packages-lock.json"]}]
        found = self.matching(self.warnings(jsonRules=[KEY_RULE], extra_areas=areas), "Packages/packages-lock.json")
        self.assertIn("no jsonRules entry and no area matches it", found[0])

    def test_wildcard_globs_and_other_file_names_are_left_alone(self):
        rules = [{"glob": "Packages/*.json", "root": "dependencies", "keys": [{"match": "*", "kind": "docs"}]},
                 {"glob": "config/settings.json", "root": "values", "keys": [{"match": "*", "kind": "docs"}]}]
        self.assertEqual([], self.warnings(jsonRules=rules))

    def test_npm_style_names_pair_too(self):
        rule = {"glob": "web/package.json", "root": "dependencies", "keys": [{"match": "*", "kind": "docs"}]}
        found = self.matching(self.warnings(jsonRules=[rule]), "web/package-lock.json")
        self.assertEqual(1, len(found))

    def test_a_fallback_key_with_player_and_device_checks_warns_until_the_key_allows_it(self):
        heavy = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kind": "media"}]}
        found = self.matching(self.warnings(jsonRules=[heavy, LOCK_RULE]), "the fallback key '*'")
        self.assertEqual(1, len(found))
        self.assertIn("jsonRules[0].keys[0]", found[0])
        self.assertIn("L3 player:win64", found[0])
        self.assertIn("L4 device:plans/open-playback", found[0])
        allowed = copy.deepcopy(heavy)
        allowed["keys"][0]["allowHeavyFallback"] = True
        self.assertEqual([], self.matching(self.warnings(jsonRules=[allowed, LOCK_RULE]), "the fallback key '*'"))

    def test_a_light_fallback_and_a_heavy_named_key_do_not_warn(self):
        rule = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [
            {"match": "core.*", "kind": "media"}, {"match": "*", "kind": "docs"}]}
        self.assertEqual([], self.matching(self.warnings(jsonRules=[rule, LOCK_RULE]), "the fallback key '*'"))

    def test_a_kind_with_l4_checks_that_only_dependency_keys_reach_warns(self):
        infra = {"infra": {"L0": ["l0"], "L4": ["device:plans/open-playback"]}}
        rule = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [
            {"match": "core.*", "kind": "infra"}, {"match": "*", "kind": "docs"}]}
        found = self.matching(self.warnings(jsonRules=[rule, LOCK_RULE], extra_kinds=infra), "only dependency keys reach it")
        self.assertEqual(1, len(found))
        self.assertIn("kind 'infra'", found[0])
        self.assertIn("Packages/manifest.json key 'core.*'", found[0])

    def test_a_kind_that_an_area_also_reaches_or_without_l4_does_not_warn(self):
        infra = {"infra": {"L0": ["l0"], "L4": ["device:plans/open-playback"]}}
        rule = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [
            {"match": "core.*", "kind": "infra"}, {"match": "*", "kind": "docs"}]}
        reached = self.warnings(jsonRules=[rule, LOCK_RULE], extra_kinds=infra, extra_areas=[{"glob": "tools/**", "kind": "infra"}])
        self.assertEqual([], self.matching(reached, "only dependency keys reach it"))
        light = {"infra": {"L0": ["l0"], "L2": ["editmode:Ui.Tests"]}}
        self.assertEqual([], self.matching(self.warnings(jsonRules=[rule, LOCK_RULE], extra_kinds=light), "only dependency keys reach it"))

    def test_an_invalid_map_gives_errors_and_none_of_the_new_warnings_and_no_exception(self):
        bad_kind = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kind": "ghost"}]}
        both = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kind": "media", "kinds": ["media"]}]}
        for rule in (bad_kind, both):
            data = copy.deepcopy(CHECKS)
            data["jsonRules"] = [rule]
            self.checks_path.write_text(json.dumps(data), encoding="utf-8")
            errors, warnings = select.validate_config(select.load_config(self.checks_path))
            self.assertTrue(errors, rule)
            self.assertEqual([], self.matching(warnings, "fallback key"), rule)
            self.assertEqual([], self.matching(warnings, "is not:"), rule)

    def test_a_lock_file_that_a_wildcard_json_rule_covers_gives_no_warning(self):
        rules = [KEY_RULE, {"glob": "Packages/*.json", "root": "dependencies", "keys": [{"match": "*", "kind": "docs"}]}]
        self.assertEqual([], self.matching(self.warnings(jsonRules=rules, extra_areas=[{"glob": "Packages/**", "kind": "media"}]), "is not:"))

    def test_the_lock_warning_for_an_area_without_heavy_checks_names_the_area_only(self):
        found = self.matching(self.warnings(jsonRules=[KEY_RULE], extra_areas=[{"glob": "Packages/**", "kind": "ui"}]), "Packages/packages-lock.json")
        self.assertEqual(1, len(found))
        self.assertIn(f"areas[{AREAS}] (kind 'ui')", found[0])
        self.assertNotIn("with L", found[0])

    def test_a_fallback_key_with_several_kinds_names_only_the_heavy_one(self):
        rule = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kinds": ["docs", "media"]}]}
        found = self.matching(self.warnings(jsonRules=[rule, LOCK_RULE]), "the fallback key '*'")
        self.assertEqual(1, len(found))
        self.assertIn("kind 'media'", found[0])

    def test_more_than_three_dependency_keys_are_shortened(self):
        infra = {"infra": {"L0": ["l0"], "L4": ["device:plans/open-playback"]}}
        keys = [{"match": f"lib{number}.*", "kind": "infra"} for number in range(5)] + [{"match": "*", "kind": "docs"}]
        rule = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": keys}
        found = self.matching(self.warnings(jsonRules=[rule, LOCK_RULE], extra_kinds=infra), "only dependency keys reach it")
        self.assertEqual(1, len(found))
        self.assertIn("+2 more", found[0])

    def test_the_l4_warning_stays_when_a_heavy_fallback_is_allowed(self):
        infra = {"infra": {"L0": ["l0"], "L3": ["player:win64"], "L4": ["device:plans/open-playback"]}}
        rule = {"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kind": "infra", "allowHeavyFallback": True}]}
        warnings = self.warnings(jsonRules=[rule, LOCK_RULE], extra_kinds=infra)
        self.assertEqual([], self.matching(warnings, "the fallback key '*'"))
        self.assertEqual(1, len(self.matching(warnings, "only dependency keys reach it")))

    def test_the_warnings_are_printed_and_the_exit_code_stays_zero(self):
        data = copy.deepcopy(CHECKS)
        data["jsonRules"] = [{"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kind": "media"}]}]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(0, code, err)
        self.assertIn("the fallback key '*'", out + err)

    def test_the_schema_knows_the_flag_and_wants_a_boolean(self):
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-allow-heavy-fallback.json"))
        self.assertEqual(0, code, out)
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-allow-heavy-fallback-not-boolean.json"))
        self.assertEqual(1, code)


if __name__ == "__main__":
    unittest.main()
