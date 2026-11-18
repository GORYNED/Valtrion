"""An area's test assemblies narrowed to the test classes that name an affected type (`fixtureselect`), through `valtrion select`: the runner
filters by test-name prefix (`{prefixes;}`), so a class can be a target; the whole assembly stays for anything that cannot be judged."""

import json

from valtrion import select

from .test_project_assemblies import RUNNERS, ProjectCase, checks_map, meta
from .test_select import git, run

PREFIX_RUNNERS = {"edit": 'run --edit "{prefixes;}"', "play": 'run --play "{prefixes;}"'}

CODE = {
    "Assets/Core/Core.cs": "namespace Game.Core { public class Wallet { public int Coins; } public class Bank { Wallet wallet; } }\n",
    "Assets/Ui/Hud.cs": "namespace Game.Ui { public class Hud { Game.Core.Wallet wallet; } }\n",
    "Assets/Ui/Panel.cs": "namespace Game.Ui { public class Panel { } }\n",
    "Assets/Tests/Core/WalletTests.cs": "namespace Game.Core.Tests { [TestFixture] public class WalletTests { [Test] public void A() { new Wallet(); } } }\n",
    "Assets/Tests/Core/BankTests.cs": "namespace Game.Core.Tests { public class BankTests { [Test] public void A() { new Bank(); } } }\n",
    "Assets/Tests/Core/MathTests.cs": "namespace Game.Core.Tests { public class MathTests { [Test] public void A() { } } }\n",
    "Assets/Tests/Ui/HudTests.cs": "namespace Game.Ui.Tests { public class HudTests { [Test] public void A() { new Hud(); } } }\n",
    "Assets/Tests/Ui/PanelTests.cs": "namespace Game.Ui.Tests { public class PanelTests { [Test] public void A() { new Panel(); } } }\n",
    "Assets/Tests/Play/HudPlayTests.cs": "namespace Game.Play.Tests { public class HudPlayTests { [UnityTest] public System.Collections.IEnumerator A() { new Hud(); yield return null; } } }\n",
}
WALLET_CHANGED = CODE["Assets/Core/Core.cs"].replace("Coins;", "Coins; public int Gems;")


class FixtureCase(ProjectCase):
    @property
    def data(self):
        data = checks_map()
        data["runners"] = dict(PREFIX_RUNNERS)
        return data

    def setUp(self):
        super().setUp()
        self.base(CODE)

    def keys(self, payload):
        return self.suites(payload)

    def commands(self, payload):
        return {item["runnerName"]: item["command"] for item in payload["editorCommands"]}


class FixtureNarrowingTests(FixtureCase):
    def test_a_change_selects_the_test_classes_that_name_an_affected_type_not_the_assemblies(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED})
        self.assertEqual(["editmode:Game.Core.Tests.BankTests", "editmode:Game.Core.Tests.WalletTests", "editmode:Game.Ui.Tests.HudTests",
                          "playmode:Game.Play.Tests.HudPlayTests"], self.keys(payload))
        commands = self.commands(payload)
        self.assertEqual('run --edit "\\AGame\\.Core\\.Tests\\.BankTests;\\AGame\\.Core\\.Tests\\.WalletTests;\\AGame\\.Ui\\.Tests\\.HudTests"', commands["edit"])
        self.assertEqual('run --play "\\AGame\\.Play\\.Tests\\.HudPlayTests"', commands["play"])

    def test_the_record_names_the_classes_and_explain_tells_which_rule_chose_them(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED}, "--explain")
        tests = {item["assembly"]: item.get("fixtures") for item in self.record(payload, "Assets/Core/Core.cs")["tests"]}
        self.assertEqual({"Game.Core.Tests": ["Game.Core.Tests.BankTests", "Game.Core.Tests.WalletTests"], "Game.Ui.Tests": ["Game.Ui.Tests.HudTests"],
                          "Game.Play.Tests": ["Game.Play.Tests.HudPlayTests"]}, tests)
        reasons = {item["check"]: item["because"] for item in payload["explain"]}
        self.assertIn("areas[0].assemblies", [entry["rule"] for entry in reasons["editmode:Game.Ui.Tests.HudTests"]])

    def test_a_change_to_a_file_no_test_names_keeps_the_whole_test_assembly_and_says_why(self):
        payload = self.changed({"Assets/Ui/Panel.cs": "namespace Game.Ui { public class Panel { int a; } }\n"})
        self.assertEqual(["editmode:Game.Ui.Tests.PanelTests", "playmode:Game.Play.Tests"], self.keys(payload))
        tests = {item["assembly"]: item for item in self.record(payload, "Assets/Ui/Panel.cs")["tests"]}
        self.assertEqual(["Game.Ui.Tests.PanelTests"], tests["Game.Ui.Tests"]["fixtures"])
        self.assertIn("fixtureReason", tests["Game.Play.Tests"])

    def test_the_flag_keeps_whole_assemblies(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED}, "--no-fixture-narrowing")
        self.assertEqual(["editmode:Game.Core.Tests", "editmode:Game.Ui.Tests", "playmode:Game.Play.Tests"], self.keys(payload))

    def test_a_change_that_is_not_only_code_keeps_the_whole_assembly(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED, "Assets/Core/Core.cs.meta": meta("c" * 32) + "executionOrder: 5\n"})
        self.assertIn("editmode:Game.Core.Tests", self.keys(payload))
        self.assertNotIn("editmode:Game.Core.Tests.WalletTests", self.keys(payload))

    def test_the_meta_file_of_an_added_script_follows_its_script(self):
        payload = self.changed({"Assets/Core/Wallet2.cs": "namespace Game.Core { public class Wallet { } }\n", "Assets/Core/Wallet2.cs.meta": meta("e" * 32)})
        self.assertEqual(["editmode:Game.Core.Tests.BankTests", "editmode:Game.Core.Tests.WalletTests", "editmode:Game.Ui.Tests.HudTests",
                          "playmode:Game.Play.Tests.HudPlayTests"], self.keys(payload))
        record = self.record(payload, "Assets/Core/Wallet2.cs.meta")
        self.assertEqual("narrowed", record["status"])
        self.assertIn("Assets/Core/Wallet2.cs.meta", record["changed"][0]["files"])

    def test_the_whole_assembly_wins_over_its_classes_when_something_else_selects_it(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED, "Assets/Tests/Core/Game.Core.Tests.asmdef.meta": meta("2" * 32)})
        keys = self.keys(payload)
        self.assertEqual(1, len([key for key in keys if key.startswith("editmode:Game.Core.Tests")]), keys)

    def test_a_runner_that_filters_by_assembly_is_not_narrowed(self):
        data = checks_map()
        data["runners"] = dict(RUNNERS)
        self.write_checks(data)
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED})
        self.assertEqual(["editmode:Game.Core.Tests", "editmode:Game.Ui.Tests", "playmode:Game.Play.Tests"], self.keys(payload))

    def test_a_file_that_cannot_be_parsed_keeps_the_whole_assembly_and_names_the_file(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED, "Assets/Ui/Broken.cs": "namespace Game.Ui { class Broken { \n"})
        self.assertEqual(["editmode:Game.Core.Tests", "editmode:Game.Ui.Tests", "playmode:Game.Play.Tests"], self.keys(payload))
        reasons = [test["fixtureReason"] for test in self.record(payload, "Assets/Core/Core.cs")["tests"]]
        self.assertTrue(all("Broken.cs" in reason for reason in reasons), reasons)

    def test_the_working_tree_is_judged_like_a_commit(self):
        (self.root / "Assets/Core/Core.cs").write_text(WALLET_CHANGED, encoding="utf-8")
        payload = self.select_json("--include-worktree")
        self.assertEqual(["editmode:Game.Core.Tests.BankTests", "editmode:Game.Core.Tests.WalletTests", "editmode:Game.Ui.Tests.HudTests",
                          "playmode:Game.Play.Tests.HudPlayTests"], self.keys(payload))

    def test_text_output_names_the_classes(self):
        self.commit({"Assets/Core/Core.cs": WALLET_CHANGED})
        from .test_select import run
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--level", "affected")
        self.assertEqual(0, code, err)
        self.assertIn("editmode:Game.Core.Tests (2 test classes: BankTests, WalletTests)", out)
        self.assertIn("playmode:Game.Play.Tests (1 test class: HudPlayTests)", out)


class FixtureHelperTests(FixtureCase):
    def test_a_runner_is_a_class_target_only_with_the_prefix_placeholder(self):
        config = select.load_config(self.checks_path)
        self.assertTrue(select._fixture_runner(config, "edit"))
        self.assertFalse(select._fixture_runner(config, "run --edit {assemblies}"))
        self.assertFalse(select._fixture_runner(config, 'run --edit "{prefixes;}" -assemblyNames "{assemblies;}"'))


class TypeNarrowingTests(FixtureCase):
    """The second pass: suites a map selects by test-name prefix (areas without an `assemblies` member) are cut to the classes the change reaches."""

    @property
    def data(self):
        data = checks_map(member=False)
        data["runners"] = dict(PREFIX_RUNNERS)
        return data

    def level(self, *arguments, level="affected"):
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", "--level", level, *arguments)
        self.assertEqual(0, code, err)
        return json.loads(out)

    def test_suites_are_cut_to_the_test_classes_that_name_an_affected_type(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED})
        self.assertEqual(["editmode:Game.Core.Tests.BankTests", "editmode:Game.Core.Tests.WalletTests", "editmode:Game.Other.Tests",
                          "editmode:Game.Ui.Tests.HudTests", "playmode:Game.Play.Tests"], self.keys(payload))
        narrowing = payload["typeNarrowing"]
        self.assertEqual({"classes": ["Game.Core.Tests.BankTests", "Game.Core.Tests.WalletTests"], "of": 3}, narrowing["editmode:Game.Core.Tests"])
        self.assertIn("no test class of the project is known under Game.Other.Tests", narrowing["editmode:Game.Other.Tests"]["kept"])
        self.assertIn("every test class", narrowing["playmode:Game.Play.Tests"]["kept"])

    def test_the_commands_name_the_classes_and_the_suites_that_stay_whole(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED})
        edit = self.commands(payload)["edit"]
        self.assertIn(r"\AGame\.Core\.Tests\.WalletTests", edit)
        self.assertIn(r"\AGame\.Other\.Tests", edit)
        self.assertNotIn(r"\AGame\.Core\.Tests;", edit)

    def test_a_path_that_is_not_c_code_keeps_the_suites_it_selects_whole(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED, "Assets/Art/Image.png": "changed\n"})
        self.assertEqual(["editmode:Game.Core.Tests", "editmode:Game.Other.Tests", "editmode:Game.Ui.Tests", "playmode:Game.Play.Tests"], self.keys(payload))
        self.assertIn("Image.png", payload["typeNarrowing"]["editmode:Game.Core.Tests"]["kept"])

    def test_a_full_set_stays_whole(self):
        self.commit({"Assets/Core/Core.cs": WALLET_CHANGED})
        data = self.data
        data["kinds"]["code"]["full"] = ["editmode:Game.Core.Tests@edit"]
        self.write_checks(data)
        payload = self.level(level="full")
        self.assertIn("editmode:Game.Core.Tests", self.keys(payload))
        self.assertEqual({}, payload["typeNarrowing"])

    def test_the_flag_keeps_the_suites_whole(self):
        payload = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED}, "--no-fixture-narrowing")
        self.assertEqual(["editmode:Game.Core.Tests", "editmode:Game.Other.Tests", "editmode:Game.Ui.Tests", "playmode:Game.Play.Tests"], self.keys(payload))
        self.assertEqual({}, payload["typeNarrowing"])

    def test_a_deleted_plain_script_is_judged_by_its_names_and_a_deleted_behaviour_is_not(self):
        self.base({"Assets/Core/Gone.cs": "namespace Game.Core { public class Gone { } }\n", "Assets/Core/Spin.cs": "public class Spin : MonoBehaviour { }\n"})
        plain = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED}, remove=["Assets/Core/Gone.cs"])
        self.assertIn("classes", plain["typeNarrowing"]["editmode:Game.Core.Tests"])
        git(self.root, "checkout", "-q", "-B", "work", "main")
        behaviour = self.changed({"Assets/Core/Core.cs": WALLET_CHANGED}, remove=["Assets/Core/Spin.cs"])
        self.assertIn("Unity object", behaviour["typeNarrowing"]["editmode:Game.Core.Tests"]["kept"])

    def test_text_output_lists_the_cut_and_the_kept_suites(self):
        self.commit({"Assets/Core/Core.cs": WALLET_CHANGED})
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--level", "affected")
        self.assertEqual(0, code, err)
        self.assertIn("editmode:Game.Core.Tests: 2 of 3 test classes", out)
        self.assertIn("editmode:Game.Other.Tests: kept whole", out)
