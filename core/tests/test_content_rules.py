"""`contentRules` of `checks.json`: a change that only touches comments, blank lines and layout of matching paths counts as a cheaper kind.
Everything not provably cosmetic keeps the normal kinds."""

import copy
import json
import os
import tempfile
import unittest

from valtrion import select

from .test_select import CHECKS, CONTRACTS, RepoCase, git, run, write

MAP = copy.deepcopy(CHECKS)
MAP["areas"] = [
    {"glob": "src/**", "kind": "code"},
    {"glob": "docs/**", "kind": "docs"},
    {"glob": "validation/**", "kind": "docs"},  # the map itself, which some tests rewrite
]
MAP["contentRules"] = [
    {"glob": "src/**/*.cs", "kind": "cosmetic", "lineComment": ["//"], "blockComment": {"start": "/*", "end": "*/"}, "reindent": True},
    {"glob": "src/**/*.sh", "kind": "cosmetic", "lineComment": ["#"], "codePrefixes": ["#!"]},
]
MAP["kinds"] = {
    "code": {"L2": ["editmode:Code.Tests@echo {assemblies}"]},
    "cosmetic": {"L0": ["l0"]},
    "docs": {"L0": ["l0"]},
}
LINE = ["//"]
BLOCK = {"start": "/*", "end": "*/"}


def _filesystem_allows_pattern_characters_in_names():
    """Whether a file named `w*.cs` can be created here (NTFS refuses `*`)."""
    with tempfile.TemporaryDirectory() as folder:
        try:
            with open(os.path.join(folder, "w*.cs"), "w", encoding="utf-8"):
                pass
        except OSError:
            return False
    return True


def _filesystem_keeps_the_executable_bit():
    """Whether `chmod 0o755` really sets an execute bit here (NTFS has none)."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "probe.sh")
        with open(path, "w", encoding="utf-8"):
            pass
        os.chmod(path, 0o755)
        return bool(os.stat(path).st_mode & 0o111)


NAMES_WITH_PATTERN_CHARACTERS = _filesystem_allows_pattern_characters_in_names()
EXECUTABLE_BIT = _filesystem_keeps_the_executable_bit()


def diff(*hunks):
    """A `git diff -U0` text from hunks given as lists of '+text' / '-text' lines."""
    out = ["diff --git a/f.cs b/f.cs", "index 1111111..2222222 100644", "--- a/f.cs", "+++ b/f.cs"]
    for number, hunk in enumerate(hunks, start=1):
        out.append(f"@@ -{number},1 +{number},1 @@")
        out.extend(hunk)
    return "\n".join(out) + "\n"


class DiffTests(unittest.TestCase):
    def cosmetic(self, *hunks, block=BLOCK, reindent=True, prefixes=()):
        return select.diff_is_cosmetic(diff(*hunks), LINE, block, reindent, prefixes)

    def test_comments_blank_lines_and_reindentation_are_cosmetic(self):
        self.assertTrue(self.cosmetic(["+// a new comment"]))
        self.assertTrue(self.cosmetic(["-    // old", "+    // new"]))
        self.assertTrue(self.cosmetic(["+", "+   ", "-"]))
        self.assertTrue(self.cosmetic(["-    foo();", "+foo();"]))  # re-indentation
        self.assertTrue(self.cosmetic(["-foo();  ", "+foo();"]))  # trailing space
        self.assertTrue(self.cosmetic(["+/* one line block */"]))
        self.assertTrue(self.cosmetic(["+// c", "-bar();", "+  bar();"], ["+// d"]))
        self.assertTrue(self.cosmetic(["+x();", "\\ No newline at end of file", "-x();", "\\ No newline at end of file"]))

    def test_anything_that_is_code_is_not_cosmetic(self):
        self.assertFalse(self.cosmetic(["-foo();", "+bar();"]))
        self.assertFalse(self.cosmetic(["+foo();"]))
        self.assertFalse(self.cosmetic(["-foo();"]))
        self.assertFalse(self.cosmetic(["-foo();", "+// foo();"]))  # code turned into a comment disables it
        self.assertFalse(self.cosmetic(["-foo();", "+foo(); // note"]))  # a trailing comment still changes the code line
        self.assertFalse(self.cosmetic(['-Log("a b");', '+Log("ab");']))  # whitespace inside a line is never ignored
        self.assertFalse(self.cosmetic(["-x();", "-y();", "+y();", "+x();"]))  # reordered
        self.assertFalse(self.cosmetic(["-x();"], ["+x();"]))  # moved to another place in the file
        self.assertFalse(self.cosmetic(["-a();", "+a();"], ["+b();"]))  # one cosmetic hunk does not excuse a code hunk

    def test_a_multi_line_block_comment_is_code_because_its_inner_lines_cannot_be_judged_alone(self):
        self.assertFalse(self.cosmetic(["+/* starts here"]))
        self.assertFalse(self.cosmetic(["+ * inside"]))
        self.assertFalse(self.cosmetic(["+ */"]))
        self.assertFalse(self.cosmetic(["+/* x */ foo();"]))  # code after the block
        self.assertFalse(self.cosmetic(["+/* x"], block=None))
        self.assertFalse(self.cosmetic(["+/* x */"], block=None))  # no blockComment configured: only line markers count

    def test_code_between_two_block_comments_on_one_line_is_code(self):
        self.assertFalse(self.cosmetic(["-/* a */ Old(); /* b */", "+/* a */ New(); /* b */"]))
        self.assertFalse(self.cosmetic(["+/* a */ x /* b */"]))
        self.assertTrue(self.cosmetic(["-/* a */", "+/* b */"]))

    def test_re_indentation_counts_only_when_the_rule_says_so(self):
        change = ["-    return 1;", "+return 1;"]
        self.assertTrue(self.cosmetic(change, reindent=True))
        self.assertFalse(self.cosmetic(change, reindent=False))  # the default: indentation may be code
        self.assertTrue(self.cosmetic(["+// only a comment", "+"], reindent=False))

    def test_a_line_ending_change_is_code_even_where_re_indentation_is_allowed(self):
        self.assertFalse(self.cosmetic(["-a();\r", "+a();"], reindent=True))
        self.assertTrue(self.cosmetic(["-a();\r", "+a();\r"], reindent=True))  # same line, nothing changed in the text of it

    def test_a_comment_marker_that_is_also_a_directive_is_code_when_the_rule_lists_its_prefix(self):
        change = ["-#!/bin/bash", "+#!/bin/zsh"]
        self.assertTrue(select.diff_is_cosmetic(diff(change), ["#"], None))  # without a code prefix the shebang reads as a comment
        self.assertFalse(select.diff_is_cosmetic(diff(change), ["#"], None, False, ["#!"]))
        self.assertFalse(select.diff_is_cosmetic(diff(["+#if 0"]), ["#"], None, False, ["#if", "#endif"]))
        self.assertTrue(select.diff_is_cosmetic(diff(["+# a note"]), ["#"], None, False, ["#!"]))

    def test_a_mode_change_or_rename_is_code_even_with_a_cosmetic_hunk(self):
        mode = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\nindex 1..2\n--- a/run.sh\n+++ b/run.sh\n@@ -1,0 +2 @@\n+# note\n"
        self.assertFalse(select.diff_is_cosmetic(mode, ["#"], None))
        rename = "diff --git a/a.sh b/b.sh\nsimilarity index 90%\nrename from a.sh\nrename to b.sh\n--- a/a.sh\n+++ b/b.sh\n@@ -1 +1 @@\n-# a\n+# b\n"
        self.assertFalse(select.diff_is_cosmetic(rename, ["#"], None))
        added = "diff --git a/n.sh b/n.sh\nnew file mode 100644\nindex 0000000..2\n--- /dev/null\n+++ b/n.sh\n@@ -0,0 +1 @@\n+# only a comment\n"
        self.assertTrue(select.diff_is_cosmetic(added, ["#"], None))

    def test_unusual_line_separators_inside_a_line_do_not_split_it(self):
        self.assertFalse(self.cosmetic(["+a();\x0cb();"]))
        self.assertFalse(self.cosmetic(["+// ok b();"]))  # one added line holding a Unicode line separator and code

    def test_a_diff_without_a_hunk_is_not_cosmetic(self):
        for text in ("", "Binary files a/f and b/f differ\n", "diff --git a/f b/f\nold mode 100644\nnew mode 100755\n"):
            self.assertFalse(select.diff_is_cosmetic(text, LINE, BLOCK), repr(text))

    def test_a_line_marker_must_start_the_trimmed_line(self):
        self.assertFalse(self.cosmetic(['+var s = "// not a comment";']))
        self.assertTrue(select.diff_is_cosmetic(diff(["+# note"]), ["#"], None))
        self.assertTrue(select.diff_is_cosmetic(diff(["+-- sql comment"]), ["--"], None))


class CosmeticSelectionTests(RepoCase):
    checks = MAP

    def select_json(self, *arguments):
        code, out, err = run("select", "--checks", str(self.checks_path), "--base-ref", "main", "--json", *arguments)
        return code, json.loads(out), err

    def test_a_comment_only_change_gets_the_cheaper_kind(self):
        write(self.root, "src/media/a.cs", "x\n// a note about x\n")
        self.commit()
        code, payload, _ = self.select_json()
        self.assertEqual(0, code)
        self.assertEqual({"src/media/a.cs": "cosmetic"}, payload["cosmeticPaths"])
        self.assertEqual({"src/media/a.cs": ["cosmetic"]}, payload["pathKinds"])
        self.assertEqual(["cosmetic"], payload["kinds"])
        self.assertEqual([], payload["editorSuites"])

    def test_a_code_change_keeps_the_normal_kind(self):
        write(self.root, "src/media/a.cs", "y\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])
        self.assertEqual(["code"], payload["kinds"])
        self.assertEqual(["editmode:Code.Tests"], [item["key"] for item in payload["editorSuites"]])

    def test_a_comment_edit_next_to_a_code_edit_in_the_same_file_is_code(self):
        write(self.root, "src/media/a.cs", "y\n// and a note\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])
        self.assertEqual(["code"], payload["kinds"])

    def test_each_path_is_judged_on_its_own(self):
        write(self.root, "src/media/a.cs", "x\n// note\n")  # comment only
        write(self.root, "src/ui/b.cs", "changed\n")  # code
        write(self.root, "docs/readme.md", "// a comment-looking line in a file no rule covers\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({"src/media/a.cs": "cosmetic"}, payload["cosmeticPaths"])
        self.assertEqual(["code", "cosmetic", "docs"], payload["kinds"])

    def test_a_path_no_rule_matches_is_never_cosmetic(self):
        write(self.root, "src/media/a.txt", "// just a note\n")  # not *.cs or *.sh
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])
        self.assertEqual(["code"], payload["kinds"])

    def test_re_indentation_only_is_cosmetic_and_a_new_comment_only_file_too(self):
        write(self.root, "src/media/a.cs", "  x\n")
        write(self.root, "src/media/new.cs", "// only a header\n\n// and another\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({"src/media/a.cs": "cosmetic", "src/media/new.cs": "cosmetic"}, payload["cosmeticPaths"])

    def test_a_deleted_file_with_code_is_code_and_a_new_file_with_code_is_code(self):
        (self.root / "src/media/old.cs").unlink()
        write(self.root, "src/media/fresh.cs", "class A {}\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])

    def test_the_working_tree_counts_only_with_the_flag_and_untracked_files_never(self):
        write(self.root, "src/media/a.cs", "x\n// not committed\n")
        write(self.root, "src/media/untracked.cs", "// not tracked yet\n")
        _, without, _ = self.select_json()
        self.assertEqual([], without["changedPaths"])
        _, with_tree, _ = self.select_json("--include-worktree")
        self.assertEqual({"src/media/a.cs": "cosmetic"}, with_tree["cosmeticPaths"])  # the untracked file has no diff to read: code
        self.assertEqual(["code", "cosmetic"], with_tree["kinds"])

    def with_rules(self, **changes):
        data = copy.deepcopy(MAP)
        data.update(changes)
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def test_re_indentation_is_code_unless_the_rule_allows_it(self):
        data = copy.deepcopy(MAP)
        del data["contentRules"][0]["reindent"]
        self.with_rules(contentRules=data["contentRules"])
        write(self.root, "src/media/a.cs", "  x\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])

    @unittest.skipUnless(EXECUTABLE_BIT, "this filesystem has no executable bit (chmod 0o755 does not set one, as on NTFS)")
    def test_a_script_made_executable_together_with_a_comment_edit_is_code(self):

        write(self.root, "src/run.sh", "echo hi\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "script")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "work")
        git(self.root, "checkout", "-q", "-b", "work2")
        os.chmod(self.root / "src" / "run.sh", 0o755)
        write(self.root, "src/run.sh", "echo hi\n# a note\n")
        os.chmod(self.root / "src" / "run.sh", 0o755)
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "work", "--json")
        payload = json.loads(out)
        self.assertEqual({}, payload["cosmeticPaths"])  # the mode changed too

    def test_bytes_that_are_not_utf_8_are_never_judged(self):
        (self.root / "src/media/a.cs").write_bytes(b"s(\"\xe9\");\n")
        self.commit()
        (self.root / "src/media/a.cs").write_bytes(b"s(\"\xe8\");\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])

    @unittest.skipUnless(NAMES_WITH_PATTERN_CHARACTERS, "this filesystem cannot create a file name with '*' (as on NTFS)")
    def test_a_file_name_with_pattern_characters_is_a_literal_name(self):
        write(self.root, "src/media/w*.cs", "x\n")
        write(self.root, "src/media/wx.cs", "x\n")
        self.commit("add two files")
        self.checks_path.write_text(json.dumps(MAP), encoding="utf-8")

        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "work")
        git(self.root, "checkout", "-q", "-b", "work3")
        write(self.root, "src/media/w*.cs", "x\n// a note\n")  # cosmetic
        write(self.root, "src/media/wx.cs", "changed code\n")  # a pattern `w*.cs` would also pick this file up
        self.commit()
        payload = json.loads(run("select", "--checks", str(self.checks_path), "--base-ref", "work", "--json")[1])
        self.assertEqual({"src/media/w*.cs": "cosmetic"}, payload["cosmeticPaths"])

    def test_a_staged_cosmetic_change_counts_with_the_worktree_flag(self):

        write(self.root, "src/media/a.cs", "x\n// staged note\n")
        git(self.root, "add", "src/media/a.cs")
        _, payload, _ = self.select_json("--include-worktree")
        self.assertEqual({"src/media/a.cs": "cosmetic"}, payload["cosmeticPaths"])

    def test_deleting_a_file_that_held_only_comments_is_cosmetic(self):

        write(self.root, "src/media/notes.cs", "// just notes\n\n// more notes\n")
        self.commit("notes")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "work")
        git(self.root, "checkout", "-q", "-b", "work4")
        (self.root / "src/media/notes.cs").unlink()
        self.commit("remove notes")
        payload = json.loads(run("select", "--checks", str(self.checks_path), "--base-ref", "work", "--json")[1])
        self.assertEqual({"src/media/notes.cs": "cosmetic"}, payload["cosmeticPaths"])

    def test_the_first_rule_that_matches_a_path_is_used(self):
        rules = [{"glob": "src/**", "kind": "cosmetic", "lineComment": ["#"]}, MAP["contentRules"][0]]
        self.with_rules(contentRules=rules)
        write(self.root, "src/media/a.cs", "x\n// a slash comment\n")  # the first rule only knows `#`, so this is code
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])

    def test_the_fail_policy_still_sees_a_path_no_rule_makes_cosmetic(self):
        self.with_rules(areas=[{"glob": "docs/**", "kind": "docs"}, {"glob": "validation/**", "kind": "docs"}], unclassified="fail")
        write(self.root, "src/media/a.cs", "x\n// note\n")
        self.commit()
        code, payload, _ = self.select_json()
        self.assertEqual(0, code)  # classified by its rule, not unclassified
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit("code change")
        code, payload, _ = self.select_json()
        self.assertEqual(1, code)  # unclassified code under the fail policy

    def test_the_text_output_lists_the_cosmetic_paths(self):
        write(self.root, "src/media/a.cs", "x\n// note\n")
        self.commit()
        code, out, _ = run("select", "--checks", str(self.checks_path), "--base-ref", "main")
        self.assertEqual(0, code)
        self.assertIn("COSMETIC (1)", out)
        self.assertIn("src/media/a.cs -> cosmetic", out)

    def test_a_map_without_content_rules_behaves_as_before(self):
        data = copy.deepcopy(MAP)
        del data["contentRules"]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        write(self.root, "src/media/a.cs", "x\n// note\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual({}, payload["cosmeticPaths"])
        self.assertEqual(["code", "docs"], payload["kinds"])  # docs: the rewritten checks.json itself

    def test_an_unclassified_path_that_is_only_cosmetic_is_classified_by_its_rule(self):
        data = copy.deepcopy(MAP)
        data["areas"] = [{"glob": "docs/**", "kind": "docs"}, {"glob": "validation/**", "kind": "docs"}]
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        write(self.root, "src/media/a.cs", "x\n// note\n")
        self.commit()
        _, payload, _ = self.select_json()
        self.assertEqual([], payload["unclassifiedPaths"])
        self.assertFalse(payload["widest"])


class ContentRuleConfigTests(RepoCase):
    checks = MAP

    def check(self, data):
        self.checks_path.write_text(json.dumps(data), encoding="utf-8")
        return run("check-config", "--checks", str(self.checks_path))

    def test_the_map_is_valid(self):
        code, _, err = self.check(MAP)
        self.assertEqual(0, code, err)

    def test_a_rule_with_an_unknown_kind_is_an_error(self):
        bad = copy.deepcopy(MAP)
        bad["contentRules"][0]["kind"] = "ghost"
        code, _, err = self.check(bad)
        self.assertEqual(1, code)
        self.assertIn("contentRules[0]", err)
        self.assertIn("ghost", err)

    def test_a_bad_glob_or_marker_is_an_error(self):
        for change in ({"glob": "/abs/**"}, {"glob": "../x/**"}, {"lineComment": [" // "]}, {"blockComment": {"start": " ", "end": "*/"}}):
            bad = copy.deepcopy(MAP)
            bad["contentRules"][0].update(change)
            code, _, err = self.check(bad)
            self.assertEqual(1, code, change)
            self.assertIn("contentRules[0]", err)

    def test_the_schema_rejects_a_rule_without_a_comment_marker(self):
        code, _, _ = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "checks-content-rule-without-comment-marker.json"))
        self.assertEqual(1, code)
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "checks-content-rules.json"))
        self.assertEqual(0, code, out)


if __name__ == "__main__":
    unittest.main()
