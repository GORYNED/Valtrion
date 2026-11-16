"""`check-config` warnings about Editor test selection: an `editor` binding that is the whole set, a runner that passes assemblies to
`-testFilter`, and `unityEditorSlots` in the map, which only `host.json` enforces. All are warnings: exit code 0."""

import copy
import json
import unittest

from valtrion import select

from .test_select import CHECKS, RepoCase, run

FILTER_RUNNER = 'valtrion unity run -- -runTests -testPlatform EditMode -testFilter "{assemblies;}"'
NAMES_RUNNER = 'valtrion unity run -- -runTests -testPlatform EditMode -assemblyNames "{assemblies;}"'
PRESET_MAP = {
    "schema": "gdv.checks/1",
    "platforms": {"required": ["ios"], "dev": ["editor"]},
    "extends": "valtrion:unity/1",
    "runners": {"unity-editmode": NAMES_RUNNER},
    "areas": [{"glob": "Assets/Game/Ui/**", "kind": "ui", "overridesPreset": True}],
    "kinds": {"ui": {"L0": ["l0"], "L2": ["editmode:Game.Ui.Tests@unity-editmode"]}},
    "bindings": {"editor": ["editmode:Game.Tests,Game.Ui.Tests@unity-editmode"], "player": [], "device": []},
    "unclassified": "fail",
}


class EditorWarningTests(RepoCase):
    def warnings(self, data):
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, warnings = select.validate_config(select.load_config(self.checks_path))
        self.assertEqual([], errors)
        return [message for message in warnings if not message.startswith("fullSetReasons")]

    def matching(self, warnings, text):
        return [item for item in warnings if text in item]

    def without_slots(self):
        data = copy.deepcopy(CHECKS)
        del data["unityEditorSlots"]
        return data

    # --- unityEditorSlots -------------------------------------------------------------------------------------------------------

    def test_unity_editor_slots_in_the_map_is_called_informational(self):
        found = self.matching(self.warnings(copy.deepcopy(CHECKS)), "unityEditorSlots")
        self.assertEqual(1, len(found))
        self.assertIn("informational", found[0])
        self.assertIn("host.json", found[0])

    def test_a_map_without_unity_editor_slots_has_no_slot_warning(self):
        self.assertEqual([], self.matching(self.warnings(self.without_slots()), "unityEditorSlots"))

    # --- -testFilter with the assembly list --------------------------------------------------------------------------------------

    def test_a_named_runner_with_test_filter_assemblies_is_warned_about(self):
        data = self.without_slots()
        data["runners"] = {"unity-editmode": FILTER_RUNNER}
        found = self.matching(self.warnings(data), "-testFilter")
        self.assertEqual(1, len(found))
        self.assertTrue(found[0].startswith("runners.unity-editmode:"), found[0])
        self.assertIn("-assemblyNames", found[0])

    def test_the_comma_placeholder_and_an_unquoted_value_are_found_too(self):
        data = self.without_slots()
        data["runners"] = {"a": "run -testFilter {assemblies}", "b": "run -testfilter='{assemblies;}'"}
        self.assertEqual(2, len(self.matching(self.warnings(data), "-testFilter gets the assembly list")))

    def test_assembly_names_and_a_test_filter_of_its_own_give_no_warning(self):
        data = self.without_slots()
        data["runners"] = {"names": NAMES_RUNNER, "own": 'run -assemblyNames "{assemblies;}" -testFilter "^Game\\.Scenarios\\."'}
        self.assertEqual([], self.matching(self.warnings(data), "-testFilter"))

    def test_a_literal_runner_of_an_entry_is_warned_about_once(self):
        data = self.without_slots()
        data["kinds"]["media"]["L2"] = [f"editmode:Media.Tests@{FILTER_RUNNER}"]
        data["kinds"]["ui"]["L2"] = [f"editmode:Ui.Tests@{FILTER_RUNNER}"]
        found = self.matching(self.warnings(data), "-testFilter")
        self.assertEqual(1, len(found), found)
        self.assertTrue(found[0].startswith("kinds."), found[0])

    def test_the_warning_is_printed_and_the_exit_code_stays_zero(self):
        data = self.without_slots()
        data["runners"] = {"unity-editmode": FILTER_RUNNER}
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(0, code, err)
        self.assertIn("-testFilter gets the assembly list", out + err)

    # --- an editor binding that is the whole set ---------------------------------------------------------------------------------

    def test_a_binding_that_names_every_assembly_of_the_project_kinds_is_the_whole_set(self):
        found = self.matching(self.warnings(copy.deepcopy(PRESET_MAP)), "bindings.editor")
        self.assertEqual(1, len(found))
        self.assertIn("2 assemblies", found[0])
        self.assertIn("**/*.cs", found[0])
        self.assertIn("overridesPreset", found[0])

    def test_a_binding_without_an_assembly_placeholder_runs_the_whole_set(self):
        data = copy.deepcopy(PRESET_MAP)
        data["runners"]["all"] = "valtrion unity run -- -runTests -testPlatform EditMode"
        data["bindings"]["editor"] = ["editmode:Game.Tests@all"]
        found = self.matching(self.warnings(data), "bindings.editor")
        self.assertEqual(1, len(found))
        self.assertIn("whichever assemblies are selected", found[0])

    def test_a_binding_smaller_than_the_project_set_gives_no_warning(self):
        data = copy.deepcopy(PRESET_MAP)
        data["bindings"]["editor"] = ["editmode:Game.Tests,Game.Core.Tests@unity-editmode"]
        self.assertEqual([], self.matching(self.warnings(data), "bindings.editor"))

    def test_a_project_without_kinds_of_its_own_gives_no_warning(self):
        data = copy.deepcopy(PRESET_MAP)
        del data["areas"], data["kinds"]
        self.assertEqual([], self.matching(self.warnings(data), "bindings.editor"))

    def test_a_project_that_replaces_managed_code_gives_no_warning(self):
        data = copy.deepcopy(PRESET_MAP)
        data["kinds"]["managed-code"] = {"L0": ["l0"], "L2": ["editmode:Game.Tests@unity-editmode"]}
        self.assertEqual([], self.matching(self.warnings(data), "bindings.editor"))

    def test_an_empty_editor_binding_gives_no_warning(self):
        data = copy.deepcopy(PRESET_MAP)
        data["bindings"]["editor"] = []
        self.assertEqual([], self.matching(self.warnings(data), "bindings.editor"))

    def test_an_override_area_that_takes_all_code_silences_the_binding_warning(self):
        for glob in ("Assets/**", "**/*.cs", "Game/Assets/**/*.cs"):
            data = copy.deepcopy(PRESET_MAP)
            data["areas"].append({"glob": glob, "kind": "ui", "overridesPreset": True})
            self.assertEqual([], self.matching(self.warnings(data), "bindings.editor"), glob)

    def test_an_unfiltered_binding_without_kinds_of_its_own_gives_no_warning(self):
        data = copy.deepcopy(PRESET_MAP)
        del data["areas"], data["kinds"]
        data["runners"]["all"] = "valtrion unity run -- -runTests -testPlatform EditMode"
        data["bindings"]["editor"] = ["editmode:Game.Tests@all"]
        self.assertEqual([], self.matching(self.warnings(data), "bindings.editor"))

    def test_a_literal_runner_of_a_binding_is_named_under_bindings_once(self):
        data = copy.deepcopy(PRESET_MAP)
        data["bindings"]["editor"] = [f"editmode:Game.Tests@{FILTER_RUNNER}"]
        data["jsonRules"] = [{"glob": "Packages/manifest.json", "root": "dependencies", "keys": [
            {"match": "*", "kind": "ui", "byContent": {"managed": "ui", "platform": "ui",
                                                        "assemblies": {"kind": "ui", "runner": FILTER_RUNNER}}}]}]
        found = self.matching(self.warnings(data), "-testFilter gets the assembly list")
        self.assertEqual(1, len(found), found)
        self.assertTrue(found[0].startswith("bindings.editor[0]:"), found[0])

    def test_a_literal_equal_to_a_named_runner_is_not_reported_twice(self):
        data = self.without_slots()
        data["runners"] = {"unity-editmode": FILTER_RUNNER}
        data["kinds"]["ui"]["L2"] = [f"editmode:Ui.Tests@{FILTER_RUNNER}"]
        self.assertEqual(1, len(self.matching(self.warnings(data), "-testFilter gets the assembly list")))

    def test_a_map_without_extends_gives_no_binding_warning(self):
        self.assertEqual([], self.matching(self.warnings(self.without_slots()), "bindings.editor"))


if __name__ == "__main__":
    unittest.main()
