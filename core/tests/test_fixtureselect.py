"""`fixtureselect`: the types a C# file declares, and which test classes a change can reach by a name."""

import unittest

from valtrion import fixtureselect as fs

WALLET = """
namespace Game.Core
{
    public class Wallet { public int Coins; }
}
"""
BANK = "namespace Game.Core { public class Bank { private Wallet wallet; } }\n"
WALLET_TESTS = """
using NUnit.Framework;
namespace Game.Core.Tests
{
    [TestFixture]
    public class WalletTests
    {
        [Test] public void Adds() { var w = new Wallet(); }
        [TestCase(1)] [TestCase(2)] public void Cases(int a) { }
    }
}
"""
BANK_TESTS = "namespace Game.Core.Tests { public class BankTests { [Test] public void Opens() { new Bank(); } } }\n"
UNRELATED_TESTS = "namespace Game.Core.Tests { public class MathTests { [Test] public void Adds() { } } }\n"


def sources(**files):
    return {path: fs.parse(text) for path, text in files.items()}


class ParseTests(unittest.TestCase):
    def test_types_get_their_namespace_and_block_or_file_scoped(self):
        block = fs.parse("namespace A.B { class X { } struct Y { } }")
        self.assertEqual(["A.B.X", "A.B.Y"], [item.full_name for item in block.types])
        scoped = fs.parse("namespace A.B;\nclass X { }\n")
        self.assertEqual(["A.B.X"], [item.full_name for item in scoped.types])
        self.assertEqual(["X"], [item.full_name for item in fs.parse("class X { }").types])

    def test_a_nested_or_generic_type_has_no_name_a_test_filter_can_use(self):
        parsed = fs.parse("namespace A { class Outer { class Inner { } } class Box<T> { } }")
        self.assertEqual({"Outer": "A.Outer", "Inner": None, "Box": None}, {item.name: item.full_name for item in parsed.types})

    def test_braces_comments_and_strings_do_not_confuse_the_types(self):
        text = '''namespace A { class X { string s = "}{ class Fake { }"; /* class Hidden { } */ // class Gone {
            char c = '{'; string v = @"a ""} b"; string r = """ } class Raw { """; } class Y { } }'''
        self.assertEqual(["X", "Y"], [item.name for item in fs.parse(text).types])

    def test_an_interpolated_string_may_hold_strings_and_braces_of_its_own(self):
        text = ('namespace A { class X { string a = $"{b ?? "(missing)"}: {{literal}} {c:F2} {(d ? "}" : "{")}"; '
                'string e = $@"{f} ""q"" {g("x")}"; char h = \'}\'; } class Y { } }')
        self.assertEqual(["X", "Y"], [item.name for item in fs.parse(text).types])

    def test_a_verbatim_string_that_ends_in_an_escaped_quote_is_no_raw_string(self):
        text = 'class X { string a = @"x" + y + @"" + z; string b = @""""; } class Y { }'
        self.assertEqual(["X", "Y"], [item.name for item in fs.parse(text).types])

    def test_unbalanced_braces_are_not_judged(self):
        with self.assertRaises(fs.Unjudgeable):
            fs.parse("class X { void M() { }")
        with self.assertRaises(fs.Unjudgeable):
            fs.parse('class X { string s = "open; }')

    def test_test_methods_cases_and_fixture_attributes_belong_to_their_class(self):
        parsed = fs.parse(WALLET_TESTS)
        item = parsed.types[0]
        self.assertTrue(item.has_tests)
        self.assertEqual(3, item.tests)  # one Test and two cases
        self.assertEqual(0, fs.parse(WALLET).types[0].tests)
        self.assertTrue(fs.parse("[TestFixture] class F { }").types[0].has_tests)
        self.assertFalse(fs.parse("class F { [SetUp] void S() { } }").types[0].has_tests)

    def test_bases_and_abstract_are_read(self):
        parsed = fs.parse("abstract class B { [Test] void T() { } } class D : B, IThing<int> where X : class { } record R(int A) : B;")
        details = {item.name: (item.abstract, item.bases) for item in parsed.types}
        self.assertEqual({"B": (True, ()), "D": (False, ("B", "IThing")), "R": (False, ("B",))}, details)

    def test_a_where_clause_is_no_type(self):
        parsed = fs.parse("class X<T> where T : class { }\nclass Y<T> where T : class\n    where U : struct { }")
        self.assertEqual(["X", "Y"], [item.name for item in parsed.types])

    def test_extension_methods_and_global_effects_are_found(self):
        parsed = fs.parse("static class E { public static int Twice(this int v) => v * 2; }")
        self.assertEqual({"Twice"}, parsed.extensions)
        self.assertTrue(fs.parse("[assembly: InternalsVisibleTo(\"X\")]\n").global_effect)
        self.assertTrue(fs.parse("global using System;\n").global_effect)


class AffectedTests(unittest.TestCase):
    def reaching(self, changed_text, scope, test_files, old=None):
        everything = {"Core/Wallet.cs": fs.parse(changed_text), **scope}
        return fs.affected_fixtures({"Core/Wallet.cs": [fs.parse(old) if old else None, everything["Core/Wallet.cs"]]}, everything, test_files)

    def scope(self):
        return sources(**{"Core/Bank.cs": BANK, "Tests/WalletTests.cs": WALLET_TESTS, "Tests/BankTests.cs": BANK_TESTS, "Tests/MathTests.cs": UNRELATED_TESTS})

    def test_only_the_test_classes_that_name_a_changed_type_directly_or_through_another_file_are_chosen(self):
        files = {"Game.Core.Tests": ["Tests/WalletTests.cs", "Tests/BankTests.cs", "Tests/MathTests.cs"]}
        result = self.reaching(WALLET, self.scope(), files, old=WALLET)
        self.assertEqual({"Game.Core.Tests": ["Game.Core.Tests.BankTests", "Game.Core.Tests.WalletTests"]}, result)

    def test_a_test_class_that_reaches_nothing_is_not_chosen_and_an_assembly_with_none_stays_whole(self):
        files = {"Game.Core.Tests": ["Tests/MathTests.cs"]}
        self.assertEqual({"Game.Core.Tests": None}, self.reaching(WALLET, self.scope(), files))

    def test_a_changed_test_file_selects_its_own_classes(self):
        scope = self.scope()
        changed = {"Tests/MathTests.cs": [scope["Tests/MathTests.cs"], scope["Tests/MathTests.cs"]]}
        files = {"Game.Core.Tests": ["Tests/WalletTests.cs", "Tests/MathTests.cs"]}
        self.assertEqual({"Game.Core.Tests": ["Game.Core.Tests.MathTests"]}, fs.affected_fixtures(changed, scope, files))

    def test_a_derived_test_class_runs_the_inherited_tests_when_its_base_is_reached(self):
        base = "namespace T { public abstract class BaseTests { [Test] public void Shared() { new Wallet(); } } }\n"
        derived = "namespace T { public class DerivedTests : BaseTests { } }\n"
        scope = sources(**{"T/Base.cs": base, "T/Derived.cs": derived})
        result = self.reaching(WALLET, scope, {"T": ["T/Base.cs", "T/Derived.cs"]})
        self.assertEqual({"T": ["T.DerivedTests"]}, result)

    def test_an_extension_method_is_reached_by_its_own_name(self):
        extension = "static class WalletExt { public static int Doubled(this Wallet w) => w.Coins * 2; }\n"
        user = "namespace T { public class UserTests { [Test] public void Uses() { new Wallet().Doubled(); } } }\n"
        other = "namespace T { public class OtherTests { [Test] public void Nothing() { } } }\n"
        scope = sources(**{"Ext.cs": extension, "T/User.cs": user, "T/Other.cs": other})
        changed = {"Ext.cs": [scope["Ext.cs"], scope["Ext.cs"]]}
        self.assertEqual({"T": ["T.UserTests"]}, fs.affected_fixtures(changed, scope, {"T": ["T/User.cs", "T/Other.cs"]}))

    def test_a_chosen_class_that_a_filter_cannot_name_keeps_the_whole_assembly(self):
        nested = "namespace T { public class Outer { public class InnerTests { [Test] public void A() { new Wallet(); } } } }\n"
        scope = sources(**{"T/Nested.cs": nested})
        self.assertEqual({"T": None}, self.reaching(WALLET, scope, {"T": ["T/Nested.cs"]}))

    def test_a_unity_object_also_selects_the_test_classes_that_load_assets(self):
        behaviour = "public class Spinner : MonoBehaviour { }\n"
        scene_test = "namespace T { public class SceneTests { [UnityTest] public IEnumerator Loads() { SceneManager.LoadScene(\"Main\"); yield return null; } } }\n"
        plain_test = "namespace T { public class PlainTests { [Test] public void Plain() { } } }\n"
        scope = sources(**{"Spinner.cs": behaviour, "T/Scene.cs": scene_test, "T/Plain.cs": plain_test})
        changed = {"Spinner.cs": [scope["Spinner.cs"], scope["Spinner.cs"]]}
        self.assertEqual({"T": ["T.SceneTests"]}, fs.affected_fixtures(changed, scope, {"T": ["T/Scene.cs", "T/Plain.cs"]}))
        plain_change = {"Wallet.cs": [fs.parse(WALLET), fs.parse(WALLET)]}
        self.assertEqual({"T": None}, fs.affected_fixtures(plain_change, {**scope, "Wallet.cs": plain_change["Wallet.cs"][1]}, {"T": ["T/Scene.cs", "T/Plain.cs"]}))

    def test_a_change_that_names_no_type_or_is_global_cannot_be_judged(self):
        empty = fs.parse("using System;\n")
        with self.assertRaises(fs.Unjudgeable):
            fs.affected_fixtures({"A.cs": [None, empty]}, {"A.cs": empty}, {"T": []})
        glob = fs.parse("[assembly: X]\nclass A { }")
        with self.assertRaises(fs.Unjudgeable):
            fs.affected_fixtures({"A.cs": [None, glob]}, {"A.cs": glob}, {"T": []})

    def test_a_deleted_type_is_still_a_name_to_look_for(self):
        scope = sources(**{"Tests/WalletTests.cs": WALLET_TESTS})
        changed = {"Wallet.cs": [fs.parse(WALLET), None]}
        result = fs.affected_fixtures(changed, {**scope}, {"T": ["Tests/WalletTests.cs"]})
        self.assertEqual({"T": ["Game.Core.Tests.WalletTests"]}, result)

    def test_count_tests(self):
        scope = self.scope()
        paths = ["Tests/WalletTests.cs", "Tests/BankTests.cs", "Tests/MathTests.cs"]
        self.assertEqual(5, fs.count_tests(scope, None, paths))
        self.assertEqual(3, fs.count_tests(scope, ["Game.Core.Tests.WalletTests"], paths))


if __name__ == "__main__":
    unittest.main()
