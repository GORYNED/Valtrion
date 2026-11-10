"""`extends` and `bindings` of `checks.json`: the built-in preset `valtrion:unity/1`, its slots, and how a project's map is merged with it."""

import copy
import json
import unittest

from valtrion import presets, select

from .test_select import CONTRACTS, RepoCase, git, run, write

PRESET = "valtrion:unity/1"
BINDINGS = {"editor": ["editmode:Game.Tests", "playmode:Game.Play.Tests"], "player": ["player:android"], "device": ["device:plans/open-playback"]}
MAP = {"schema": "gdv.checks/1", "platforms": {"required": ["ios"], "dev": ["editor"]}, "extends": PRESET, "bindings": BINDINGS, "unclassified": "fail"}

#: The preset is versioned: this table is the contract of `valtrion:unity/1`. A change to a row is a change of the preset and needs a new
#: preset version (`valtrion:unity/2`), never an edit of this one.
GOLDEN = {
    "docs/guide.md": ["docs"],
    "README.md": ["docs"],
    "tools/run.py": ["tooling"],
    "Assets/Game/Foo.cs": ["managed-code"],
    "Assets/Game/Editor/Tool.cs": ["editor-code", "managed-code"],
    "Assets/Game/Tests/FooTests.cs": ["managed-code", "tests"],
    "Assets/Game/Foo.cs.meta": ["asset-meta"],
    "Assets/Game/Game.asmdef": ["assembly-config"],
    "Assets/Plugins/Android/lib.aar": ["native"],
    "Assets/Game/Native/lib.so": ["native"],
    "Assets/Plugins/lib.so.meta": ["asset-meta", "native"],
    "Assets/Game/Bridge.mm": ["native"],
    "Assets/Game/Effect.shader": ["shader"],
    "Assets/Game/Effect.shader.meta": ["asset-meta", "shader"],
    "Assets/Game/link.xml": ["linking"],
    "ProjectSettings/ProjectSettings.asset": ["platform-settings"],
    "ProjectSettings/QualitySettings.asset": ["platform-settings"],
    "ProjectSettings/NewThing.asset": ["platform-settings"],
    "ProjectSettings/TagManager.asset": ["project-settings"],
    "ProjectSettings/EditorSettings.asset": ["project-settings"],
    "Assets/Settings/UniversalRP.asset": ["platform-settings"],
    "Assets/AddressableAssetsData/Group.asset": ["platform-settings"],
    "tools/Showcase/ProjectSettings/QualitySettings.asset": ["platform-settings"],
    "docs/img/diagram.png": ["docs"],
    "tools/check/run.json": ["tooling"],
}
#: A Unity project nested under `docs/` or `tools/` is never lowered to L0 by its folder: assets and prefabs there are unclassified.
UNCLASSIFIED = ["Assets/Game/Data.asset", "Packages/manifest.json", "Packages/packages-lock.json", "other/x.txt",
                "docs/Sample/Assets/Scene.unity", "tools/Showcase/Assets/Hero.prefab"]
#: The levels of every class of `valtrion:unity/1`, by slot: the contract of the preset next to the path table above.
LEVELS = {
    "docs": ["L0"], "tooling": ["L0"], "comment-only": ["L0"],
    "asset-meta": ["L0", "L2"], "managed-code": ["L0", "L2"], "editor-code": ["L0", "L2"], "tests": ["L0", "L2"], "assembly-config": ["L0", "L2"],
    "project-settings": ["L0", "L2"], "package-managed": ["L0", "L2"],
    "linking": ["L0", "L2", "L3"],
    "native": ["L0", "L2", "L3", "L4"], "shader": ["L0", "L2", "L3", "L4"], "platform-settings": ["L0", "L2", "L3", "L4"], "package-platform": ["L0", "L2", "L3", "L4"],
}


class PresetCase(RepoCase):
    checks = MAP

    def write_map(self, **changes):
        data = copy.deepcopy(MAP)
        data.update(changes)
        # The test rewrites checks.json itself: classify that path, or the policy `fail` would select nothing.
        data["areas"] = list(data.get("areas", [])) + [{"glob": "validation/**", "kind": "docs"}]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")

    def load(self):
        return select.load_config(self.checks_path)

    def base_file(self, relative, text):
        git(self.root, "checkout", "-q", "main")
        write(self.root, relative, text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", f"base {relative}")
        git(self.root, "checkout", "-q", "-B", "work")

    def select_json(self, *arguments):
        code, out, err = run("select", "--level", "L4", "--checks", str(self.checks_path), "--base-ref", "main", "--json", *arguments)
        self.assertIn(code, (0, 1), err)
        payload = json.loads(out)
        # The test rewrites checks.json itself; leave that path out of what is compared.
        payload["pathKinds"].pop("validation/device/checks.json", None)
        return payload

    def heavy(self, payload):
        """What a change needs beyond the Editor: `player` when player tests are selected, `device` when device plans are."""
        return sorted(name for name, key in (("device", "devicePlans"), ("player", "playerTests")) if payload[key])


class ExpandTests(unittest.TestCase):
    def test_a_map_without_extends_is_returned_as_it_is(self):
        data = {"areas": [], "kinds": {}}
        self.assertEqual((data, None), presets.expand(data))

    def test_the_project_comes_first_and_its_kinds_replace_a_preset_kind_of_the_same_name(self):
        data = copy.deepcopy(MAP)
        data["areas"] = [{"glob": "game/**", "kind": "game"}]
        data["jsonRules"] = [{"glob": "Packages/manifest.json", "root": "dependencies", "keys": [{"match": "*", "kind": "game"}]}]
        data["kinds"] = {"game": {"L0": ["l0"]}, "docs": {"L0": ["l0"], "L2": ["editmode:Doc.Tests"]}}
        effective, info = presets.expand(data)
        self.assertEqual({"areas": 1, "jsonRules": 1, "contentRules": 0}, info["projectCounts"])
        self.assertEqual({"glob": "game/**", "kind": "game"}, effective["areas"][0])
        self.assertEqual("Packages/manifest.json", effective["jsonRules"][0]["glob"])
        self.assertEqual("**/Packages/manifest.json", effective["jsonRules"][1]["glob"])
        self.assertEqual(["editmode:Doc.Tests"], effective["kinds"]["docs"]["L2"])
        self.assertEqual(["l0"], effective["kinds"]["tooling"]["L0"])
        self.assertEqual(data["kinds"]["docs"], {"L0": ["l0"], "L2": ["editmode:Doc.Tests"]}, "the project's own data is not changed")

    def test_the_slots_become_the_bound_checks(self):
        effective, _ = presets.expand(copy.deepcopy(MAP))
        self.assertEqual(BINDINGS["editor"], effective["kinds"]["managed-code"]["L2"])
        self.assertNotIn("L3", effective["kinds"]["managed-code"])
        self.assertEqual(BINDINGS["player"], effective["kinds"]["native"]["L3"])
        self.assertEqual(BINDINGS["device"], effective["kinds"]["native"]["L4"])
        self.assertEqual({"L0": ["l0"], "L2": BINDINGS["editor"], "L3": BINDINGS["player"]}, effective["kinds"]["linking"])

    def test_the_levels_of_every_class_are_the_contract_and_no_editor_check_is_lost(self):
        effective, _ = presets.expand(copy.deepcopy(MAP))
        self.assertEqual(LEVELS, {name: sorted(levels) for name, levels in effective["kinds"].items()})
        for name, levels in effective["kinds"].items():
            if name not in ("docs", "tooling", "comment-only"):
                self.assertEqual(BINDINGS["editor"], levels["L2"], f"{name} keeps the Editor suites")
        self.assertEqual({"L0": ["l0"]}, {"L0": effective["kinds"]["docs"]["L0"]})

    def test_a_project_kind_that_leaves_out_a_level_is_recorded_as_replaced(self):
        data = copy.deepcopy(MAP)
        data["kinds"] = {"shader": {"L0": ["l0"], "L2": ["editmode:Game.Tests"]}, "docs": {"L0": ["l0"]}}
        _, info = presets.expand(data)
        self.assertEqual({"shader": ["L3", "L4"], "docs": []}, info["replaced"])

    def test_an_empty_binding_is_an_explicit_answer(self):
        data = copy.deepcopy(MAP)
        data["bindings"] = {"editor": [], "player": [], "device": []}
        effective, _ = presets.expand(data)
        self.assertEqual([], effective["kinds"]["native"]["L4"])

    def test_a_missing_or_unknown_slot_or_preset_is_an_error_that_says_what_to_do(self):
        missing = copy.deepcopy(MAP)
        del missing["bindings"]["device"]
        with self.assertRaisesRegex(presets.PresetError, "needs a list of checks for device"):
            presets.expand(missing)
        none = copy.deepcopy(MAP)
        del none["bindings"]
        with self.assertRaisesRegex(presets.PresetError, "editor, player, device"):
            presets.expand(none)
        extra = copy.deepcopy(MAP)
        extra["bindings"]["devices"] = []
        with self.assertRaisesRegex(presets.PresetError, "devices is not a slot"):
            presets.expand(extra)
        unknown = copy.deepcopy(MAP)
        unknown["extends"] = "valtrion:unity/9"
        with self.assertRaisesRegex(presets.PresetError, "unknown preset .*valtrion:unity/1"):
            presets.expand(unknown)

    def test_a_caller_can_never_change_the_preset(self):
        first, _ = presets.expand(copy.deepcopy(MAP))
        first["kinds"]["native"]["L4"].append("device:plans/x")
        first["areas"].append({"glob": "x", "kind": "docs"})
        second, _ = presets.expand(copy.deepcopy(MAP))
        self.assertEqual(BINDINGS["device"], second["kinds"]["native"]["L4"])
        self.assertNotIn({"glob": "x", "kind": "docs"}, second["areas"])


class SelectionTests(PresetCase):
    def test_the_golden_table_of_the_preset(self):
        config = self.load()
        classified, unclassified = select.classify(config, list(GOLDEN) + UNCLASSIFIED)
        self.assertEqual(GOLDEN, {path: kinds for path, kinds in classified.items()})
        self.assertEqual(sorted(UNCLASSIFIED), sorted(unclassified))

    def test_managed_code_selects_the_editor_suites_and_nothing_heavier(self):
        write(self.root, "Assets/Game/Foo.cs", "changed\n")
        self.commit()
        payload = self.select_json()
        self.assertEqual(["managed-code"], payload["kinds"])
        self.assertEqual([], self.heavy(payload))
        self.assertEqual(["editmode:Game.Tests", "playmode:Game.Play.Tests"], sorted(item["key"] for item in payload["editorSuites"]))

    def test_native_shader_and_platform_settings_select_a_player_and_a_device_plan(self):
        for path in ("Assets/Plugins/Android/lib.aar", "Assets/Game/Effect.shader", "ProjectSettings/QualitySettings.asset", "ProjectSettings/Brand.asset"):
            write(self.root, path, "changed\n")
            self.commit()
            self.assertEqual(["device", "player"], self.heavy(self.select_json()), path)
            git(self.root, "reset", "-q", "--hard", "main")

    def test_light_settings_and_links_and_docs(self):
        cases = {"ProjectSettings/TagManager.asset": [], "Assets/Game/link.xml": ["player"], "docs/guide.md": [], "tools/run.py": []}
        for path, expected in cases.items():
            write(self.root, path, "changed\n")
            self.commit()
            self.assertEqual(expected, self.heavy(self.select_json()), path)
            git(self.root, "reset", "-q", "--hard", "main")

    def test_a_dependency_key_is_judged_by_the_preset_and_the_fallback_is_heavy(self):
        manifest = {"dependencies": {"com.goryned.valtrion.unity": "1.0.0", "com.unity.ide.rider": "1.0.0", "com.example.lib": "1.0.0"}}
        self.base_file("Packages/manifest.json", json.dumps(manifest))
        self.base_file("Packages/packages-lock.json", json.dumps(manifest))
        for name, expected_kind, expected_heavy in (("com.goryned.valtrion.unity", "package-managed", []), ("com.unity.ide.rider", "package-managed", []),
                                                     ("com.example.lib", "package-platform", ["device", "player"])):
            changed = copy.deepcopy(manifest)
            changed["dependencies"][name] = "2.0.0"
            for relative in ("Packages/manifest.json", "Packages/packages-lock.json"):
                write(self.root, relative, json.dumps(changed))
            self.commit()
            payload = self.select_json()
            self.assertEqual([expected_kind], payload["kinds"], name)
            self.assertEqual(expected_heavy, self.heavy(payload), name)
            git(self.root, "reset", "-q", "--hard", "main")

    def test_a_project_area_adds_to_the_preset_unless_it_overrides_it(self):
        area = {"glob": "Assets/Game/Plugins/Managed/**", "kind": "tests"}
        self.write_map(areas=[area])
        write(self.root, "Assets/Game/Plugins/Managed/Util.cs", "changed\n")
        self.commit()
        added = self.select_json()
        self.assertEqual(["device", "player"], self.heavy(added), "the preset still sees Plugins/** and adds native")
        self.write_map(areas=[{**area, "overridesPreset": True}])
        lighter = self.select_json()
        self.assertEqual([], self.heavy(lighter))
        self.assertEqual(["tests"], lighter["pathKinds"]["Assets/Game/Plugins/Managed/Util.cs"])

    def test_a_comment_only_change_of_code_is_cosmetic_and_the_explanation_names_the_preset_rule(self):
        self.base_file("Assets/Game/Foo.cs", "int a;\n")
        write(self.root, "Assets/Game/Foo.cs", "int a;\n// a note\n")
        self.commit()
        payload = self.select_json("--explain")
        self.assertEqual({"Assets/Game/Foo.cs": "comment-only"}, payload["cosmeticPaths"])
        self.assertEqual([], self.heavy(payload))
        self.assertEqual([], payload["editorSuites"])
        because = [item for item in next(entry for entry in payload["explain"] if entry["check"] == "l0")["because"] if "Assets/Game/Foo.cs" in item["paths"]]
        self.assertEqual(1, len(because))
        self.assertEqual(("comment-only", PRESET), (because[0]["kind"], because[0]["from"]))
        self.assertTrue(because[0]["rule"].startswith("contentRules["))

    def test_nested_assets_under_tools_or_docs_are_not_lowered_to_l0(self):
        for path in ("tools/Showcase/Assets/Hero.prefab", "docs/Sample/Assets/Scene.unity"):
            write(self.root, path, "changed\n")
            self.commit()
            code, out, _ = run("select", "--level", "L4", "--checks", str(self.checks_path), "--base-ref", "main", "--json")
            self.assertEqual(1, code, path)
            self.assertIn(path, json.loads(out)["unclassifiedPaths"], path)
            git(self.root, "reset", "-q", "--hard", "main")

    def test_the_preset_does_not_hide_an_unclassified_path_from_the_policy(self):
        write(self.root, "other/x.txt", "changed\n")
        self.commit()
        code, out, _ = run("select", "--level", "L4", "--checks", str(self.checks_path), "--base-ref", "main", "--json")
        self.assertEqual(1, code)
        self.assertIn("other/x.txt", json.loads(out)["unclassifiedPaths"])

    def test_explain_names_the_preset_rules(self):
        write(self.root, "Assets/Plugins/Android/lib.aar", "changed\n")
        self.commit()
        payload = self.select_json("--explain")
        because = next(item for item in payload["explain"] if item["check"] == "player:android")["because"]
        self.assertTrue(all(item["from"] == PRESET for item in because), because)
        code, out, _ = run("select", "--level", "L4", "--checks", str(self.checks_path), "--base-ref", "main", "--explain")
        self.assertIn(f"glob **/Android/** of {PRESET}", out)

    def test_a_project_rule_is_named_without_the_preset(self):
        self.write_map(areas=[{"glob": "Assets/Game/**", "kind": "docs"}])
        write(self.root, "Assets/Game/Foo.cs", "changed\n")
        self.commit()
        because = next(item for item in self.select_json("--explain")["explain"] if item["check"] == "l0")["because"]
        source = {(item["kind"], item["glob"]): item.get("from") for item in because if "src" not in item["glob"]}
        self.assertEqual(None, source[("docs", "Assets/Game/**")])
        self.assertEqual(PRESET, source[("managed-code", "**/*.cs")])


class CheckConfigTests(PresetCase):
    def test_a_valid_map_has_no_warning_about_the_presets_own_kinds_or_rules(self):
        self.write_map(fullSetReasons=["release"])
        errors, warnings = select.validate_config(self.load())
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_an_unknown_preset_is_a_config_error_that_names_the_built_in_ones(self):
        self.write_map(extends="valtrion:unity/9")
        code, _, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("valtrion:unity/1", err)

    def test_a_missing_binding_is_an_error(self):
        data = copy.deepcopy(MAP)
        del data["bindings"]["player"]
        self.write_map(bindings=data["bindings"])
        code, _, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("player", err)

    def test_a_bad_bound_check_is_reported_once_under_bindings(self):
        self.write_map(bindings={**BINDINGS, "player": ["bogus:x"]})
        errors, _ = select.validate_config(self.load())
        self.assertEqual(1, len(errors), errors)
        self.assertTrue(errors[0].startswith("bindings.player[0]: "), errors)
        self.assertIn("unknown check type", errors[0])

    def test_a_project_kind_that_leaves_out_a_preset_level_warns(self):
        self.write_map(kinds={"shader": {"L0": ["l0"], "L2": ["editmode:Game.Tests"]}, "docs": {"L0": ["l0"]}})
        _, warnings = select.validate_config(self.load())
        found = [item for item in warnings if item.startswith("kinds.shader:")]
        self.assertEqual(1, len(found), warnings)
        self.assertIn("L3, L4", found[0])
        self.assertEqual([], [item for item in warnings if item.startswith("kinds.docs:")], "a replacement that keeps every level is silent")

    def test_bindings_and_overrides_preset_without_extends_warn_that_they_do_nothing(self):
        data = {"schema": "gdv.checks/1", "platforms": {"required": ["ios"]}, "bindings": BINDINGS, "unclassified": "fail",
                "areas": [{"glob": "src/**", "kind": "docs", "overridesPreset": True}], "kinds": {"docs": {"L0": ["l0"]}}}
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        errors, warnings = select.validate_config(select.load_config(self.checks_path))
        self.assertEqual([], errors)
        self.assertEqual(["bindings: has no effect without extends", "areas[0]: overridesPreset has no effect without extends"],
                         [item for item in warnings if "has no effect" in item])

    def test_the_schema_accepts_extends_and_bindings_and_rejects_a_bad_id_or_a_map_with_neither_areas_nor_extends(self):
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-extends-preset.json"))
        self.assertEqual(0, code, out)
        for name in ("checks-extends-bad-preset-id.json", "checks-without-areas-and-extends.json"):
            code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / name))
            self.assertEqual(1, code, name)


if __name__ == "__main__":
    unittest.main()
