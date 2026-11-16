"""The runner placeholder `{prefixes;}`: each selected target as an anchored, escaped test-name prefix for `-testFilter`."""

import copy
import json
import re
import subprocess
import unittest

from valtrion import select

from .test_select import CHECKS, RepoCase

PREFIX_RUNNER = 'valtrion unity run -- -runTests -testPlatform EditMode -assemblyNames Game.Tests -testFilter "{prefixes;}"'


class ExpansionTests(unittest.TestCase):
    def test_each_target_is_anchored_and_its_dots_and_dashes_are_literal(self):
        self.assertEqual(r"\AGame\.Ui\.Tests\.Hud;\AGame\.Core\x2DTests",
                         select.expand_runner("{prefixes;}", ["Game.Ui.Tests.Hud", "Game.Core-Tests"]))

    def test_the_other_placeholders_still_get_the_plain_names(self):
        self.assertEqual(r"-assemblyNames A.Tests;B -names A.Tests,B -testFilter \AA\.Tests;\AB",
                         select.expand_runner("-assemblyNames {assemblies;} -names {assemblies} -testFilter {prefixes;}", ["A.Tests", "B"]))

    def test_a_prefix_matches_from_the_start_and_takes_class_name_prefixes(self):
        # Python's `re` reads `\A` and `\x2D` like .NET's Regex, which the Unity Test Framework uses for -testFilter.
        patterns = [re.compile(item) for item in select.expand_runner("{prefixes;}", ["Game.Ui.Tests.Hud"]).split(";")]
        matches = lambda name: any(pattern.search(name) for pattern in patterns)
        self.assertTrue(matches("Game.Ui.Tests.HudBindingTests.Binds"))
        self.assertTrue(matches("Game.Ui.Tests.Hud.Shows"))
        self.assertFalse(matches("Other.Game.Ui.Tests.Hud.Shows"))  # not inside a longer name
        self.assertFalse(matches("GameXUi.Tests.Hud.Shows"))  # a dot is a dot

    def test_the_value_has_no_character_that_cmd_or_a_quoted_shell_acts_on(self):
        value = select.expand_runner("{prefixes;}", ["Game.Ui.Tests", "A-B.Tests_1"])
        self.assertEqual(value, subprocess.list2cmdline([value]))  # no quoting needed for one name or for the Windows .bat hand-over
        self.assertIsNone(re.search(r"[\^&|<>%\"'()!$` \t]", value))


class CheckConfigTests(RepoCase):
    def warnings_and_errors(self, data):
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        return select.validate_config(select.load_config(self.checks_path))

    def base(self):
        data = copy.deepcopy(CHECKS)
        del data["unityEditorSlots"]
        return data

    def test_the_placeholder_is_known_and_counts_as_filtering(self):
        data = self.base()
        data["runners"] = {"prefixes": PREFIX_RUNNER}
        data["kinds"]["ui"]["L2"] = ["editmode:Game.Ui.Tests.Hud@prefixes"]
        errors, warnings = self.warnings_and_errors(data)
        self.assertEqual([], errors)
        self.assertEqual([], [item for item in warnings if "placeholder" in item or "-testFilter" in item], warnings)

    def test_a_mistyped_prefixes_placeholder_is_an_error_in_a_named_runner(self):
        data = self.base()
        data["runners"] = {"prefixes": "run -testFilter {prefixes}"}
        errors, _ = self.warnings_and_errors(data)
        self.assertTrue(any("unknown placeholder '{prefixes}'" in item and "{prefixes;}" in item for item in errors), errors)

    def test_assemblies_in_test_filter_point_to_the_prefixes_placeholder(self):
        data = self.base()
        data["runners"] = {"a": 'run -testFilter "{assemblies;}"'}
        _, warnings = self.warnings_and_errors(data)
        (found,) = [item for item in warnings if "-testFilter gets the assembly list" in item]
        self.assertIn("{prefixes;}", found)
        self.assertIn("unanchored", found)

    def test_prefixes_in_assembly_names_are_warned_about(self):
        data = self.base()
        data["runners"] = {"a": 'run -assemblyNames "{prefixes;}"'}
        _, warnings = self.warnings_and_errors(data)
        self.assertEqual(1, len([item for item in warnings if "-assemblyNames gets {prefixes;}" in item]), warnings)

    def test_select_puts_the_anchored_prefixes_into_the_united_command(self):
        data = self.base()
        data["runners"] = {"prefixes": PREFIX_RUNNER}
        data["kinds"]["ui"]["L2"] = ["editmode:Game.Ui.Tests.Hud@prefixes", "editmode:Game.Ui.Tests.Menu@prefixes"]
        config = select.Config(self.checks_path, data)
        (command,) = select.select(config, [], "L2", named_kinds=["ui"]).editor_commands()
        self.assertTrue(command["command"].endswith(r'-testFilter "\AGame\.Ui\.Tests\.Hud;\AGame\.Ui\.Tests\.Menu"'), command["command"])


if __name__ == "__main__":
    unittest.main()
