"""Named modes over the levels (`select --mode`), the default level and the closed list of reasons for a full set (`fullSetReasons`)."""

import copy
import json

from valtrion import select

from .test_select import RepoCase, run
from .test_select_kinds import MAP, keys

REASONS = ["release", "data-migration", "unity-version"]


class ModeCase(RepoCase):
    checks = {**copy.deepcopy(MAP), "fullSetReasons": REASONS}

    def select(self, *arguments, kind="release"):
        code, out, err = run("select", "--checks", str(self.checks_path), "--kind", kind, "--json", *arguments)
        return code, (json.loads(out) if out.strip().startswith("{") else None), err


class ModeTests(ModeCase):
    def test_each_mode_selects_up_to_its_level(self):
        for mode, level in (("fast", "L1"), ("affected", "L2")):
            code, payload, _ = self.select("--mode", mode)
            self.assertEqual((0, level, mode), (code, payload["level"], payload["mode"]))
        code, payload, _ = self.select("--mode", "full", "--reason", "data-migration", kind="scheduled")
        self.assertEqual((0, "L3", "full"), (code, payload["level"], payload["mode"]))
        self.assertEqual(["player:win64"], keys(payload, "playerTests"))
        code, payload, _ = self.select("--mode", "release")
        self.assertEqual((0, "L4", "release"), (code, payload["level"], payload["mode"]))
        self.assertEqual(["example.perf.open-playback"], keys(payload, "devicePlans"))

    def test_the_default_is_the_affected_checks(self):
        code, payload, _ = self.select()
        self.assertEqual((0, "L2", "affected", None), (code, payload["level"], payload["mode"], payload["fullSet"]))
        self.assertEqual([], payload["devicePlans"])

    def test_fast_leaves_out_the_suites_of_level_two(self):
        _, payload, _ = self.select("--mode", "fast")
        self.assertEqual([], payload["editorSuites"])
        _, payload, _ = self.select("--mode", "affected")
        self.assertEqual(["editmode:Release.Tests"], keys(payload))

    def test_mode_and_level_exclude_each_other(self):
        code, _, err = self.select("--mode", "fast", "--level", "L1")
        self.assertEqual(1, code)
        self.assertIn("--mode or --level", err)


class ReasonTests(ModeCase):
    def test_the_full_mode_needs_a_reason_from_the_list(self):
        code, _, err = self.select("--mode", "full")
        self.assertEqual(1, code)
        self.assertIn("release, data-migration, unity-version", err)
        code, _, err = self.select("--mode", "full", "--reason", "because")
        self.assertEqual(1, code)
        self.assertIn("not in fullSetReasons", err)

    def test_the_full_mode_needs_a_map_that_lists_reasons(self):
        data = copy.deepcopy(MAP)
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = self.select("--mode", "full", "--reason", "release")
        self.assertEqual(1, code)
        self.assertIn("needs fullSetReasons", err)

    def test_the_release_mode_needs_no_reason_and_checks_one_that_is_given(self):
        code, payload, _ = self.select("--mode", "release")
        self.assertEqual((0, "release"), (code, payload["fullSet"]["reason"]))
        code, payload, _ = self.select("--mode", "release", "--reason", "unity-version")
        self.assertEqual("unity-version", payload["fullSet"]["reason"])
        code, _, _ = self.select("--mode", "release", "--reason", "because")
        self.assertEqual(1, code)

    def test_a_level_above_the_default_needs_a_reason_when_the_map_lists_them(self):
        for level in ("L3", "L4"):
            code, _, err = self.select("--level", level)
            self.assertEqual(1, code)
            self.assertIn("is a full set", err)
        code, payload, _ = self.select("--level", "L4", "--reason", "release")
        self.assertEqual((0, "release", "release"), (code, payload["fullSet"]["reason"], payload["mode"]))

    def test_a_reason_without_a_full_set_is_an_error(self):
        code, _, err = self.select("--reason", "release")
        self.assertEqual(1, code)
        self.assertIn("applies to a level above", err)

    def test_a_map_without_the_list_allows_any_level_and_records_the_reason_as_given(self):
        self.checks_path.write_text(json.dumps(copy.deepcopy(MAP)), encoding="utf-8")
        code, payload, _ = self.select("--level", "L4")
        self.assertEqual((0, None), (code, payload["fullSet"]["reason"]))
        code, payload, _ = self.select("--level", "L4", "--reason", "anything")
        self.assertEqual("anything", payload["fullSet"]["reason"])


class ExplainTests(ModeCase):
    def test_explain_names_the_reason_and_the_checks_by_level(self):
        code, text, _ = run("select", "--checks", str(self.checks_path), "--kind", "release", "--mode", "release", "--reason", "data-migration", "--explain")
        self.assertEqual(0, code)
        self.assertIn("Level: mode release, L4 is a full set, reason data-migration", text)
        self.assertIn("  L2: 1 (editmode:Release.Tests)", text)
        self.assertIn("  L4: 1 (example.perf.open-playback)", text)
        code, text, _ = run("select", "--checks", str(self.checks_path), "--kind", "release", "--explain")
        self.assertIn("Level: mode affected, L2: the checks the change affects", text)
        self.assertNotIn("L4:", text)

    def test_the_json_has_the_breakdown(self):
        _, payload, _ = self.select("--mode", "release")
        self.assertEqual(["editmode:Release.Tests"], payload["levelBreakdown"]["L2"])
        self.assertEqual(["example.perf.open-playback"], payload["levelBreakdown"]["L4"])


class ConfigTests(RepoCase):
    checks = copy.deepcopy(MAP)

    def test_a_map_with_full_levels_and_no_reasons_gets_a_warning(self):
        _, warnings = select.validate_config(self.config())
        self.assertTrue(any(message.startswith("fullSetReasons: not declared") for message in warnings), warnings)

    def test_a_map_with_reasons_does_not(self):
        data = {**copy.deepcopy(MAP), "fullSetReasons": REASONS}
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        _, warnings = select.validate_config(self.config())
        self.assertFalse(any("fullSetReasons" in message for message in warnings), warnings)

    def test_a_reason_listed_twice_is_an_error(self):
        data = {**copy.deepcopy(MAP), "fullSetReasons": ["release", "release"]}
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, _ = select.validate_config(self.config())
        self.assertTrue(any("'release' is listed twice" in message for message in errors), errors)

    def test_the_schema_knows_the_list(self):
        from .test_select import CONTRACTS
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-full-set-reasons.json"))
        self.assertEqual(0, code, out)
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-full-set-reasons-empty.json"))
        self.assertEqual(1, code)
