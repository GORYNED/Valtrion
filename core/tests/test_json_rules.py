"""`jsonRules` of `checks.json`: a changed JSON file is classified by which keys under a root changed, and falls back to its area
kinds whenever the change cannot be judged by keys."""

import copy
import json
import os
import unittest
from pathlib import Path

from valtrion import select

from .test_select import CHECKS, CONTRACTS, RepoCase, git, run, write

MAP = copy.deepcopy(CHECKS)
MAP["areas"] = [
    {"glob": "libs/**", "kind": "libs-all"},
    {"glob": "app/**", "kind": "app"},
    {"glob": "validation/**", "kind": "docs"},
]
MAP["jsonRules"] = [
    {"glob": "libs/manifest.json", "root": "dependencies",
     "keys": [{"match": "libs.core*", "kind": "libs-core"}, {"match": "libs.ui*", "kind": "libs-ui"}]},
    {"glob": "libs/lock.json", "root": "packages", "keys": [{"match": "libs.*", "kind": "libs-lock"}]},
]
MAP["kinds"] = {
    "libs-all": {"L2": ["editmode:Libs.All.Tests@echo {assemblies}"]},
    "libs-core": {"L0": ["l0:core"]},
    "libs-ui": {"L0": ["l0:ui"]},
    "libs-lock": {"L0": ["l0:lock"]},
    "app": {"L0": ["l0"]},
    "docs": {"L0": ["l0"]},
}
MANIFEST = {"name": "app", "version": "1", "dependencies": {"libs.core": "1.0.0", "libs.ui": {"version": "2.0.0", "optional": False}, "tools.lint": "3"}}
LOCK = {"packages": {"libs.core": {"hash": "aa"}, "libs.ui": {"hash": "bb"}}}


def dump(document, newline="\n", bom=False):
    text = json.dumps(document, indent=2).replace("\n", newline) + newline
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


class JsonKeyTests(unittest.TestCase):
    """`json_changed_keys` and `parse_json_object` on their own."""

    KEYS = MAP["jsonRules"][0]["keys"]

    def changed(self, old, new, root="dependencies"):
        return select.json_changed_keys(old, new, root, self.KEYS)

    def test_changed_added_and_removed_keys_get_the_kind_of_their_first_matching_rule(self):
        old = {"dependencies": {"libs.core": "1", "libs.ui": "1", "libs.core.extra": "1"}}
        new = {"dependencies": {"libs.core": "2", "libs.ui2": "1", "libs.core.extra": "1"}}
        self.assertEqual({"libs.core": ["libs-core"], "libs.ui": ["libs-ui"], "libs.ui2": ["libs-ui"]}, self.changed(old, new))

    def test_a_value_is_compared_as_a_whole_and_exactly(self):
        base = {"dependencies": {"libs.core": {"a": {"b": 1}}}}
        self.assertEqual({"libs.core": ["libs-core"]}, self.changed(base, {"dependencies": {"libs.core": {"a": {"b": 2}}}}))
        for other in (1.0, True):
            self.assertEqual({"libs.core": ["libs-core"]},
                             self.changed({"dependencies": {"libs.core": 1}}, {"dependencies": {"libs.core": other}}), repr(other))

    def test_key_order_is_not_a_change(self):
        old = {"dependencies": {"libs.core": "1", "libs.ui": "1"}, "name": "a", "version": "1"}
        new = {"version": "1", "name": "a", "dependencies": {"libs.ui": "1", "libs.core": "1"}}
        self.assertIsNone(self.changed(old, new))  # nothing changed by key: not judged

    def test_everything_that_cannot_be_judged_by_keys_is_none(self):
        old = {"name": "a", "dependencies": {"libs.core": "1"}}
        self.assertIsNone(self.changed(old, {"name": "b", "dependencies": {"libs.core": "2"}}))  # something outside root changed too
        self.assertIsNone(self.changed(old, {"name": "a", "dependencies": {"libs.core": "1", "tools.x": "1"}}))  # a key no rule matches
        self.assertIsNone(self.changed(old, {"name": "a", "dependencies": {"libs.core": "1"}, "extra": 1}))  # a new key outside root
        self.assertIsNone(self.changed(old, {"name": "a", "dependencies": ["libs.core"]}))  # root is no longer an object
        self.assertIsNone(self.changed(old, old))  # the rule fired on a file without a changed key

    def test_a_missing_root_is_an_empty_one(self):
        self.assertEqual({"libs.core": ["libs-core"]}, self.changed({"name": "a"}, {"name": "a", "dependencies": {"libs.core": "1"}}))
        self.assertEqual({"libs.core": ["libs-core"]}, self.changed({"name": "a", "dependencies": {"libs.core": "1"}}, {"name": "a"}))

    def test_json_parsing_accepts_a_bom_and_any_line_ending_and_rejects_the_rest(self):
        document = {"a": {"b": 1}}
        for data in (dump(document), dump(document, "\r\n"), dump(document, bom=True), dump(document, "\r\n", bom=True)):
            self.assertEqual(document, select.parse_json_object(data))
        for data in (b"", b"{", b"[1]", b"null", b'{"a": 1, "a": 2}', b'{"a": \xff}', b'{"a": NaN, "b": }'):
            with self.assertRaises(select._NotJson, msg=repr(data)):
                select.parse_json_object(data)


class JsonSelectionTests(RepoCase):
    checks = MAP

    def setUp(self):
        super().setUp()
        git(self.root, "checkout", "-q", "main")
        (self.root / "libs").mkdir(exist_ok=True)
        (self.root / "libs" / "manifest.json").write_bytes(dump(MANIFEST))
        (self.root / "libs" / "lock.json").write_bytes(dump(LOCK))
        write(self.root, "app/main.cs", "x\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "libs")
        git(self.root, "checkout", "-q", "work")
        git(self.root, "rebase", "-q", "main")

    def edit(self, name, document, **options):
        (self.root / "libs" / name).write_bytes(dump(document, **options))

    def manifest(self, **changes):
        document = copy.deepcopy(MANIFEST)
        for key, value in changes.items():
            document["dependencies"][key] = value
        return document

    def select_json(self, *arguments):
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", *arguments)
        return code, json.loads(out), err

    def test_one_changed_key_selects_the_kind_of_that_key_only(self):
        self.edit("manifest.json", self.manifest(**{"libs.core": "1.1.0"}))
        self.commit()
        code, payload, _ = self.select_json()
        self.assertEqual(0, code)
        self.assertEqual({"libs/manifest.json": {"libs.core": ["libs-core"]}}, payload["jsonKeyKinds"])
        self.assertEqual({"libs/manifest.json": ["libs-core"]}, payload["pathKinds"])
        self.assertEqual(["libs-core"], payload["kinds"])  # not libs-all: the area kind of the file is not used
        self.assertEqual([], payload["editorSuites"])

    def test_added_and_removed_keys_count(self):
        document = self.manifest(**{"libs.ui.extra": "1"})
        del document["dependencies"]["libs.core"]
        self.edit("manifest.json", document)
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({"libs.ui.extra": ["libs-ui"], "libs.core": ["libs-core"]}, payload["jsonKeyKinds"]["libs/manifest.json"])
        self.assertEqual(["libs-core", "libs-ui"], payload["kinds"])  # the union

    def test_a_change_inside_a_nested_entry_changes_the_key(self):
        self.edit("manifest.json", self.manifest(**{"libs.ui": {"version": "2.0.0", "optional": True}}))
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({"libs/manifest.json": {"libs.ui": ["libs-ui"]}}, payload["jsonKeyKinds"])

    def test_an_unknown_key_falls_back_to_the_area_kind_of_the_file(self):
        self.edit("manifest.json", self.manifest(**{"tools.lint": "4"}))
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["jsonKeyKinds"])
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_one_unknown_key_among_known_ones_makes_the_whole_file_fall_back(self):
        self.edit("manifest.json", self.manifest(**{"libs.core": "9", "tools.lint": "4"}))
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["jsonKeyKinds"])
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_a_change_outside_root_falls_back(self):
        document = self.manifest(**{"libs.core": "9"})
        document["version"] = "2"
        self.edit("manifest.json", document)
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_a_reordered_file_without_a_changed_key_falls_back(self):
        document = {"dependencies": MANIFEST["dependencies"], "version": "1", "name": "app"}
        self.edit("manifest.json", document)
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["jsonKeyKinds"])
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_crlf_and_a_bom_do_not_matter(self):
        self.edit("manifest.json", self.manifest(**{"libs.core": "1.2.0"}), newline="\r\n", bom=True)
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({"libs/manifest.json": {"libs.core": ["libs-core"]}}, payload["jsonKeyKinds"])  # a line ending change is not a key change

    def test_a_broken_new_file_falls_back(self):
        (self.root / "libs" / "manifest.json").write_bytes(b'{"dependencies": {"libs.core": ')
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_a_file_that_is_not_utf_8_or_has_duplicate_keys_falls_back(self):
        (self.root / "libs" / "manifest.json").write_bytes(b'{"dependencies": {"libs.core": "\xff"}}')
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual(["libs-all"], payload["kinds"])
        (self.root / "libs" / "manifest.json").write_bytes(b'{"dependencies": {"libs.core": "1", "libs.core": "2"}}')
        self.commit("duplicate")
        _, payload, _ = self.select_json()
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_a_broken_old_file_falls_back(self):
        git(self.root, "checkout", "-q", "main")
        (self.root / "libs" / "manifest.json").write_bytes(b"not json")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "break")
        git(self.root, "checkout", "-q", "work")
        git(self.root, "rebase", "-q", "main")
        self.edit("manifest.json", self.manifest())
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_an_added_and_a_deleted_file_fall_back(self):
        (self.root / "libs" / "lock.json").unlink()
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["jsonKeyKinds"])
        self.assertEqual(["libs-all"], payload["kinds"])
        git(self.root, "checkout", "-q", "main")
        git(self.root, "branch", "-q", "-D", "work")
        git(self.root, "checkout", "-q", "-b", "work")
        (self.root / "libs" / "manifest.json").unlink()
        self.commit("remove manifest")
        _, payload, _ = self.select_json()
        self.assertEqual(["libs-all"], payload["kinds"])

    def test_two_files_with_their_own_rules_give_the_union(self):
        self.edit("manifest.json", self.manifest(**{"libs.ui": "3"}))
        lock = copy.deepcopy(LOCK)
        lock["packages"]["libs.core"] = {"hash": "cc"}
        self.edit("lock.json", lock)
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({"libs/manifest.json": {"libs.ui": ["libs-ui"]}, "libs/lock.json": {"libs.core": ["libs-lock"]}}, payload["jsonKeyKinds"])
        self.assertEqual(["libs-lock", "libs-ui"], payload["kinds"])

    def test_a_file_without_a_rule_keeps_its_area_kind_next_to_a_judged_one(self):
        self.edit("manifest.json", self.manifest(**{"libs.core": "2"}))
        write(self.root, "libs/other.json", "{}\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual(["libs-all", "libs-core"], payload["kinds"])

    def test_the_working_tree_counts_only_with_the_flag(self):
        self.edit("manifest.json", self.manifest(**{"libs.core": "uncommitted"}))
        _, without, _ = self.select_json()
        self.assertEqual([], without["changedPaths"])
        _, with_tree, _ = self.select_json("--include-worktree")
        self.assertEqual({"libs/manifest.json": {"libs.core": ["libs-core"]}}, with_tree["jsonKeyKinds"])
        git(self.root, "add", "libs/manifest.json")
        _, staged, _ = self.select_json("--include-worktree")
        self.assertEqual({"libs/manifest.json": {"libs.core": ["libs-core"]}}, staged["jsonKeyKinds"])

    def test_the_text_output_names_the_keys(self):
        self.edit("manifest.json", self.manifest(**{"libs.core": "1.5"}))
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main")
        self.assertEqual(0, code)
        self.assertIn("JSON KEYS (1)", out)
        self.assertIn("libs.core -> libs-core", out)

    def test_a_map_without_json_rules_behaves_as_before(self):
        data = copy.deepcopy(MAP)
        del data["jsonRules"]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        self.edit("manifest.json", self.manifest(**{"libs.core": "9"}))
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["jsonKeyKinds"])
        self.assertEqual(["docs", "libs-all"], payload["kinds"])  # docs: the rewritten checks.json

    def test_without_a_merge_base_nothing_is_judged(self):
        config = select.Config(self.checks_path, copy.deepcopy(MAP))
        self.assertEqual({}, select.json_rule_paths(self.root, config, ["libs/manifest.json"], None, False))


class JsonKindListTests(unittest.TestCase):
    """A key maps to a list of kinds (or to one `kind`); a last `*` is an explicit fallback for every other key."""

    KEYS = [
        {"match": "libs.core*", "kinds": ["libs-core", "libs-ui"]},
        {"match": "libs.ui*", "kind": "libs-ui"},
        {"match": "libs.*", "kinds": ["libs-all", "libs-ui", "libs-core"]},
    ]

    def changed(self, old, new, keys=None):
        return select.json_changed_keys({"d": old}, {"d": new}, "d", keys or self.KEYS)

    def test_a_key_can_give_several_kinds_and_the_short_form_gives_one(self):
        self.assertEqual({"libs.core": ["libs-core", "libs-ui"]}, self.changed({"libs.core": 1}, {"libs.core": 2}))
        self.assertEqual({"libs.ui": ["libs-ui"]}, self.changed({"libs.ui": 1}, {"libs.ui": 2}))

    def test_the_first_matching_entry_decides_for_a_key(self):
        # libs.core.x also fits the later `libs.*`, but the first entry wins; libs.other reaches the third
        changed = self.changed({"libs.core.x": 1, "libs.other": 1}, {"libs.core.x": 2, "libs.other": 2})
        self.assertEqual({"libs.core.x": ["libs-core", "libs-ui"], "libs.other": ["libs-all", "libs-ui", "libs-core"]}, changed)

    def test_a_final_star_is_the_explicit_fallback_and_without_it_an_unknown_key_fails_closed(self):
        old, new = {"libs.core": 1, "tools.x": 1}, {"libs.core": 2, "tools.x": 2}
        self.assertIsNone(self.changed(old, new))  # no fallback: tools.x fits nothing
        with_fallback = self.KEYS + [{"match": "*", "kind": "everything-else"}]
        self.assertEqual({"libs.core": ["libs-core", "libs-ui"], "tools.x": ["everything-else"]}, self.changed(old, new, with_fallback))

    def test_the_fallback_does_not_rescue_a_change_outside_root_or_a_file_without_a_changed_key(self):
        keys = self.KEYS + [{"match": "*", "kind": "everything-else"}]
        self.assertIsNone(select.json_changed_keys({"d": {"a": 1}, "n": 1}, {"d": {"a": 2}, "n": 2}, "d", keys))
        self.assertIsNone(select.json_changed_keys({"d": {"a": 1}}, {"d": {"a": 1}}, "d", keys))

    def test_key_kinds_reads_either_form(self):
        self.assertEqual(["a", "b"], select.key_kinds({"match": "*", "kinds": ["a", "b"]}))
        self.assertEqual(["a"], select.key_kinds({"match": "*", "kind": "a"}))


class JsonValueTests(unittest.TestCase):
    """The exact comparison of values, and the documents that cannot be judged."""

    def changed(self, old, new):
        return select.json_changed_keys(select.parse_json_object(old), select.parse_json_object(new), "d", [{"match": "*", "kind": "k"}])

    def test_numbers_are_compared_by_the_text_they_are_written_with(self):
        # Two different numbers must never collapse into one double: these were equal after rounding.
        self.assertEqual({"a": ["k"]}, self.changed(b'{"d":{"a":0.1}}', b'{"d":{"a":0.10000000000000000001}}'))
        self.assertEqual({"a": ["k"], "b": ["k"]}, self.changed(b'{"d":{"a":1e400,"b":1}}', b'{"d":{"a":1e999,"b":2}}'))
        self.assertEqual({"a": ["k"]}, self.changed(b'{"d":{"a":1.0}}', b'{"d":{"a":1.00}}'))  # fail closed: a different text
        self.assertEqual({"a": ["k"]}, self.changed(b'{"d":{"a":1}}', b'{"d":{"a":1.0}}'))
        self.assertEqual({"a": ["k"]}, self.changed(b'{"d":{"a":"1.0"}}', b'{"d":{"a":1.0}}'))  # a string is not a number
        self.assertIsNone(self.changed(b'{"d":{"a":1.5}}', b'{"d":{"a":1.5}}'))

    def test_non_json_tokens_make_the_document_unusable(self):
        for text in (b'{"d":{"a":NaN}}', b'{"d":{"a":Infinity}}', b'{"d":{"a":-Infinity}}'):
            with self.assertRaises(select._NotJson, msg=repr(text)):
                select.parse_json_object(text)

    def test_a_non_ascii_key_and_a_key_named_like_the_root_elsewhere(self):
        self.assertEqual({"ké": ["k"]}, self.changed('{"d":{"ké":1}}'.encode(), '{"d":{"ké":2}}'.encode()))
        old = b'{"d":{"a":1},"x":{"d":{"a":1}}}'
        self.assertIsNone(self.changed(old, b'{"d":{"a":1},"x":{"d":{"a":2}}}'))  # `d` inside `x` is outside the root

    def test_duplicate_keys_in_either_document_are_refused(self):
        for text in (b'{"d":{"a":1,"a":2}}', b'{"d":{"a":{"b":1,"b":2}}}', b'{"d":{},"d":{}}'):
            with self.assertRaises(select._NotJson, msg=repr(text)):
                select.parse_json_object(text)

    def test_match_patterns_are_case_sensitive_and_follow_fnmatch(self):
        keys = [{"match": "Libs.*", "kind": "k"}]
        self.assertIsNone(select.json_changed_keys({"d": {"libs.a": 1}}, {"d": {"libs.a": 2}}, "d", keys))
        self.assertEqual({"Libs.a": ["k"]}, select.json_changed_keys({"d": {"Libs.a": 1}}, {"d": {"Libs.a": 2}}, "d", keys))
        bracket = [{"match": "a[12]", "kind": "k"}]
        self.assertEqual({"a1": ["k"]}, select.json_changed_keys({"d": {"a1": 1}}, {"d": {"a1": 2}}, "d", bracket))
        self.assertIsNone(select.json_changed_keys({"d": {"a[1]": 1}}, {"d": {"a[1]": 2}}, "d", bracket))

    def test_root_may_change_between_missing_empty_and_null(self):
        keys = [{"match": "*", "kind": "k"}]
        self.assertIsNone(select.json_changed_keys({"d": {}}, {}, "d", keys))  # nothing changed by key
        self.assertIsNone(select.json_changed_keys({"d": None}, {"d": {"a": 1}}, "d", keys))  # null is not an object
        self.assertIsNone(select.json_changed_keys({"d": {"a": 1}}, {"d": None}, "d", keys))


class JsonSelectionExtraTests(RepoCase):
    checks = MAP

    def setUp(self):
        super().setUp()
        git(self.root, "checkout", "-q", "main")
        (self.root / "libs").mkdir(exist_ok=True)
        (self.root / "libs" / "manifest.json").write_bytes(dump(MANIFEST))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "libs")
        git(self.root, "checkout", "-q", "work")
        git(self.root, "rebase", "-q", "main")

    def with_rules(self, keys):
        data = copy.deepcopy(MAP)
        data["jsonRules"][0]["keys"] = keys
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")

    def select_json(self, *arguments):
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", *arguments)
        return code, json.loads(out), err

    def test_a_list_of_kinds_is_unioned_once_and_the_fallback_catches_the_rest(self):
        self.with_rules([{"match": "libs.core*", "kinds": ["libs-core", "libs-ui"]}, {"match": "libs.ui*", "kind": "libs-ui"},
                         {"match": "*", "kinds": ["libs-all", "libs-ui"]}])
        document = copy.deepcopy(MANIFEST)
        document["dependencies"]["libs.core"] = "2"
        document["dependencies"]["libs.ui"] = "3"
        document["dependencies"]["tools.lint"] = "9"
        (self.root / "libs" / "manifest.json").write_bytes(dump(document))
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({"libs.core": ["libs-core", "libs-ui"], "libs.ui": ["libs-ui"], "tools.lint": ["libs-all", "libs-ui"]},
                         payload["jsonKeyKinds"]["libs/manifest.json"])
        self.assertEqual(["docs", "libs-all", "libs-core", "libs-ui"], payload["kinds"])  # libs-ui once
        self.assertEqual(["libs-all", "libs-core", "libs-ui"], payload["pathKinds"]["libs/manifest.json"])

    def test_without_the_fallback_the_unknown_key_sends_the_file_back_to_its_area_kind(self):
        self.with_rules([{"match": "libs.core*", "kinds": ["libs-core"]}])
        document = copy.deepcopy(MANIFEST)
        document["dependencies"]["tools.lint"] = "9"
        (self.root / "libs" / "manifest.json").write_bytes(dump(document))
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["jsonKeyKinds"])
        self.assertEqual(["docs", "libs-all"], payload["kinds"])

    def test_the_text_output_lists_all_kinds_of_a_key(self):
        self.with_rules([{"match": "*", "kinds": ["libs-core", "libs-ui"]}])
        document = copy.deepcopy(MANIFEST)
        document["dependencies"]["libs.core"] = "5"
        (self.root / "libs" / "manifest.json").write_bytes(dump(document))
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main")
        self.assertEqual(0, code)
        self.assertIn("libs.core -> libs-core, libs-ui", out)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_manifest_replaced_by_a_symlink_is_not_read_through(self):
        target = self.root / "elsewhere.json"
        document = copy.deepcopy(MANIFEST)
        document["dependencies"]["libs.core"] = "changed"
        target.write_bytes(dump(document))
        (self.root / "libs" / "manifest.json").unlink()
        os.symlink(target, self.root / "libs" / "manifest.json")
        _, payload, _ = self.select_json("--include-worktree")
        self.assertEqual({}, payload["jsonKeyKinds"])
        self.assertIn("libs-all", payload["kinds"])


class JsonRuleConfigTests(RepoCase):
    checks = MAP

    def check(self, data):
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        return run("check-config", "--checks", str(self.checks_path))

    def test_the_map_is_valid(self):
        code, _, err = self.check(MAP)
        self.assertEqual(0, code, err)

    def test_a_rule_with_an_unknown_kind_a_bad_glob_or_an_empty_root_is_an_error(self):
        for change, text in (
                ({"keys": [{"match": "libs.*", "kind": "ghost"}]}, "ghost"),
                ({"glob": "/abs/manifest.json"}, "glob"),
                ({"glob": "../x.json"}, "glob"),
                ({"root": " "}, "root"),
                ({"keys": [{"match": " ", "kind": "libs-core"}]}, "match")):
            bad = copy.deepcopy(MAP)
            bad["jsonRules"][0].update(change)
            code, _, err = self.check(bad)
            self.assertEqual(1, code, change)
            self.assertIn("jsonRules[0]", err)
            self.assertIn(text, err)

    def test_a_kind_list_is_checked_kind_by_kind_and_kind_with_kinds_is_refused(self):
        bad = copy.deepcopy(MAP)
        bad["jsonRules"][0]["keys"] = [{"match": "libs.*", "kinds": ["libs-core", "ghost", "libs-ui"]}]
        code, _, err = self.check(bad)
        self.assertEqual(1, code)
        self.assertIn("ghost", err)
        self.assertIn("jsonRules[0].keys[0]", err)
        bad["jsonRules"][0]["keys"] = [{"match": "libs.*", "kind": "libs-core", "kinds": ["libs-ui"]}]
        code, _, err = self.check(bad)
        self.assertEqual(1, code)
        self.assertIn("either kind", err)
        good = copy.deepcopy(MAP)
        good["jsonRules"][0]["keys"] = [{"match": "libs.*", "kinds": ["libs-core", "libs-ui"]}, {"match": "*", "kind": "libs-all"}]
        code, _, err = self.check(good)
        self.assertEqual(0, code, err)

    def test_the_schema_rejects_a_key_without_a_kind_and_an_empty_kind_list(self):
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-json-key-without-kind.json"))
        self.assertEqual(1, code)
        empty = copy.deepcopy(MAP)
        empty["jsonRules"][0]["keys"] = [{"match": "libs.*", "kinds": []}]
        self.checks_path.write_text(json.dumps(empty), encoding="utf-8")
        code, _, _ = run("validate", str(self.checks_path))
        self.assertEqual(1, code)

    def test_the_schema_rejects_a_rule_without_keys(self):
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-json-rule-without-keys.json"))
        self.assertEqual(1, code)
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-json-rules.json"))
        self.assertEqual(0, code, out)


if __name__ == "__main__":
    unittest.main()
