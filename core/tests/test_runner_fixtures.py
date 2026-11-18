"""Fixture targets keep common Unity kinds focused without matching neighboring class names."""

import copy
import json
import re
import subprocess
import unittest

from valtrion import select

from .test_select import RepoCase


RUNNER = 'valtrion unity run -- -runTests -testPlatform EditMode -testFilter "{fixtures;}"'


class FixtureExpansionTests(unittest.TestCase):
    def test_only_the_named_fixture_and_its_parameterized_cases_match(self):
        pattern = re.compile(select.expand_runner("{fixtures;}", ["Game.Tests.AssetTests"]))
        for name in ("Game.Tests.AssetTests.Loads", "Game.Tests.AssetTests.Loads(1)",
                     "Game.Tests.AssetTests(1).Loads", 'Game.Tests.AssetTests("asset").Loads'):
            self.assertIsNotNone(pattern.search(name), name)
        for name in ("Game.Tests.AssetTestsExtra.Loads", "Other.Game.Tests.AssetTests.Loads", "GameXTests.AssetTests.Loads"):
            self.assertIsNone(pattern.search(name), name)

    def test_filter_survives_the_windows_batch_handover(self):
        value = select.expand_runner("{fixtures;}", ["Game.Tests.AssetTests", "Game.Core-Tests"])
        self.assertEqual(value, subprocess.list2cmdline([value]))
        self.assertIsNone(re.search(r"[\^&|<>%\"'()!$` \t]", value))


class FixtureSelectionTests(RepoCase):
    def test_common_preset_kinds_select_fixtures_and_mixed_changes_take_the_union(self):
        data = {
            "schema": "gdv.checks/1", "platforms": {"required": ["editor"]},
            "extends": "valtrion:unity/1", "bindings": {"editor": [], "player": [], "device": []},
            "runners": {"fixtures": RUNNER}, "unclassified": "fail",
            "kinds": {
                "managed-code": {"structure": ["structure"], "affected": ["editmode:Game.Tests.RuntimeTests@fixtures"]},
                "editor-code": {"structure": ["structure"], "affected": ["editmode:Game.Tests.EditorTests@fixtures"]},
                "tests": {"structure": ["structure"], "affected": ["editmode:Game.Tests.HarnessTests@fixtures"]},
                "asset-meta": {"structure": ["structure"], "affected": ["editmode:Game.Tests.AssetTests@fixtures"]},
            },
        }
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        config = select.load_config(self.checks_path)
        errors, warnings = select.validate_config(config)
        self.assertEqual([], errors)
        self.assertFalse([warning for warning in warnings if "placeholder" in warning or "-testFilter" in warning], warnings)
        selection = select.select(config, ["Assets/Editor/Tool.cs", "Assets/Tests/ToolTests.cs", "Assets/Data.asset.meta"], "affected")
        (command,) = selection.editor_commands()
        expected = ["Game.Tests.AssetTests", "Game.Tests.EditorTests", "Game.Tests.HarnessTests", "Game.Tests.RuntimeTests"]
        self.assertEqual(expected, command["assemblies"])
        self.assertEqual(RUNNER.replace("{fixtures;}", ";".join(select.anchored_prefix(name) + r"[.\x28]" for name in expected)), command["command"])
        self.assertFalse(select.select(config, ["unknown.file"], "affected").checks)

    def test_fixture_typo_is_rejected_and_assembly_names_warns(self):
        from .test_select import CHECKS

        data = copy.deepcopy(CHECKS)
        data["runners"] = {"fixtures": "run -testFilter {fixtures}"}
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, _ = select.validate_config(select.load_config(self.checks_path))
        self.assertTrue(any("unknown placeholder '{fixtures}'" in message for message in errors), errors)
        data["runners"]["fixtures"] = 'run -assemblyNames "{fixtures;}"'
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, warnings = select.validate_config(select.load_config(self.checks_path))
        self.assertEqual([], errors)
        self.assertTrue(any("-assemblyNames gets {fixtures;}" in message for message in warnings), warnings)


if __name__ == "__main__":
    unittest.main()
